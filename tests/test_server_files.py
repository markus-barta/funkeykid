"""Dateien (FUNK-78): what uses each file, and the archive never takes a file that is still used.

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
    def __init__(self, body=None):
        self._body, self.match_info, self.headers = body, {}, {}

    async def json(self):
        return self._body


def call(handler, body=None):
    resp = asyncio.run(handler(Req(body)))
    return resp.status, json.loads(resp.body)


@pytest.fixture
def st(monkeypatch, tmp_path):
    for d in ("sounds", "images"):
        (tmp_path / d).mkdir()
    for f in ("a_affe_de.mp3", "a_affe.mp3", "old.mp3", "3_drei.mp3", "key.mp3", "free.mp3"):
        (tmp_path / "sounds" / f).write_bytes(b"x")
    for f in ("a_affe.png", "3_bg.png", "key.png", "free.png"):
        (tmp_path / "images" / f).write_bytes(b"x")
    data = {"active_set": "s", "sets": {
        "s": {"name": "Komplett", "letters": {"A": {"entries": [
            {"id": "affeid01", "word": "Affe", "image": "a_affe.png",
             "tracks": {"FX": {"file": "a_affe.mp3"}, "DE": {"file": "a_affe_de.mp3"}}},
            {"id": "altid001", "word": "Alt", "sound": "old.mp3"},             # before multi-track
        ]}}, "numbers": {"3": {"word": "Drei", "sound": "3_drei.mp3", "backgrounds": [{"image": "3_bg.png", "enabled": False}]}}},
        "t": {"name": "Tiere", "letters": {"A": {"entries": [{"id": "affeid02", "word": "Affe", "image": "a_affe.png", "tracks": {}}]}}},
        "broken": "not a set",
    }, "keymaps": {"acme": {"F1": {"action": "imgsound", "file": "key.mp3", "image": "key.png", "word": "schere"},
                            "F2": {"action": "func", "func": "quiz"}}}}
    monkeypatch.setattr(server, "settings", data)
    monkeypatch.setattr(server, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(server, "SOUNDS_DIR", str(tmp_path / "sounds"))
    monkeypatch.setattr(server, "IMAGES_DIR", str(tmp_path / "images"))
    return types.SimpleNamespace(settings=data, tmp=tmp_path)


def test_usage_names_every_user_of_a_file(st):
    status, u = call(server.api_file_usage)
    assert status == 200
    s, i = u["sounds"], u["images"]
    assert s["a_affe_de.mp3"] == [{"set_id": "s", "set": "Komplett", "key": "A", "entry_id": "affeid01", "word": "Affe", "kind": "DE"}]
    assert s["a_affe.mp3"][0]["kind"] == "FX"
    assert s["old.mp3"][0]["kind"] == "FX" and s["old.mp3"][0]["word"] == "Alt"             # legacy sound
    assert s["3_drei.mp3"][0] == {"set_id": "s", "set": "Komplett", "key": "3", "entry_id": "number", "word": "Drei", "kind": "DE"}
    assert {(x["set_id"], x["kind"]) for x in i["a_affe.png"]} == {("s", "IMG"), ("t", "IMG")}
    assert i["3_bg.png"][0]["kind"] == "BG"                                                # switched off still counts
    assert s["key.mp3"] == [{"profile": "acme", "key": "F1", "kind": "KEY", "word": "schere"}] and i["key.png"][0]["kind"] == "KEY"
    assert "free.mp3" not in s and "free.png" not in i


def test_usage_is_capped_per_file(st, monkeypatch):
    monkeypatch.setattr(server, "FILE_USES_MAX", 1)
    assert len(server._file_usage()["images"]["a_affe.png"]) == 1


@pytest.mark.parametrize("ftype,name", [("sound", "a_affe_de.mp3"), ("sound", "old.mp3"), ("sound", "3_drei.mp3"),
                                        ("sound", "key.mp3"), ("image", "3_bg.png"), ("image", "a_affe.png")])
def test_a_file_in_use_is_never_archived(st, ftype, name):
    status, d = call(server.api_archive_file, {"type": ftype, "filename": name})
    assert status == 409 and "Wird noch benutzt" in d["error"] and d["uses"]
    folder = "sounds" if ftype == "sound" else "images"
    assert (st.tmp / folder / name).exists()


def test_the_refusal_says_who_uses_it(st):
    _, d = call(server.api_archive_file, {"type": "sound", "filename": "a_affe_de.mp3"})
    assert "A · Affe (Stimme)" in d["error"]
    _, d = call(server.api_archive_file, {"type": "image", "filename": "key.png"})
    assert "Taste F1" in d["error"]


def test_an_unused_file_is_archived(st):
    status, d = call(server.api_archive_file, {"type": "sound", "filename": "free.mp3"})
    assert status == 200 and d["archived"] == "free.mp3" and not (st.tmp / "sounds" / "free.mp3").exists()
    status, _ = call(server.api_archive_file, {"type": "image", "filename": "free.png"})
    assert status == 200 and not (st.tmp / "images" / "free.png").exists()
