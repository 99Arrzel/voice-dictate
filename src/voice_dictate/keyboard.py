import time

from evdev import UInput
from evdev import ecodes as e

# US layout: character -> (keycode, needs shift). GNOME applies the active xkb layout to
# virtual devices too, so this must match `gsettings get org.gnome.desktop.input-sources sources`.
_UNSHIFTED = {
    "`": e.KEY_GRAVE, "-": e.KEY_MINUS, "=": e.KEY_EQUAL, "[": e.KEY_LEFTBRACE, "]": e.KEY_RIGHTBRACE,
    "\\": e.KEY_BACKSLASH, ";": e.KEY_SEMICOLON, "'": e.KEY_APOSTROPHE, ",": e.KEY_COMMA,
    ".": e.KEY_DOT, "/": e.KEY_SLASH, " ": e.KEY_SPACE, "\t": e.KEY_TAB,
}
_SHIFTED = {
    "~": e.KEY_GRAVE, "!": e.KEY_1, "@": e.KEY_2, "#": e.KEY_3, "$": e.KEY_4, "%": e.KEY_5,
    "^": e.KEY_6, "&": e.KEY_7, "*": e.KEY_8, "(": e.KEY_9, ")": e.KEY_0, "_": e.KEY_MINUS,
    "+": e.KEY_EQUAL, "{": e.KEY_LEFTBRACE, "}": e.KEY_RIGHTBRACE, "|": e.KEY_BACKSLASH,
    ":": e.KEY_SEMICOLON, '"': e.KEY_APOSTROPHE, "<": e.KEY_COMMA, ">": e.KEY_DOT, "?": e.KEY_SLASH,
}

KEYMAP: dict[str, tuple[int, bool]] = {}
for char, code in _UNSHIFTED.items():
    KEYMAP[char] = (code, False)
for char, code in _SHIFTED.items():
    KEYMAP[char] = (code, True)
for letter in "abcdefghijklmnopqrstuvwxyz":
    code = getattr(e, f"KEY_{letter.upper()}")
    KEYMAP[letter] = (code, False)
    KEYMAP[letter.upper()] = (code, True)
for digit in "0123456789":
    KEYMAP[digit] = (getattr(e, f"KEY_{digit}"), False)


def open_keyboard() -> UInput:
    keys = {code for code, _ in KEYMAP.values()} | {e.KEY_LEFTSHIFT, e.KEY_LEFTCTRL, e.KEY_ENTER}
    device = UInput({e.EV_KEY: sorted(keys)}, name="voice-dictate-keyboard")
    # The compositor needs a moment to pick up a new input device before it accepts events.
    time.sleep(0.5)
    return device


NEWLINE_KEYS = {
    "enter": (e.KEY_ENTER, None),
    "shift+enter": (e.KEY_ENTER, e.KEY_LEFTSHIFT),
    "ctrl+j": (e.KEY_J, e.KEY_LEFTCTRL),
}


def _tap(device: UInput, code: int, modifier: int | None, delay: float) -> None:
    if modifier:
        device.write(e.EV_KEY, modifier, 1)
    device.write(e.EV_KEY, code, 1)
    device.syn()
    time.sleep(delay)
    device.write(e.EV_KEY, code, 0)
    if modifier:
        device.write(e.EV_KEY, modifier, 0)
    device.syn()
    time.sleep(delay)


def type_text(device: UInput, text: str, newline: str, key_delay_ms: float) -> list[str]:
    """Types text into the focused window; returns characters that have no key on the layout."""
    delay = key_delay_ms / 1000
    skipped = []
    for char in text:
        if char == "\n":
            if newline == "space":
                _tap(device, e.KEY_SPACE, None, delay)
            else:
                code, modifier = NEWLINE_KEYS[newline]
                _tap(device, code, modifier, delay)
            continue
        mapping = KEYMAP.get(char)
        if mapping is None:
            skipped.append(char)
            continue
        _tap(device, mapping[0], e.KEY_LEFTSHIFT if mapping[1] else None, delay)
    return skipped
