"""FUNK-84: a suggestion must belong to its letter. Ä/Ö/Ü words start with the umlaut (not A/O/U),
ß words contain ß; a wrong word is retried with a correction and never shown.

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


class Req:
    def __init__(self, body):
        self._body, self.match_info, self.headers = body, {}, {}

    async def json(self):
        return self._body


@pytest.mark.parametrize("word,letter,ok", [
    ("Überraschung", "Ü", True), ("Uhr", "Ü", False), ("übung", "Ü", True), ("Öl", "Ö", True), ("Ofen", "Ö", False),
    ("Ärmel", "Ä", True), ("Apfel", "Ä", False), ("Apfel", "A", True), ("Äpfel", "A", False), ("Fuß", "ß", True),
    ("Fuss", "ß", False), ("Ball", "B", True), ("ball", "B", True), ("Hund", "B", False), ("", "B", False), ("Eins", "1", True),
    ("FUẞ", "ß", True), ("STRAẞE", "ß", True), ("die Ölkanne", "Ö", True), ("der Übertopf", "Ü", True), ("Die Uhr", "Ü", False),
    ("O\u0308lkanne", "Ö", True), ("O\u0308lkanne", "O", False), ("Dino", "D", True), ("Ei", "E", True), ("eine Ente", "E", True),
])
def test_a_word_fits_its_letter(word, letter, ok):
    assert server._fits_letter(word, letter) is ok


def suggest(monkeypatch, letter, answers):
    import urllib.request
    monkeypatch.setattr(server, "settings", {"sets": {}, "ai_prompts": {}})
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-not-a-key")
    monkeypatch.setattr(server, "_ai_log_entry", lambda *a, **k: None)
    monkeypatch.setattr(server, "_to_english", lambda t, w="sound": t)
    asked, queue = [], list(answers)

    def fake(req, timeout=None):
        asked.append(json.loads(req.data))
        word = queue.pop(0)
        return Resp(json.dumps({"choices": [{"message": {"content": json.dumps(
            {"word": word, "sound_description": "a sound", "image_description": "a picture"})}}]}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    d = json.loads(asyncio.run(server.api_suggest_word(Req({"letter": letter}))).body)
    return d, asked


def test_an_umlaut_letter_retries_a_plain_vowel_word_with_a_correction(monkeypatch):
    d, asked = suggest(monkeypatch, "Ü", ["Uhr", "Überraschung"])
    assert d["word"] == "Überraschung" and len(asked) == 2
    first, second = asked[0]["messages"][1]["content"], asked[1]["messages"][1]["content"]
    assert "eigener Buchstabe" in first and "mit U beginnen, sind FALSCH" in first
    assert "NICHT „Uhr“: es beginnt mit U, nicht mit Ü." in second
    assert "Umlaut Ü" in asked[0]["messages"][0]["content"]


def test_three_wrong_letters_give_an_honest_error_not_a_word(monkeypatch):
    d, asked = suggest(monkeypatch, "Ö", ["Ofen", "Ohr", "Obst"])
    assert "word" not in d and "kein passendes Wort mit Ö" in d["error"] and len(asked) == 3


def test_sz_needs_a_word_with_sz(monkeypatch):
    d, asked = suggest(monkeypatch, "ß", ["Fuss", "Fuß"])
    assert d["word"] == "Fuß" and "kein ß" in asked[1]["messages"][1]["content"]


def test_an_excluded_word_is_corrected_too(monkeypatch):
    monkeypatch.setattr(server, "settings", {"sets": {"s": {"letters": {"B": {"entries": [{"word": "Ball"}]}}}}, "ai_prompts": {}})
    import urllib.request
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-not-a-key")
    monkeypatch.setattr(server, "_ai_log_entry", lambda *a, **k: None)
    monkeypatch.setattr(server, "_to_english", lambda t, w="sound": t)
    asked, queue = [], ["Ball", "Bus"]

    def fake(req, timeout=None):
        asked.append(json.loads(req.data))
        return Resp(json.dumps({"choices": [{"message": {"content": json.dumps({"word": queue.pop(0)})}}]}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    d = json.loads(asyncio.run(server.api_suggest_word(Req({"letter": "B"}))).body)
    assert d["word"] == "Bus" and "NICHT „Ball“: das ist verboten" in asked[1]["messages"][1]["content"]


def test_the_returned_word_has_no_article_and_is_nfc(monkeypatch):
    d, asked = suggest(monkeypatch, "Ö", ["die O\u0308lkanne"])
    assert d["word"] == "Ölkanne" and len(d["word"]) == 7 and len(asked) == 1


def test_an_excluded_word_with_an_article_is_still_excluded(monkeypatch):
    monkeypatch.setattr(server, "settings", {"sets": {"s": {"letters": {"Ä": {"entries": [{"word": "Äpfel"}]}}}}, "ai_prompts": {}})
    import urllib.request
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-not-a-key")
    monkeypatch.setattr(server, "_ai_log_entry", lambda *a, **k: None)
    monkeypatch.setattr(server, "_to_english", lambda t, w="sound": t)
    queue = ["die Äpfel", "der Ärmel"]
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: Resp(json.dumps({"choices": [{"message": {"content": json.dumps({"word": queue.pop(0)})}}]}).encode()))
    d = json.loads(asyncio.run(server.api_suggest_word(Req({"letter": "Ä"}))).body)
    assert d["word"] == "Ärmel"


@pytest.mark.parametrize("stored,answer", [("die Ölkanne", "Ölkanne"), ("O\u0308lkanne", "Ölkanne"), ("Ölkanne", "die O\u0308lkanne")])
def test_stored_exclusions_are_compared_normalized(monkeypatch, stored, answer):
    import urllib.request
    monkeypatch.setattr(server, "settings", {"sets": {}, "blacklist": {"Ö": [stored]}, "ai_prompts": {}})
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-not-a-key")
    monkeypatch.setattr(server, "_ai_log_entry", lambda *a, **k: None)
    monkeypatch.setattr(server, "_to_english", lambda t, w="sound": t)
    queue = [answer, "Öl"]
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: Resp(json.dumps({"choices": [{"message": {"content": json.dumps({"word": queue.pop(0)})}}]}).encode()))
    d = json.loads(asyncio.run(server.api_suggest_word(Req({"letter": "Ö"}))).body)
    assert d["word"] == "Öl" and queue == []
