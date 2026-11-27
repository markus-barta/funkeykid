"""The entry editor's server side (FUNK-72): entry ids, putting a finished file on an entry,
and the parent-facing reason for a failed AI call.

server.py needs aiohttp; these tests run where aiohttp is present (the lab venv).
"""
import asyncio
import io
import json
import os
import sys
import types
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

pytest.importorskip("aiohttp")
import server  # noqa: E402


class Req:
    def __init__(self, body=None, raw=None, **match):
        self._body, self._raw, self.match_info = body, raw, match

    async def json(self):
        if self._raw is not None:
            return json.loads(self._raw)
        return self._body


def call(handler, req):
    resp = asyncio.run(handler(req))
    return resp.status, json.loads(resp.body)


@pytest.fixture
def st(monkeypatch, tmp_path):
    """A set 's' with letter B (two entries) and a library of files on disk."""
    for d in ("sounds", "images"):
        (tmp_path / d).mkdir()
    for f in ("b_ball.mp3", "b_ball_de.mp3", "b_biene_de.mp3"):
        (tmp_path / "sounds" / f).write_bytes(b"x")
    (tmp_path / "images" / "b_ball_002.png").write_bytes(b"x")
    data = {"active_set": "s", "sets": {"s": {"letters": {"B": {"entries": [
        {"id": "ballid01", "word": "Ball", "image": "b_ball.png", "enabled": True,
         "tracks": {"FX": {"file": "b_ball.mp3", "enabled": True, "prompt": "", "volume": 100}}},
        {"id": "bieneid1", "word": "Biene", "image": "", "enabled": True, "tracks": {}},
    ]}}}}}
    monkeypatch.setattr(server, "settings", data)
    monkeypatch.setattr(server, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(server, "SOUNDS_DIR", str(tmp_path / "sounds"))
    monkeypatch.setattr(server, "IMAGES_DIR", str(tmp_path / "images"))
    sent = []
    monkeypatch.setattr(server, "sse_broadcast", lambda ev, d: sent.append((ev, d)))
    return types.SimpleNamespace(settings=data, sent=sent,
                                 disk=lambda: json.loads((tmp_path / "settings.json").read_text()))


def entries(st, letter="B"):
    return st.settings["sets"]["s"]["letters"][letter]["entries"]


# ── ids ──────────────────────────────────────────────────────────────────────

def test_ids_are_given_kept_and_made_unique():
    es = [{"word": "a"}, {"id": "keepme01", "word": "b"}, {"id": "keepme01", "word": "c"},
          {"id": "no", "word": "d"}, {"id": 5, "word": "e"}, "junk"]
    assert server._ensure_entry_ids(es) is True
    ids = [e["id"] for e in es[:5]]
    assert ids[1] == "keepme01" and len(set(ids)) == 5
    assert all(server._ENTRY_ID.fullmatch(i) for i in ids)
    assert server._ensure_entry_ids(es) is False                  # stable once given
    assert server._ensure_entry_ids(None) is False


def test_a_new_id_never_takes_one_a_later_entry_has():
    es = [{"word": "a"}, {"id": "abcdef12", "word": "b"}]
    server._ensure_entry_ids(es)
    assert es[0]["id"] != "abcdef12" and es[1]["id"] == "abcdef12"


def test_startup_gives_ids_and_saves(st, tmp_path):
    entries(st)[1].pop("id")
    (tmp_path / "settings.json").write_text(json.dumps(st.settings))
    server.load_settings()
    es = server.settings["sets"]["s"]["letters"]["B"]["entries"]
    assert es[0]["id"] == "ballid01" and server._ENTRY_ID.fullmatch(es[1]["id"])
    assert json.loads((tmp_path / "settings.json").read_text())["sets"]["s"]["letters"]["B"]["entries"][1]["id"] == es[1]["id"]


def test_get_set_and_letter_give_missing_ids(st):
    entries(st)[1].pop("id")
    status, body = call(server.api_get_set, Req(set_id="s"))
    eid = body["letters"]["B"]["entries"][1]["id"]
    assert status == 200 and server._ENTRY_ID.fullmatch(eid) and entries(st)[1]["id"] == eid
    assert st.disk()["sets"]["s"]["letters"]["B"]["entries"][1]["id"] == eid
    status, body = call(server.api_get_letter, Req(set_id="s", letter="B"))
    assert [e["id"] for e in body["entries"]] == ["ballid01", eid]


def test_put_letter_keeps_the_editors_ids_and_replaces_bad_ones(st):
    body = {"entries": [{"id": "clientid1", "word": "Bus", "image": "", "enabled": True, "tracks": {}},
                        {"id": "clientid1", "word": "Boot", "image": "", "enabled": True, "tracks": {}},
                        {"word": "Bär", "image": "", "enabled": True, "tracks": {}}]}
    status, out = call(server.api_put_letter, Req(body, set_id="s", letter="b"))
    assert status == 200 and out["ids"][0] == "clientid1" and len(set(out["ids"])) == 3
    assert [e["id"] for e in entries(st)] == out["ids"]
    assert ("letter", {"set_id": "s", "letter": "B"}) in st.sent


@pytest.mark.parametrize("req", [Req({"entries": "x"}), Req({"entries": [1]}), Req([]), Req(raw="{bad")])
def test_put_letter_rejects_bad_bodies(st, req):
    req.match_info = {"set_id": "s", "letter": "B"}
    status, out = call(server.api_put_letter, req)
    assert status == 400 and out["error"] and len(entries(st)) == 2


# ── a finished file onto its entry ───────────────────────────────────────────

def media(st, body, entry_id="bieneid1", letter="B", set_id="s"):
    return call(server.api_entry_media, Req(body, set_id=set_id, letter=letter, entry_id=entry_id))


def test_a_finished_voice_goes_on_its_empty_track(st):
    status, out = media(st, {"kind": "DE", "file": "b_biene_de.mp3", "if_empty": True})
    assert status == 200 and out == {"ok": True, "applied": True}
    t = entries(st)[1]["tracks"]["DE"]
    assert t["file"] == "b_biene_de.mp3" and t["enabled"] is True and t["volume"] == 100
    assert st.disk()["sets"]["s"]["letters"]["B"]["entries"][1]["tracks"]["DE"]["file"] == "b_biene_de.mp3"
    assert st.sent[-1] == ("letter", {"set_id": "s", "letter": "B", "entry_id": "bieneid1"})


def test_if_empty_never_overwrites_what_the_parent_chose(st):
    status, out = media(st, {"kind": "FX", "file": "b_ball_de.mp3", "if_empty": True}, entry_id="ballid01")
    assert status == 200 and out == {"ok": True, "applied": False, "current": "b_ball.mp3"}
    assert entries(st)[0]["tracks"]["FX"]["file"] == "b_ball.mp3" and st.sent == []


def test_taking_a_new_version_replaces_and_a_picture_works_too(st):
    assert media(st, {"kind": "FX", "file": "b_ball_de.mp3"}, entry_id="ballid01")[1]["applied"] is True
    assert entries(st)[0]["tracks"]["FX"]["file"] == "b_ball_de.mp3"
    assert media(st, {"kind": "IMG", "file": "b_ball_002.png"}, entry_id="ballid01")[1]["applied"] is True
    assert entries(st)[0]["image"] == "b_ball_002.png"


def test_found_by_id_not_by_word_or_position(st):
    entries(st).reverse()
    entries(st)[1]["word"] = "Biene"                     # two entries with one word
    media(st, {"kind": "DE", "file": "b_biene_de.mp3", "if_empty": True})
    assert [e["tracks"].get("DE", {}).get("file", "") for e in entries(st)] == ["b_biene_de.mp3", ""]


@pytest.mark.parametrize("body,code", [
    ({"kind": "XX", "file": "b_ball.mp3"}, 400),
    ({"kind": "DE", "file": "../settings.json"}, 400),
    ({"kind": "DE", "file": ".hidden"}, 400),
    ({"kind": "DE", "file": 5}, 400),
    ({"kind": "DE", "file": "missing.mp3"}, 404),
    ({"kind": "IMG", "file": "b_ball.mp3"}, 404),        # a picture comes from the images folder
    ([], 400),
])
def test_bad_media_requests(st, body, code):
    before = json.dumps(entries(st))
    status, out = media(st, body)
    assert status == code and out["error"] and json.dumps(entries(st)) == before


def test_unknown_entry_letter_or_set(st):
    body = {"kind": "DE", "file": "b_biene_de.mp3"}
    assert media(st, body, entry_id="nope0000")[0] == 404
    assert media(st, body, letter="Q")[0] == 404
    assert media(st, body, set_id="other")[0] == 404


# ── the reason for a failed AI call ──────────────────────────────────────────

def http_error(code, body=b""):
    return urllib.error.HTTPError("https://api.example", code, "x", {}, io.BytesIO(body))


def test_a_402_from_elevenlabs_says_paid_plan_not_http():
    body = json.dumps({"detail": {"status": "payment_required", "message": "Upgrade to use library voices"}}).encode()
    msg, code, detail = server._provider_error("elevenlabs", http_error(402, body))
    assert code == 402 and "bezahltes Abo" in msg and "HTTP" not in msg
    assert detail == "Upgrade to use library voices"


@pytest.mark.parametrize("provider,code,body,needle", [
    ("elevenlabs", 401, {"detail": {"status": "quota_exceeded", "message": "quota exceeded"}}, "Kontingent"),
    ("elevenlabs", 401, {"detail": {"status": "invalid_api_key"}}, "Schlüssel abgelehnt"),
    ("openrouter", 402, {"error": {"message": "Insufficient credits"}}, "Guthaben"),
    ("openrouter", 429, {"error": "rate limited"}, "zu viele Anfragen"),
    ("elevenlabs", 503, None, "Fehler 503"),
    ("openrouter", 400, {"error": {"message": "bad model"}}, "abgelehnt: bad model"),
    ("elevenlabs", 403, None, "Berechtigungen"),
    ("elevenlabs", 418, None, "Fehler 418"),
])
def test_status_codes_get_a_reason(provider, code, body, needle):
    raw = json.dumps(body).encode() if body is not None else b""
    msg, got, _ = server._provider_error(provider, http_error(code, raw))
    assert got == code and needle in msg


def test_an_html_error_page_is_not_echoed():
    msg, code, detail = server._provider_error("openrouter", http_error(502, b"<html><body>" + b"x" * 9000))
    assert code == 502 and detail == "" and "<html" not in msg


def test_long_provider_text_is_cut():
    _, _, detail = server._provider_error("elevenlabs", http_error(400, json.dumps({"detail": "y " * 900}).encode()))
    assert len(detail) <= 300


@pytest.mark.parametrize("exc,needle", [
    (urllib.error.URLError("no route"), "Keine Verbindung"),
    (TimeoutError("timed out"), "Keine Verbindung"),
    (ValueError("Keine Bilddaten erhalten"), "Keine Bilddaten erhalten"),
    (KeyError("choices"), "unerwarteter Fehler (KeyError)"),
])
def test_other_failures(exc, needle):
    msg, code, _ = server._provider_error("openrouter", exc)
    assert code is None and needle in msg


def test_a_failed_job_carries_the_reason(monkeypatch):
    monkeypatch.setattr(server, "gen_jobs", {"j1": {"id": "j1", "status": "generating"}})
    logged = []
    monkeypatch.setattr(server, "_ai_log_entry", lambda *a: logged.append(a))
    server._job_failed("j1", "elevenlabs", http_error(402, b'{"detail":{"message":"paid only"}}'), "tts", "m", "Kran")
    j = server.gen_jobs["j1"]
    assert j["status"] == "error" and "bezahltes Abo" in j["error"] and j["error_code"] == 402
    assert j["error_detail"] == "paid only" and j["finished"] > 0
    assert logged[0][3] == "402 paid only" and logged[0][4] == "error"


def test_the_job_list_forgets_old_finished_jobs(monkeypatch):
    monkeypatch.setattr(server, "gen_jobs", {})
    monkeypatch.setattr(server, "GEN_JOBS_KEEP", 3)
    server._register_job({"id": "run", "status": "generating", "started": 0})
    for i in range(6):
        server._register_job({"id": f"d{i}", "status": "done", "started": i, "finished": i})
    assert sorted(server.gen_jobs) == ["d3", "d4", "d5", "run"]


# ── thumbnails for the page ──────────────────────────────────────────────────

@pytest.fixture
def pics(monkeypatch, tmp_path):
    from PIL import Image
    d = tmp_path / "data"
    for sub in ("images", "ai-generated/images-original"):
        (d / sub).mkdir(parents=True)
    Image.new("RGB", (512, 512), (200, 40, 40)).save(d / "ai-generated/images-original/b_ball.png")
    Image.new("RGB", (64, 64), (40, 200, 40)).save(d / "images/b_ball.png")
    Image.new("RGB", (64, 64), (40, 40, 200)).save(d / "images/k_kuh.png")
    (d / "images/broken.png").write_bytes(b"not a png")
    monkeypatch.setattr(server, "AI_DIR", str(d / "ai-generated"))
    monkeypatch.setattr(server, "AI_IMAGES_ORIG_DIR", str(d / "ai-generated/images-original"))
    monkeypatch.setattr(server, "IMAGES_DIR", str(d / "images"))
    return d


def thumb(size, name):
    resp = asyncio.run(server.serve_thumb(Req(size=str(size), filename=name)))
    return resp


def test_a_thumb_comes_from_the_original_scaled_down(pics):
    from PIL import Image
    resp = thumb(160, "b_ball.png")
    path = pics / "ai-generated/thumbs/160/b_ball.png.jpg"
    assert resp.status == 200 and resp.headers["Cache-Control"].startswith("public")
    with Image.open(path) as im:
        assert im.size == (160, 160) and im.format == "JPEG" and im.getpixel((80, 80))[0] > 150


def test_a_pixoo_picture_is_scaled_up_sharp(pics):
    from PIL import Image
    thumb(480, "k_kuh.png")
    with Image.open(pics / "ai-generated/thumbs/480/k_kuh.png.png") as im:
        assert im.size == (448, 448) and im.format == "PNG"           # 7 × 64: whole pixels only


def test_a_thumb_is_rebuilt_when_the_picture_is_newer(pics):
    thumb(160, "k_kuh.png")
    t = pics / "ai-generated/thumbs/160/k_kuh.png.png"
    os.utime(t, (1, 1))
    thumb(160, "k_kuh.png")
    assert os.path.getmtime(t) > 1


@pytest.mark.parametrize("size,name", [(100, "b_ball.png"), ("x", "b_ball.png"), (160, "../settings.json"),
                                       (160, ".hidden.png"), (160, "missing.png"), (160, "a/b.png")])
def test_bad_thumb_requests(pics, size, name):
    assert thumb(size, name).status == 404


def test_a_picture_pil_cannot_read_is_served_as_it_is(pics):
    resp = thumb(160, "broken.png")
    assert resp.status == 200 and not (pics / "ai-generated/thumbs/160/broken.png.png").exists()
