import ctypes
import importlib.util
import json
import logging
import math
import re
import signal
import socketserver
import subprocess
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import httpx
import numpy as np

from . import cleanup, keyboard, server
from .indicator import Shell
from .config import DATA_DIR, SOCKET_PATH, Config, load_config
from .tray import Tray

log = logging.getLogger("voice-dictate")

SOUND_DIR = Path("/usr/share/sounds/freedesktop/stereo")
AUDIO_PATH = DATA_DIR / "last.wav"
# RMS below this for the first seconds means the mic is muted, not that the room is quiet.
SILENT_RMS = 0.0005
# A live mic never outputs exact zeros for this long; a hardware or PipeWire mute does.
MUTED_AFTER_SECONDS = 1.0
# Whisper's stock outputs for silence or noise, learned from subtitle data.
HALLUCINATIONS = {
    "subtitles by the amaraorg community", "thanks for watching", "thank you for watching",
    "thank you for watching please subscribe", "please subscribe", "subscribe to my channel",
}
HISTORY_PATH = DATA_DIR / "history.jsonl"
# How long a finished or failed dictation stays on the tray icon before it returns to idle.
LINGER_SECONDS = {"done": 1.5, "error": 4.0}


def copy_to_clipboard(text: str) -> bool:
    # Mutter bridges the XWayland clipboard to Wayland; wl-copy would need a focused surface on GNOME.
    try:
        subprocess.run(["xclip", "-selection", "clipboard"], input=text, text=True, timeout=2, check=True)
        return True
    except Exception as error:
        log.warning("clipboard copy failed: %s", error)
        return False


def query_default_mic() -> tuple[bool, bool, str]:
    """(present, muted, name) of PipeWire's default source. A hardware mute button is invisible here."""
    volume = subprocess.run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SOURCE@"], capture_output=True, text=True)
    if volume.returncode != 0:
        return False, False, ""
    inspect = subprocess.run(["wpctl", "inspect", "@DEFAULT_AUDIO_SOURCE@"], capture_output=True, text=True)
    match = re.search(r'node\.description = "([^"]*)"', inspect.stdout)
    return True, "[MUTED]" in volume.stdout, match.group(1) if match else "default mic"


def preload_cuda_libraries() -> None:
    # CTranslate2 dlopens cuBLAS/cuDNN by soname; the pip wheels are not on the loader path.
    for package in ("nvidia.cublas", "nvidia.cudnn"):
        spec = importlib.util.find_spec(package)
        if spec is None or not spec.submodule_search_locations:
            continue
        lib_dir = Path(list(spec.submodule_search_locations)[0]) / "lib"
        for library in sorted(lib_dir.glob("lib*.so.*")):
            ctypes.CDLL(str(library), mode=ctypes.RTLD_GLOBAL)


