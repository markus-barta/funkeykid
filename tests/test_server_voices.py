"""FUNK-85: every spoken track uses its language's voice, model and language code; which voice made a
file is recorded; voices are picked per language (shared library / own voices) and tested once, cached.

server.py needs aiohttp; these tests run where aiohttp is present (the lab venv).
"""
import asyncio
import io
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

pytest.importorskip("aiohttp")
import server  # noqa: E402

RACHEL, ADAM, LEA = "21m00Tcm4TlvDq8ikWAM", "pNInz6obpgDQGcFmaJgB", "LeaVoice1234567890ab"


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class Req:
    def __init__(self, body=None, match=None, query=None):
        self._body, self.match_info, self.query, self.headers = body, match or {}, query or {}, {}

    async def json(self):
        return self._body


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "settings", {"tts_voices": {"de": RACHEL, "en": ADAM}, "numbers_tts": {"voice_id": RACHEL}})
    monkeypatch.setattr(server, "AI_DIR", str(tmp_path / "ai"))
    monkeypatch.setattr(server, "AI_SOUNDS_DIR", str(tmp_path / "ai" / "sounds"))
    monkeypatch.setattr(server, "_voice_meta_cache", None)
    monkeypatch.setattr(server, "_voice_list_cache", {})
    monkeypatch.setattr(server, "save_settings", lambda: None)
    monkeypatch.setattr(server, "_ai_log_entry", lambda *a, **k: None)
    monkeypatch.setattr(server, "sse_broadcast", lambda *a: None)
    monkeypatch.setattr(server, "_loop", None)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-not-a-key")
    server._ensure_voice_languages()
    return tmp_path


def body(resp):
    return json.loads(resp.body)


def test_migration_keeps_the_old_voices_and_names_them(env):
    vl = server.settings["voice_languages"]
    assert list(vl) == ["de", "en"]
    assert vl["de"] == {"label": "Deutsch", "voice_id": RACHEL, "voice_name": "Rachel (amerikanisch)", "model": "eleven_v4"}
    assert vl["en"]["voice_id"] == ADAM and server._ensure_voice_languages() is False


def test_the_request_names_voice_model_and_language(env):
    req = server._tts_request("Bär", server._voice_profile("de"))
    sent = json.loads(req.data)
    assert req.full_url.endswith("/" + RACHEL) and sent["model_id"] == "eleven_v4" and sent["language_code"] == "de"
    old = dict(server._voice_profile("de"), model="eleven_multilingual_v2")
    assert "language_code" not in json.loads(server._tts_request("Bär", old).data)        # v2 does not take it


def test_an_unknown_model_falls_back_to_the_default(env):
    server.settings["voice_languages"]["de"]["model"] = "eleven_made_up"
    assert server._voice_profile("de")["model"] == "eleven_v4"


def test_the_worker_speaks_with_the_profile_and_records_the_voice(env, monkeypatch):
    import urllib.request
    sent = []
    monkeypatch.setattr(server, "gen_jobs", {"j1": {"id": "j1", "status": "queued"}})
    monkeypatch.setattr(server, "_store_generated_sound", lambda data, name: False)
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: sent.append(req) or Resp(b"x" * 2000))
    server.settings["voice_languages"]["de"].update(voice_id=LEA, voice_name="Lea")
    server._gen_tts_worker("j1", "Bär", server._profile_for_kind("DE"), "b_baer_de.mp3")
    assert sent[0].full_url.endswith("/" + LEA) and json.loads(sent[0].data)["language_code"] == "de"
    assert server.gen_jobs["j1"]["status"] == "done" and server.gen_jobs["j1"]["voice"]["voice_name"] == "Lea"
    meta = json.load(open(os.path.join(server.AI_DIR, "voice_meta.json")))
    assert meta["b_baer_de.mp3"]["voice_id"] == LEA and meta["b_baer_de.mp3"]["model"] == "eleven_v4" and meta["b_baer_de.mp3"]["lang"] == "de"


