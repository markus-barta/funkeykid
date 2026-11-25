"""Keyboard profiles (FUNK-63): what a keyboard looks like and what each key sends.

A profile is data, not artwork. The web UI draws the keyboard from it, so a
custom keyboard is a key list, not a picture:

    {"id", "name", "kind": "builtin" | "custom", "base", "note", "hint",
     "rows": [{"h": 1.0, "before": 0.0, "keys": [item, ...]}, ...]}

Row heights, widths and offsets are in key units (1 = one letter key).
``before`` is extra space above a row. An item is one of:

- a key: ``{"slot", "code", "label", "name", "w", "h", "dy", "shift", "altgr",
  "fn", "icon", "guess", "suggest"}``
- a gap: ``{"gap": 0.5}``
- a stack: ``{"stack": [key, key], "w": 1}``. The keys share the row height
  (the BK03's half-height ↑ and ↓).

A key's ``code`` is what it sends, as a key-map id (``CUT``, ``Y``, ``CTRL+X``).
It is the link to the key map (FUNK-62); ``""`` means nothing reaches
funkeykid (the fn key). ``slot`` is the key's stable name within its profile.
Its legends are positional: ``label`` in the middle, ``shift`` at the top left,
``altgr`` at the bottom left and ``fn`` at the bottom right, in red. ``guess``
marks a code that is assumed and not yet confirmed by a real press.
``suggest`` is an optional key-map action the UI offers for an unassigned key.

Built-in profiles live here and are read-only. A custom profile is a copy,
stored in ``settings["keyboard_profiles"]``, whose legends, codes and widths
can be edited. Pure helpers only: unit-tested in tests/test_profiles.py.
"""
import copy
import re

import keymap

DEFAULT_PROFILE = keymap.DEFAULT_PROFILE

ICONS = ("cut", "copy", "paste", "globe", "search", "prev", "play", "next",
         "mute", "vold", "volu", "screen")
LEGEND_MAX = {"label": 12, "shift": 4, "altgr": 8, "fn": 8}
NAME_MAX = 40
PROFILE_NAME_MAX = 40
NOTE_MAX = 300
W_RANGE = (0.25, 12.0)
H_RANGE = (0.25, 3.0)
DY_RANGE = (0.0, 2.0)
GAP_RANGE = (0.05, 12.0)
MAX_ROWS = 12
MAX_KEYS = 200
MAX_CUSTOM = 20           # custom profiles (settings.json stays small)
SHIFT_CODES = ("LEFTSHIFT", "RIGHTSHIFT")
SUGGEST_ACTIONS = ("func", "letter", "none")
_SLOT = re.compile(r"[a-z0-9_-]{1,24}")
_CUSTOM_ID = re.compile(r"custom-[a-z0-9-]{1,40}")
_KEY_FIELDS = ("slot", "code", "label", "name", "w", "h", "dy", "shift", "altgr",
               "fn", "icon", "guess", "suggest")
EDITABLE = ("label", "name", "shift", "altgr", "fn", "icon", "code", "w")


class ProfileError(ValueError):
    pass


# ── built-in profiles ────────────────────────────────────────────────────────

def _k(slot, code, label="", name="", **kw):
    key = {"slot": slot, "code": code, "label": label, "name": name or label or code}
    key.update(kw)
    return key


def _fx(func):
    return {"action": "func", "func": func}


_SHIFTED = dict(zip("1234567890", '!"§$%&/()='))
_ALTGR = {"7": "{", "8": "[", "9": "]", "0": "}"}


def _digit(d):
    extra = {"altgr": _ALTGR[d]} if d in _ALTGR else {}
    return _k("d" + d, d, d, f"Ziffer {d}", shift=_SHIFTED[d], **extra)


def _letter(ch, code=None, **kw):
    return _k(ch.lower(), code or ch, ch, f"Buchstabe {ch}", **kw)


def _number_row(bksp_label, bksp_w):
    return ([_k("grave", "GRAVE", "^", "Zirkumflex", shift="°")]
            + [_digit(d) for d in "1234567890"]
            + [_k("sz", "MINUS", "ß", "ß", shift="?", altgr="\\"),
               _k("acute", "EQUAL", "´", "Akzent", shift="`"),
               _k("bksp", "BACKSPACE", bksp_label, "Rücktaste", w=bksp_w)])


def _qwertz(q=None, w=None, e=None):
    """Q … P on a German keyboard: the key next to T is Z and sends Y."""
    return [_letter("Q", altgr="@", **(q or {})), _letter("W", **(w or {})),
            _letter("E", altgr="€", **(e or {})), _letter("R"), _letter("T"),
            _letter("Z", code="Y"), _letter("U"), _letter("I"), _letter("O"), _letter("P")]