def read_wav(path: Path) -> np.ndarray:
    # pw-record writes 16 kHz mono s16, so this skips faster-whisper's PyAV decoder,
    # whose `metadata_errors` argument PyAV 19 no longer accepts.
    with wave.open(str(path)) as audio:
        frames = audio.readframes(audio.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


class Dictation:
    def __init__(self, config: Config):
        self.config = config
        self.lock = threading.Lock()
        self.model_lock = threading.Lock()
        self.history_lock = threading.Lock()
        self.state = "idle"
        self.recorder: subprocess.Popen | None = None
        self.started_at = 0.0
        self.stop_timer: threading.Timer | None = None
        self.shell = Shell()
        self.tray = Tray()
        self.tray.set("loading", "", ["Loading Whisper…"])
        self.mic = (True, False, config.input_device or "default mic")
        self.last_result = ""
        self.idle_after = 0.0
        self.meter_stop = threading.Event()
        self.meter: threading.Thread | None = None
        self.http = httpx.Client()
        self.keyboard = keyboard.open_keyboard()

        preload_cuda_libraries()
        from faster_whisper import WhisperModel

        began = time.monotonic()
        whisper = config.whisper
        self.model = WhisperModel(whisper.model, device=whisper.device, compute_type=whisper.compute_type)
        log.info("loaded whisper %s (%s) in %.1fs", whisper.model, whisper.compute_type, time.monotonic() - began)
        if config.cleanup.enabled:
            threading.Thread(target=self._warm_cleanup, daemon=True).start()
        threading.Thread(target=self._watch_idle, daemon=True).start()

    def _warm_cleanup(self) -> None:
        try:
            cleanup.warm_up(self.config.cleanup, self.http)
            log.info("cleanup backend %s ready", self.config.cleanup.backend)
        except Exception as error:
            log.warning("cleanup warm-up failed: %s", error)

    def show(self, icon: str, label: str, line: str) -> None:
        """Puts a state on the tray icon; `line` heads the click-to-open menu."""
        lines = [line, f"Mic: {self.mic[2]}", self.last_result]
        self.tray.set(icon, label, [entry for entry in lines if entry])

    def finish(self, icon: str, label: str, result: str) -> None:
        self.last_result = f"Last ({datetime.now():%H:%M}): {result}"
        self.idle_after = time.monotonic() + LINGER_SECONDS[icon if icon == "done" else "error"]
        self.show(icon, label, result)

    def _show_idle(self) -> None:
        present, muted, name = self.mic
        if not present:
            self.show("muted", "no mic", "No microphone found")
        elif muted:
            self.show("muted", "muted", f"{name} is muted in Sound settings")
        else:
            self.show("idle", "", "Ready · Ctrl+Super+Space to dictate")

    def _watch_idle(self) -> None:
        while True:
            if not self.config.input_device:
                try:
                    self.mic = query_default_mic()
                except Exception as error:
                    log.warning("mic check failed: %s", error)
            if self.state == "idle" and time.monotonic() >= self.idle_after:
                self._show_idle()
            time.sleep(1)

    def _run_meter(self) -> None:
        offset = 44
        heard = False
        last_sound = self.started_at
        while not self.meter_stop.wait(0.07):
            try:
                with AUDIO_PATH.open("rb") as audio:
                    audio.seek(offset)
                    chunk = audio.read()
            except FileNotFoundError:
                chunk = b""
            usable = len(chunk) - len(chunk) % 2
            offset += usable
            samples = np.frombuffer(chunk[:usable], dtype=np.int16).astype(np.float32) / 32768.0
            now = time.monotonic()
            rms = float(np.sqrt(np.mean(samples**2))) if usable else 0.0
            if np.any(samples):
                last_sound = now
            heard = heard or rms > SILENT_RMS
            level = min(max((20 * math.log10(rms + 1e-9) + 60) / 50, 0.0), 1.0)
            elapsed = int(now - self.started_at)
            timer = f"{elapsed // 60}:{elapsed % 60:02d}"
            if now - last_sound >= MUTED_AFTER_SECONDS:
                self.show("muted", f"MUTED {timer}", "Mic is muted - nothing is being recorded")
            elif not heard and elapsed >= 2:
                self.show("no-audio", timer, "No sound from the mic")
            else:
                strength = "high" if level >= 0.55 else "medium" if level >= 0.2 else "low"
                self.show(f"recording-{strength}", timer, "Recording · press the shortcut again to stop")

    def sound(self, name: str) -> None:
        if self.config.sounds:
            subprocess.Popen(["pw-play", str(SOUND_DIR / f"{name}.oga")], stderr=subprocess.DEVNULL)

    def handle(self, command: str) -> str:
        with self.lock:
            if command == "status":
                return self.state
            if command == "cancel":
                return self._cancel()
            if command == "toggle":
                if self.state == "idle":
                    return self._start()
                if self.state == "recording":
                    return self._stop()
                return "busy"
            if command == "start":
                return self._start() if self.state == "idle" else self.state
            if command == "stop":
                return self._stop() if self.state == "recording" else self.state
        return f"unknown command: {command}"

    def _start(self) -> str:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        command = ["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16"]
        if self.config.input_device:
            command += ["--target", self.config.input_device]
        self.recorder = subprocess.Popen(command + [str(AUDIO_PATH)], stderr=subprocess.DEVNULL)
        self.started_at = time.monotonic()
        self.state = "recording"
        self.stop_timer = threading.Timer(self.config.max_record_seconds, lambda: self.handle("stop"))
        self.stop_timer.start()
        self.sound("audio-volume-change")
        self.show("recording-low", "0:00", "Recording · press the shortcut again to stop")
        self.meter_stop.clear()
        self.meter = threading.Thread(target=self._run_meter, daemon=True)
        self.meter.start()
        return "recording"

    def _end_recording(self) -> float:
        if self.stop_timer:
            self.stop_timer.cancel()
        self.meter_stop.set()
        if self.meter:
            self.meter.join()
            self.meter = None
        if self.recorder:
            self.recorder.send_signal(signal.SIGINT)
            self.recorder.wait(timeout=5)
            self.recorder = None
        return time.monotonic() - self.started_at

    def _cancel(self) -> str:
        if self.state != "recording":
            return self.state
        self._end_recording()
        self.state = "idle"
        self.sound("dialog-warning")
        self.finish("error", "cancelled", "cancelled")
        return "cancelled"

    def _stop(self) -> str:
        duration = self._end_recording()
        if duration < 0.4:
            self.state = "idle"
            self.finish("error", "too short", "too short, ignored")
            return "ignored"
        self.state = "processing"
        self.sound("message")
        self.show("processing", "…", "Transcribing…")
        threading.Thread(target=self._process, args=(duration,), daemon=True).start()
        return "processing"

    def _transcribe(self, audio: np.ndarray) -> str:
        whisper = self.config.whisper
        prompt = None
        if self.config.vocabulary:
            prompt = "Vocabulary: " + ", ".join(self.config.vocabulary) + "."
        segments, _ = self.model.transcribe(
            audio,
            language=whisper.language,
            beam_size=whisper.beam_size,
            vad_filter=True,
            condition_on_previous_text=False,
            initial_prompt=prompt,
        )
        spoken = [
            segment.text.strip() for segment in segments
            if not (segment.no_speech_prob > 0.6 and segment.avg_logprob < -1.0)
        ]
        text = " ".join(spoken).strip()
        if re.sub(r"[^a-z ]", "", text.lower()).strip() in HALLUCINATIONS:
            log.info("dropped whisper hallucination: %s", text)
            return ""
        return text

    def _pipeline(self, audio: np.ndarray, entry: dict, on_cleanup=lambda: None) -> str:
        """Transcribes and cleans up `audio`, recording timings in `entry`; "" when nothing was said."""
        began = time.monotonic()
        # Local and remote dictations share one model; CTranslate2 is not safe to call concurrently.
        with self.model_lock:
            raw = self._transcribe(audio)
        entry["raw"] = raw
        entry["transcribe_seconds"] = round(time.monotonic() - began, 2)
        if not raw:
            return ""

        text, verdict = raw, "disabled"
        if self.config.cleanup.enabled:
            on_cleanup()
            began = time.monotonic()
            try:
                text, verdict = cleanup.clean(raw, self.config.vocabulary, self.config.cleanup, self.http)
            except Exception as error:
                verdict = f"failed: {error}"
            entry["cleanup_seconds"] = round(time.monotonic() - began, 2)
        entry["cleanup"] = verdict
        entry["text"] = text
        return text

    def _record(self, entry: dict) -> None:
        with self.history_lock, HISTORY_PATH.open("a") as history:
            history.write(json.dumps(entry) + "\n")

    def transcribe_remote(self, audio: np.ndarray) -> dict:
        """Pipeline for a recording sent by another machine; the client types the result itself."""
        entry = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "source": "remote",
            "audio_seconds": round(len(audio) / 16000, 1),
        }
        try:
            if not np.any(audio):
                entry["error"] = "mic muted"
                return {"text": "", "reason": "mic muted"}
            text = self._pipeline(audio, entry)
            log.info("remote dictation %s", json.dumps(entry))
            if not text:
                return {"text": "", "reason": "nothing heard"}
            return {"text": text, "raw": entry["raw"], "cleanup": entry["cleanup"]}
        except Exception as error:
            entry["error"] = str(error)
            raise
        finally:
            self._record(entry)

    def _process(self, duration: float) -> None:
        entry = {"at": datetime.now().isoformat(timespec="seconds"), "audio_seconds": round(duration, 1)}
        try:
            audio = read_wav(AUDIO_PATH)
            if not np.any(audio):
                entry["error"] = "mic muted"
                self.sound("dialog-error")
                self.finish("muted", "muted", "mic was muted - nothing recorded")
                return
            text = self._pipeline(audio, entry, lambda: self.show("processing", "…", "Cleaning up…"))
            if not text:
                self.finish("error", "nothing heard", "nothing heard")
                return
            entry["copied"] = copy_to_clipboard(text)

            self.show("processing", "…", "Typing…")
            output = self.config.output
            app = self.shell.focused_app()
            newline = output.unknown_newline if app is None else output.newline
            if app in output.terminal_apps:
                newline = output.terminal_newline
            entry["app"] = app
            if output.trailing_space and not text.endswith("\n"):
                text += " "
            if newline == "space":
                text = re.sub(r"\n+", " ", text)
            skipped = keyboard.type_text(self.keyboard, text, newline, output.key_delay_ms)
            if skipped:
                entry["skipped_chars"] = "".join(sorted(set(skipped)))
            self.sound("complete")
            copied = " · also on the clipboard" if entry["copied"] else ""
            self.finish("done", "", f"typed {len(text.split())} words{copied}")
            log.info("dictation %s", json.dumps(entry))
        except Exception as error:
            log.exception("dictation failed")
            entry["error"] = str(error)
            self.sound("dialog-error")
            copied = " · text is on the clipboard" if entry.get("copied") else ""
            self.finish("error", "failed", f"failed: {error}{copied}")
        finally:
            self._record(entry)
            with self.lock:
                self.state = "idle"


