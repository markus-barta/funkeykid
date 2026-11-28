"""Zahlen in the entry editor (FUNK-76): a digit as one entry ("number"), field-level writes that
never keep what could not be saved, backgrounds by name, and generation jobs that target a digit.

server.py needs aiohttp; these tests run where aiohttp is present (the lab venv).
"""
import asyncio
import json
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

pytest.importorskip("aiohttp")
import server  # noqa: E402


class Req:
    def __init__(self, body=None, headers=None, **match):
        self._body, self.match_info, self.headers = body, match, headers or {}

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def call(handler, body=None, **match):
    match.setdefault("set_id", "s")
    resp = asyncio.run(handler(Req(body, headers={"X-FK-Client": "page1"}, **match)))
    return resp.status, json.loads(resp.body)


@pytest.fixture
def st(monkeypatch, tmp_path):
    """Set 's': 3 has a word, a legacy sound and two backgrounds; 7 does not exist yet."""
    for d in ("sounds", "images"):
        (tmp_path / d).mkdir()
    for f in ("3_drei.mp3", "3_fx.mp3", "new_de.mp3"):
        (tmp_path / "sounds" / f).write_bytes(b"x")
    for f in ("3_a.png", "3_b.png", "3_c.png"):
        (tmp_path / "images" / f).write_bytes(b"x")
    data = {"active_set": "s", "sets": {"s": {
        "name": "S", "track_order": [{"kind": "FX", "enabled": True}, {"kind": "DE", "enabled": True}],
        "letters": {},
        "numbers": {"3": {"word": "Drei Äpfel", "image_subject": "three apples", "sound": "3_drei.mp3",
                          "backgrounds": [{"image": "3_a.png", "enabled": True}, {"image": "3_b.png", "enabled": False}]}},
    }}}
    monkeypatch.setattr(server, "settings", data)
    monkeypatch.setattr(server, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(server, "SOUNDS_DIR", str(tmp_path / "sounds"))
    monkeypatch.setattr(server, "IMAGES_DIR", str(tmp_path / "images"))
    monkeypatch.setattr(server, "gen_jobs", {})
    monkeypatch.setattr(server, "_loop", None)
    sent = []
    monkeypatch.setattr(server, "sse_broadcast", lambda ev, d: sent.append((ev, json.loads(json.dumps(d)))))
    return types.SimpleNamespace(settings=data, sent=sent, tmp=tmp_path,
                                 disk=lambda: json.loads((tmp_path / "settings.json").read_text()))


def num(st, digit="3"):
    return st.settings["sets"]["s"]["numbers"].get(digit)


def fail_saves(monkeypatch):
    def boom():
        raise OSError("disk full")
    monkeypatch.setattr(server, "save_settings", boom)


# ── reading ──────────────────────────────────────────────────────────────────

def test_a_digit_is_one_entry_with_its_backgrounds_and_the_track_order(st):
    status, d = call(server.api_get_number, digit="3")
    assert status == 200
    [e] = d["entries"]
    assert e["id"] == "number" and e["word"] == "Drei Äpfel" and e["_imageDesc"] == "three apples"
    assert e["enabled"] is True and e["image"] == ""
    assert e["tracks"]["DE"]["file"] == "3_drei.mp3"                 # the legacy sound, as DE
    assert d["backgrounds"] == [{"image": "3_a.png", "enabled": True}, {"image": "3_b.png", "enabled": False}]
    assert [k["kind"] for k in d["track_order"]][:2] == ["FX", "DE"]


def test_a_digit_without_config_reads_empty_and_is_not_created(st):
    status, d = call(server.api_get_number, digit="7")
    assert status == 200 and d["entries"][0]["word"] == "" and d["backgrounds"] == []
    assert num(st, "7") is None


@pytest.mark.parametrize("match,code", [({"digit": "x"}, 400), ({"digit": "10"}, 400), ({"digit": "3", "set_id": "nope"}, 404)])
def test_bad_digits_and_sets(st, match, code):
    assert call(server.api_get_number, **match)[0] == code


# ── field-level writes ───────────────────────────────────────────────────────

def test_patch_changes_only_the_fields_it_names(st):
    status, d = call(server.api_patch_number, {"word": "Drei Birnen", "tracks": {"FX": {"file": "3_fx.mp3"}}},
                     digit="3", entry_id="number")
    assert status == 200 and d["entry"]["word"] == "Drei Birnen"
    n = num(st)
    assert n["word"] == "Drei Birnen" and n["image_subject"] == "three apples"
    assert n["tracks"]["FX"]["file"] == "3_fx.mp3" and n["tracks"]["DE"]["file"] == "3_drei.mp3"
    assert "sound" not in n and len(n["backgrounds"]) == 2
    assert st.disk()["sets"]["s"]["numbers"]["3"]["word"] == "Drei Birnen"
    assert ("letter", {"set_id": "s", "letter": "3", "origin": "page1", "entry_id": "number"}) in st.sent


def test_the_picture_idea_is_the_image_subject(st):
    call(server.api_patch_number, {"_imageDesc": "three pears"}, digit="3", entry_id="number")
    assert num(st)["image_subject"] == "three pears"


@pytest.mark.parametrize("body", [
    {"image": "3_a.png"}, {"enabled": False}, {"word": 5}, {"tracks": {"DE": {"file": "missing.mp3"}}},
    {"tracks": {"DE": {"file": "../settings.json"}}}, {"tracks": {"XX": {}}}, {"what": 1}, ["word"],
    {"tracks": {"DE": {"volume": 300}}},
])
def test_patch_refuses_what_a_number_cannot_take(st, body):
    before = json.dumps(num(st))
    status, _ = call(server.api_patch_number, body, digit="3", entry_id="number")
    assert status == 400 and json.dumps(num(st)) == before


def test_restore_allows_a_missing_file(st):
    status, _ = call(server.api_patch_number, {"restore": True, "tracks": {"DE": {"file": "gone.mp3"}}},
                     digit="3", entry_id="number")
    assert status == 200 and num(st)["tracks"]["DE"]["file"] == "gone.mp3"


def test_only_the_number_entry_exists(st):
    assert call(server.api_patch_number, {"word": "x"}, digit="3", entry_id="abcdef12")[0] == 404


def test_a_first_write_creates_the_digit(st):
    status, _ = call(server.api_patch_number, {"word": "Sieben Fische"}, digit="7", entry_id="number")
    assert status == 200 and num(st, "7")["word"] == "Sieben Fische" and num(st, "7")["backgrounds"] == []


def test_a_failed_save_keeps_nothing(st, monkeypatch):
    before = json.dumps(num(st))
    fail_saves(monkeypatch)
    assert call(server.api_patch_number, {"word": "Neu"}, digit="3", entry_id="number")[0] == 500
    assert json.dumps(num(st)) == before
    assert call(server.api_patch_number, {"word": "Neu"}, digit="7", entry_id="number")[0] == 500
    assert num(st, "7") is None                                      # created and taken back


def test_choosing_a_file_detaches_a_running_job_for_that_slot(st):
    server.gen_jobs["j1"] = {"id": "j1", "status": "generating",
                             "target": {"set_id": "s", "letter": "3", "entry_id": "number", "kind": "DE"}}
    call(server.api_patch_number, {"tracks": {"DE": {"file": "new_de.mp3"}}}, digit="3", entry_id="number")
    assert server.gen_jobs["j1"]["target"] is None


# ── backgrounds ──────────────────────────────────────────────────────────────

def test_add_a_background_at_a_position_and_again_is_a_no_op(st):
    status, d = call(server.api_number_bg_add, {"image": "3_c.png", "index": 1}, digit="3")
    assert status == 200 and d["index"] == 1 and len(d["token"]) == 12
    assert [b["image"] for b in num(st)["backgrounds"]] == ["3_a.png", "3_c.png", "3_b.png"]
    status, d = call(server.api_number_bg_add, {"image": "3_c.png"}, digit="3")
    assert status == 200 and d["existed"] is True and len(num(st)["backgrounds"]) == 3


def test_add_appends_by_default_and_clamps_the_index(st):
    call(server.api_number_bg_add, {"image": "3_c.png", "index": 99, "enabled": False}, digit="3")
    assert num(st)["backgrounds"][-1] == {"image": "3_c.png", "enabled": False}


@pytest.mark.parametrize("body", [
    {"image": "nope.png"}, {"image": ""}, {"image": "../x.png"}, {"image": "3_c.png", "enabled": "yes"},
    {"image": "3_c.png", "index": "1"}, {"image": "3_c.png", "index": True}, {"image": "3_c.png", "x": 1}, "3_c.png",
])
def test_add_refuses_bad_backgrounds(st, body):
    assert call(server.api_number_bg_add, body, digit="3")[0] == 400 and len(num(st)["backgrounds"]) == 2


def test_add_restores_a_missing_picture_on_undo(st):
    status, _ = call(server.api_number_bg_add, {"image": "gone.png", "restore": True, "index": 0}, digit="3")
    assert status == 200 and num(st)["backgrounds"][0]["image"] == "gone.png"


def test_add_has_a_limit(st, monkeypatch):
    monkeypatch.setattr(server, "NUMBER_BG_MAX", 2)
    assert call(server.api_number_bg_add, {"image": "3_c.png"}, digit="3")[0] == 400


def test_switch_a_background(st):
    status, d = call(server.api_number_bg_patch, {"enabled": True}, digit="3", image="3_b.png")
    assert status == 200 and (d["changed"], d["was"]) == (True, False) and d["token"] and num(st)["backgrounds"][1]["enabled"] is True
    d2 = call(server.api_number_bg_patch, {"enabled": True}, digit="3", image="3_b.png")[1]
    assert (d2["changed"], d2["was"], d2["token"]) == (False, True, d["token"])
    assert call(server.api_number_bg_patch, {"enabled": 1}, digit="3", image="3_b.png")[0] == 400
    assert call(server.api_number_bg_patch, {"enabled": True}, digit="3", image="nope.png")[0] == 404
    assert call(server.api_number_bg_patch, {"enabled": True}, digit="3", image="..%2f3_b.png")[0] == 404


def test_remove_a_background_and_say_where_it_was(st):
    status, d = call(server.api_number_bg_delete, digit="3", image="3_b.png")
    assert status == 200 and d == {"ok": True, "removed": True, "index": 1, "enabled": False}
    assert [b["image"] for b in num(st)["backgrounds"]] == ["3_a.png"]
    assert os.path.isfile(st.tmp / "images" / "3_b.png")             # the picture stays in the library
    status, d = call(server.api_number_bg_delete, digit="3", image="3_b.png")
    assert status == 200 and d["removed"] is False


def test_background_writes_keep_nothing_when_the_save_fails(st, monkeypatch):
    before = json.dumps(num(st))
    fail_saves(monkeypatch)
    assert call(server.api_number_bg_add, {"image": "3_c.png"}, digit="3")[0] == 500
    assert call(server.api_number_bg_patch, {"enabled": True}, digit="3", image="3_b.png")[0] == 500
    assert call(server.api_number_bg_delete, digit="3", image="3_a.png")[0] == 500
    assert json.dumps(num(st)) == before


# ── generation jobs that target a digit ──────────────────────────────────────

def job(st, kind, filename, entry_id="number", digit="3"):
    target = server._job_target({"set_id": "s", "letter": digit, "entry_id": entry_id}, kind)
    server.gen_jobs["j1"] = {"id": "j1", "kind": kind, "filename": filename, "status": "generating", "_done": True, "target": target}
    server._job_finished("j1")
    return server.gen_jobs["j1"]


def test_a_voice_job_fills_an_empty_number_slot(st):
    j = job(st, "FX", "3_fx.mp3")
    assert j["applied"] is True and num(st)["tracks"]["FX"]["file"] == "3_fx.mp3"
    assert ("letter", {"set_id": "s", "letter": "3", "entry_id": "number", "origin": "job"}) in st.sent


def test_a_voice_job_never_replaces_a_number_file(st):
    j = job(st, "DE", "new_de.mp3")
    assert j["applied"] is False and num(st)["tracks"]["DE"]["file"] == "3_drei.mp3"


def test_a_picture_job_adds_a_background(st):
    j = job(st, "IMG", "3_c.png")
    assert j["target"]["kind"] == "BG" and j["applied"] is True
    assert num(st)["backgrounds"][-1] == {"image": "3_c.png", "enabled": True}
    j = job(st, "IMG", "3_c.png")                                    # already there: nothing twice
    assert j["applied"] is True and [b["image"] for b in num(st)["backgrounds"]].count("3_c.png") == 1


def test_a_number_target_must_exist(st):
    with pytest.raises(server._TargetError):
        server._job_target({"set_id": "s", "letter": "7", "entry_id": "number"}, "DE")
    with pytest.raises(server._TargetError):
        server._job_target({"set_id": "s", "letter": "3", "entry_id": "abcdef12"}, "DE")


def test_a_failed_save_keeps_the_number_as_it_was(st, monkeypatch):
    fail_saves(monkeypatch)
    j = job(st, "IMG", "3_c.png")
    assert j["applied"] is False and j["save_failed"] and len(num(st)["backgrounds"]) == 2


# ── undo safety: a retried add or removal answers like the first time ──────────

def test_a_retried_add_still_says_inserted(st, monkeypatch):
    monkeypatch.setattr(server, "_bg_ops", {})
    first = call(server.api_number_bg_add, {"image": "3_c.png", "op": "op-add-0001"}, digit="3")
    again = call(server.api_number_bg_add, {"image": "3_c.png", "op": "op-add-0001"}, digit="3")
    assert first == again and first[1]["index"] == 2 and first[1]["token"]
    other = call(server.api_number_bg_add, {"image": "3_c.png", "op": "op-add-0002"}, digit="3")
    assert other == (200, {"ok": True, "existed": True})          # another page's add of the same picture


def test_a_retried_removal_still_says_removed(st, monkeypatch):
    monkeypatch.setattr(server, "_bg_ops", {})
    first = call(server.api_number_bg_delete, {"op": "op-del-0001"}, digit="3", image="3_a.png")
    again = call(server.api_number_bg_delete, {"op": "op-del-0001"}, digit="3", image="3_a.png")
    assert first == again and first[1]["removed"] is True
    assert call(server.api_number_bg_delete, {"op": "op-del-0002"}, digit="3", image="3_a.png")[1]["removed"] is False
    assert call(server.api_number_bg_delete, ValueError("no body"), digit="3", image="3_b.png")[1]["removed"] is True


@pytest.mark.parametrize("op", [5, "short", "x" * 41])
def test_bad_op_ids_are_refused(st, op):
    assert call(server.api_number_bg_add, {"image": "3_c.png", "op": op}, digit="3")[0] == 400
    assert call(server.api_number_bg_delete, {"op": op}, digit="3", image="3_a.png")[0] == 400


def test_a_failed_save_is_not_remembered_as_the_answer(st, monkeypatch):
    monkeypatch.setattr(server, "_bg_ops", {})
    real = server.save_settings
    fail_saves(monkeypatch)
    assert call(server.api_number_bg_add, {"image": "3_c.png", "op": "op-add-0003"}, digit="3")[0] == 500
    monkeypatch.setattr(server, "save_settings", real)
    status, d = call(server.api_number_bg_add, {"image": "3_c.png", "op": "op-add-0003"}, digit="3")
    assert status == 200 and d["index"] == 2


# ── the limit ─────────────────────────────────────────────────────────────────

def test_undo_may_put_back_a_background_at_the_limit(st, monkeypatch):
    monkeypatch.setattr(server, "NUMBER_BG_MAX", 2)
    call(server.api_number_bg_delete, digit="3", image="3_a.png")
    call(server.api_number_bg_add, {"image": "3_c.png"}, digit="3")                   # full again
    assert call(server.api_number_bg_add, {"image": "3_a.png"}, digit="3")[0] == 400
    status, _ = call(server.api_number_bg_add, {"image": "3_a.png", "restore": True, "index": 0}, digit="3")
    assert status == 200 and num(st)["backgrounds"][0]["image"] == "3_a.png"


def test_a_full_digit_takes_no_new_ki_background(st, monkeypatch):
    monkeypatch.setattr(server, "NUMBER_BG_MAX", 2)
    with pytest.raises(server._TargetError) as ex:
        server._job_target({"set_id": "s", "letter": "3", "entry_id": "number"}, "IMG")
    assert ex.value.status == 400 and "Hintergründe" in ex.value.msg


def test_a_picture_that_lands_on_a_full_digit_waits_for_review(st, monkeypatch):
    target = server._job_target({"set_id": "s", "letter": "3", "entry_id": "number"}, "IMG")
    monkeypatch.setattr(server, "NUMBER_BG_MAX", 2)                                   # filled meanwhile
    server.gen_jobs["j1"] = {"id": "j1", "kind": "IMG", "filename": "3_c.png", "status": "generating", "_done": True, "target": target}
    server._job_finished("j1")
    j = server.gen_jobs["j1"]
    assert j["applied"] is False and j["target"] and len(num(st)["backgrounds"]) == 2



# ── an undo of an add takes off only that insertion; a retried switch never re-applies ──

def test_add_undo_takes_off_only_its_own_insertion(st, monkeypatch):
    monkeypatch.setattr(server, "_bg_tokens", {})
    _, a = call(server.api_number_bg_add, {"image": "3_c.png"}, digit="3")                      # page A
    call(server.api_number_bg_delete, digit="3", image="3_c.png")                                # page B removes
    _, b = call(server.api_number_bg_add, {"image": "3_c.png", "enabled": False}, digit="3")    # and adds it again
    status, d = call(server.api_number_bg_delete, {"if_token": a["token"]}, digit="3", image="3_c.png")   # A's undo
    assert status == 200 and d["removed"] is False and server._bg_index(num(st), "3_c.png") >= 0
    status, d = call(server.api_number_bg_delete, {"if_token": b["token"]}, digit="3", image="3_c.png")
    assert d["removed"] is True and ("s", "3", "3_c.png") not in server._bg_tokens


def test_an_unknown_token_takes_off_nothing(st, monkeypatch):
    monkeypatch.setattr(server, "_bg_tokens", {})
    assert call(server.api_number_bg_delete, {"if_token": ""}, digit="3", image="3_a.png")[1]["removed"] is False
    assert call(server.api_number_bg_delete, {"if_token": 5}, digit="3", image="3_a.png")[0] == 400
    assert len(num(st)["backgrounds"]) == 2


def test_a_generated_background_is_an_insertion_and_a_put_forgets_them(st, monkeypatch):
    monkeypatch.setattr(server, "_bg_tokens", {})
    job(st, "IMG", "3_c.png")
    assert ("s", "3", "3_c.png") in server._bg_tokens
    asyncio.run(server.api_put_number(Req({"word": "Drei", "backgrounds": [{"image": "3_c.png"}]}, set_id="s", digit="3")))
    assert ("s", "3", "3_c.png") not in server._bg_tokens


def test_a_retried_switch_never_reapplies_over_a_later_choice(st, monkeypatch):
    monkeypatch.setattr(server, "_bg_ops", {})
    assert call(server.api_number_bg_patch, {"enabled": True, "op": "op-sw-00001"}, digit="3", image="3_b.png")[0] == 200
    call(server.api_number_bg_patch, {"enabled": False}, digit="3", image="3_b.png")             # another page, later
    status, d = call(server.api_number_bg_patch, {"enabled": True, "op": "op-sw-00001"}, digit="3", image="3_b.png")
    assert status == 200 and (d["changed"], d["was"]) == (True, False) and num(st)["backgrounds"][1]["enabled"] is False
    assert call(server.api_number_bg_patch, {"enabled": True, "op": 7}, digit="3", image="3_b.png")[0] == 400



def test_a_switch_undo_only_takes_back_what_is_still_as_it_was_left(st):
    # page A switched 3_a off; page B switched it on again since: A's undo (on, if it is still off) does nothing
    call(server.api_number_bg_patch, {"enabled": False}, digit="3", image="3_a.png")
    call(server.api_number_bg_patch, {"enabled": True}, digit="3", image="3_a.png")
    status, d = call(server.api_number_bg_patch, {"enabled": True, "if_enabled": False}, digit="3", image="3_a.png")
    assert (d["changed"], d["was"]) == (False, True)
    call(server.api_number_bg_patch, {"enabled": False}, digit="3", image="3_a.png")
    status, d = call(server.api_number_bg_patch, {"enabled": True, "if_enabled": False}, digit="3", image="3_a.png")
    assert d["changed"] is True and num(st)["backgrounds"][0]["enabled"] is True
    assert call(server.api_number_bg_patch, {"enabled": True, "if_enabled": "no"}, digit="3", image="3_a.png")[0] == 400


def test_a_switch_that_changes_nothing_writes_nothing(st, monkeypatch):
    sent = len(st.sent)
    fail_saves(monkeypatch)                                          # would answer 500 if it tried to save
    assert call(server.api_number_bg_patch, {"enabled": True}, digit="3", image="3_a.png")[1]["changed"] is False
    assert len(st.sent) == sent



def test_a_switch_undo_leaves_a_replaced_background_alone(st, monkeypatch):
    monkeypatch.setattr(server, "_bg_tokens", {})
    # page A switches 3_b on; page B removes it and adds it again (on); A's undo must not touch B's
    _, a = call(server.api_number_bg_patch, {"enabled": True}, digit="3", image="3_b.png")
    call(server.api_number_bg_delete, digit="3", image="3_b.png")
    call(server.api_number_bg_add, {"image": "3_b.png", "enabled": True}, digit="3")
    _, d = call(server.api_number_bg_patch, {"enabled": False, "if_enabled": True, "if_token": a["token"]}, digit="3", image="3_b.png")
    assert d["changed"] is False and num(st)["backgrounds"][-1] == {"image": "3_b.png", "enabled": True}


def test_undo_chains_keep_the_identity(st, monkeypatch):
    monkeypatch.setattr(server, "_bg_tokens", {})
    _, add = call(server.api_number_bg_add, {"image": "3_c.png"}, digit="3")                   # add
    _, sw = call(server.api_number_bg_patch, {"enabled": False}, digit="3", image="3_c.png")    # then switch off
    assert sw["token"] == add["token"]
    _, u1 = call(server.api_number_bg_patch, {"enabled": True, "if_enabled": False, "if_token": sw["token"]}, digit="3", image="3_c.png")
    _, u2 = call(server.api_number_bg_delete, {"if_token": add["token"]}, digit="3", image="3_c.png")
    assert u1["changed"] is True and u2["removed"] is True and server._bg_index(num(st), "3_c.png") < 0