def _ue_plus():
    return [_k("ue", "LEFTBRACE", "Ü", "Ü"), _k("plus", "RIGHTBRACE", "+", "Plus", shift="*", altgr="~")]


def _home_row():
    return ([_letter(c) for c in "ASDFGHJKL"]
            + [_k("oe", "SEMICOLON", "Ö", "Ö"), _k("ae", "APOSTROPHE", "Ä", "Ä")])


def _hash():
    return _k("hash", "BACKSLASH", "#", "Raute", shift="'")


def _bottom_letters(y=None, x=None):
    return ([_letter("Y", code="Z", **(y or {})), _letter("X", **(x or {}))]
            + [_letter(c) for c in "CVBN"] + [_letter("M", altgr="µ")]
            + [_k("comma", "COMMA", ",", "Komma", shift=";"), _k("dot", "DOT", ".", "Punkt", shift=":"),
               _k("minus", "SLASH", "-", "Minus", shift="_")])


def _acme_bk03():
    tw = 14.4 / 14
    top = [
        ("cut", "CUT", "Schere (Ausschneiden)", "F1", None),
        ("copy", "COPY", "Kopieren", "F2", None),
        ("paste", "PASTE", "Einfügen (Zwischenablage)", "F3", None),
        ("globe", "WWW", "Weltkugel (Internet)", "F4", None),
        ("search", "SEARCH", "Lupe (Suchen)", "F5", None),
        ("prev", "PREVIOUSSONG", "Zurückspulen", "F6", "prevsound"),
        ("play", "PLAYPAUSE", "Abspielen / Pause", "F7", "replay"),
        ("next", "NEXTSONG", "Vorspulen", "F8", "nextsound"),
        ("mute", "MUTE", "Ton aus", "F9", "mute"),
        ("vold", "VOLUMEDOWN", "Leiser", "F10", "voldown"),
        ("volu", "VOLUMEUP", "Lauter", "F11", "volup"),
        ("screen", "SWITCHVIDEOMODE", "Bildschirm", "F12", None),
    ]
    row0 = [_k("esc", "ESC", "esc", "Escape", w=tw)]
    for slot, code, name, fn, sug in top:
        key = _k(slot, code, "", name, w=tw, icon=slot, fn=fn, guess=True)
        if sug:
            key["suggest"] = _fx(sug)
        row0.append(key)
    row0.append(_k("ins", "INSERT", "einfg", "Einfügen-Taste (einfg)", w=tw))
    return {
        "id": "acme-bk03", "name": "ACME BK03", "kind": "builtin", "base": "",
        "note": "Die Tastatur am Kinderplatz: kompakt, Medientasten oben (mit fn: F1–F12).",
        "hint": ("fn+Q / fn+W / fn+E schaltet die Tastatur auf Android / Windows / iOS um und ändert, "
                 "was manche Tasten senden. Die Codes der oberen Reihe sind vermutet: "
                 "im Lernmodus drücken bestätigt sie."),
        "rows": [
            {"h": 0.65, "keys": row0},
            {"keys": _number_row("rück", 1.4)},
            {"keys": [_k("tab", "TAB", "tab", "Tab", w=1.4)]
             + _qwertz(q={"fn": "And"}, w={"fn": "Win"}, e={"fn": "iOS"}) + _ue_plus() + [_hash()]},
            {"keys": [_k("caps", "CAPSLOCK", "feststell", "Feststelltaste", w=1.7)] + _home_row()
             + [_k("enter", "ENTER", "eingabe", "Eingabe", w=1.7)]},
            {"keys": [_k("lshift", "LEFTSHIFT", "umsch", "Umschalt links", w=2.2)]
             + _bottom_letters(y={"fn": "<"}, x={"fn": ">"})
             + [_k("rshift", "RIGHTSHIFT", "umsch", "Umschalt rechts", w=2.2)]},
            {"keys": [
                _k("lctrl", "LEFTCTRL", "strg", "Steuerung (strg)"),
                _k("fn", "", "fn", "fn"),
                _k("lmeta", "LEFTMETA", "cmd", "cmd / Windows", altgr="win"),
                _k("lalt", "LEFTALT", "alt", "alt / option", altgr="option", w=1.25),
                _k("space", "SPACE", "acme", "Leertaste", w=4.9),
                _k("altgr", "RIGHTALT", "alt gr", "Alt Gr / option", altgr="option", w=1.25),
                _k("del", "DELETE", "entf", "Entfernen (entf)"),
                _k("left", "LEFT", "←", "Pfeil links", fn="Pos1", h=0.5, dy=0.5),
                {"stack": [_k("up", "UP", "↑", "Pfeil hoch", fn="Bild↑"),
                           _k("down", "DOWN", "↓", "Pfeil runter", fn="Bild↓")], "w": 1},
                _k("right", "RIGHT", "→", "Pfeil rechts", fn="Ende", h=0.5, dy=0.5),
            ]},
        ],
    }


