import hmac
import json
import logging
import struct
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from .config import ServerConfig

log = logging.getLogger("voice-dictate")

SAMPLE_RATE = 16000


def parse_wav(data: bytes) -> np.ndarray:
    """Samples of a 16 kHz mono 16-bit WAV; tolerates the unfinalized sizes an interrupted recorder leaves."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    position, fmt = 12, None
    while position + 8 <= len(data):
        chunk_id = data[position:position + 4]
        size = int.from_bytes(data[position + 4:position + 8], "little")
        body = position + 8
        if chunk_id == b"fmt ":
            fmt = struct.unpack("<HHIIHH", data[body:body + 16])
        elif chunk_id == b"data":
            if fmt is None:
                raise ValueError("WAV data chunk comes before its fmt chunk")
            audio_format, channels, rate, _, _, bits = fmt
            if audio_format not in (1, 0xFFFE) or (channels, rate, bits) != (1, SAMPLE_RATE, 16):
                raise ValueError(f"need 16 kHz mono 16-bit PCM, got {rate} Hz, {channels} channel(s), {bits}-bit")
            samples = data[body:body + size] if body + size <= len(data) else data[body:]
            usable = len(samples) - len(samples) % 2
            return np.frombuffer(samples[:usable], dtype="<i2").astype(np.float32) / 32768.0
        position = body + size + size % 2
    raise ValueError("WAV has no data chunk")


def start(config: ServerConfig, transcribe: Callable[[np.ndarray], dict], max_seconds: int) -> None:
    """Serves POST /transcribe (WAV body -> JSON) and GET /health on a background thread."""
    expected = f"Bearer {config.token}".encode()
    max_bytes = max_seconds * SAMPLE_RATE * 2 + 65536

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/health":
                return self._reply(404, {"error": "not found"})
            if not self._authorized():
                return self._reply(401, {"error": "bad token"})
            self._reply(200, {"ok": True})

        def do_POST(self) -> None:
            if self.path != "/transcribe":
                return self._reply(404, {"error": "not found"})
            if not self._authorized():
                return self._reply(401, {"error": "bad token"})
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return self._reply(411, {"error": "Content-Length required"})
            if length > max_bytes:
                return self._reply(413, {"error": f"recording longer than {max_seconds}s"})
            body = self.rfile.read(length)
            try:
                audio = parse_wav(body)
            except ValueError as error:
                return self._reply(400, {"error": str(error)})
            try:
                self._reply(200, transcribe(audio))
            except Exception as error:
                log.exception("remote dictation failed")
                self._reply(500, {"error": str(error)})

        def _authorized(self) -> bool:
            return hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected)

        def _reply(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:
            log.info("http %s %s", self.address_string(), format % args)

    server = ThreadingHTTPServer((config.host, config.port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("remote dictation listening on http://%s:%d", config.host, config.port)
