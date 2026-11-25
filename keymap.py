"""Key map (FUNK-62): what each key of the kids' keyboard does.

A key is identified by the code the keyboard sends: the evdev key name
without ``KEY_`` (``CUT``, ``A``, ``Y`` …), optionally prefixed with the
modifiers held at the time (``CTRL+X``). The id never depends on the language
layout. Physical Z on a German keyboard sends ``Y``, so its id is ``Y`` even
though it plays the letter Z by default. That keeps ids stable for any
keyboard, and for a future virtual keyboard (FUNK-65).

A key map is ``{key_id: action}``, stored per keyboard profile in
``settings["keymaps"][profile]``. Keys without an entry keep today's built-in
behaviour (letters, digits, Space, + / −, Enter, Tab, arrows; other keys play a
random sound only if that setting is on). An entry overrides it:

- ``{"action": "letter", "letter": "A"}``: play the content of a letter or digit
- ``{"action": "sound", "file": "x.mp3", "volume": 100}``
- ``{"action": "imgsound", "file": "x.mp3", "image": "x.png", "word": "schere", "volume": 100}``
- ``{"action": "func", "func": "volup"}``: one of FUNCTIONS
- ``{"action": "none"}``: the key stays silent

Pure helpers only: no I/O, unit-tested in tests/test_keymap.py.
"""
import re

DEFAULT_PROFILE = "acme-bk03"

# id → label (German, as shown in the UI)
FUNCTIONS = {
    "volup": "Lauter",
    "voldown": "Leiser",
    "mute": "Stumm an/aus",
    "stop": "Stopp (alle Töne aus)",
    "replay": "Nochmal abspielen",
    "fav": "Favorit umschalten",
    "nextset": "Nächstes Set",
    "random": "Zufallsgeräusch",
    "prevsound": "Voriger Ton",
    "nextsound": "Nächster Ton",
    "prevletter": "Buchstabe zurück",
    "nextletter": "Buchstabe vor",
}

ACTIONS = ("letter", "sound", "imgsound", "func", "none")

# evdev modifier → canonical modifier name, in canonical id order
MODIFIER_CODES = {
    "LEFTCTRL": "CTRL", "RIGHTCTRL": "CTRL",
    "LEFTALT": "ALT",
    "LEFTMETA": "META", "RIGHTMETA": "META",
}
MODIFIER_ORDER = ("CTRL", "ALT", "META")
# keys that only modify others: never listed as "pressed without effect"
SILENT_MODIFIERS = frozenset(MODIFIER_CODES) | {"LEFTSHIFT", "RIGHTSHIFT", "RIGHTALT", "CAPSLOCK", "FN"}

# Shift is reserved: Shift + digit plays a favourite. Never mapped, and an entry
# stored before FUNK-63 is ignored (it can still be deleted).
RESERVED = ("LEFTSHIFT", "RIGHTSHIFT")

LETTER_TARGETS = tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ") + ("Ä", "Ö", "Ü", "ß") + tuple("0123456789")

# Built-in behaviour per (layout-translated) key name, for the UI.
DEFAULT_LABELS = {
    "SPACE": "Stopp (alle Töne aus)",
    "EQUAL": "Lauter", "KPPLUS": "Lauter",
    "MINUS": "Leiser", "KPMINUS": "Leiser",
    "ENTER": "Nochmal abspielen",
    "TAB": "Favorit umschalten",
    "LEFT": "Voriger Ton", "RIGHT": "Nächster Ton",
    "UP": "Buchstabe zurück", "DOWN": "Buchstabe vor",
}

_BASE = r"[A-Z0-9_]{1,40}"
_KEY_ID = re.compile(r"(?:(?:CTRL|ALT|META)\+){0,3}" + _BASE)
_FILE = re.compile(r"[^/\\\x00]{1,200}")
WORD_MAX = 24
VOLUME_MAX = 200


class KeymapError(ValueError):
    pass


def canonical_mods(mods):
    """Sorted, de-duplicated modifier names in canonical order."""
    names = {MODIFIER_CODES.get(m, m) for m in (mods or ())}
    return tuple(m for m in MODIFIER_ORDER if m in names)


def key_id(raw, mods=()):
    """The id of a press: ``raw`` (evdev name without KEY_) plus held modifiers."""
    raw = str(raw).upper()
    if raw.startswith("KEY_"):
        raw = raw[4:]
    held = tuple(m for m in canonical_mods(mods) if MODIFIER_CODES.get(raw) != m)
    return "+".join(held + (raw,))