def _f_keys(a, b, **kw):
    return [_k(f"f{n}", f"F{n}", f"F{n}", f"F{n}", **kw) for n in range(a, b + 1)]


# ── languages and generic PC / Mac profiles (FUNK-64) ────────────────────────

LANGUAGES = {"de": "Deutsch (QWERTZ)", "us": "Englisch (US, QWERTY)"}
_LANG_NAMES = {"de": "Deutsch", "us": "Englisch (US)"}
FAMILIES = {"pc": "PC-Tastatur", "mac": "Mac-Tastatur"}
DEFAULT_ALIAS = "default"       # keyboard_profile "default" = the generic profile of language × family × number pad

_US_SHIFTED = dict(zip("1234567890", "!@#$%^&*()"))


def _us_number_row():
    return ([_k("grave", "GRAVE", "`", "Gravis", shift="~")]
            + [_k("d" + d, d, d, f"Ziffer {d}", shift=_US_SHIFTED[d]) for d in "1234567890"]
            + [_k("minus", "MINUS", "-", "Minus", shift="_"), _k("equal", "EQUAL", "=", "Gleich", shift="+"),
               _k("bksp", "BACKSPACE", "⌫", "Rücktaste", w=2)])


def _main_block(lang, mac):
    """The four rows from the digits to the bottom letters: 15 units wide."""
    tab = _k("tab", "TAB", "⇥" if mac else "tab", "Tab", w=1.5)
    if lang == "de":
        return [
            _number_row("⌫", 2),
            [tab] + _qwertz() + _ue_plus() + [_k("enter", "ENTER", "↵", "Eingabe", w=1.5)],
            [_k("caps", "CAPSLOCK", "⇪" if mac else "feststell", "Feststelltaste", w=1.75)] + _home_row()
            + [_hash(), _k("enter2", "ENTER", "", "Eingabe", w=1.25)],
            [_k("lshift", "LEFTSHIFT", "⇧" if mac else "umsch", "Umschalt links", w=1.25),
             _k("lt", "102ND", "<", "Kleiner / Größer", shift=">", altgr="|")] + _bottom_letters()
            + [_k("rshift", "RIGHTSHIFT", "⇧" if mac else "umsch", "Umschalt rechts", w=2.75)],
        ]
    return [
        _us_number_row(),
        [tab] + [_letter(c) for c in "QWERTYUIOP"]
        + [_k("lbracket", "LEFTBRACE", "[", "Eckige Klammer auf", shift="{"),
           _k("rbracket", "RIGHTBRACE", "]", "Eckige Klammer zu", shift="}"),
           _k("backslash", "BACKSLASH", "\\", "Backslash", shift="|", w=1.5)],
        [_k("caps", "CAPSLOCK", "⇪" if mac else "caps", "Feststelltaste", w=1.75)] + [_letter(c) for c in "ASDFGHJKL"]
        + [_k("semicolon", "SEMICOLON", ";", "Semikolon", shift=":"),
           _k("quote", "APOSTROPHE", "'", "Apostroph", shift='"'),
           _k("enter", "ENTER", "↵", "Eingabe", w=2.25)],
        [_k("lshift", "LEFTSHIFT", "⇧" if mac else "shift", "Umschalt links", w=2.25)] + [_letter(c) for c in "ZXCVBNM"]
        + [_k("comma", "COMMA", ",", "Komma", shift="<"), _k("dot", "DOT", ".", "Punkt", shift=">"),
           _k("slash", "SLASH", "/", "Schrägstrich", shift="?"),
           _k("rshift", "RIGHTSHIFT", "⇧" if mac else "shift", "Umschalt rechts", w=2.75)],
    ]


def _row_width(items):
    return sum(i["gap"] if "gap" in i else i.get("w", 1.0) for i in items)


