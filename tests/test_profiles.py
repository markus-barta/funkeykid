"""Keyboard profiles (FUNK-63): built-in data, geometry, validation, custom edits.

Run: python -m pytest tests/ (no evdev or hardware needed).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import keymap as km  # noqa: E402
import profiles as pf  # noqa: E402


def _slots(profile):
    return [k["slot"] for k in pf.iter_keys(profile)]


def _key(profile, slot):
    return next(k for k in pf.iter_keys(profile) if k["slot"] == slot)


# ── built-in profiles ────────────────────────────────────────────────────────

def test_builtins_are_the_three_approved_keyboards_and_default_is_bk03():
    assert list(pf.BUILTIN) == ["acme-bk03", "pc-de", "mac-de"]
    assert pf.DEFAULT_PROFILE == km.DEFAULT_PROFILE == "acme-bk03"


@pytest.mark.parametrize("pid", list(pf.BUILTIN))
def test_builtin_profiles_validate_as_custom_copies(pid):
    # every built-in passes the same validation a custom copy must pass
    copy_ = pf.duplicate(pf.BUILTIN[pid], "custom-x", "X")
    assert _slots(copy_) == _slots(pf.BUILTIN[pid])
    assert len(set(_slots(copy_))) == len(_slots(copy_))
    for key in pf.iter_keys(pf.BUILTIN[pid]):
        assert pf.valid_code(key["code"]) == key["code"]
        assert key["name"]


@pytest.mark.parametrize("pid,width", [("acme-bk03", 14.4), ("pc-de", 18.5), ("mac-de", 15.0)])
def test_rows_line_up(pid, width):
    """Every row of the main block ends at the same edge: no key is missing or too wide."""
    geo = pf.layout(pf.BUILTIN[pid])
    assert geo["width"] == pytest.approx(width, abs=0.01)
    for i, row in enumerate(pf.BUILTIN[pid]["rows"]):
        edge = sum(item.get("gap", item.get("w", 1.0)) for item in row["keys"])
        if pid == "pc-de" and i == 4:       # PC: right shift, then the ↑ of the arrow cluster
            edge = sum(item.get("gap", item.get("w", 1.0)) for item in row["keys"][:-2])
            width_ = 15.0
        elif pid == "pc-de" and i in (3,):  # PC: the home row has no nav cluster
            width_ = 15.0
        else:
            width_ = width
        assert edge == pytest.approx(width_, abs=0.01), (pid, i)


def test_bk03_shows_every_key_from_the_photo():
    bk = pf.BUILTIN["acme-bk03"]
    keys = list(pf.iter_keys(bk))
    assert len(keys) == 14 + 14 + 14 + 13 + 12 + 11   # esc…einfg, ^…rück, tab…#, feststell…eingabe, umsch…umsch, strg…→
    top = [k for k in keys if k.get("icon")]
    assert [k["icon"] for k in top] == list(pf.ICONS)
    assert [k["fn"] for k in top] == [f"F{n}" for n in range(1, 13)]
    assert all(k.get("guess") for k in top)               # unconfirmed until pressed in learn mode
    assert _key(bk, "cut")["code"] == "CUT" and _key(bk, "cut")["name"].startswith("Schere")
    # OS-mode legends on fn+Q/W/E, fn legends on Y/X and the arrows
    assert [_key(bk, s)["fn"] for s in ("q", "w", "e")] == ["And", "Win", "iOS"]
    assert [_key(bk, s)["fn"] for s in ("y", "x", "left", "right", "up", "down")] == ["<", ">", "Pos1", "Ende", "Bild↑", "Bild↓"]
    # German layout: the key next to T is Z and sends Y
    assert (_key(bk, "z")["code"], _key(bk, "y")["code"]) == ("Y", "Z")
    assert _key(bk, "fn")["code"] == ""                     # fn never reaches funkeykid
    assert "fn+Q" in bk["hint"]


def test_bk03_suggestions_are_valid_key_map_actions():
    bk = pf.BUILTIN["acme-bk03"]
    sug = {k["slot"]: k["suggest"] for k in pf.iter_keys(bk) if "suggest" in k}
    assert sug["volu"] == {"action": "func", "func": "volup"}
    assert sug["mute"] == {"action": "func", "func": "mute"}
    for action in sug.values():
        assert km.validate(action) == action


def test_bk03_arrows_up_and_down_share_one_key_slot():
    geo = pf.layout(pf.BUILTIN["acme-bk03"])
    by = {k["slot"]: k for k in geo["keys"]}
    up, down, left = by["up"], by["down"], by["left"]
    assert up["x"] == down["x"] and up["h"] == down["h"] == pytest.approx(0.5)
    assert down["y"] == pytest.approx(up["y"] + 0.5)
    assert left["y"] == pytest.approx(down["y"]) and left["h"] == pytest.approx(0.5)


def test_layout_and_codes():
    geo = pf.layout(pf.BUILTIN["pc-de"])
    by = {k["slot"]: k for k in geo["keys"]}
    assert by["f1"]["x"] == pytest.approx(2.0)              # esc, a 1-unit gap, then F1
    assert by["d1"]["y"] == pytest.approx(1.25)             # the number row sits below a 0.25 spacing
    assert by["up"]["x"] == pytest.approx(16.5)
    codes = pf.codes(pf.BUILTIN["pc-de"])
    assert codes.count("ENTER") == 1 and "" not in codes    # ISO enter is drawn as two parts


def test_render_and_summary():
    r = pf.render(pf.BUILTIN["acme-bk03"])
    assert r["id"] == "acme-bk03" and r["kind"] == "builtin" and len(r["keys"]) == 78
    s = pf.summary(pf.BUILTIN["mac-de"])
    assert s["keys"] == len(s["shape"]) and s["width"] == pytest.approx(15.0)


# ── custom profiles ──────────────────────────────────────────────────────────

def test_new_id_is_a_free_slug():
    assert pf.new_id("Meine Tastatur", {}) == "custom-meine-tastatur"
    assert pf.new_id("Meine Tastatur", {"custom-meine-tastatur": 1}) == "custom-meine-tastatur-2"
    assert pf.new_id("Größe ÄÖÜ", {}) == "custom-groesse-aeoeue"
    assert pf.new_id("!!!", {}) == "custom-tastatur"


def test_duplicate_is_a_deep_independent_copy():
    src = pf.BUILTIN["acme-bk03"]
    c = pf.duplicate(src, "custom-kids", "Kinder")
    assert (c["id"], c["name"], c["kind"], c["base"]) == ("custom-kids", "Kinder", "custom", "acme-bk03")
    c2 = pf.edit_key(c, "cut", {"label": "✂"})
    assert _key(c2, "cut")["label"] == "✂"
    assert _key(c, "cut")["label"] == "" and _key(src, "cut")["label"] == ""
    # a copy of a copy remembers the built-in it came from
    assert pf.duplicate(c, "custom-kids-2", "Kinder 2")["base"] == "acme-bk03"


def test_edit_key_changes_legends_code_icon_and_width():
    c = pf.duplicate(pf.BUILTIN["acme-bk03"], "custom-k", "K")
    c = pf.edit_key(c, "cut", {"code": "CTRL+X", "label": "Schere", "fn": "", "icon": "copy", "w": 1.5})
    cut = _key(c, "cut")
    assert cut["code"] == "CTRL+X" and "guess" not in cut     # a set code is no longer a guess
    assert cut["label"] == "Schere" and "fn" not in cut and cut["icon"] == "copy" and cut["w"] == 1.5
    assert "guess" in _key(pf.edit_key(c, "copy", {"label": "c"}), "copy")   # other edits keep the guess
    assert _key(pf.edit_key(c, "fn", {"code": "FN"}), "fn")["code"] == "FN"


@pytest.mark.parametrize("slot,patch", [
    ("cut", {"code": "cut"}), ("cut", {"code": "KEY_CUT"}), ("cut", {"code": "CTRL+LEFTCTRL"}),
    ("cut", {"code": "SHIFT_1"}), ("cut", {"icon": "rocket"}), ("cut", {"w": 0}), ("cut", {"w": 13}),
    ("cut", {"w": True}), ("cut", {"label": "x" * 13}), ("cut", {"fn": "line\nbreak"}),
    ("cut", {"guess": True}), ("cut", {"slot": "other"}), ("cut", {}), ("cut", None),
    ("up", {"w": 2}), ("nope", {"label": "x"}),
])
def test_edit_key_rejects(slot, patch):
    c = pf.duplicate(pf.BUILTIN["acme-bk03"], "custom-k", "K")
    with pytest.raises(pf.ProfileError):
        pf.edit_key(c, slot, patch)


def test_shift_keys_keep_their_code_but_shift_cannot_be_mapped():
    assert pf.valid_code("LEFTSHIFT") == "LEFTSHIFT"
    for kid in ("LEFTSHIFT", "RIGHTSHIFT", "CTRL+LEFTSHIFT"):
        with pytest.raises(km.KeymapError):
            km.parse_key_id(kid)


def test_old_shift_entries_are_ignored_but_can_be_deleted():
    """Review round 1: a LEFTSHIFT entry saved under FUNK-62 must not fire and must stay removable."""
    old = {"LEFTSHIFT": {"action": "func", "func": "volup"}, "CTRL+LEFTSHIFT": {"action": "none"}}
    assert km.resolve(old, "LEFTSHIFT") == (None, None)
    assert km.resolve(old, "LEFTSHIFT", ("LEFTCTRL",)) == (None, None)
    assert km.parse_key_id("LEFTSHIFT", allow_reserved=True) == ("LEFTSHIFT", ())
    with pytest.raises(km.KeymapError):
        km.parse_key_id("SHIFT_1", allow_reserved=True)    # never storable, so never deletable


def test_custom_profile_cap_is_small():
    assert 1 <= pf.MAX_CUSTOM <= 50


def test_rename():
    c = pf.duplicate(pf.BUILTIN["pc-de"], "custom-p", "P")
    assert pf.rename(c, name="Papa", note="am Schreibtisch")["name"] == "Papa"
    for bad in ("", "   ", "x" * 41, 3):
        with pytest.raises(pf.ProfileError):
            pf.rename(c, name=bad)


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(id="acme-bk03"),
    lambda p: p.update(id="custom-UPPER"),
    lambda p: p.update(rows=[]),
    lambda p: p["rows"][0]["keys"].append({"slot": "esc", "code": "ESC"}),     # duplicate slot
    lambda p: p["rows"][0]["keys"].append({"gap": -1}),
    lambda p: p["rows"][0]["keys"].append({"stack": [{"slot": "s1", "code": "A"}]}),
    lambda p: p["rows"][0]["keys"].append({"slot": "x1", "code": "A", "suggest": {"action": "sound", "file": "a.mp3"}}),
    lambda p: p["rows"][0].update(h="1"),
    lambda p: p["rows"][0].update(keys=[{"slot": "Bad Slot", "code": "A"}]),
])
def test_validate_rejects_broken_custom_profiles(mutate):
    p = pf.duplicate(pf.BUILTIN["acme-bk03"], "custom-k", "K")
    mutate(p)
    with pytest.raises(pf.ProfileError):
        pf.validate(p)


def test_customs_skips_invalid_stored_profiles_and_mismatched_ids():
    good = pf.duplicate(pf.BUILTIN["mac-de"], "custom-m", "M")
    stored = {"custom-m": good, "custom-bad": {"id": "custom-bad", "rows": []}, "custom-other": dict(good)}
    out = pf.customs(stored)
    assert list(out) == ["custom-m"]
    assert pf.get("custom-m", out)["name"] == "M"
    assert pf.get("acme-bk03", out) is pf.BUILTIN["acme-bk03"]
    assert pf.get("custom-gone", out) is None
    assert [p["id"] for p in pf.all_profiles(out)] == ["acme-bk03", "pc-de", "mac-de", "custom-m"]


def test_validate_strips_unknown_fields():
    p = pf.duplicate(pf.BUILTIN["acme-bk03"], "custom-k", "K")
    p["evil"] = "<script>"
    p["rows"][1]["keys"][0]["onclick"] = "x"
    v = pf.validate(p)
    assert "evil" not in v and "onclick" not in v["rows"][1]["keys"][0]


# ── default kind (for colouring keys by their built-in role) ─────────────────

def test_default_kind():
    assert km.default_kind("SPACE") == "func"
    assert km.default_kind("A") == "letter" and km.default_kind("7") == "letter"
    assert km.default_kind("CUT") is None
    assert km.default_kind("LEFTBRACE", has_content=True) == "letter"