def transcribe_file(path: str) -> None:
    """Runs the whole pipeline on a 16 kHz mono WAV and prints instead of typing."""
    config = load_config()
    preload_cuda_libraries()
    from faster_whisper import WhisperModel

    whisper = config.whisper
    model = WhisperModel(whisper.model, device=whisper.device, compute_type=whisper.compute_type)
    began = time.monotonic()
    segments, _ = model.transcribe(
        read_wav(Path(path)), language=whisper.language, beam_size=whisper.beam_size,
        vad_filter=True, condition_on_previous_text=False,
    )
    raw = " ".join(segment.text.strip() for segment in segments).strip()
    print(f"raw     ({time.monotonic() - began:.2f}s): {raw}")
    began = time.monotonic()
    text, verdict = cleanup.clean(raw, config.vocabulary, config.cleanup, httpx.Client())
    print(f"cleaned ({time.monotonic() - began:.2f}s, {verdict}): {text}")


def serve() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = load_config()
    dictation = Dictation(config)
    if config.server.enabled:
        if config.server.token:
            server.start(config.server, dictation.transcribe_remote, config.max_record_seconds)
        else:
            log.error("server.enabled is set but server.token is empty; remote dictation stays off")

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            command = self.rfile.readline().decode().strip()
            self.wfile.write((dictation.handle(command) + "\n").encode())

    SOCKET_PATH.unlink(missing_ok=True)
    with socketserver.ThreadingUnixStreamServer(str(SOCKET_PATH), Handler) as socket_server:
        SOCKET_PATH.chmod(0o600)
        log.info("listening on %s", SOCKET_PATH)
        socket_server.serve_forever()