def _with_numpad(rows, x0, mac, lang):
    """Append the number pad at x0 to the five rows below the F row."""
    def kp(d):
        return _k("kp" + d, "KP" + d, d, f"Ziffernblock {d}")
    minus = _k("kpminus", "KPMINUS", "-", "Ziffernblock Minus")
    slash = _k("kpslash", "KPSLASH", "/", "Ziffernblock Geteilt")
    star = _k("kpstar", "KPASTERISK", "*", "Ziffernblock Mal")
    dot = _k("kpdot", "KPDOT", "," if lang == "de" else ".", "Ziffernblock Komma" if lang == "de" else "Ziffernblock Punkt")
    if mac:     # clear = Num Lock on Linux; = on the pad; + is one unit high
        pad = [[_k("clear", "NUMLOCK", "⌧", "clear (Löschen)"), _k("kpequal", "KPEQUAL", "=", "Ziffernblock Gleich"), slash, star],
               [kp("7"), kp("8"), kp("9"), minus],
               [kp("4"), kp("5"), kp("6"), _k("kpplus", "KPPLUS", "+", "Ziffernblock Plus")]]
    else:
        pad = [[_k("numlock", "NUMLOCK", "num", "Num-Taste"), slash, star, minus],
               [kp("7"), kp("8"), kp("9"), _k("kpplus", "KPPLUS", "+", "Ziffernblock Plus", h=2)],
               [kp("4"), kp("5"), kp("6")]]
    pad += [[kp("1"), kp("2"), kp("3"), _k("kpenter", "KPENTER", "↵", "Ziffernblock Eingabe", h=2)],
            [_k("kp0", "KP0", "0", "Ziffernblock 0", w=2), dot]]
    for row, keys in zip(rows[1:], pad):
        row["keys"] = row["keys"] + [{"gap": round(x0 - _row_width(row["keys"]), 4)}] + keys
    return rows


def _generic_meta(family, lang, numpad):
    pid = default_id(lang, family, numpad)
    name = f"{'PC' if family == 'pc' else 'Mac'} {_LANG_NAMES[lang]}" + (" mit Ziffernblock" if numpad else "")
    return pid, name


def _pc(lang, numpad):
    us = lang == "us"
    main = _main_block(lang, mac=False)
    rows = [
        {"keys": [_k("esc", "ESC", "esc", "Escape"), {"gap": 1}] + _f_keys(1, 4) + [{"gap": 0.5}]
         + _f_keys(5, 8) + [{"gap": 0.5}] + _f_keys(9, 12)
         + [{"gap": 0.5}, _k("print", "SYSRQ", "prt sc" if us else "druck", "Druck"),
            _k("scroll", "SCROLLLOCK", "scroll" if us else "rollen", "Rollen"), _k("pause", "PAUSE", "pause", "Pause")]},
        {"before": 0.25, "keys": main[0]
         + [{"gap": 0.5}, _k("ins", "INSERT", "ins" if us else "einfg", "Einfügen (einfg)"),
            _k("home", "HOME", "home" if us else "pos1", "Pos1"), _k("pgup", "PAGEUP", "pg up" if us else "bild↑", "Bild hoch")]},
        {"keys": main[1]
         + [{"gap": 0.5}, _k("del", "DELETE", "del" if us else "entf", "Entfernen (entf)"),
            _k("end", "END", "end" if us else "ende", "Ende"), _k("pgdn", "PAGEDOWN", "pg dn" if us else "bild↓", "Bild runter")]},
        {"keys": main[2]},
        {"keys": main[3] + [{"gap": 1.5}, _k("up", "UP", "↑", "Pfeil hoch")]},
        {"keys": [_k("lctrl", "LEFTCTRL", "ctrl" if us else "strg", "Steuerung links", w=1.25),
                  _k("lmeta", "LEFTMETA", "win", "Windows-Taste", w=1.25),
                  _k("lalt", "LEFTALT", "alt", "alt", w=1.25),
                  _k("space", "SPACE", "", "Leertaste", w=6.25),
                  _k("altgr", "RIGHTALT", "alt" if us else "alt gr", "Alt rechts" if us else "Alt Gr", w=1.25),
                  _k("rmeta", "RIGHTMETA", "win", "Windows-Taste rechts", w=1.25),
                  _k("menu", "COMPOSE", "menu" if us else "menü", "Menü", w=1.25),
                  _k("rctrl", "RIGHTCTRL", "ctrl" if us else "strg", "Steuerung rechts", w=1.25),
                  {"gap": 0.5}, _k("left", "LEFT", "←", "Pfeil links"),
                  _k("down", "DOWN", "↓", "Pfeil runter"), _k("right", "RIGHT", "→", "Pfeil rechts")]},
    ]
    if numpad:
        _with_numpad(rows, 19.0, mac=False, lang=lang)
    pid, name = _generic_meta("pc", lang, numpad)
    note = (f"PC-Tastatur {'mit' if numpad else 'ohne'} Ziffernblock: F1–F12, "
            + ("ISO-Eingabetaste, <>-Taste." if lang == "de" else "US-Anordnung (ANSI)."))
    return {"id": pid, "name": name, "kind": "builtin", "base": "", "note": note, "hint": "", "rows": rows}