def test_english_tracks_use_the_english_profile(env):
    p = server._profile_for_kind("EN")
    assert p["voice_id"] == ADAM and p["lang"] == "en" and server._profile_for_kind("FX") is None


def test_setting_a_voice_validates_and_keeps_the_old_copies_in_step(env):
    r = asyncio.run(server.api_patch_voice_language(Req({"voice_id": LEA, "voice_name": "Lea", "model": "eleven_flash_v2_5"}, {"code": "de"})))
    assert r.status == 200
    de = next(l for l in body(r)["languages"] if l["code"] == "de")
    assert de["voice_name"] == "Lea" and de["model"] == "eleven_flash_v2_5" and de["sets_language"] is True
    assert server.settings["tts_voices"]["de"] == LEA and server.settings["numbers_tts"] == {"voice_id": LEA}
    for bad in ({"voice_id": "../x"}, {"voice_id": ""}, {"model": "gpt"}):
        assert asyncio.run(server.api_patch_voice_language(Req(bad, {"code": "de"}))).status == 400
    assert asyncio.run(server.api_patch_voice_language(Req({"voice_id": LEA}, {"code": "xx"}))).status == 404


def test_a_settings_put_cannot_overwrite_the_voice_copies(env, monkeypatch):
    monkeypatch.setattr(server, "_reload_runtime", lambda: None)
    asyncio.run(server.api_patch_voice_language(Req({"voice_id": LEA}, {"code": "de"})))
    asyncio.run(server.api_put_settings(Req({"numbers_tts": {"voice_id": RACHEL}, "volume": 50})))
    assert server.settings["numbers_tts"] == {"voice_id": LEA} and server.settings["volume"] == 50


def test_the_picker_lists_library_voices_and_caches_them(env, monkeypatch):
    calls = []

    def fake(path, timeout=20):
        calls.append(path)
        return {"voices": [{"voice_id": LEA, "name": "Lea - Young female", "accent": "standard", "locale": "de-DE", "language": "de",
                            "gender": "female", "age": "young", "preview_url": "https://x/p.mp3", "rate": 1.0, "category": "professional",
                            "descriptive": "clear"}]}
    monkeypatch.setattr(server, "_eleven_get", fake)
    r = body(asyncio.run(server.api_voice_list(Req(query={"source": "library", "lang": "de", "gender": "female", "age": "bad"}))))
    assert r["voices"][0]["voice_id"] == LEA and r["voices"][0]["locale"] == "de-DE" and r["voices"][0]["preview_url"]
    assert "language=de" in calls[0] and "gender=female" in calls[0] and "&age=" not in calls[0]
    asyncio.run(server.api_voice_list(Req(query={"source": "library", "lang": "de", "gender": "female"})))
    assert len(calls) == 1


def test_a_key_without_voices_read_says_so(env, monkeypatch):
    import urllib.error

    def denied(path, timeout=20):
        raise urllib.error.HTTPError("u", 401, "x", {}, io.BytesIO(
            b'{"detail":{"status":"missing_permissions","message":"The API key you used is missing the permission voices_read to execute this operation."}}'))
    monkeypatch.setattr(server, "_eleven_get", denied)
    r = asyncio.run(server.api_voice_list(Req(query={"source": "mine"})))
    assert r.status == 502 and "Voices: Read" in body(r)["error"]


def test_a_voice_test_is_made_once_then_served_from_the_cache(env, monkeypatch):
    made = []
    monkeypatch.setattr(server, "_tts_bytes", lambda text, profile, timeout=30: made.append((text, dict(profile))) or b"y" * 2000)
    r1 = body(asyncio.run(server.api_voice_test(Req({"lang": "de", "voice_id": LEA, "voice_name": "Lea"}))))
    r2 = body(asyncio.run(server.api_voice_test(Req({"lang": "de", "voice_id": LEA, "voice_name": "Lea"}))))
    assert r1["cached"] is False and r2["cached"] is True and r1["url"] == r2["url"] and len(made) == 1
    assert made[0][0] == "Bär. Eis. Ball. Bagger." and made[0][1]["voice_id"] == LEA and made[0][1]["lang"] == "de"
    r3 = body(asyncio.run(server.api_voice_test(Req({"lang": "de"}))))       # the saved (Rachel) voice: another file
    assert r3["url"] != r1["url"] and len(made) == 2
    name = r1["url"].rsplit("/", 1)[1]
    assert server._safe_file(server._voice_test_dir(), name)
    assert asyncio.run(server.api_voice_test(Req({"lang": "de", "voice_id": "../../etc"}))).status == 400
    assert asyncio.run(server.api_voice_test(Req({"lang": "xx"}))).status == 404


