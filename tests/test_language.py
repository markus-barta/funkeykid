"""Keyboard language, "Standard" profile, number pad, export/import (FUNK-64).

Run: python -m pytest tests/ (no evdev or hardware needed).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import keyboard as kb  # noqa: E402
import keymap as km  # noqa: E402
import profiles as pf  # noqa: E402

# The layouts as they were before FUNK-64 (keyboard.py at 261125122211.0.0)
OLD_LAYOUTS = {
    "us": {},
    "de": {"Y": "Z", "Z": "Y", "RIGHTBRACE": "EQUAL", "SLASH": "MINUS"},
}


# ── AC1: existing settings migrate without behaviour change ──────────────────

@pytest.mark.parametrize("stored,lang", [
    ({}, "de"), ({"keyboard_layout": "de"}, "de"), ({"keyboard_layout": "us"}, "us"),
    ({"keyboard_layout": "fr"}, "de"), ({"language": "us", "keyboard_layout": "de"}, "us"),
    ({"language": "xx", "keyboard_layout": "us"}, "us"), ({"language": None}, "de"),
])
def test_language_migrates_from_keyboard_layout(stored, lang):
    assert pf.language_of(stored) == lang


def test_languages_are_the_keyboard_layouts():
    assert set(pf.LANGUAGES) == set(kb.LAYOUTS) == set(OLD_LAYOUTS)


@pytest.mark.parametrize("lang", ["de", "us"])
def test_every_old_mapping_is_unchanged_and_new_ones_only_give_keys_without_a_role_one(lang):
    """A key that did something before does the same; only keys that had no built-in
    role (umlaut keys, number pad) gain one (FUNK-64 AC1)."""
    old, new = OLD_LAYOUTS[lang], kb.LAYOUTS[lang]
    for raw, key in old.items():
        assert new[raw] == key
    for raw in set(new) - set(old) - DELIBERATE[lang]:
        assert km.default_label(raw) is None, raw          # nothing specific before
        assert km.default_label(new[raw]) is not None, raw  # a letter, a digit or Enter now


# The only key that gave up a built-in role, on Markus's decision (FUNK-46, 2026-11-26):
# the German ß key (MINUS) plays ß; − (SLASH) and the volume keys still lower the volume.
DELIBERATE = {"de": {"MINUS"}, "us": set()}


def test_sz_is_a_letter_on_german_keyboards():
    de, us = kb.LAYOUTS["de"], kb.LAYOUTS["us"]
    assert de["MINUS"] == "ß" and de["SLASH"] == "MINUS"
    assert km.default_label("MINUS") == "Leiser"           # the − key, unchanged
    assert "MINUS" not in us                               # US: the - key lowers the volume as before
    assert km.default_label("ß") == "Buchstabe ß (Set-Inhalt)" and km.default_kind("ß") == "letter"
    assert not km.is_unassigned("ß", "MINUS", None)
    assert km.validate({"action": "letter", "letter": "ß"}) == {"action": "letter", "letter": "ß"}
    assert km.validate({"action": "letter", "letter": "ẞ"}) == {"action": "letter", "letter": "ß"}
    assert km.LETTERS[-4:] == ("Ä", "Ö", "Ü", "ß") and km.LETTER_TARGETS.count("ß") == 1


@pytest.mark.parametrize("raw,stored", [("ß", "ß"), ("ẞ", "ß"), ("SS", "ß"), ("ss", "ß"), ("a", "A"), ("ä", "Ä"),
                                        ("Ü", "Ü"), ("7", "7"), (None, None)])
def test_norm_letter_keeps_sz(raw, stored):
    assert "ß".upper() == "SS"                             # why a plain upper() is wrong
    assert km.norm_letter(raw) == stored


def test_de_umlauts_and_number_pad():
    de = kb.LAYOUTS["de"]
    assert (de["LEFTBRACE"], de["SEMICOLON"], de["APOSTROPHE"]) == ("Ü", "Ö", "Ä")
    assert (de["Y"], de["Z"]) == ("Z", "Y")
    for lang in ("de", "us"):
        assert all(kb.LAYOUTS[lang][f"KP{d}"] == d for d in "0123456789")
        assert kb.LAYOUTS[lang]["KPENTER"] == "ENTER"
    assert "Y" not in kb.LAYOUTS["us"] and "LEFTBRACE" not in kb.LAYOUTS["us"]


def test_umlauts_are_letters_with_set_content():
    assert km.default_label("Ü") == "Buchstabe Ü (Set-Inhalt)" and km.default_kind("Ä") == "letter"
    assert not km.is_unassigned("Ö", "SEMICOLON", None)
    assert km.validate({"action": "letter", "letter": "ü"}) == {"action": "letter", "letter": "Ü"}


def test_number_pad_digits_survive_the_listener(monkeypatch):
    """A pad digit reaches the callback as the digit; Shift + pad digit is a favourite."""
    from types import SimpleNamespace
    from test_keymap import _FakeEvdev, _ev
    monkeypatch.setattr(kb, "evdev", _FakeEvdev)
    listener = kb.KeyboardListener("Any", layout="de", debounce_seconds=0)
    got = []
    listener.on_key(lambda *args: got.append(args))
    dev = SimpleNamespace(path="/dev/input/event3")
    for ev in (_ev("KP7", 1), _ev("KPENTER", 1), _ev("LEFTSHIFT", 1, code=42), _ev("KP3", 1)):
        listener._handle_event(dev, ev)
    assert got == [("7", "KP7", ()), ("ENTER", "KPENTER", ()), ("LEFTSHIFT", "LEFTSHIFT", ()), ("SHIFT_3", "KP3", ())]


# ── AC2: the language decides the letters and what "Standard" means ─────────

@pytest.mark.parametrize("lang,family,numpad,pid", [
    ("de", "pc", False, "pc-de"), ("de", "mac", True, "mac-de-num"),
    ("us", "pc", True, "pc-us-num"), ("us", "mac", False, "mac-us"),
])
def test_default_profile(lang, family, numpad, pid):
    assert pf.default_id(lang, family, numpad) == pid and pid in pf.BUILTIN


def test_family_and_numpad_defaults():
    assert (pf.family_of({}), pf.numpad_of({})) == ("pc", False)
    assert (pf.family_of({"keyboard_family": "mac"}), pf.numpad_of({"keyboard_numpad": True})) == ("mac", True)
    assert (pf.family_of({"keyboard_family": "amiga"}), pf.numpad_of({"keyboard_numpad": "yes"})) == ("pc", False)


def test_generic_profiles_draw_their_language():
    de = {k["slot"]: k for k in pf.iter_keys(pf.BUILTIN["pc-de"])}
    us = {k["slot"]: k for k in pf.iter_keys(pf.BUILTIN["pc-us"])}
    assert (de["z"]["code"], us["z"]["code"]) == ("Y", "Z")          # QWERTZ vs QWERTY
    assert de["ue"]["label"] == "Ü" and us["lbracket"]["label"] == "["
    assert "lt" in de and "lt" not in us                                # ISO <> key only on German keyboards


# ── AC3: export then import round-trips a custom profile and its key map ────

def test_export_import_round_trip():
    src = pf.edit_key(pf.duplicate(pf.BUILTIN["acme-bk03"], "custom-kinder", "Kinder"), "cut", {"label": "✂"})
    entries = {"CUT": {"action": "sound", "file": "schere.mp3", "volume": 120},
               "CTRL+X": {"action": "func", "func": "nextset"}, "LEFTBRACE": {"action": "letter", "letter": "Ü"}}
    bundle = pf.export_bundle(src, entries)
    assert bundle["format"] == pf.EXPORT_FORMAT
    prof, imported = pf.import_bundle(bundle, "custom-kinder-2", None)
    assert imported == entries
    assert prof["id"] == "custom-kinder-2" and prof["kind"] == "custom" and prof["rev"] == 0
    assert (prof["name"], prof["base"]) == ("Kinder", "acme-bk03")
    assert prof["rows"] == src["rows"]
    assert pf.import_bundle(bundle, "custom-x", "Neu")[0]["name"] == "Neu"


def test_a_builtin_exports_and_imports_as_a_custom_copy():
    prof, imported = pf.import_bundle(pf.export_bundle(pf.BUILTIN["mac-de-num"], {}), "custom-mac", None)
    assert prof["kind"] == "custom" and prof["base"] == "mac-de-num" and imported == {}
    assert pf.layout(prof)["keys"] == pf.layout(pf.BUILTIN["mac-de-num"])["keys"]


@pytest.mark.parametrize("mutate", [
    lambda b: b.update(format="something.else"),
    lambda b: b.pop("profile"),
    lambda b: b["profile"].update(rows=[]),
    lambda b: b["profile"]["rows"][0]["keys"].append({"slot": "esc", "code": "ESC"}),
    lambda b: b.update(keymap=[1]),
    lambda b: b["keymap"].update({"LEFTSHIFT": {"action": "none"}}),
    lambda b: b["keymap"].update({"cut": {"action": "none"}}),
    lambda b: b["keymap"].update({"CUT": {"action": "sound", "file": "../settings.json"}}),
    lambda b: b.update(keymap={f"F{n}": {"action": "none"} for n in range(1, 25)} | {f"K{n}": {"action": "none"} for n in range(500)}),
])
def test_import_rejects_broken_files(mutate):
    bundle = pf.export_bundle(pf.duplicate(pf.BUILTIN["acme-bk03"], "custom-k", "K"), {"CUT": {"action": "none"}})
    mutate(bundle)
    with pytest.raises(pf.ProfileError):
        pf.import_bundle(bundle, "custom-new", None)


def test_import_ignores_kind_rev_and_unknown_fields():
    bundle = pf.export_bundle(pf.BUILTIN["pc-us"], {})
    bundle["profile"].update(kind="builtin", rev=99, evil="<script>")
    prof, _ = pf.import_bundle(bundle, "custom-u", None)
    assert prof["kind"] == "custom" and prof["rev"] == 0 and "evil" not in prof