_MAC_HINT = ("Mac-Tastaturen senden oben je nach fn-Einstellung F-Tasten oder Helligkeit, Lautstärke … "
             "Die Codes dort sind vermutet: im Lernmodus drücken bestätigt sie.")


def _mac(lang, numpad):
    main = _main_block(lang, mac=True)
    fn = _k("fn", "FN", "fn", "fn (Mac-Tastaturen senden sie)", guess=True)
    if not numpad:      # compact, like the Magic Keyboard
        rows = [
            {"h": 0.6, "keys": [_k("esc", "ESC", "esc", "Escape", w=1.5)] + _f_keys(1, 12, guess=True)
             + [_k("eject", "EJECTCD", "⏏", "Auswerfen / Sperren", w=1.5, guess=True)]},
            {"keys": main[0]}, {"keys": main[1]}, {"keys": main[2]}, {"keys": main[3]},
            {"keys": [
                fn,
                _k("lctrl", "LEFTCTRL", "ctrl", "control"),
                _k("lalt", "LEFTALT", "⌥", "option", altgr="option"),
                _k("lmeta", "LEFTMETA", "⌘", "command", altgr="cmd", w=1.25),
                _k("space", "SPACE", "", "Leertaste", w=5.5),
                _k("rmeta", "RIGHTMETA", "⌘", "command rechts", altgr="cmd", w=1.25),
                _k("ralt", "RIGHTALT", "⌥", "option rechts", altgr="option"),
                _k("left", "LEFT", "←", "Pfeil links", h=0.5, dy=0.5),
                {"stack": [_k("up", "UP", "↑", "Pfeil hoch"), _k("down", "DOWN", "↓", "Pfeil runter")], "w": 1},
                _k("right", "RIGHT", "→", "Pfeil rechts", h=0.5, dy=0.5),
            ]},
        ]
    else:               # full size, like the Magic Keyboard with numeric keypad
        rows = [
            {"h": 0.6, "keys": [_k("esc", "ESC", "esc", "Escape"), {"gap": 1}] + _f_keys(1, 4, guess=True) + [{"gap": 0.5}]
             + _f_keys(5, 8, guess=True) + [{"gap": 0.5}] + _f_keys(9, 12, guess=True) + [{"gap": 0.5}]
             + _f_keys(13, 15, guess=True) + [{"gap": 0.5}] + _f_keys(16, 19, guess=True)},
            {"before": 0.25, "keys": main[0] + [{"gap": 0.5}, fn, _k("home", "HOME", "↖", "Pos1 (home)"),
                                                _k("pgup", "PAGEUP", "⇞", "Bild hoch")]},
            {"keys": main[1] + [{"gap": 0.5}, _k("del", "DELETE", "⌦", "Entfernen"), _k("end", "END", "↘", "Ende (end)"),
                                _k("pgdn", "PAGEDOWN", "⇟", "Bild runter")]},
            {"keys": main[2]},
            {"keys": main[3] + [{"gap": 1.5}, _k("up", "UP", "↑", "Pfeil hoch")]},
            {"keys": [_k("lctrl", "LEFTCTRL", "ctrl", "control", w=1.5),
                      _k("lalt", "LEFTALT", "⌥", "option", altgr="option", w=1.25),
                      _k("lmeta", "LEFTMETA", "⌘", "command", altgr="cmd", w=1.5),
                      _k("space", "SPACE", "", "Leertaste", w=6.5),
                      _k("rmeta", "RIGHTMETA", "⌘", "command rechts", altgr="cmd", w=1.5),
                      _k("ralt", "RIGHTALT", "⌥", "option rechts", altgr="option", w=1.25),
                      _k("rctrl", "RIGHTCTRL", "ctrl", "control rechts", w=1.5),
                      {"gap": 0.5}, _k("left", "LEFT", "←", "Pfeil links"),
                      _k("down", "DOWN", "↓", "Pfeil runter"), _k("right", "RIGHT", "→", "Pfeil rechts")]},
        ]
        _with_numpad(rows, 19.0, mac=True, lang=lang)
    pid, name = _generic_meta("mac", lang, numpad)
    note = (("Mac-Anordnung mit Ziffernblock (wie das Magic Keyboard mit Ziffernblock): F1–F19, fn, Pos1/Ende."
             if numpad else "Mac-Anordnung (wie das Magic Keyboard) mit fn, control, option und command.")
            + (" US-Anordnung (ANSI)." if lang == "us" else ""))
    return {"id": pid, "name": name, "kind": "builtin", "base": "", "note": note, "hint": _MAC_HINT, "rows": rows}