def parse_key_id(kid, allow_reserved=False):
    """Validate an id; return (raw, mods). Raises KeymapError.

    ``allow_reserved`` accepts the Shift keys, only for deleting an old entry.
    """
    if not isinstance(kid, str) or not _KEY_ID.fullmatch(kid):
        raise KeymapError(f"invalid key id: {kid!r}")
    parts = kid.split("+")
    raw, mods = parts[-1], tuple(parts[:-1])
    if len(set(mods)) != len(mods) or canonical_mods(mods) != mods:
        raise KeymapError(f"modifiers must be unique and in the order {'+'.join(MODIFIER_ORDER)}: {kid!r}")
    if raw.startswith("SHIFT_") or (raw in RESERVED and not allow_reserved):
        raise KeymapError("Shift (Shift+digit is the favourite shortcut) cannot be mapped")
    if key_id(raw, mods) != kid:
        # e.g. KEY_CUT (→ CUT) or CTRL+LEFTCTRL (→ LEFTCTRL): an id a press can never produce
        raise KeymapError(f"not a canonical key id (a press yields {key_id(raw, mods)!r}): {kid!r}")
    return raw, mods


def lookup_chain(raw, mods=()):
    """Ids to try for a press: with modifiers first, then the plain key."""
    plain = key_id(raw)
    full = key_id(raw, mods)
    return [full, plain] if full != plain else [plain]


def resolve(keymap, raw, mods=()):
    """Return (matched_id, action) for a press, or (None, None) for the built-in behaviour."""
    if key_id(raw) in RESERVED:
        return None, None
    for kid in lookup_chain(raw, mods):
        action = (keymap or {}).get(kid)
        if action is not None:
            return kid, action
    return None, None


def _safe_file(value, what):
    if not isinstance(value, str) or not _FILE.fullmatch(value) or value in (".", "..") or value.startswith("."):
        raise KeymapError(f"invalid {what} file name")
    return value


def _volume(value):
    if value is None:
        return 100
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= VOLUME_MAX:
        raise KeymapError(f"volume must be an integer 0..{VOLUME_MAX}")
    return value


def validate(action):
    """Normalize an action dict; raise KeymapError if it is not valid."""
    if not isinstance(action, dict):
        raise KeymapError("action must be an object")
    kind = action.get("action")
    if kind not in ACTIONS:
        raise KeymapError(f"action must be one of {', '.join(ACTIONS)}")
    if kind == "none":
        return {"action": "none"}
    if kind == "letter":
        letter = action.get("letter")
        if isinstance(letter, str) and letter not in ("ß",):
            letter = letter.upper()
        if letter not in LETTER_TARGETS:
            raise KeymapError("letter must be A-Z, Ä, Ö, Ü, ß or a digit")
        return {"action": "letter", "letter": letter}
    if kind == "func":
        func = action.get("func")
        if not isinstance(func, str) or func not in FUNCTIONS:
            raise KeymapError(f"func must be one of {', '.join(FUNCTIONS)}")
        return {"action": "func", "func": func}
    out = {"action": kind, "file": _safe_file(action.get("file"), "sound"), "volume": _volume(action.get("volume"))}
    if kind == "imgsound":
        out["image"] = _safe_file(action.get("image"), "image")
        word = action.get("word", "")
        if not isinstance(word, str) or len(word) > WORD_MAX:
            raise KeymapError(f"word must be text of at most {WORD_MAX} characters")
        out["word"] = word.strip()
    return out


def describe(action):
    """German one-line description of an action, for logs and the UI."""
    if not action:
        return ""
    kind = action.get("action")
    if kind == "none":
        return "Nichts"
    if kind == "letter":
        return f"Inhalt von {action.get('letter')}"
    if kind == "func":
        return FUNCTIONS.get(action.get("func"), "?")
    if kind == "sound":
        return f"Geräusch {action.get('file')}"
    if kind == "imgsound":
        return f"Bild + Geräusch {action.get('file')}"
    return "?"


def default_label(key, has_content=False):
    """What a key does without a key-map entry (key = layout-translated name).

    Returns None when the key has no built-in function (it then only plays a
    random sound if "Zufällige Sounds" is on): such keys count as unassigned.
    """
    if key in DEFAULT_LABELS:
        return DEFAULT_LABELS[key]
    if key.startswith("SHIFT_") and key[6:].isdigit():
        return f"Favorit {key[6:]} abspielen"
    if len(key) == 1 and key.isdigit():
        return f"Ziffer {key} (Nummern)"
    if len(key) == 1 and "A" <= key <= "Z":
        return f"Buchstabe {key} (Set-Inhalt)"
    if has_content:
        return f"{key} (Set-Inhalt)"
    return None


def default_kind(key, has_content=False):
    """The kind of a key's built-in behaviour: "func", "letter" (set content) or None."""
    if default_label(key, has_content) is None:
        return None
    if key in DEFAULT_LABELS or key.startswith("SHIFT_"):
        return "func"
    return "letter"


def is_unassigned(key, raw, action, has_content=False):
    """True if a press did nothing specific: no key-map entry and no built-in role."""
    if action is not None:
        return False
    if raw in SILENT_MODIFIERS or key in SILENT_MODIFIERS:
        return False
    return default_label(key, has_content) is None
