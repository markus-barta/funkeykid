"""Quiz wiring in server.py (FUNK-39): key routing, the question pool, playback callbacks.

server.py needs aiohttp; CI installs only the pure-helper dependencies, so these
tests run where aiohttp is present (the lab venv) and are skipped elsewhere.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

pytest.importorskip("aiohttp")
import keymap  # noqa: E402
import progress  # noqa: E402
import server  # noqa: E402


class FakeQuiz:
    def __init__(self):
        self.active, self.calls, self.origins = True, [], []

    def touch(self):
        self.calls.append("touch")

    def answer(self, letter, origin=None):
        self.calls.append(("answer", letter))
        self.origins.append(origin)
        return "right"

    def repeat(self):
        self.calls.append("repeat")

    def stop(self):
        self.calls.append("stop")


@pytest.fixture
def fq(monkeypatch):
    q = FakeQuiz()
    monkeypatch.setattr(server, "quiz_engine", q)
    monkeypatch.setattr(server, "startup_time", 0)
    monkeypatch.setattr(server, "sse_broadcast", lambda *a, **k: None)
    played, volume, stopped = [], [], []
    monkeypatch.setattr(server, "play_sound_sequence", lambda *a, **k: played.append(a))
    monkeypatch.setattr(server, "change_volume", volume.append)
    monkeypatch.setattr(server, "stop_all_sounds", lambda: stopped.append(1))
    monkeypatch.setattr(server, "_active_keymap", lambda: {})
    monkeypatch.setattr(server, "letter_progress", progress.Progress(keymap.LETTER_TARGETS))
    q.played, q.volume, q.stopped = played, volume, stopped
    return q


@pytest.mark.parametrize("key,raw,want", [
    ("A", "A", [("answer", "A")]), ("ß", "MINUS", [("answer", "ß")]), ("Ü", "LEFTBRACE", [("answer", "Ü")]),
    ("ENTER", "ENTER", ["repeat"]), ("TAB", "TAB", []), ("RIGHT", "RIGHT", []), ("3", "3", []),
    ("SHIFT_3", "3", []),
])
def test_keys_during_the_quiz(fq, key, raw, want):
    server.handle_key(key, raw)
    assert fq.calls == ["touch"] + want
    assert fq.played == []                          # nothing else plays over the question
    assert fq.origins == ["keyboard"] * len(fq.origins)


def test_space_stops_sound_and_volume_keys_work(fq):
    server.handle_key("SPACE", "SPACE")
    server.handle_key("EQUAL", "RIGHTBRACE")
    server.handle_key("MINUS", "SLASH")
    assert fq.stopped == [1] and fq.volume == [10, -10]
    assert fq.calls == ["touch"] * 3


@pytest.mark.parametrize("action,want", [
    ({"action": "letter", "letter": "B"}, [("answer", "B")]),
    ({"action": "letter", "letter": "7"}, []),
    ({"action": "sound", "file": "x.mp3", "volume": 100}, []),
    ({"action": "func", "func": "replay"}, ["repeat"]),
    ({"action": "func", "func": "quiz"}, ["stop"]),
    ({"action": "func", "func": "nextset"}, []),
    ({"action": "none"}, []),
])
def test_key_map_entries_during_the_quiz(fq, monkeypatch, action, want):
    monkeypatch.setattr(server, "_active_keymap", lambda: {"CUT": action})
    server.handle_key("CUT", "CUT")
    assert fq.calls == ["touch"] + want and fq.played == []


def test_without_a_quiz_keys_behave_as_before(fq, monkeypatch):
    fq.active = False
    seen = []
    monkeypatch.setattr(server, "_handle_builtin", seen.append)
    server.handle_key("A", "A")
    assert seen == ["A"] and fq.calls == []


def test_quiz_function_starts_and_stops(monkeypatch):
    q = FakeQuiz()
    q.active = False
    started = []
    q.start = started.append
    monkeypatch.setattr(server, "quiz_engine", q)
    monkeypatch.setattr(server, "settings", {"quiz": {"mode": "sound", "questions": 5}})
    server._KEYMAP_FUNCS["quiz"]()
    assert started[0]["mode"] == "sound" and started[0]["questions"] == 5
    q.active = True
    server._KEYMAP_FUNCS["quiz"]()
    assert q.calls == ["stop"]


# ── the question pool ────────────────────────────────────────────────────────

def _set(tmp_path, monkeypatch, language="de"):
    sounds = tmp_path / "sounds"
    sounds.mkdir()
    for f in ("a_de.mp3", "a_fx.mp3", "s_de.mp3", "ue_fx.mp3"):
        (sounds / f).write_bytes(b"x")
    letters = {
        "A": {"entries": [{"word": "Affe", "image": "a.png", "enabled": True,
                           "tracks": {"DE": {"file": "a_de.mp3"}, "FX": {"file": "a_fx.mp3", "volume": 80}}},
                          {"word": "Aus", "enabled": False, "tracks": {"DE": {"file": "a_de.mp3"}}}]},
        "B": {"entries": [{"word": "Ball", "enabled": True, "tracks": {"DE": {"file": "missing.mp3"}}}]},
        "S": {"entries": [{"word": "Sonne", "enabled": True, "tracks": {"DE": {"file": "s_de.mp3", "enabled": False}}}]},
        "Ü": {"entries": [{"word": "Uhu", "enabled": True, "tracks": {"FX": {"file": "ue_fx.mp3"}}}]},
    }
    monkeypatch.setattr(server, "SOUNDS_DIR", str(sounds))
    monkeypatch.setattr(server, "settings", {"active_set": "s", "language": language,
                                             "sets": {"s": {"letters": letters}}})
    return str(sounds)


def test_pool_has_only_entries_with_a_playable_clip(tmp_path, monkeypatch):
    sounds = _set(tmp_path, monkeypatch)
    pool = server._quiz_pool([])
    assert sorted(pool) == ["A", "Ü"]                        # B: file missing, S: track off, Aus: disabled
    (affe,) = pool["A"]
    assert affe["clips"] == {"word": (os.path.join(sounds, "a_de.mp3"), 100),
                             "sound": (os.path.join(sounds, "a_fx.mp3"), 80)}
    assert affe["word"] == "Affe" and affe["image"] == "a.png" and len(affe["files"]) == 2
    assert pool["Ü"][0]["clips"]["word"] is None
    assert sorted(server._quiz_pool(["Ü", "B"])) == ["Ü"]


def test_pool_follows_the_keyboard_language(tmp_path, monkeypatch):
    _set(tmp_path, monkeypatch, language="us")
    assert sorted(server._quiz_pool([])) == ["A"]           # no Ü key on a US keyboard


# ── playback: on_done once, a stopped sequence never plays on ────────────────

class FakeProc:
    def __init__(self, log, path):
        self.log, self.path, self.ended = log, path, threading.Event()
        log.append(path)

    def wait(self):
        self.ended.wait(2)

    def poll(self):
        return 0 if self.ended.is_set() else None

    def terminate(self):
        self.ended.set()


def test_sequence_calls_on_done_once_and_stops_for_a_newer_sound(monkeypatch):
    log, procs = [], []

    def popen(cmd, **kw):
        procs.append(FakeProc(log, cmd[-1]))
        return procs[-1]

    monkeypatch.setattr(server.subprocess, "Popen", popen)
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: None)
    done = []
    server.play_sound_sequence([("one", 100), ("two", 100)], gap_ms=0, on_done=lambda: done.append("first"))
    for _ in range(100):
        if log:
            break
        time.sleep(0.01)
    server.play_sound_sequence([("three", 100)], gap_ms=0, on_done=lambda: done.append("second"))
    for _ in range(100):
        if "three" in log:
            break
        time.sleep(0.01)
    procs[-1].terminate()
    for _ in range(200):
        if len(done) == 2:
            break
        time.sleep(0.01)
    time.sleep(0.05)
    assert log == ["one", "three"]                   # "two" never starts: its sequence was replaced
    assert sorted(done) == ["first", "second"]


def test_empty_sequence_still_calls_on_done(monkeypatch):
    done = threading.Event()
    server.play_sound_sequence([], on_done=done.set)
    assert done.wait(1)


def test_failed_playback_calls_on_done(monkeypatch):
    def popen(*a, **k):
        raise FileNotFoundError("paplay")
    monkeypatch.setattr(server.subprocess, "Popen", popen)
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: None)
    done = threading.Event()
    server.play_sound_sequence([("x", 100)], on_done=done.set)
    assert done.wait(1)


def test_concurrent_starts_never_share_a_generation(monkeypatch):
    """The generation is taken with the stop: of two sequences started back to back, the older
    one never plays a track, even if its thread only runs after the newer one started."""
    runners, popened, done = [], [], []
    RealThread = threading.Thread
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(server.subprocess, "Popen", lambda cmd, **k: popened.append(cmd[-1]) or FakeProc([], cmd[-1]))

    class Held:                                     # a thread that runs only when the test says so
        def __init__(self, target, args=(), daemon=None):
            self.run = lambda: target(*args)

        def start(self):
            runners.append(self)

    monkeypatch.setattr(server.threading, "Thread", Held)
    server.play_sound_sequence([("old-1", 100), ("old-2", 100)], gap_ms=0, on_done=lambda: done.append("old"))
    server.play_sound_sequence([("new", 100)], gap_ms=0, on_done=lambda: done.append("new"))
    runners[0].run()                                # the older thread runs late
    assert popened == [] and done == ["old"]
    newer = RealThread(target=runners[1].run, daemon=True)
    newer.start()
    for _ in range(100):
        if server.active_processes:
            break
        time.sleep(0.01)
    for p in list(server.active_processes):
        p.terminate()                               # "new" finishes playing
    newer.join(2)
    assert popened == ["new"] and done == ["old", "new"]