def default_id(lang, family, numpad):
    """The generic built-in profile for a language, PC or Mac, with or without number pad."""
    return f"{family}-{lang}" + ("-num" if numpad else "")


def language_of(settings):
    """The keyboard language (FUNK-64): settings["language"], else the older keyboard_layout, else de."""
    for value in (settings.get("language"), settings.get("keyboard_layout")):
        if value in LANGUAGES:
            return value
    return "de"


def family_of(settings):
    return settings.get("keyboard_family") if settings.get("keyboard_family") in FAMILIES else "pc"


def numpad_of(settings):
    return settings.get("keyboard_numpad") is True


BUILTIN = {p["id"]: p for p in [_acme_bk03()] + [
    build(lang, numpad) for lang in LANGUAGES for build in (_pc, _mac) for numpad in (False, True)]}


# ── geometry ─────────────────────────────────────────────────────────────────

def iter_keys(profile):
    """Every key of a profile, in drawing order (stacks flattened)."""
    for row in profile["rows"]:
        for item in row["keys"]:
            if "stack" in item:
                yield from item["stack"]
            elif "gap" not in item:
                yield item


def layout(profile):
    """Absolute geometry for drawing, in key units: {"width", "height", "keys": [...]}.

    Each key carries its legends plus x, y, w, h. Keys in a stack split the row height.
    """
    keys, y, width = [], 0.0, 0.0

    def place(key, x, ky, w, h, stacked=False):
        out = {f: key[f] for f in _KEY_FIELDS if f in key and f not in ("w", "h", "dy")}
        out.update(x=round(x, 4), y=round(ky, 4), w=round(w, 4), h=round(h, 4))
        if stacked:
            out["stacked"] = True       # shares the stack's width: not editable per key
        keys.append(out)

    for row in profile["rows"]:
        y += row.get("before", 0.0)
        rh = row.get("h", 1.0)
        x = 0.0
        for item in row["keys"]:
            if "gap" in item:
                x += item["gap"]
                continue
            w = item.get("w", 1.0)
            if "stack" in item:
                n = len(item["stack"])
                for i, key in enumerate(item["stack"]):
                    place(key, x, y + i * rh / n, w, rh / n, stacked=True)
            else:
                place(item, x, y + item.get("dy", 0.0), w, item.get("h", rh))
            x += w
            width = max(width, x)
        y += rh
    return {"width": round(width, 4), "height": round(y, 4), "keys": keys}


def render(profile):
    """A profile for the UI: its metadata plus its layout."""
    out = {f: profile.get(f, "") for f in ("id", "name", "kind", "base", "note", "hint")}
    out["rev"] = profile.get("rev", 0)        # grows with every edit: the UI never installs an older snapshot
    out.update(layout(profile))
    return out


def summary(profile):
    """Metadata plus a small silhouette ([x, y, w, h] per key) for the profile list."""
    geo = layout(profile)
    out = {f: profile.get(f, "") for f in ("id", "name", "kind", "base", "note")}
    out.update(width=geo["width"], height=geo["height"], keys=len(geo["keys"]),
               shape=[[k["x"], k["y"], k["w"], k["h"], 1 if k.get("icon") else 0] for k in geo["keys"]])
    return out


def codes(profile):
    """The distinct key-map ids the keys of a profile send (without "")."""
    seen = []
    for key in iter_keys(profile):
        if key.get("code") and key["code"] not in seen:
            seen.append(key["code"])
    return seen


# ── validation ───────────────────────────────────────────────────────────────

