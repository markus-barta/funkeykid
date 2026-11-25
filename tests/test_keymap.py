"""Key map (FUNK-62): ids, validation, resolution, built-in labels, modifier tracking.

Run: python -m pytest tests/ (no evdev or hardware needed).
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import keyboard as kb  # noqa: E402
import keymap as km  # noqa: E402


# ── ids ──────────────────────────────────────────────────────────────────────

def test_key_id_is_raw_code_plus_canonical_modifiers():
    assert km.key_id("CUT") == "CUT"
    assert km.key_id("KEY_cut") == "CUT"
    assert km.key_id("X", ("LEFTMETA", "RIGHTCTRL", "LEFTCTRL")) == "CTRL+META+X"
    assert km.key_id("X", ("LEFTALT", "LEFTCTRL")) == "CTRL+ALT+X"
    # a modifier pressed alone is just itself, never CTRL+LEFTCTRL
    assert km.key_id("LEFTCTRL", ("LEFTCTRL",)) == "LEFTCTRL"


def test_parse_key_id_accepts_only_canonical_ids():
    assert km.parse_key_id("CUT") == ("CUT", ())
    assert km.parse_key_id("CTRL+ALT+X") == ("X", ("CTRL", "ALT"))
    for bad in ("", "cut", "ALT+CTRL+X", "CTRL+CTRL+X", "SHIFT+X", "X+", "A B", "../X", "X" * 41, None, 3):
        with pytest.raises(km.KeymapError):
            km.parse_key_id(bad)


def test_lookup_chain_tries_combo_then_plain_key():
    assert km.lookup_chain("X", ("LEFTCTRL",)) == ["CTRL+X", "X"]
    assert km.lookup_chain("CUT") == ["CUT"]


def test_resolve_override_wins_and_falls_back_to_plain_key():
    m = {"CTRL+X": {"action": "func", "func": "volup"}, "X": {"action": "none"}, "CUT": {"action": "none"}}
    assert km.resolve(m, "X", ("LEFTCTRL",)) == ("CTRL+X", m["CTRL+X"])
    assert km.resolve(m, "X", ("LEFTALT",)) == ("X", m["X"])         # ALT+X unmapped → plain X
    assert km.resolve(m, "CUT") == ("CUT", m["CUT"])
    assert km.resolve(m, "A") == (None, None)                       # built-in behaviour
    assert km.resolve({}, "A") == (None, None) and km.resolve(None, "A") == (None, None)


# ── actions ──────────────────────────────────────────────────────────────────

def test_validate_normalizes_every_action_kind():
    assert km.validate({"action": "none", "junk": 1}) == {"action": "none"}
    assert km.validate({"action": "letter", "letter": "a"}) == {"action": "letter", "letter": "A"}
    assert km.validate({"action": "letter", "letter": "ä"}) == {"action": "letter", "letter": "Ä"}
    assert km.validate({"action": "letter", "letter": "ß"}) == {"action": "letter", "letter": "ß"}
    assert km.validate({"action": "letter", "letter": "7"}) == {"action": "letter", "letter": "7"}
    assert km.validate({"action": "func", "func": "nextset"}) == {"action": "func", "func": "nextset"}
    assert km.validate({"action": "sound", "file": "schere.mp3"}) == {"action": "sound", "file": "schere.mp3", "volume": 100}
    assert km.validate({"action": "imgsound", "file": "s.mp3", "image": "s.png", "word": " schere ", "volume": 150}) == {
        "action": "imgsound", "file": "s.mp3", "volume": 150, "image": "s.png", "word": "schere"}


@pytest.mark.parametrize("bad", [
    None, [], {"action": "explode"},
    {"action": "letter", "letter": "AB"}, {"action": "letter"},
    {"action": "func", "func": "reboot"},
    {"action": "sound"}, {"action": "sound", "file": "../settings.json"}, {"action": "sound", "file": "a/b.mp3"},
    {"action": "sound", "file": ".hidden.mp3"}, {"action": "sound", "file": "x.mp3", "volume": 201},
    {"action": "sound", "file": "x.mp3", "volume": True}, {"action": "sound", "file": "x.mp3", "volume": "80"},
    {"action": "imgsound", "file": "x.mp3"}, {"action": "imgsound", "file": "x.mp3", "image": "x.png", "word": "w" * 25},
])
def test_validate_rejects(bad):
    with pytest.raises(km.KeymapError):
        km.validate(bad)


def test_every_function_has_a_label_and_describe():
    assert set(km.FUNCTIONS) >= {"volup", "voldown", "mute", "stop", "replay", "fav", "nextset", "random",
                                 "prevsound", "nextsound", "prevletter", "nextletter"}
    assert km.describe({"action": "func", "func": "volup"}) == "Lauter"
    assert km.describe({"action": "none"}) == "Nichts"
    assert km.describe(None) == ""


# ── built-in behaviour (the default map = today's behaviour) ─────────────────

def test_default_labels_cover_todays_special_keys():
    for key in ("SPACE", "EQUAL", "KPPLUS", "MINUS", "KPMINUS", "ENTER", "TAB", "LEFT", "RIGHT", "UP", "DOWN"):
        assert km.default_label(key)
    assert km.default_label("A").startswith("Buchstabe A")
    assert km.default_label("7").startswith("Ziffer 7")
    assert km.default_label("SHIFT_3") == "Favorit 3 abspielen"
    assert km.default_label("CUT") is None
    assert km.default_label("LEFTBRACE") is None and km.default_label("LEFTBRACE", has_content=True)


def test_unassigned_means_no_entry_and_no_builtin_role():
    assert km.is_unassigned("CUT", "CUT", None)
    assert not km.is_unassigned("CUT", "CUT", {"action": "none"})     # explicitly silent is assigned
    assert not km.is_unassigned("A", "A", None)
    assert not km.is_unassigned("Z", "Y", None)                         # German Z sends Y
    for mod in ("LEFTCTRL", "RIGHTALT", "LEFTSHIFT", "CAPSLOCK"):
        assert not km.is_unassigned(mod, mod, None)


# ── keyboard.py passes held modifiers to the callback ────────────────────────

class _FakeEvdev:
    class KeyEvent:
        key_up, key_down, key_hold = 0, 1, 2

    ecodes = SimpleNamespace(EV_KEY=1, KEY_LEFTSHIFT=42, KEY_RIGHTSHIFT=54)

    @staticmethod
    def categorize(event):
        return SimpleNamespace(keycode="KEY_" + event.name, keystate=event.value)


def _ev(name, value, code=0):
    return SimpleNamespace(type=1, code=code, value=value, name=name)


def test_listener_passes_held_modifiers(monkeypatch):
    monkeypatch.setattr(kb, "evdev", _FakeEvdev)
    listener = kb.KeyboardListener("ACME BK03", layout="de", debounce_seconds=0)
    got = []
    listener.on_key(lambda *args: got.append(args))
    dev = SimpleNamespace(path="/dev/input/event7")
    for ev in (_ev("LEFTCTRL", 1), _ev("X", 1), _ev("X", 0), _ev("LEFTCTRL", 0), _ev("X", 1), _ev("Y", 1), _ev("CUT", 1)):
        listener._handle_event(dev, ev)
    assert got == [
        ("LEFTCTRL", "LEFTCTRL", ()),
        ("X", "X", ("LEFTCTRL",)),
        ("X", "X", ()),
        ("Z", "Y", ()),             # German layout: physical Z sends Y
        ("CUT", "CUT", ()),
    ]
    assert km.key_id(got[1][1], got[1][2]) == "CTRL+X"


# ── review round 1 ───────────────────────────────────────────────────────────

def test_non_string_function_is_a_validation_error_not_a_crash():
    for bad in ([], {}, 3, None):
        with pytest.raises(km.KeymapError):
            km.validate({"action": "func", "func": bad})


def test_ids_a_press_can_never_produce_are_rejected():
    for bad in ("KEY_CUT", "CTRL+LEFTCTRL", "META+RIGHTMETA", "SHIFT_1", "CTRL+SHIFT_1"):
        with pytest.raises(km.KeymapError):
            km.parse_key_id(bad)
    # every id the listener produces round-trips
    for raw, mods in (("CUT", ()), ("X", ("LEFTCTRL",)), ("X", ("LEFTMETA", "LEFTALT", "RIGHTCTRL")), ("LEFTCTRL", ())):
        kid = km.key_id(raw, mods)
        assert km.key_id(*km.parse_key_id(kid)) == kid


def test_chords_and_shift_digit_survive_the_default_debounce(monkeypatch):
    """Ctrl then X (or Shift then 1) within the 0.8 s any-key debounce: the
    modified key must still reach the callback; two plain keys stay debounced."""
    monkeypatch.setattr(kb, "evdev", _FakeEvdev)
    listener = kb.KeyboardListener("ACME BK03", layout="de")          # default debounce 0.8 s
    assert listener.debounce_seconds == 0.8
    got = []
    listener.on_key(lambda *args: got.append(args))
    dev = SimpleNamespace(path="/dev/input/event7")
    for ev in (_ev("LEFTCTRL", 1), _ev("X", 1), _ev("X", 0), _ev("LEFTCTRL", 0)):
        listener._handle_event(dev, ev)
    assert got == [("LEFTCTRL", "LEFTCTRL", ()), ("X", "X", ("LEFTCTRL",))]

    listener2 = kb.KeyboardListener("ACME BK03", layout="de")
    got2 = []
    listener2.on_key(lambda *args: got2.append(args))
    for ev in (_ev("LEFTSHIFT", 1, code=42), _ev("1", 1), _ev("1", 0), _ev("LEFTSHIFT", 0, code=42), _ev("A", 1), _ev("B", 1)):
        listener2._handle_event(dev, ev)
    assert [g[0] for g in got2] == ["LEFTSHIFT", "SHIFT_1"]          # A within 0.8 s of SHIFT_1 → debounced, as before


def test_modifier_on_main_node_counts_for_media_key_on_sibling_node(monkeypatch):
    """Review round 3: Ctrl on the keyboard node + VolumeUp on its Consumer Control node."""
    monkeypatch.setattr(kb, "evdev", _FakeEvdev)
    listener = kb.KeyboardListener("ACME BK03", layout="de")
    got = []
    listener.on_key(lambda *args: got.append(args))
    main, consumer = SimpleNamespace(path="/dev/input/event12"), SimpleNamespace(path="/dev/input/event13")
    listener._handle_event(main, _ev("LEFTCTRL", 1))
    listener._handle_event(consumer, _ev("VOLUMEUP", 1))
    listener._handle_event(main, _ev("LEFTCTRL", 0))
    listener._handle_event(consumer, _ev("VOLUMEUP", 0))
    assert got == [("LEFTCTRL", "LEFTCTRL", ()), ("VOLUMEUP", "VOLUMEUP", ("LEFTCTRL",))]
    # after the release on the main node, a later media key carries no modifier
    quick = kb.KeyboardListener("ACME BK03", layout="de", debounce_seconds=0)
    got2 = []
    quick.on_key(lambda *args: got2.append(args))
    for dev, ev in ((main, _ev("LEFTCTRL", 1)), (consumer, _ev("VOLUMEUP", 1)), (main, _ev("LEFTCTRL", 0)), (consumer, _ev("MUTE", 1))):
        quick._handle_event(dev, ev)
    assert got2[-2:] == [("VOLUMEUP", "VOLUMEUP", ("LEFTCTRL",)), ("MUTE", "MUTE", ())]
