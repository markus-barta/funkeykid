"""Progress wiring in server.py (FUNK-40): which presses count, quiz results, saving, the API.

server.py needs aiohttp; CI installs only the pure-helper dependencies, so these
tests run where aiohttp is present (the lab venv) and are skipped elsewhere.
"""
import asyncio
import json
import os
import sys
import threading
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

pytest.importorskip("aiohttp")
import keymap  # noqa: E402
import progress  # noqa: E402
import server  # noqa: E402


@pytest.fixture
def lp(monkeypatch, tmp_path):
    p = progress.Progress(keymap.LETTER_TARGETS, today=lambda: "2026-11-27")
    monkeypatch.setattr(server, "letter_progress", p)
    monkeypatch.setattr(server, "_progress_saved_rev", 0)
    monkeypatch.setattr(server, "settings", {"active_set": "s", "sets": {"s": {"letters": {}}}, "progress": {}})
    monkeypatch.setattr(server, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(server, "startup_time", 0)
    monkeypatch.setattr(server, "sse_broadcast", lambda *a, **k: None)
    played = []
    monkeypatch.setattr(server, "_handle_builtin", played.append)
    monkeypatch.setattr(server, "_run_keymap_action", lambda kid, action: played.append(action))
    monkeypatch.setattr(server, "_active_keymap", lambda: {})
    p.played = played
    return p


def presses(p):
    return {k: v["presses"] for k, v in p.snapshot()["letters"].items()}


def test_real_letter_and_digit_presses_count(lp):
    server.handle_key("A", "A")
    server.handle_key("ß", "MINUS")
    server.handle_key("7", "7")
    for key, raw in [("ENTER", "ENTER"), ("SPACE", "SPACE"), ("SHIFT_3", "3"), ("RIGHT", "RIGHT")]:
        server.handle_key(key, raw)
    assert presses(lp) == {"A": 1, "ß": 1, "7": 1}
    assert lp.played == ["A", "ß", "7", "ENTER", "SPACE", "SHIFT_3", "RIGHT"]    # every key still plays


def test_test_keys_on_the_parent_page_do_not_count(lp):
    server.handle_key("A", "A", virtual=True)
    assert presses(lp) == {} and lp.played == ["A"]


@pytest.mark.parametrize("action,want", [
    ({"action": "letter", "letter": "B"}, {"B": 1}),
    ({"action": "letter", "letter": "4"}, {"4": 1}),
    ({"action": "func", "func": "volup"}, {}),
    ({"action": "sound", "file": "x.mp3"}, {}),
    ({"action": "none"}, {}),
])
def test_key_map_letters_count_for_the_letter_they_play(lp, monkeypatch, action, want):
    monkeypatch.setattr(server, "_active_keymap", lambda: {"CUT": action})
    server.handle_key("CUT", "CUT")
    assert presses(lp) == want and lp.played == [action]


def test_presses_during_the_quiz_are_answers_not_presses(lp, monkeypatch):
    answers = []

    class Q:
        active = True

        def touch(self):
            pass

        def answer(self, letter, origin=None):
            answers.append((letter, origin))
            return "right"

    monkeypatch.setattr(server, "quiz_engine", Q())
    server.handle_key("A", "A")
    server.handle_key("B", "B", virtual=True)
    assert presses(lp) == {} and answers == [("A", "keyboard"), ("B", "test")]


def test_quiz_results_count_from_the_real_keyboard_only(lp):
    server._progress_quiz_result("C", True, True, "keyboard")
    server._progress_quiz_result("C", True, True, "test")
    server._progress_quiz_result("C", True, True, None)
    server._progress_quiz_result("C", False, False, "keyboard")
    c = lp.snapshot()["letters"]["C"]
    assert (c["asked"], c["right"], c["first"], c["streak"]) == (2, 1, 1, 0)


def test_a_broken_counter_never_breaks_a_key(lp, monkeypatch):
    def boom(*a):
        raise RuntimeError("boom")
    monkeypatch.setattr(lp, "press", boom)
    monkeypatch.setattr(lp, "quiz_result", boom)
    server.handle_key("A", "A")
    server._progress_quiz_result("A", True, True, "keyboard")
    assert lp.played == ["A"]


def test_the_quiz_engine_reports_to_the_progress_hook():
    assert server.quiz_engine._result is server._progress_quiz_result


def test_save_writes_settings_once_per_change(lp, tmp_path):
    lp.press("A")
    assert server._progress_save() is True
    saved = json.loads((tmp_path / "settings.json").read_text())
    assert saved["progress"]["letters"]["A"]["presses"] == 1 and server._progress_saved_rev == lp.rev
    (tmp_path / "settings.json").unlink()
    assert server._progress_save() is False and not (tmp_path / "settings.json").exists()
    lp.press("A")
    assert server._progress_save() is True
    assert json.loads((tmp_path / "settings.json").read_text())["progress"]["letters"]["A"]["presses"] == 2


def test_other_saves_never_see_a_half_updated_copy(lp):
    lp.press("A")
    server._progress_save()
    held = server.settings["progress"]
    lp.press("A")
    assert held["letters"]["A"]["presses"] == 1          # the saved copy is never changed in place


def test_the_loop_pushes_changes_and_new_days_and_saves_every_10_s(lp, monkeypatch, tmp_path):
    sent, saves = [], []
    monkeypatch.setattr(server, "sse_broadcast", lambda event, data: sent.append((event, data["rev"])))
    real = server._progress_save
    monkeypatch.setattr(server, "_progress_save", lambda: saves.append(1) or real())
    state = {"sent": lp.rev, "day": "2026-11-27", "saved_at": 100.0}
    server._progress_tick(state, "2026-11-27", 101.0)
    assert sent == [] and saves == []                  # nothing changed
    lp.press("A")
    server._progress_tick(state, "2026-11-27", 102.0)
    assert sent == [("progress", 1)] and saves == []   # pushed at once, saved later
    server._progress_tick(state, "2026-11-27", 110.0)
    assert sent == [("progress", 1)] and saves == [1] and server._progress_saved_rev == 1
    server._progress_tick(state, "2026-11-27", 125.0)
    assert saves == [1]                                # saved already: no write
    server._progress_tick(state, "2026-11-28", 126.0)
    assert sent == [("progress", 1), ("progress", 1)]  # a new day: the page's "heute" starts again


def test_start_loads_the_saved_counters(lp, monkeypatch):
    started = []
    monkeypatch.setattr(server.threading, "Thread", lambda **kw: type("T", (), {"start": lambda s: started.append(kw["name"])})())
    server.settings["progress"] = {"mastery": 2, "letters": {"A": {"presses": 5, "streak": 2}}, "days": {}}
    server._progress_start()
    snap = lp.snapshot()
    assert snap["mastery"] == 2 and snap["letters"]["A"]["presses"] == 5 and snap["letters"]["A"]["learned"]
    assert server.settings["progress"]["letters"]["A"]["presses"] == 5 and started == ["progress"]


def test_a_failed_set_import_never_undoes_a_progress_save(lp, tmp_path, monkeypatch):
    """A set import whose save fails puts settings.json back as it was. A progress save from
    the keyboard thread meanwhile has to wait for that, or the restore would undo it."""
    lp.press("A")
    server._progress_save()                            # on disk: A pressed once
    real_save, other = server.save_settings, {}

    def save():
        if threading.current_thread() is threading.main_thread() and "t" not in other:
            lp.press("A")                              # the child presses on while the import saves
            other["t"] = threading.Thread(target=server._progress_save)
            other["t"].start()
            other["t"].join(0.3)
            other["waited"] = other["t"].is_alive()
            raise OSError("disk full")                 # the import's save fails
        real_save()

    monkeypatch.setattr(server, "save_settings", save)
    txn = types.SimpleNamespace(rollback=lambda: None, commit=lambda: None)
    monkeypatch.setattr(server.setbundle, "apply", lambda *a: ({"name": "Neu", "letters": {}}, {}, txn))
    monkeypatch.setattr(server.shutil, "disk_usage", lambda p: types.SimpleNamespace(free=10**12))
    monkeypatch.setattr(server, "SOUNDS_DIR", str(tmp_path / "sounds"))
    monkeypatch.setattr(server, "IMAGES_DIR", str(tmp_path / "images"))
    resp = asyncio.run(server._commit_import({"members": {}, "path": "unused", "set": {}}, "", {}, False))
    other["t"].join(2)
    assert resp.status == 500 and other["waited"]
    disk = json.loads((tmp_path / "settings.json").read_text())
    assert disk["progress"]["letters"]["A"]["presses"] == 2 and server._progress_saved_rev == lp.rev
    assert list(disk["sets"]) == ["s"]


# ── API ──────────────────────────────────────────────────────────────────────

class Req:
    def __init__(self, body=None, raw=None):
        self._body, self._raw = body, raw

    async def json(self):
        if self._raw is not None:
            return json.loads(self._raw)
        return self._body


def call(handler, req=None):
    resp = asyncio.run(handler(req or Req()))
    return resp.status, json.loads(resp.body)


def test_api_get(lp):
    lp.press("A")
    status, body = call(server.api_progress_get)
    assert status == 200 and body["letters"]["A"]["presses"] == 1 and body["mastery"] == 3
    assert body["boot"] == lp.boot and body["rev"] == lp.rev


def test_api_config_saves_at_once(lp, tmp_path):
    status, body = call(server.api_progress_config, Req({"mastery": 5}))
    assert status == 200 and body["mastery"] == 5
    assert json.loads((tmp_path / "settings.json").read_text())["progress"]["mastery"] == 5


@pytest.mark.parametrize("req", [Req({"mastery": 0}), Req({"mastery": "5"}), Req({}), Req([5]),
                                 Req(raw="{nope")])
def test_api_config_rejects_bad_input(lp, tmp_path, req):
    status, body = call(server.api_progress_config, req)
    assert status == 400 and body["error"] and lp.mastery == 3
    assert not (tmp_path / "settings.json").exists()


def test_api_reset_clears_and_saves_at_once(lp, tmp_path):
    lp.press("A")
    lp.set_mastery(4)
    status, body = call(server.api_progress_reset)
    assert status == 200 and body["letters"] == {} and body["mastery"] == 4
    saved = json.loads((tmp_path / "settings.json").read_text())["progress"]
    assert saved["letters"] == {} and saved["mastery"] == 4