def _num(value, rng, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not rng[0] <= value <= rng[1]:
        raise ProfileError(f"{what} must be a number {rng[0]}..{rng[1]}")
    return float(value)


def _text(value, limit, what):
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ProfileError(f"{what} must be text of at most {limit} characters")
    return value


def valid_code(code):
    """A key code: "" (sends nothing) or a canonical key-map id; Shift counts too."""
    if code == "" or code in SHIFT_CODES:
        return code
    try:
        keymap.parse_key_id(code)
    except keymap.KeymapError as e:
        raise ProfileError(f"invalid key code {code!r}: {e}") from None
    return code


def _norm_key(key):
    if not isinstance(key, dict):
        raise ProfileError("a key must be an object")
    slot = key.get("slot")
    if not isinstance(slot, str) or not _SLOT.fullmatch(slot):
        raise ProfileError(f"invalid key slot {slot!r}")
    out = {"slot": slot, "code": valid_code(key.get("code", "")),
           "label": _text(key.get("label"), LEGEND_MAX["label"], "label"),
           "name": _text(key.get("name"), NAME_MAX, "name")}
    for f in ("shift", "altgr", "fn"):
        if key.get(f):
            out[f] = _text(key[f], LEGEND_MAX[f], f)
    if key.get("icon"):
        if key["icon"] not in ICONS:
            raise ProfileError(f"icon must be one of {', '.join(ICONS)}")
        out["icon"] = key["icon"]
    if "w" in key:
        out["w"] = _num(key["w"], W_RANGE, "width")
    if "h" in key:
        out["h"] = _num(key["h"], H_RANGE, "height")
    if "dy" in key:
        out["dy"] = _num(key["dy"], DY_RANGE, "offset")
    if key.get("guess"):
        out["guess"] = True
    if key.get("suggest") is not None:
        try:
            sug = keymap.validate(key["suggest"])
        except keymap.KeymapError as e:
            raise ProfileError(f"invalid suggestion: {e}") from None
        if sug["action"] not in SUGGEST_ACTIONS:
            raise ProfileError("a suggestion must be a function, a letter or nothing")
        out["suggest"] = sug
    return out


def validate(profile):
    """Normalize a custom profile (only known fields survive); raise ProfileError if invalid."""
    if not isinstance(profile, dict):
        raise ProfileError("a profile must be an object")
    pid = profile.get("id")
    if not isinstance(pid, str) or not _CUSTOM_ID.fullmatch(pid):
        raise ProfileError(f"invalid custom profile id {pid!r}")
    name = _text(profile.get("name"), PROFILE_NAME_MAX, "name").strip()
    if not name:
        raise ProfileError("a profile needs a name")
    rows = profile.get("rows")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
        raise ProfileError(f"a profile needs 1..{MAX_ROWS} rows")
    out_rows, slots = [], set()

    def key(k):
        nk = _norm_key(k)
        if nk["slot"] in slots:
            raise ProfileError(f"duplicate key slot {nk['slot']!r}")
        slots.add(nk["slot"])
        return nk

    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("keys"), list):
            raise ProfileError("a row must be an object with a key list")
        out = {"keys": []}
        if "h" in row:
            out["h"] = _num(row["h"], H_RANGE, "row height")
        if "before" in row:
            out["before"] = _num(row["before"], DY_RANGE, "row spacing")
        for item in row["keys"]:
            if isinstance(item, dict) and "gap" in item:
                out["keys"].append({"gap": _num(item["gap"], GAP_RANGE, "gap")})
            elif isinstance(item, dict) and "stack" in item:
                stack = item["stack"]
                if not isinstance(stack, list) or not 2 <= len(stack) <= 3:
                    raise ProfileError("a stack holds 2 or 3 keys")
                out["keys"].append({"stack": [key(k) for k in stack],
                                    "w": _num(item.get("w", 1.0), W_RANGE, "width")})
            else:
                out["keys"].append(key(item))
        out_rows.append(out)
    if not slots or len(slots) > MAX_KEYS:
        raise ProfileError(f"a profile needs 1..{MAX_KEYS} keys")
    base = profile.get("base", "")
    rev = profile.get("rev", 0)
    return {"id": pid, "name": name, "kind": "custom",
            "rev": rev if isinstance(rev, int) and not isinstance(rev, bool) and rev >= 0 else 0,
            "base": base if isinstance(base, str) and len(base) <= 60 else "",
            "note": _text(profile.get("note"), NOTE_MAX, "note"),
            "hint": _text(profile.get("hint"), NOTE_MAX, "hint"),
            "rows": out_rows}


def customs(stored):
    """Valid custom profiles from settings ({id: profile}); invalid ones are skipped."""
    out = {}
    for pid, prof in (stored or {}).items():
        try:
            p = validate(prof)
        except ProfileError:
            continue
        if p["id"] == pid:
            out[pid] = p
    return out


def get(profile_id, custom):
    """A profile by id: a built-in or one of the (validated) custom profiles, else None."""
    return BUILTIN.get(profile_id) or (custom or {}).get(profile_id)