def test_a_failed_save_keeps_the_old_voice_in_use(env, monkeypatch):
    def boom():
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(server, "save_settings", boom)
    r = asyncio.run(server.api_patch_voice_language(Req({"voice_id": LEA, "voice_name": "Lea"}, {"code": "de"})))
    assert r.status == 500 and "Nicht gespeichert" in body(r)["error"]
    assert server._voice_profile("de")["voice_id"] == RACHEL
    assert server.settings["tts_voices"]["de"] == RACHEL and server.settings["numbers_tts"] == {"voice_id": RACHEL}


def test_overlapping_saves_keep_both_languages(env):
    class SlowReq(Req):
        def __init__(self, body, match, gate):
            super().__init__(body, match)
            self.gate = gate

        async def json(self):
            await self.gate.wait()
            return self._body

    async def run():
        gate = asyncio.Event()
        a = asyncio.ensure_future(server.api_patch_voice_language(SlowReq({"voice_id": LEA, "voice_name": "Lea"}, {"code": "de"}, gate)))
        b = asyncio.ensure_future(server.api_patch_voice_language(Req({"voice_id": "EnglishVoice123456", "voice_name": "Ann"}, {"code": "en"})))
        await b
        gate.set()
        await a
    asyncio.run(run())
    assert server._voice_profile("de")["voice_id"] == LEA and server._voice_profile("en")["voice_id"] == "EnglishVoice123456"
    assert server.settings["tts_voices"] == {"de": LEA, "en": "EnglishVoice123456"}


def test_the_same_test_at_the_same_time_is_paid_once(env, monkeypatch):
    import time as _t
    made = []

    def slow(text, profile, timeout=30):
        made.append(1)
        _t.sleep(0.3)
        return b"z" * 2000
    monkeypatch.setattr(server, "_tts_bytes", slow)
    monkeypatch.setattr(server, "_voice_test_inflight", {})

    async def run():
        return await asyncio.gather(server.api_voice_test(Req({"lang": "de", "voice_id": LEA})),
                                    server.api_voice_test(Req({"lang": "de", "voice_id": LEA})))
    r1, r2 = asyncio.run(run())
    assert r1.status == r2.status == 200 and body(r1)["url"] == body(r2)["url"] and len(made) == 1
    assert server._voice_test_inflight == {}


def test_a_fill_run_takes_the_voice_as_it_is_at_each_word(env, monkeypatch):
    import types
    server.settings["sets"] = {"s": {"name": "S", "letters": {"A": {"entries": [
        {"id": "e1", "word": "Affe", "tracks": {}}, {"id": "e2", "word": "Ast", "tracks": {}}]}}}}
    used = []

    async def one(run, letter, entry_id, word, text, profile):
        used.append(profile["voice_id"])
        if len(used) == 1:                  # the parent picks Lea while the first word is being made
            server.settings["voice_languages"]["de"]["voice_id"] = LEA
        return {"status": "done", "applied": True}
    monkeypatch.setattr(server, "_fill_one", one)
    monkeypatch.setattr(server, "_fill_broadcast", lambda run: None)
    run = {"id": "r", "set_id": "s", "kind": "DE", "stopping": False, "status": "running", "total": 2, "done": 0, "skipped": 0,
           "failed": 0, "failed_words": [], "current": "", "current_letter": "", "error": "", "seq": 0}
    asyncio.run(server._fill_worker(run, [("A", "e1"), ("A", "e2")]))
    assert used == [RACHEL, LEA]
