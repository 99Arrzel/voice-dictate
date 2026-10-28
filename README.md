# voice-dictate

Push-to-talk dictation for GNOME Wayland: Whisper large-v3 on the GPU transcribes, Claude Haiku (through
`claude -p` on your Claude login) cleans the text up, and a virtual keyboard types it into the focused window.
Set `cleanup.backend = "ollama"` to clean up locally with qwen3:8b instead, so nothing leaves the machine.

A mic icon in the top bar (a StatusNotifierItem the daemon publishes itself, shown by Ubuntu's AppIndicator
extension) is the whole UI; nothing pops over the screen. Other machines on the network can dictate through it too:
see [Dictating from another machine](#dictating-from-another-machine-macos-client). If the icon is missing, the daemon is not running.

| Icon | Meaning |
|---|---|
| white mic | ready |
| amber crossed-out mic + `muted` / `no mic` | the default source is muted in PipeWire, or there is none |
| red mic with signal bars + `0:12` | recording; the bars follow your voice level |
| amber crossed-out mic + `MUTED 0:12` | recording exact digital silence: a hardware mute button is on |
| amber mic + timer | recording, but no sound yet |
| blue mic + `…` | transcribing, cleaning up, typing |
| green mic | typed |
| amber warning + reason | nothing typed: `nothing heard`, `muted`, `failed`, … |

Click the icon for status lines: current state, which mic, and the last result. The GNOME extension in
`gnome-extension/` (symlinked into `~/.local/share/gnome-shell/extensions/voice-dictate@link.local`) only tells
the daemon which app is focused, so terminals get Ctrl+J line breaks.

| Shortcut | Action |
|---|---|
| `Ctrl+Super+Space` | start recording / stop and type |
| `Ctrl+Super+Backspace` | cancel the recording |

Say "new line" or "new paragraph" as its own sentence to insert breaks. Line breaks are typed as
Shift+Enter so chat inputs don't submit.

## Pieces

- `voice-dictate.service` (systemd user unit, copy of `systemd/voice-dictate.service`): this daemon; keeps Whisper loaded, listens on `$XDG_RUNTIME_DIR/voice-dictate.sock`.
- `ollama.service` (systemd user unit): Ollama from `~/.local/opt/ollama`, serving `qwen3:8b` for the `ollama` backend.
- `bin/dictate toggle|start|stop|cancel|status`: the client the shortcuts call.
- Config: `~/.config/voice-dictate/config.toml` (see `config.example.toml`); add jargon to `vocabulary`.
- History: `~/.local/share/voice-dictate/history.jsonl` (raw + cleaned text, timings); last recording: `last.wav`.

## Commands

```sh
systemctl --user restart voice-dictate         # after editing config
journalctl --user -u voice-dictate -f          # logs
.venv/bin/voice-dictate transcribe file.wav    # full pipeline on a 16 kHz mono WAV, prints instead of typing
uv run python bench/cleanup_bench.py claude-code:haiku ollama:qwen3:8b  # compare cleanup backends
```

Typing goes through `/dev/uinput` with the US layout; if that device stops being writable for your user,
a udev rule `KERNEL=="uinput", TAG+="uaccess"` restores it.

## Dictating from another machine (macOS client)

A laptop without the GPU or memory for Whisper can use this machine instead: it records, sends the WAV here,
and pastes back the cleaned text. The daemon serves the request with the model it already has loaded, so
local and remote dictations share one copy of Whisper and run one at a time.

```
Mac (Hammerspoon + sox)                          this machine (voice-dictate daemon)
hotkey → record 16 kHz mono WAV ── POST /transcribe ──▶ Whisper → cleanup
paste into the focused app     ◀──── {"text": …} ─────
```

**Server.** Add to `~/.config/voice-dictate/config.toml`, then `systemctl --user restart voice-dictate`:

```toml
[server]
enabled = true
host = "0.0.0.0"
port = 8765
token = "…"            # openssl rand -hex 32
```

Every request runs the cleanup on your Claude login, so the token is required and the port belongs on a
trusted network only. With ufw active, open it to the LAN: `sudo ufw allow from 192.168.1.0/24 to any port 8765 proto tcp`.
The Mac reaches the server by mDNS name (`http://<hostname>.local:8765`), which survives DHCP address changes.

```sh
curl -H "Authorization: Bearer $TOKEN" http://<hostname>.local:8765/health
curl -H "Authorization: Bearer $TOKEN" --data-binary @file.wav http://<hostname>.local:8765/transcribe
```

`POST /transcribe` takes a 16 kHz mono 16-bit WAV and answers `{"text", "raw", "cleanup"}`, or
`{"text": "", "reason": "mic muted" | "nothing heard"}`. Remote dictations land in the history with
`"source": "remote"`.

**Mac client.** `mac/voice_dictate.lua` is a Hammerspoon module; Hammerspoon is resident anyway, so the
client costs almost no memory, and `sox` does the recording.

```sh
git clone <this repo> && cd voice-dictate
./mac/install.sh http://<hostname>.local:8765 <token>   # installs Hammerspoon + sox, wires up ~/.hammerspoon/init.lua
```

| Shortcut | Action |
|---|---|
| `Ctrl+Option+Space` | start recording / stop and paste |
| `Esc` (while recording) | cancel |

The menu bar shows 🎙 ready, 🔴 recording, ⏳ transcribing, ✅ pasted, ⚠️ with a reason, and `🎙 offline` when
the server cannot be reached. The text is pasted, not typed, so Unicode and line breaks survive and chat
inputs don't submit; it also stays on the clipboard. macOS asks once for Microphone and Accessibility
permission for Hammerspoon. Options for `start{…}`: `server`, `token`, `hotkey = {mods, key}`, `rec`
(path to sox's `rec`), `maxSeconds`, `sounds`, `trailingSpace`.

## License

MIT (see `LICENSE`). The tray icons in `src/voice_dictate/icons/` are adapted from the GNOME Adwaita icon theme
(CC BY-SA 3.0 / LGPL-3.0).
