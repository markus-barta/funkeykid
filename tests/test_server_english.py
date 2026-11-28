"""FUNK-83: sound-effect instructions in English. Every sound instruction is rewritten to short English
before it goes to ElevenLabs (cached; unchanged if already English); suggestions get English descriptions;
a failed rewrite stops the job instead of paying for a German prompt.

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


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(server, "_ai_log_entry", lambda *a, **k: None)
    monkeypatch.setattr(server, "_en_cache", {})


def openrouter(monkeypatch, answer=None, fail=None):
    import urllib.request
    calls = []

    def fake(req, timeout=None):
        calls.append(json.loads(req.data))
        if fail:
            raise fail
        return Resp(json.dumps({"choices": [{"message": {"content": answer}}]}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-not-a-key")
    return calls


@pytest.mark.parametrize("text", ["Hund bellt", "Katze miaut", "a fox sleeping in its den", "AM radio tuning static"])
def test_every_instruction_is_rewritten_without_guessing_the_language(monkeypatch, text):
    calls = openrouter(monkeypatch, answer="dog barking")
    assert server._to_english(text, "sound") == "dog barking"
    msg = calls[0]["messages"][0]["content"]
    assert text in msg and "already English" in msg and "sound effect" in msg


def test_the_rewrite_is_cached(monkeypatch):
    calls = openrouter(monkeypatch, answer=' "branch cracking" ')
    assert server._to_english("Knacken wenn ein Ast bricht") == "branch cracking"
    assert server._to_english("Knacken wenn ein Ast bricht") == "branch cracking"
    assert len(calls) == 1


@pytest.mark.parametrize("fail,answer", [(OSError("down"), None), (None, ""), (None, "x" * 500)])
def test_a_failed_rewrite_gives_none(monkeypatch, fail, answer):
    openrouter(monkeypatch, answer=answer, fail=fail)
    assert server._to_english("Knacken", "sound") is None


def test_without_an_openrouter_key_the_instruction_goes_as_it_is(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert server._english_sfx_prompt("Hund bellt") == ("Hund bellt", None)


def test_with_a_key_a_failed_rewrite_is_an_error_with_its_cause(monkeypatch):
    openrouter(monkeypatch, fail=OSError("timeout"))
    text, err = server._english_sfx_prompt("Knacken wenn ein Ast bricht")
    assert text is None and isinstance(err, OSError)


def test_a_rewrite_that_fails_for_credit_says_so(monkeypatch, tmp_path):
    import urllib.error
    sent = sound_worker(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "_to_english_ex", lambda t, w="sound": (None, urllib.error.HTTPError("u", 402, "x", {}, io.BytesIO(b'{"error":{"message":"Insufficient credits"}}'))))
    server._gen_sound_worker("j1", "Hund", "dog barking", 4, "h_hund_fx.mp3")
    job = server.gen_jobs["j1"]
    assert sent == [] and job["status"] == "error" and job["error_code"] == 402
    assert job["error"].startswith("Übersetzung ins Englische: ") and "Guthaben" in job["error"]


def sound_worker(monkeypatch, tmp_path):
    import urllib.request
    monkeypatch.setattr(server, "gen_jobs", {"j1": {"id": "j1", "status": "queued"}})
    monkeypatch.setattr(server, "_loop", None)
    monkeypatch.setattr(server, "sse_broadcast", lambda *a: None)
    monkeypatch.setattr(server, "AI_SOUNDS_DIR", str(tmp_path / "ai"))
    monkeypatch.setattr(server, "_store_generated_sound", lambda data, name: False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-not-a-key")
    sent = []

    def eleven(req, timeout=None):
        sent.append(json.loads(req.data))
        return Resp(b"x" * 2000)
    monkeypatch.setattr(urllib.request, "urlopen", eleven)
    return sent


def test_the_sound_worker_sends_english_and_records_it(monkeypatch, tmp_path):
    sent = sound_worker(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "_to_english_ex", lambda t, w="sound": ("branch cracking", None))
    server._gen_sound_worker("j1", "Äste", "Knacken wenn ein Ast bricht", 4, "ae_aeste_fx.mp3")
    assert sent[0]["text"] == "branch cracking"
    assert server.gen_jobs["j1"]["prompt_sent"] == "branch cracking" and server.gen_jobs["j1"]["status"] == "done"


def test_a_failed_rewrite_stops_the_job_before_elevenlabs(monkeypatch, tmp_path):
    sent = sound_worker(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "_to_english_ex", lambda t, w="sound": (None, OSError("connection reset")))
    server._gen_sound_worker("j1", "Äste", "Knacken wenn ein Ast bricht", 4, "ae_aeste_fx.mp3")
    job = server.gen_jobs["j1"]
    assert sent == [] and job["status"] == "error" and job["error"].startswith("Übersetzung ins Englische: ")


def test_voices_are_never_rewritten(monkeypatch, tmp_path):
    sent = sound_worker(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "_to_english_ex", lambda *a: pytest.fail("TTS must not be rewritten"))
    server._gen_tts_worker("j1", "Äste", "voice", "ae_aeste_de.mp3")
    assert sent[0]["text"] == "Äste"


class Req:
    def __init__(self, body):
        self._body, self.match_info, self.headers = body, {}, {}

    async def json(self):
        return self._body


def test_a_suggestion_asks_for_english_and_gets_english_descriptions(monkeypatch):
    monkeypatch.setattr(server, "settings", {"sets": {}, "ai_prompts": {"suggest": "Schlage EIN deutsches Wort vor mit {letter}. VERBOTEN: {excluded}"}})
    asked = openrouter(monkeypatch, answer=json.dumps({"word": "Äste", "word_en": "branches", "sound_description": "Knacken wenn ein Ast bricht",
                                                      "image_description": "Mehrere Äste mit Blättern"}))
    rewrites = []
    monkeypatch.setattr(server, "_to_english", lambda t, w="sound": rewrites.append((t, w)) or ("EN " + w))
    d = json.loads(asyncio.run(server.api_suggest_word(Req({"letter": "Ä"}))).body)
    user = asked[0]["messages"][1]["content"]
    assert "IMMER auf Englisch" in user and "word_en" in user
    assert d["sound_description"] == "EN sound" and d["image_description"] == "EN picture" and d["word"] == "Äste"
    assert rewrites == [("Knacken wenn ein Ast bricht", "sound"), ("Mehrere Äste mit Blättern", "picture")]


def test_a_suggestion_keeps_its_descriptions_if_the_rewrite_fails(monkeypatch):
    monkeypatch.setattr(server, "settings", {"sets": {}, "ai_prompts": {}})
    openrouter(monkeypatch, answer=json.dumps({"word": "Ball", "sound_description": "ball bouncing", "image_description": "a red ball"}))
    monkeypatch.setattr(server, "_to_english", lambda t, w="sound": None)
    d = json.loads(asyncio.run(server.api_suggest_word(Req({"letter": "B"}))).body)
    assert d["sound_description"] == "ball bouncing" and d["image_description"] == "a red ball"
