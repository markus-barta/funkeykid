"""Fehlende Stimmen erzeugen (FUNK-75): one run fills every empty German voice slot of a set,
word by word, as ordinary jobs with a target. Skips what changed meanwhile, stops on a key or plan
error, retries a 429, and only one run goes at a time.

server.py needs aiohttp; these tests run where aiohttp is present (the lab venv).
"""
import asyncio
import io
import json
import os
import sys
import time
import types
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

pytest.importorskip("aiohttp")
import server  # noqa: E402


class Req:
    def __init__(self, body=None, **match):
        self._body, self.match_info, self.headers = body, match, {}

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def http_error(code, body=b""):
    return urllib.error.HTTPError("https://api.example", code, "x", {}, io.BytesIO(body))


@pytest.fixture
def st(monkeypatch, tmp_path):
    """Set 's': A has Affe (no voice) and Ameise (voice); B has Ball (no voice), Biene (no voice,
    spoken text "Bienchen") and an entry without a word; set 't' has one word without a voice."""
    sounds = tmp_path / "sounds"
    sounds.mkdir()
    (tmp_path / "images").mkdir()
    (sounds / "a_ameise_de.mp3").write_bytes(b"x")
    track = lambda f="", p="": {"file": f, "enabled": True, "prompt": p, "volume": 100}  # noqa: E731
    de = {"voice_id": server.DEFAULT_TTS_VOICES["de"], "model": server.TTS_MODEL_DEFAULT}
    data = {"active_set": "s",
            # FUNK-87: an approved voice runs through; the sample pause has its own tests
            "voice_languages": {"de": {"label": "Deutsch", **de, "approved": dict(de)}},
            "sets": {
        "s": {"name": "Komplett", "letters": {
            "B": {"entries": [
                {"id": "ballid01", "word": "Ball", "enabled": True, "tracks": {"FX": track("b_ball.mp3")}},
                {"id": "bieneid1", "word": "Biene", "enabled": False, "tracks": {"DE": track("", "Bienchen")}},
                {"id": "leerid01", "word": "  ", "enabled": True, "tracks": {}},
            ]},
            "A": {"entries": [
                {"id": "affeid01", "word": "Affe", "enabled": True, "tracks": {}},
                {"id": "ameiseid", "word": "Ameise", "enabled": True, "tracks": {"DE": track("a_ameise_de.mp3")}},
            ]},
        }},
        "t": {"name": "Tiere", "letters": {"K": {"entries": [
            {"id": "katzeid1", "word": "Katze", "enabled": True, "tracks": {}}]}}},
    }}
    monkeypatch.setattr(server, "settings", data)
    monkeypatch.setattr(server, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(server, "SOUNDS_DIR", str(sounds))
    monkeypatch.setattr(server, "SOUND_DIRS", (str(sounds),))
    monkeypatch.setattr(server, "IMAGES_DIR", str(tmp_path / "images"))
    monkeypatch.setattr(server, "gen_jobs", {})
    monkeypatch.setattr(server, "fill_run", None)
    monkeypatch.setattr(server, "_issued_media_names", set())
    monkeypatch.setattr(server, "_loop", None)
    monkeypatch.setattr(server, "FILL_RETRY_429", (0, 0))
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-not-a-key")
    sent = []
    monkeypatch.setattr(server, "sse_broadcast", lambda ev, d: sent.append((ev, json.loads(json.dumps(d)))))
    monkeypatch.setattr(server, "_ai_log_entry", lambda *a, **k: None)
    calls = []
    plan = []          # per call: "ok", an HTTP status, or a function run before the outcome

    def worker(job_id, text, voice_id, filename):
        calls.append({"job": job_id, "text": text, "file": filename, "voice": voice_id})
        if isinstance(voice_id, dict):          # like the real worker: the job says which voice made it (FUNK-87)
            server.gen_jobs[job_id]["voice"] = {k: voice_id.get(k) for k in ("voice_id", "voice_name", "model", "lang")}
        out = plan.pop(0) if plan else "ok"
        if callable(out):
            out = out() or "ok"
        if out == "ok":
            (sounds / filename).write_bytes(b"x")
            server.gen_jobs[job_id].update({"_done": True, "finished": time.time()})
        else:
            server._job_failed(job_id, "elevenlabs", http_error(out, b'{"detail":{"message":"nope"}}'), "tts", "m", text)
        server._job_finished(job_id)

    monkeypatch.setattr(server, "_gen_tts_worker", worker)
    return types.SimpleNamespace(settings=data, sent=sent, calls=calls, plan=plan, sounds=sounds)


def entry(st, letter, eid, set_id="s"):
    return next(e for e in st.settings["sets"][set_id]["letters"][letter]["entries"] if e["id"] == eid)


def de(st, letter, eid, set_id="s"):
    return (entry(st, letter, eid, set_id).get("tracks", {}).get("DE") or {}).get("file", "")


def start(body=None, set_id="s", before=None):
    """api_fill_start, then wait for the run to end; (status, response body, final run)."""
    async def go():
        if before:
            before()
        resp = await server.api_fill_start(Req(body if body is not None else {}, set_id=set_id))
        run = server.fill_run
        if run is not None and run.get("_task") is not None:
            await run["_task"]
        return resp.status, json.loads(resp.body), server._fill_public(server.fill_run)
    return asyncio.run(go())


# ── what is missing ─────────────────────────────────────────────────────────

def test_missing_are_words_with_an_empty_voice_slot_in_letter_order(st):
    assert server._fill_missing("s", "DE") == [("A", "affeid01"), ("B", "ballid01"), ("B", "bieneid1")]
    assert server._fill_missing("t", "DE") == [("K", "katzeid1")]
    assert server._fill_missing("nope", "DE") == []


def test_a_voice_switched_off_for_a_word_is_left_alone(st):
    entry(st, "A", "affeid01")["tracks"]["DE"] = {"file": "", "enabled": False, "prompt": "", "volume": 100}
    assert ("A", "affeid01") not in server._fill_missing("s", "DE")
    _, _, run = start()
    assert run["total"] == 2 and de(st, "A", "affeid01") == ""


def test_a_voice_switched_off_meanwhile_is_skipped(st):
    st.plan.append(lambda: entry(st, "B", "ballid01").setdefault("tracks", {}).update(
        DE={"file": "", "enabled": False, "prompt": "", "volume": 100}))
    _, _, run = start()
    assert (run["done"], run["skipped"]) == (2, 1) and de(st, "B", "ballid01") == ""


def test_the_spoken_text_wins_over_the_word(st):
    assert server._fill_text(entry(st, "B", "bieneid1"), "DE") == "Bienchen"
    assert server._fill_text(entry(st, "A", "affeid01"), "DE") == "Affe"
    assert server._fill_text(entry(st, "B", "leerid01"), "DE") == ""


# ── a run ────────────────────────────────────────────────────────────────────

def test_a_run_fills_every_missing_voice_of_its_set_only(st):
    status, body, run = start()
    assert status == 200 and body["ok"] and body["run"]["total"] == 3
    assert run["status"] == "done" and (run["done"], run["skipped"], run["failed"]) == (3, 0, 0)
    assert [c["text"] for c in st.calls] == ["Affe", "Ball", "Bienchen"]
    assert de(st, "A", "affeid01") == "a_affe_de.mp3" and de(st, "B", "ballid01") == "b_ball_de.mp3"
    assert de(st, "B", "bieneid1") == "b_biene_de.mp3"
    assert de(st, "A", "ameiseid") == "a_ameise_de.mp3"          # had one: untouched
    assert de(st, "K", "katzeid1", "t") == ""                    # another set: untouched
    assert all(c["voice"] == server._profile_for_kind("DE") for c in st.calls)   # FUNK-85: the German profile
    jobs = list(server.gen_jobs.values())
    assert len(jobs) == 3 and all(j["batch"] == run["id"] and j["kind"] == "DE" and j["applied"] for j in jobs)
    assert {j["target"]["entry_id"] for j in jobs} == {"affeid01", "ballid01", "bieneid1"}
    assert json.loads((st.sounds.parent / "settings.json").read_text())["sets"]["s"]["letters"]["A"]["entries"][0]["tracks"]["DE"]["file"] == "a_affe_de.mp3"


def test_progress_is_announced_and_ends_without_a_current_word(st):
    start()
    fills = [d for ev, d in st.sent if ev == "fill_update"]
    assert fills[0]["status"] == "running" and fills[0]["total"] == 3
    assert any(d["current"] == "Ball" and d["current_letter"] == "B" for d in fills)
    assert fills[-1]["status"] == "done" and fills[-1]["current"] == "" and fills[-1]["finished"]
    assert not any(k.startswith("_") for d in fills for k in d)


def test_nothing_missing_is_done_at_once(st):
    for e in st.settings["sets"]["t"]["letters"]["K"]["entries"]:
        e["tracks"]["DE"] = {"file": "k_katze_de.mp3", "enabled": True, "prompt": "", "volume": 100}
    status, body, run = start(set_id="t")
    assert status == 200 and run["status"] == "done" and run["total"] == 0 and st.calls == []


def test_a_slot_filled_or_a_word_removed_meanwhile_is_skipped(st):
    def change():
        entry(st, "B", "ballid01")["tracks"]["DE"] = {"file": "b_ball_mine.mp3", "enabled": True, "prompt": "", "volume": 100}
        es = st.settings["sets"]["s"]["letters"]["B"]["entries"]
        es[:] = [e for e in es if e["id"] != "bieneid1"]
    st.plan.append(change)                                      # while Affe is made
    _, _, run = start()
    assert (run["done"], run["skipped"], run["failed"]) == (1, 2, 0) and len(st.calls) == 1
    assert de(st, "B", "ballid01") == "b_ball_mine.mp3"


def test_a_slot_another_job_fills_is_skipped(st):
    def busy():
        server.gen_jobs["other"] = {"id": "other", "status": "generating", "target":
                                    {"set_id": "s", "letter": "B", "entry_id": "ballid01", "kind": "DE"}}
    _, _, run = start(before=busy)
    assert (run["done"], run["skipped"]) == (2, 1) and [c["text"] for c in st.calls] == ["Affe", "Bienchen"]


def test_a_slot_filled_while_its_voice_is_made_keeps_its_file(st):
    st.plan.append(lambda: entry(st, "A", "affeid01").setdefault("tracks", {}).update(
        DE={"file": "a_affe_mine.mp3", "enabled": True, "prompt": "", "volume": 100}))
    _, _, run = start()
    assert de(st, "A", "affeid01") == "a_affe_mine.mp3"
    assert (run["done"], run["skipped"]) == (2, 1)
    affe = next(j for j in server.gen_jobs.values() if j["batch"] and j["word"] == "Affe")
    assert affe["applied"] is False and affe["target"] is None   # only in the library: nothing to review


@pytest.mark.parametrize("code", [401, 402, 403])
def test_a_key_or_plan_error_stops_the_run_with_the_reason(st, code):
    st.plan.append(code)
    _, _, run = start()
    assert run["status"] == "error" and run["failed"] == 1 and len(st.calls) == 1
    assert run["error"] and run["failed_words"] == ["Affe"]
    assert de(st, "B", "ballid01") == ""


def test_an_exhausted_quota_stops_the_run(st, monkeypatch):
    def quota(job_id, text, voice_id, filename):
        st.calls.append(text)
        server._job_failed(job_id, "elevenlabs", http_error(400, b'{"detail":{"status":"quota_exceeded","message":"quota exceeded"}}'), "tts", "m", text)
        server._job_finished(job_id)
    monkeypatch.setattr(server, "_gen_tts_worker", quota)
    _, _, run = start()
    assert run["status"] == "error" and "Kontingent" in run["error"] and st.calls == ["Affe"]


def test_a_429_is_retried(st):
    st.plan.extend([429, 429])
    _, _, run = start()
    assert run["status"] == "done" and (run["done"], run["failed"]) == (3, 0)
    assert [c["text"] for c in st.calls] == ["Affe", "Affe", "Affe", "Ball", "Bienchen"]


def test_a_429_that_stays_counts_as_failed_and_the_run_goes_on(st):
    st.plan.extend([429, 429, 429])
    _, _, run = start()
    assert run["status"] == "done" and (run["done"], run["failed"]) == (2, 1) and run["failed_words"] == ["Affe"]


def test_three_failures_in_a_row_stop_the_run(st, monkeypatch):
    for e in st.settings["sets"]["s"]["letters"]["A"]["entries"]:
        e["tracks"].pop("DE", None)                             # now 4 are missing
    st.plan.extend([500, 500, 500])
    _, _, run = start()
    assert run["status"] == "error" and run["failed"] == 3 and len(st.calls) == 3


def test_a_failure_between_successes_does_not_stop_it(st):
    st.plan.extend(["ok", 500])
    _, _, run = start()
    assert run["status"] == "done" and (run["done"], run["failed"]) == (2, 1)


def test_stop_ends_the_run_after_the_word_it_is_on(st):
    def press_stop():
        asyncio.run(server.api_fill_stop(Req()))
    st.plan.append(press_stop)
    _, _, run = start()
    assert run["status"] == "stopped" and run["done"] == 1 and len(st.calls) == 1
    assert de(st, "A", "affeid01") == "a_affe_de.mp3" and de(st, "B", "ballid01") == ""


def test_a_crash_in_the_run_never_leaves_it_running(st, monkeypatch):
    monkeypatch.setattr(server, "_fill_one", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    _, _, run = start()
    assert run["status"] == "error" and "RuntimeError" in run["error"] and run["finished"]


# ── the requests ─────────────────────────────────────────────────────────────

def test_only_one_run_at_a_time(st):
    server.fill_run = {"id": "fill_x", "status": "running", "total": 9}
    status, body, _ = start()
    assert status == 409 and body["run"]["id"] == "fill_x" and st.calls == []


@pytest.mark.parametrize("body,set_id,code", [
    ({"kind": "FX"}, "s", 400), ({"kind": "EN"}, "s", 400), ({}, "nope", 404),
])
def test_bad_requests_are_refused(st, body, set_id, code):
    status, _, _ = start(body, set_id)
    assert status == code and st.calls == [] and server.fill_run is None


def test_no_body_means_german(st):
    async def go():                                             # a request without JSON
        resp = await server.api_fill_start(Req(None, set_id="t"))
        await server.fill_run["_task"]
        return resp.status, json.loads(resp.body)
    status, body = asyncio.run(go())
    assert status == 200 and body["run"]["kind"] == "DE" and de(st, "K", "katzeid1", "t") == "k_katze_de.mp3"


def test_without_a_key_nothing_starts(st, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    status, body, _ = start()
    assert status == 500 and "ELEVENLABS_API_KEY" in body["error"] and server.fill_run is None


def test_the_state_names_the_process(st):
    start()
    resp = asyncio.run(server.api_fill_get(Req()))
    data = json.loads(resp.body)
    assert data["run"]["status"] == "done" and resp.headers["X-FK-Boot"] == server.BOOT_ID
    assert "_task" not in data["run"]


def test_stop_without_a_run_is_harmless(st):
    resp = asyncio.run(server.api_fill_stop(Req()))
    assert resp.status == 200 and json.loads(resp.body)["run"] is None


def test_every_announcement_is_newer_than_the_last(st):
    start()
    seqs = [d["seq"] for ev, d in st.sent if ev == "fill_update"]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs) and server.fill_run["seq"] == seqs[-1]


def test_a_word_renamed_while_its_voice_is_made_does_not_get_it(st):
    st.plan.append(lambda: entry(st, "A", "affeid01").update(word="Adler"))
    _, _, run = start()
    assert de(st, "A", "affeid01") == "" and (run["done"], run["skipped"]) == (2, 1)
    affe = next(j for j in server.gen_jobs.values() if j["word"] == "Affe")
    assert affe["applied"] is False and affe["target"] is None and affe["text"] == "Affe"


def test_a_new_spoken_text_while_its_voice_is_made_does_not_get_it(st):
    st.plan.append(lambda: entry(st, "A", "affeid01").setdefault("tracks", {}).update(
        DE={"file": "", "enabled": True, "prompt": "Äffchen", "volume": 100}))
    _, _, run = start()
    assert de(st, "A", "affeid01") == "" and run["skipped"] == 1


class TrackReq(Req):
    pass


def track(st, body):
    async def go():
        return await server.api_generate_track(TrackReq(body))
    resp = asyncio.run(go())
    return resp.status, json.loads(resp.body)


def test_the_editor_asking_for_a_slot_a_run_fills_gets_that_job(st, monkeypatch):
    started = []
    monkeypatch.setattr(server.threading, "Thread", lambda *a, **k: types.SimpleNamespace(start=lambda: started.append(k)))
    target = {"set_id": "s", "letter": "B", "entry_id": "ballid01"}
    server.gen_jobs["trk_run1"] = {"id": "trk_run1", "status": "generating", "batch": "fill_1", "text": "Ball",
                                   "filename": "b_ball_de.mp3", "kind": "DE", "target": dict(target, kind="DE")}
    status, body = track(st, {"kind": "DE", "word": "Ball", "slug_prefix": "b", "target": target})
    assert status == 200 and body == {"ok": True, "job_id": "trk_run1", "filename": "b_ball_de.mp3", "existing": True}
    assert started == [] and len(server.gen_jobs) == 1
    # another text is what the parent asked for: its own job
    status, body = track(st, {"kind": "DE", "word": "Ball", "prompt": "Bällchen", "slug_prefix": "b", "target": target})
    assert status == 200 and body["job_id"] != "trk_run1" and "existing" not in body and len(started) == 1
    assert server.gen_jobs[body["job_id"]]["text"] == "Bällchen"
    # a finished or detached job does not hold the slot
    server.gen_jobs["trk_run1"]["status"] = "done"
    status, body = track(st, {"kind": "DE", "word": "Ball", "slug_prefix": "b", "target": target})
    assert body["job_id"] not in ("trk_run1",) and "existing" not in body


def test_every_state_is_ordered_across_runs(st):
    first = asyncio.run(server.api_fill_get(Req()))
    seq0 = json.loads(first.body)["seq"]
    _, body1, run1 = start()
    _, body2, run2 = start(set_id="t")
    assert seq0 < body1["seq"] <= run1["seq"] < body2["seq"] <= run2["seq"]
    got = json.loads(asyncio.run(server.api_fill_get(Req())).body)
    assert got["seq"] == run2["seq"] and got["run"]["id"] == run2["id"]


def test_stop_twice_announces_once(st):
    server.fill_run = {"id": "fill_x", "status": "running", "stopping": False, "seq": 1}
    asyncio.run(server.api_fill_stop(Req()))
    asyncio.run(server.api_fill_stop(Req()))
    assert [ev for ev, _ in st.sent].count("fill_update") == 1 and server.fill_run["stopping"] is True


def test_a_failed_save_stops_the_run_as_a_failure(st, monkeypatch):
    def boom():
        raise OSError("disk full")
    monkeypatch.setattr(server, "save_settings", boom)
    _, _, run = start()
    assert run["status"] == "error" and "Speicher" in run["error"] and len(st.calls) == 1
    assert (run["done"], run["skipped"], run["failed"]) == (0, 0, 1) and run["failed_words"] == ["Affe"]
    affe = next(j for j in server.gen_jobs.values() if j["word"] == "Affe")
    assert affe["applied"] is False and affe["target"] and affe["save_failed"]   # its file waits for review
    assert de(st, "A", "affeid01") == ""


def test_a_worker_that_raises_ends_its_job_and_frees_the_slot(st, monkeypatch):
    def raises(job_id, text, voice_id, filename):
        st.calls.append(text)
        raise PermissionError("no folder")
    monkeypatch.setattr(server, "_gen_tts_worker", raises)
    _, _, run = start()
    jobs = list(server.gen_jobs.values())
    assert run["status"] == "error" and run["failed"] == 3 and len(st.calls) == 3   # three in a row
    assert all(j["status"] == "error" and "PermissionError" in j["error"] for j in jobs)
    assert not server._slot_busy("s", "A", "affeid01", "DE")


def test_the_real_worker_ends_its_job_when_its_folder_fails(st, monkeypatch, tmp_path):
    monkeypatch.undo()                       # the real _gen_tts_worker, no network reached
    monkeypatch.setattr(server, "gen_jobs", {"j1": {"id": "j1", "status": "queued"}})
    monkeypatch.setattr(server, "_loop", None)
    monkeypatch.setattr(server, "sse_broadcast", lambda *a: None)
    monkeypatch.setattr(server, "_ai_log_entry", lambda *a, **k: None)
    blocker = tmp_path / "file"
    blocker.write_bytes(b"x")
    monkeypatch.setattr(server, "AI_SOUNDS_DIR", str(blocker / "sounds"))   # a file in the way: makedirs fails
    server._gen_tts_worker("j1", "Affe", "v", "a_affe_de.mp3")
    assert server.gen_jobs["j1"]["status"] == "error"


def test_a_repeated_request_finds_its_job_behind_another(st, monkeypatch):
    started = []
    monkeypatch.setattr(server.threading, "Thread", lambda *a, **k: types.SimpleNamespace(start=lambda: started.append(k)))
    target = {"set_id": "s", "letter": "B", "entry_id": "ballid01"}
    server.gen_jobs["trk_run1"] = {"id": "trk_run1", "status": "generating", "batch": "fill_1", "text": "Ball",
                                   "filename": "b_ball_de.mp3", "kind": "DE", "target": dict(target, kind="DE")}
    _, first = track(st, {"kind": "DE", "word": "Ball", "prompt": "Bällchen", "slug_prefix": "b", "target": target})
    _, again = track(st, {"kind": "DE", "word": "Ball", "prompt": "Bällchen", "slug_prefix": "b", "target": target})
    assert again == {"ok": True, "job_id": first["job_id"], "filename": first["filename"], "existing": True}
    assert len(started) == 1


def test_weiter_after_a_failed_save_attaches_the_made_voice_without_paying_again(st, monkeypatch):
    real = server.save_settings
    fails = [True]

    def flaky():
        if fails[0]:
            raise OSError("disk full")
        return real()
    monkeypatch.setattr(server, "save_settings", flaky)
    _, _, run1 = start()
    affe = next(j for j in server.gen_jobs.values() if j["word"] == "Affe")
    assert run1["status"] == "error" and affe["save_failed"] and len(st.calls) == 1
    fails[0] = False                                            # storage repaired: "Weiter"
    _, _, run2 = start()
    assert run2["status"] == "done" and (run2["done"], run2["failed"]) == (3, 0)
    assert [c["text"] for c in st.calls] == ["Affe", "Ball", "Bienchen"]       # Affe was not made again
    assert de(st, "A", "affeid01") == affe["filename"]
    assert affe["applied"] is True and affe["target"] is None and "save_failed" not in affe
    assert ("letter", {"set_id": "s", "letter": "A", "entry_id": "affeid01", "origin": "job"}) in st.sent


def test_a_made_voice_for_another_text_is_not_attached(st, monkeypatch):
    real = server.save_settings
    fails = [True]
    monkeypatch.setattr(server, "save_settings", lambda: (_ for _ in ()).throw(OSError("full")) if fails[0] else real())
    start()
    fails[0] = False
    entry(st, "A", "affeid01")["word"] = "Adler"                # renamed since: the "Affe" voice is not it
    _, _, run = start()
    assert [c["text"] for c in st.calls] == ["Affe", "Adler", "Ball", "Bienchen"]
    assert de(st, "A", "affeid01") == "a_adler_de.mp3"


def test_a_second_failed_save_on_attach_stops_the_run_again(st, monkeypatch):
    monkeypatch.setattr(server, "save_settings", lambda: (_ for _ in ()).throw(OSError("full")))
    start()
    _, _, run = start()
    assert run["status"] == "error" and run["failed"] == 1 and len(st.calls) == 1
    assert de(st, "A", "affeid01") == ""


# ── FUNK-87: redo voices an older voice made; a new voice starts with one word to approve ─────────

def unapprove(st):
    st.settings["voice_languages"]["de"].pop("approved", None)


@pytest.fixture
def meta(monkeypatch):
    m = {}
    monkeypatch.setattr(server, "_voice_meta_cache", m)
    return m


def made_now(meta, filename):
    p = server._profile_for_kind("DE")
    meta[filename] = {"voice_id": p["voice_id"], "model": p["model"], "lang": "de"}


def test_redo_takes_the_voices_another_voice_made(st, meta):
    entry(st, "A", "affeid01")["tracks"]["DE"] = {"file": "a_affe_de.mp3", "enabled": True, "prompt": "", "volume": 100}
    (st.sounds / "a_affe_de.mp3").write_bytes(b"x")
    made_now(meta, "a_affe_de.mp3")                          # made by the current voice: left alone
    assert server._fill_items("s", "DE", "redo") == [("A", "ameiseid")]   # no record: an older voice
    meta["a_ameise_de.mp3"] = {"voice_id": "Rachel000000000000", "model": "eleven_multilingual_v2", "lang": "de"}
    assert server._fill_items("s", "DE", "redo") == [("A", "ameiseid")]
    entry(st, "A", "ameiseid")["tracks"]["DE"]["enabled"] = False            # switched off for this word
    assert server._fill_items("s", "DE", "redo") == []


def test_redo_includes_the_digits(st, meta):
    st.settings["sets"]["s"]["numbers"] = {"3": {"word": "Drei Bälle", "tracks": {"DE": {"file": "3_drei_de.mp3", "enabled": True, "prompt": "", "volume": 100}}},
                                           "4": {"word": "Vier", "tracks": {}}}
    assert ("3", server.NUMBER_ENTRY_ID) in server._fill_items("s", "DE", "redo")
    assert ("4", server.NUMBER_ENTRY_ID) in server._fill_items("s", "DE", "missing")


def test_a_redo_run_swaps_the_old_file_and_keeps_it_in_the_library(st, meta):
    status, body, run = start({"mode": "redo"})
    assert status == 200 and run["mode"] == "redo" and run["total"] == 1 and run["done"] == 1
    new = de(st, "A", "ameiseid")
    assert new and new != "a_ameise_de.mp3" and (st.sounds / "a_ameise_de.mp3").exists()
    assert st.calls[0]["text"] == "Ameise"


def test_a_redo_never_overwrites_a_choice_made_meanwhile(st, meta):
    st.plan.append(lambda: entry(st, "A", "ameiseid")["tracks"]["DE"].update(file="mine.mp3"))
    _, _, run = start({"mode": "redo"})
    assert de(st, "A", "ameiseid") == "mine.mp3" and run["done"] == 0 and run["skipped"] == 1


def test_an_unknown_mode_is_refused(st):
    status, body, _ = start({"mode": "everything"})
    assert status == 400 and st.calls == []


def with_sample(st, decide, body=None):
    """Start a run, wait for its first word, let decide(run) act, then wait for the end."""
    async def go():
        await server.api_fill_start(Req(body or {}, set_id="s"))
        run = server.fill_run
        for _ in range(300):
            if run.get("phase") == "sample" or run["status"] != "running":
                break
            await asyncio.sleep(0.01)
        sample = dict(run.get("sample") or {})
        await decide(run)
        if run.get("_task") is not None:
            await run["_task"]
        return sample, server._fill_public(server.fill_run)
    return asyncio.run(go())


def test_a_new_voice_waits_after_the_first_word_then_goes_on(st):
    unapprove(st)
    seen = {}

    async def ok(run):
        seen["before"] = de(st, "A", "affeid01")             # not on the word until approved
        seen["job"] = dict(server.gen_jobs[run["sample"]["job_id"]])
        stale = await server.api_fill_continue(Req({"run_id": run["id"], "job_id": "trk_other"}))
        seen["stale"] = stale.status                          # another sample than the one played: refused
        r = await server.api_fill_continue(Req({"run_id": run["id"], "job_id": run["sample"]["job_id"]}))
        seen["status"] = r.status
    sample, run = with_sample(st, ok)
    assert sample["word"] == "Affe" and sample["voice_id"] == server._profile_for_kind("DE")["voice_id"]
    assert seen["before"] == "" and seen["job"].get("target") is None and seen["status"] == 200 and seen["stale"] == 409
    assert de(st, "A", "affeid01") == sample["file"]          # landed after "Klingt gut"
    assert run["status"] == "done" and run["done"] == 3 and len(st.calls) == 3
    assert st.settings["voice_languages"]["de"]["approved"]["voice_id"] == sample["voice_id"]
    assert server.gen_jobs[sample["job_id"]]["applied"] is True


def test_stopping_at_the_first_word_leaves_the_word_alone(st):
    unapprove(st)

    async def stop(run):
        await server.api_fill_stop(Req())
    sample, run = with_sample(st, stop)
    assert run["status"] == "stopped" and run["phase"] is None and len(st.calls) == 1
    assert de(st, "A", "affeid01") == "" and (st.sounds / sample["file"]).exists()
    assert "approved" not in st.settings["voice_languages"]["de"]
    assert server.gen_jobs[sample["job_id"]].get("target") is None


def test_a_voice_chosen_during_the_first_word_stops_the_run(st):
    unapprove(st)

    async def change_then_go(run):
        st.settings["voice_languages"]["de"]["voice_id"] = "OtherVoice1234567890"
        await server.api_fill_continue(Req({"run_id": run["id"], "job_id": run["sample"]["job_id"]}))
    sample, run = with_sample(st, change_then_go)
    assert run["status"] == "stopped" and "andere Stimme" in run["error"]
    assert de(st, "A", "affeid01") == "" and len(st.calls) == 1


def test_continue_only_for_the_waiting_run(st):
    unapprove(st)

    async def wrong_then_stop(run):
        r = await server.api_fill_continue(Req({"run_id": "fill_other"}))
        assert r.status == 409
        await server.api_fill_stop(Req())
    with_sample(st, wrong_then_stop)
    assert asyncio.run(server.api_fill_continue(Req({"run_id": "x"}))).status == 409


def test_an_approved_voice_runs_through_without_waiting(st):
    _, _, run = start()
    assert run["status"] == "done" and run["done"] == 3 and run.get("sample") is None


class QReq(Req):
    def __init__(self, body=None, query=None, **match):
        super().__init__(body, **match)
        self.query = query or {}


def test_approving_needs_the_voice_that_is_set_now(st, monkeypatch):
    unapprove(st)
    p = server._profile_for_kind("DE")
    assert asyncio.run(server.api_approve_voice_language(Req({"voice_id": "Old", "model": p["model"]}, code="de"))).status == 409
    r = asyncio.run(server.api_approve_voice_language(Req({"voice_id": p["voice_id"], "model": p["model"]}, code="de")))
    assert r.status == 200 and next(l for l in json.loads(r.body)["languages"] if l["code"] == "de")["approved"] is True
    assert server._voice_approved(p)


def test_outdated_counts_what_a_redo_would_make(st, meta):
    meta["a_ameise_de.mp3"] = {"voice_id": "R", "voice_name": "Rachel (amerikanisch)", "model": "eleven_multilingual_v2", "lang": "de"}
    d = json.loads(asyncio.run(server.api_voice_outdated(QReq(query={"set_id": "s"}))).body)
    assert d["count"] == 1 and d["chars"] == len("Ameise") and d["by_voice"] == [{"name": "Rachel (amerikanisch)", "count": 1}]
    assert asyncio.run(server.api_voice_outdated(QReq(query={"set_id": "nope"}))).status == 404



def test_stopping_while_the_first_word_is_made_ends_the_run(st):
    unapprove(st)
    st.plan.append(lambda: server.fill_run.update(stopping=True))   # "Anhalten" while the first word is made
    _, _, run = start()
    assert run["status"] == "stopped" and run["phase"] is None and len(st.calls) == 1 and de(st, "A", "affeid01") == ""
    later = asyncio.run(server.api_fill_stop(Req()))       # a stop after the end changes nothing
    assert later.status == 200


def test_stop_releases_a_waiting_sample_even_when_already_stopping(st):
    unapprove(st)

    async def stop_twice(run):
        run["stopping"] = True                              # e.g. "Anhalten" just before the sample landed
        await server.api_fill_stop(Req())
    _, run = with_sample(st, stop_twice)
    assert run["status"] == "stopped"


def test_a_redo_keeps_a_choice_made_after_the_run_started(st, meta):
    entry(st, "A", "affeid01")["tracks"]["DE"] = {"file": "a_affe_de.mp3", "enabled": True, "prompt": "", "volume": 100}
    (st.sounds / "a_affe_de.mp3").write_bytes(b"x")
    # the run starts with Affe and Ameise to redo; while Affe is made, the parent picks a file for Ameise
    st.plan.append(lambda: entry(st, "A", "ameiseid")["tracks"]["DE"].update(file="parent-choice.mp3"))
    _, _, run = start({"mode": "redo"})
    assert de(st, "A", "ameiseid") == "parent-choice.mp3" and run["done"] == 1 and run["skipped"] == 1 and len(st.calls) == 1


def test_a_digit_voice_that_could_not_be_saved_is_attached_by_the_next_run(st, monkeypatch):
    st.settings["sets"]["s"]["numbers"] = {"4": {"word": "Vier", "tracks": {}}}
    for e in [entry(st, "A", "affeid01"), entry(st, "B", "ballid01"), entry(st, "B", "bieneid1")]:
        e["tracks"]["DE"] = {"file": "x.mp3", "enabled": True, "prompt": "", "volume": 100}
    real = server.save_settings
    monkeypatch.setattr(server, "save_settings", lambda: (_ for _ in ()).throw(OSError(28, "full")))
    _, _, run = start()
    assert run["status"] == "error" and len(st.calls) == 1
    monkeypatch.setattr(server, "save_settings", real)
    _, _, run2 = start()
    num = st.settings["sets"]["s"]["numbers"]["4"]
    assert run2["done"] == 1 and len(st.calls) == 1 and num["tracks"]["DE"]["file"] == st.calls[0]["file"]


def test_an_approved_sample_that_could_not_be_saved_is_not_paid_again(st, monkeypatch):
    unapprove(st)
    real = server.save_settings
    state = {"fail": False}

    def maybe():
        if state["fail"]:
            raise OSError(28, "full")
        return real()
    monkeypatch.setattr(server, "save_settings", maybe)

    async def go_with_full_disk(run):
        state["fail"] = True                                # the approval and the word can not be saved
        await server.api_fill_continue(Req({"run_id": run["id"], "job_id": run["sample"]["job_id"]}))
    sample, run = with_sample(st, go_with_full_disk)
    assert run["status"] == "error" and de(st, "A", "affeid01") == "" and len(st.calls) == 1
    state["fail"] = False                                   # storage back; the approval was not saved, so the next run
                                                            # attaches the paid sample and asks again at its next word
    async def stop(run2):
        await server.api_fill_stop(Req())
    sample2, _ = with_sample(st, stop)
    assert de(st, "A", "affeid01") == sample["file"] and [c["text"] for c in st.calls] == ["Affe", "Ball"]
    assert sample2["word"] == "Ball"



def test_a_saved_result_of_an_earlier_voice_is_not_attached_for_a_new_one(st, monkeypatch, meta):
    real = server.save_settings
    monkeypatch.setattr(server, "save_settings", lambda: (_ for _ in ()).throw(OSError(28, "full")))
    _, _, run = start({"mode": "redo"})
    assert run["status"] == "error" and len(st.calls) == 1             # made with voice A, not saved
    monkeypatch.setattr(server, "save_settings", real)
    st.settings["voice_languages"]["de"]["voice_id"] = "VoiceB1234567890ab"   # the parent picks voice B
    st.settings["voice_languages"]["de"]["approved"] = {"voice_id": "VoiceB1234567890ab", "model": server.TTS_MODEL_DEFAULT}
    _, _, run2 = start({"mode": "redo"})
    assert len(st.calls) == 2 and st.calls[1]["voice"]["voice_id"] == "VoiceB1234567890ab"
    assert de(st, "A", "ameiseid") == st.calls[1]["file"]