def all_profiles(custom):
    return list(BUILTIN.values()) + sorted((custom or {}).values(), key=lambda p: p["name"].lower())


# ── custom profile edits ─────────────────────────────────────────────────────

_UMLAUTS = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}


def new_id(name, existing):
    """A free custom-<slug> id for a profile name."""
    slug = "".join(_UMLAUTS.get(c, c) for c in name.lower())
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")[:30] or "tastatur"
    pid, n = f"custom-{slug}", 2
    while pid in existing or pid in BUILTIN:
        pid, n = f"custom-{slug}-{n}", n + 1
    return pid


def copy_name(name, suffix=" (Kopie)"):
    """A default name for a copy that still fits the name limit."""
    return name[:PROFILE_NAME_MAX - len(suffix)].rstrip() + suffix


def duplicate(source, pid, name):
    """A custom copy of a profile (built-in or custom) under a new id and name."""
    name = _text(name, PROFILE_NAME_MAX, "name").strip()
    if not name:
        raise ProfileError("a profile needs a name")
    copy_ = copy.deepcopy(source)
    copy_.update(id=pid, name=name, kind="custom", base=source.get("base") or source["id"], rev=0)
    return validate(copy_)


def _next_rev(profile):
    out = copy.deepcopy(profile)
    out["rev"] = profile.get("rev", 0) + 1
    return out


def rename(profile, name=None, note=None):
    out = _next_rev(profile)
    if name is not None:
        out["name"] = name
    if note is not None:
        out["note"] = note
    return validate(out)


def edit_key(profile, slot, patch):
    """Change a key's legends, code, icon or width; a new code is no longer a guess."""
    if not isinstance(patch, dict) or not patch:
        raise ProfileError("nothing to change")
    unknown = set(patch) - set(EDITABLE)
    if unknown:
        raise ProfileError(f"cannot edit {', '.join(sorted(unknown))}")
    out = _next_rev(profile)
    for key in iter_keys(out):
        if key["slot"] == slot:
            break
    else:
        raise ProfileError(f"no key {slot!r} in this profile")
    in_stack = any(slot in [k["slot"] for k in item["stack"]]
                   for row in out["rows"] for item in row["keys"] if "stack" in item)
    if "w" in patch and in_stack:
        raise ProfileError("keys in a stack share the stack's width")
    if "code" in patch and patch["code"] != key.get("code"):
        key.pop("guess", None)
    for f, value in patch.items():
        if f in ("shift", "altgr", "fn", "icon") and not value:
            key.pop(f, None)
        else:
            key[f] = value
    return validate(out)


# ── export / import as JSON (FUNK-64) ────────────────────────────────────────

EXPORT_FORMAT = "funkeykid.keyboard.v1"
MAX_IMPORT_KEYMAP = 500


def export_bundle(profile, entries):
    """A profile (built-in or custom) and its key map, as one JSON-ready file."""
    prof = {f: copy.deepcopy(profile[f]) for f in ("id", "name", "kind", "base", "note", "hint", "rows") if f in profile}
    return {"format": EXPORT_FORMAT, "profile": prof, "keymap": copy.deepcopy(entries or {})}


def import_bundle(bundle, pid, name=None):
    """(custom profile, key map) from an exported file, under a new id. Raises ProfileError.

    Everything is validated like an edit in the UI: the profile as a custom
    profile, every key-map entry as a canonical key id with a valid action.
    Whether sound and image files exist is the server's check.
    """
    if not isinstance(bundle, dict) or bundle.get("format") != EXPORT_FORMAT:
        raise ProfileError(f"not a funkeykid keyboard file (format {EXPORT_FORMAT} expected)")
    src = bundle.get("profile")
    if not isinstance(src, dict):
        raise ProfileError("the file has no keyboard profile")
    prof = copy.deepcopy(src)
    base = src.get("base") or src.get("id") or ""
    prof.update(id=pid, kind="custom", rev=0, base=base if isinstance(base, str) else "")
    if name is not None:
        prof["name"] = name
    prof = validate(prof)
    entries = bundle.get("keymap") or {}
    if not isinstance(entries, dict) or len(entries) > MAX_IMPORT_KEYMAP:
        raise ProfileError(f"the key map must be an object with at most {MAX_IMPORT_KEYMAP} entries")
    out = {}
    for kid, action in entries.items():
        try:
            keymap.parse_key_id(kid)
            out[kid] = keymap.validate(action)
        except keymap.KeymapError as e:
            raise ProfileError(f"invalid key map entry {kid!r}: {e}") from None
    return prof, out
