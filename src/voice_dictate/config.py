import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "voice-dictate" / "config.toml"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "voice-dictate"
SOCKET_PATH = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "voice-dictate.sock"


@dataclass
class WhisperConfig:
    model: str = "large-v3"
    device: str = "cuda"
    compute_type: str = "int8_float16"
    language: str = "en"
    beam_size: int = 5


@dataclass
class CleanupConfig:
    enabled: bool = True
    # "claude-code" runs `claude -p` on your Claude login; "ollama" runs a local model.
    backend: str = "claude-code"
    claude_path: str = str(Path.home() / ".local" / "bin" / "claude")
    claude_model: str = "haiku"
    claude_effort: str = "low"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:8b"
    keep_alive: str = "30m"
    timeout_seconds: float = 20.0
    # Below this word-level similarity to the raw transcript, the cleanup is rejected as a rewrite.
    min_similarity: float = 0.55


@dataclass
class OutputConfig:
    # Line-break keys: "shift+enter", "ctrl+j", "enter" or "space". VTE terminals (Ptyxis) send
    # Shift+Enter as plain Enter, so terminals get Ctrl+J, which Claude Code reads as a newline.
    newline: str = "shift+enter"
    terminal_newline: str = "ctrl+j"
    terminal_apps: list[str] = field(default_factory=lambda: [
        "org.gnome.Ptyxis", "gnome-terminal-server", "kitty", "com.mitchellh.ghostty",
        "Alacritty", "org.wezfurlong.wezterm", "foot",
    ])
    # Used when the focused app is unknown (extension not loaded), so a break never submits.
    unknown_newline: str = "space"
    key_delay_ms: float = 4.0
    trailing_space: bool = True


@dataclass
class ServerConfig:
    # HTTP endpoint other machines send recordings to (the macOS client in mac/). Off unless enabled.
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8765
    # Required when enabled: every request runs Whisper and the cleanup model on this machine.
    token: str = ""


@dataclass
class Config:
    vocabulary: list[str] = field(default_factory=list)
    # PipeWire node.name of the mic (`wpctl status`, then `wpctl inspect <id>`); empty uses the default source.
    input_device: str = ""
    max_record_seconds: int = 600
    sounds: bool = True
    whisper: WhisperConfig = field(default_factory=WhisperConfig)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    server: ServerConfig = field(default_factory=ServerConfig)


def _apply(target, values: dict) -> None:
    known = {f.name for f in fields(target)}
    for key, value in values.items():
        if key not in known:
            raise ValueError(f"unknown config key: {key}")
        current = getattr(target, key)
        if isinstance(value, dict):
            _apply(current, value)
        else:
            setattr(target, key, value)


def load_config() -> Config:
    config = Config()
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("rb") as handle:
            _apply(config, tomllib.load(handle))
    return config
