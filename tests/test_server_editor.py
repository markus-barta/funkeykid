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
    def __init__(self, body=None, raw=None, headers=None, **match):
        self._body, self._raw, self.match_info = body, raw, match
        self.headers = headers or {}

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
    assert ("letter", {"set_id": "s", "letter": "B", "origin": ""}) in st.sent


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
    assert st.sent[-1] == ("letter", {"set_id": "s", "letter": "B", "entry_id": "bieneid1", "origin": ""})


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
    assert resp.status == 200 and resp.headers["Cache-Control"] == "no-cache"
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



# ── edits by entry id (FUNK-72 round 1: no whole-letter overwrites) ──────────

def patch(st, body, entry_id="bieneid1", letter="B", headers=None):
    return call(server.api_patch_entry, Req(body, headers=headers, set_id="s", letter=letter, entry_id=entry_id))


def test_a_patch_changes_only_its_fields(st):
    status, out = patch(st, {"word": "Bienchen", "tracks": {"DE": {"prompt": "Biene!", "volume": 120}}},
                        headers={"X-FK-Client": "page-a"})
    e = entries(st)[1]
    assert status == 200 and e["word"] == "Bienchen" and e["tracks"]["DE"]["prompt"] == "Biene!"
    assert e["tracks"]["DE"]["volume"] == 120 and e["tracks"]["DE"]["file"] == "" and out["entry"]["id"] == "bieneid1"
    assert st.sent[-1] == ("letter", {"set_id": "s", "letter": "B", "entry_id": "bieneid1", "origin": "page-a"})
    patch(st, {"enabled": False, "image": "b_ball_002.png", "_imageDesc": "a bee", "tracks": {"FX": {"file": "b_ball.mp3"}}})
    e = entries(st)[1]
    assert (e["enabled"], e["image"], e["_imageDesc"], e["tracks"]["FX"]["file"]) == (False, "b_ball_002.png", "a bee", "b_ball.mp3")
    assert e["word"] == "Bienchen" and e["tracks"]["DE"]["prompt"] == "Biene!"          # untouched fields stay


def test_two_pages_editing_different_fields_both_win(st):
    patch(st, {"word": "Biene"}, headers={"X-FK-Client": "a"})
    patch(st, {"tracks": {"DE": {"file": "b_biene_de.mp3"}}}, headers={"X-FK-Client": "b"})
    e = entries(st)[1]
    assert e["word"] == "Biene" and e["tracks"]["DE"]["file"] == "b_biene_de.mp3"


@pytest.mark.parametrize("body", [{"nope": 1}, {"word": 5}, {"word": "x" * 61}, {"enabled": "yes"}, {"image": "missing.png"},
                                  {"image": "../settings.json"}, {"tracks": {"XX": {}}}, {"tracks": {"DE": {"volume": 201}}},
                                  {"tracks": {"DE": {"volume": True}}}, {"tracks": {"DE": {"file": "missing.mp3"}}},
                                  {"tracks": {"DE": {"bad": 1}}}, {"tracks": "x"}, [], {"_imageDesc": 3}])
def test_bad_patches_change_nothing(st, body):
    before = json.dumps(entries(st))
    status, out = patch(st, body)
    assert status == 400 and out["error"] and json.dumps(entries(st)) == before and st.sent == []


def test_patching_an_unknown_entry_is_404(st):
    assert patch(st, {"word": "x"}, entry_id="nope0000")[0] == 404


def test_add_keeps_the_editors_id_and_position(st):
    status, out = call(server.api_add_entry, Req({"id": "newbus01", "index": 1, "word": "Bus", "tracks": {"FX": {"prompt": "bus"}}},
                                                 set_id="s", letter="B"))
    assert status == 200 and out["id"] == "newbus01" and out["index"] == 1
    assert [e["word"] for e in entries(st)] == ["Ball", "Bus", "Biene"] and entries(st)[1]["tracks"]["FX"]["prompt"] == "bus"


def test_repeating_an_add_never_makes_two_entries_and_a_new_letter_is_made(st):
    status, out = call(server.api_add_entry, Req({"id": "ballid01", "word": "Ball 2"}, set_id="s", letter="B"))
    assert status == 200 and out["id"] == "ballid01" and out["existed"] is True and len(entries(st)) == 2
    assert entries(st)[0]["word"] == "Ball"
    status, out = call(server.api_add_entry, Req({"word": "Qualle"}, set_id="s", letter="Q"))
    assert status == 200 and entries(st, "Q")[0]["word"] == "Qualle" and server._ENTRY_ID.fullmatch(out["id"])


@pytest.mark.parametrize("body,code", [({"index": "1", "word": "x"}, 400), ({"word": 3}, 400), ([], 400)])
def test_bad_adds(st, body, code):
    assert call(server.api_add_entry, Req(body, set_id="s", letter="B"))[0] == code and len(entries(st)) == 2


def test_add_to_a_missing_set_is_404(st):
    assert call(server.api_add_entry, Req({"word": "x"}, set_id="nope", letter="B"))[0] == 404


def test_delete_by_id_returns_what_undo_needs(st):
    status, out = call(server.api_delete_entry_by_id, Req(set_id="s", letter="B", entry_id="ballid01"))
    assert status == 200 and out["index"] == 0 and out["entry"]["word"] == "Ball" and [e["id"] for e in entries(st)] == ["bieneid1"]
    assert call(server.api_delete_entry_by_id, Req(set_id="s", letter="B", entry_id="ballid01"))[0] == 404


def test_archive_entry_moves_only_unshared_files_then_removes_the_entry(st, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DATA_DIR", str(tmp_path))
    st.settings["sets"]["s"]["letters"]["K"] = {"entries": [{"id": "kuhid001", "word": "Kuh", "image": "b_ball_002.png", "tracks": {}}]}
    entries(st)[0]["image"] = "b_ball_002.png"                                        # shared with Kuh
    status, out = call(server.api_archive_entry, Req(set_id="s", letter="B", entry_id="ballid01"))
    assert status == 200 and out["archived"] == ["b_ball.mp3"] and out["kept"] == ["b_ball_002.png"]
    assert (tmp_path / "archive/sounds/b_ball.mp3").exists() and (tmp_path / "images/b_ball_002.png").exists()
    assert [e["id"] for e in entries(st)] == ["bieneid1"]


def test_archive_entry_is_all_or_nothing(st, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DATA_DIR", str(tmp_path))
    entries(st)[0]["image"] = "b_ball_002.png"
    real = server._archive_one

    def second_fails(file_type, name):
        if file_type == "sound":
            raise server._ArchiveError(507, "disk full")
        return real(file_type, name)
    monkeypatch.setattr(server, "_archive_one", second_fails)
    status, out = call(server.api_archive_entry, Req(set_id="s", letter="B", entry_id="ballid01"))
    assert status == 507 and "disk full" in out["error"]
    assert (tmp_path / "images/b_ball_002.png").exists() and not (tmp_path / "archive/images/b_ball_002.png").exists()
    assert [e["id"] for e in entries(st)] == ["ballid01", "bieneid1"]


def test_the_letter_brings_its_sets_track_order(st):
    st.settings["sets"]["s"]["track_order"] = [{"kind": "EN", "enabled": False}, {"kind": "FX", "enabled": True}, {"kind": "DE", "enabled": True}]
    status, out = call(server.api_get_letter, Req(set_id="s", letter="B"))
    assert [t["kind"] for t in out["track_order"] if t["enabled"]] == ["FX", "DE"]


def test_media_if_empty_must_be_a_boolean(st):
    status, out = media(st, {"kind": "FX", "file": "b_ball_de.mp3", "if_empty": "true"}, entry_id="ballid01")
    assert status == 400 and entries(st)[0]["tracks"]["FX"]["file"] == "b_ball.mp3"


# ── a job's file goes on its entry on the server, only if the slot is still empty ──

@pytest.fixture
def job(st, monkeypatch):
    monkeypatch.setattr(server, "gen_jobs", {})
    monkeypatch.setattr(server, "_loop", None)

    def make(kind="DE", entry_id="bieneid1", filename="b_biene_de.mp3", status="done"):
        target = server._job_target({"set_id": "s", "letter": "B", "entry_id": entry_id}, kind)
        server.gen_jobs["j1"] = {"id": "j1", "kind": kind, "filename": filename, "status": status, "target": target}
        return server.gen_jobs["j1"]
    return make


def test_a_done_job_fills_its_empty_slot(st, job):
    j = job()
    server._job_finished("j1")
    assert j["applied"] is True and entries(st)[1]["tracks"]["DE"]["file"] == "b_biene_de.mp3"
    assert ("letter", {"set_id": "s", "letter": "B", "entry_id": "bieneid1", "origin": "job"}) in st.sent
    assert st.sent[-1] == ("gen_update", j) and st.disk()["sets"]["s"]["letters"]["B"]["entries"][1]["tracks"]["DE"]["file"] == "b_biene_de.mp3"


def test_a_done_job_never_overwrites_a_filled_slot(st, job):
    j = job(kind="FX", entry_id="ballid01", filename="b_ball_de.mp3")
    server._job_finished("j1")
    assert j["applied"] is False and entries(st)[0]["tracks"]["FX"]["file"] == "b_ball.mp3"


def test_a_detached_job_only_lands_in_the_library(st, job):
    j = job(status="generating")
    status, out = call(server.api_job_detach, Req(job_id="j1"))
    assert status == 200 and out["detached"] is True and j["target"] is None
    j["status"] = "done"
    server._job_finished("j1")
    assert j.get("applied") is False and entries(st)[1]["tracks"].get("DE", {}).get("file", "") == ""


def test_detaching_a_finished_job_marks_it_reviewed(st, job):
    j = job()
    status, out = call(server.api_job_detach, Req(job_id="j1"))
    assert out == {"ok": True, "detached": False, "status": "done"} and j["target"] is None
    assert call(server.api_job_detach, Req(job_id="nope"))[0] == 404


def test_a_job_for_a_removed_entry_changes_nothing(st, job):
    j = job()
    entries(st).pop(1)
    server._job_finished("j1")
    assert j["applied"] is False


def test_a_picture_job_fills_an_empty_picture(st, job):
    j = job(kind="IMG", filename="b_ball_002.png")
    server._job_finished("j1")
    assert j["applied"] is True and entries(st)[1]["image"] == "b_ball_002.png"


def test_a_failed_job_is_only_announced(st, job):
    j = job(status="error")
    server._job_finished("j1")
    assert "applied" not in j and st.sent == [("gen_update", j)]


@pytest.mark.parametrize("raw,code", [("x", 400), ({"set_id": "s", "letter": "B"}, 400),
                                      ({"set_id": "s", "letter": "B", "entry_id": "nope0000"}, 404),
                                      ({"set_id": "zz", "letter": "B", "entry_id": "bieneid1"}, 404)])
def test_a_bad_target_is_refused_before_the_job_starts(st, raw, code):
    with pytest.raises(server._TargetError) as e:
        server._job_target(raw, "DE")
    assert e.value.status == code


# ── round 2: containment for thumbs, nothing half-saved, choices from any page detach jobs ──

def test_a_thumb_never_reads_through_a_symlink(pics, tmp_path):
    (tmp_path / "secret.txt").write_text("SECRET-CANARY")
    os.symlink(tmp_path / "secret.txt", pics / "images/leak.png")
    os.symlink(tmp_path / "secret.txt", pics / "ai-generated/images-original/leak2.png")
    assert thumb(160, "leak.png").status == 404 and thumb(160, "leak2.png").status == 404


def failing_save(monkeypatch):
    def boom():
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(server, "save_settings", boom)


def test_a_failed_save_undoes_a_patch_and_a_retry_saves_it(st, monkeypatch):
    real = server.save_settings
    failing_save(monkeypatch)
    status, out = patch(st, {"word": "Bienchen"})
    assert status == 500 and entries(st)[1]["word"] == "Biene" and st.sent == []
    monkeypatch.setattr(server, "save_settings", real)
    assert patch(st, {"word": "Bienchen"})[0] == 200 and st.disk()["sets"]["s"]["letters"]["B"]["entries"][1]["word"] == "Bienchen"


def test_a_failed_save_undoes_add_and_delete(st, monkeypatch):
    failing_save(monkeypatch)
    assert call(server.api_add_entry, Req({"id": "newbus01", "word": "Bus"}, set_id="s", letter="B"))[0] == 500
    assert [e["id"] for e in entries(st)] == ["ballid01", "bieneid1"]
    assert call(server.api_delete_entry_by_id, Req(set_id="s", letter="B", entry_id="ballid01"))[0] == 500
    assert [e["id"] for e in entries(st)] == ["ballid01", "bieneid1"]


def test_a_failed_save_after_archiving_brings_the_files_back(st, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DATA_DIR", str(tmp_path))
    failing_save(monkeypatch)
    status, out = call(server.api_archive_entry, Req(set_id="s", letter="B", entry_id="ballid01"))
    assert status == 500 and [e["id"] for e in entries(st)] == ["ballid01", "bieneid1"]
    assert (tmp_path / "sounds/b_ball.mp3").exists() and not (tmp_path / "archive/sounds/b_ball.mp3").exists()


def test_choosing_a_file_on_any_page_detaches_running_jobs_for_that_slot(st, monkeypatch):
    monkeypatch.setattr(server, "gen_jobs", {})
    t = server._job_target({"set_id": "s", "letter": "B", "entry_id": "ballid01"}, "IMG")
    server.gen_jobs["img"] = {"id": "img", "status": "generating", "target": t}
    t2 = server._job_target({"set_id": "s", "letter": "B", "entry_id": "ballid01"}, "DE")
    server.gen_jobs["de"] = {"id": "de", "status": "generating", "target": t2}
    patch(st, {"word": "Ball!"}, entry_id="ballid01")                       # not a choice of a file
    assert server.gen_jobs["img"]["target"] and server.gen_jobs["de"]["target"]
    patch(st, {"image": ""}, entry_id="ballid01")                           # removing the picture is one
    assert server.gen_jobs["img"]["target"] is None and server.gen_jobs["de"]["target"]



# ── round 3: a finishing job stays detachable; a replaced library picture wins ──

def test_a_job_that_is_finishing_is_still_detached_by_a_removal(st, monkeypatch):
    monkeypatch.setattr(server, "gen_jobs", {})
    monkeypatch.setattr(server, "_loop", None)
    entries(st)[0]["image"] = "b_ball.png"
    t = server._job_target({"set_id": "s", "letter": "B", "entry_id": "ballid01"}, "IMG")
    # the worker has its result, the event loop has not finalized it yet
    server.gen_jobs["j"] = {"id": "j", "status": "generating", "_done": True, "filename": "b_ball_002.png", "target": t}
    patch(st, {"image": ""}, entry_id="ballid01")                 # the parent removes the picture now
    server._job_finished("j")
    j = server.gen_jobs["j"]
    assert j["status"] == "done" and j["applied"] is False and "_done" not in j and entries(st)[0]["image"] == ""


def test_a_library_picture_replaced_after_its_original_is_shown(pics):
    from PIL import Image
    orig = pics / "ai-generated/images-original/b_ball.png"
    lib = pics / "images/b_ball.png"
    os.utime(orig, (1_000_000, 1_000_000))
    os.utime(lib, (1_000_500, 1_000_500))                         # an import wrote it 500 s later
    thumb(160, "b_ball.png")
    with Image.open(pics / "ai-generated/thumbs/160/b_ball.png.png") as im:
        assert im.size == (128, 128)                              # the 64 × 64 library picture, scaled sharp


# ── round 5: undo restores an entry with a missing file; detaching tells every page ──

def test_undo_restores_an_entry_whose_file_is_missing(st):
    body = {"id": "gone0001", "word": "Bus", "image": "missing.png", "tracks": {"FX": {"file": "missing.mp3"}}}
    assert call(server.api_add_entry, Req(dict(body), set_id="s", letter="B"))[0] == 400          # a normal add checks
    status, out = call(server.api_add_entry, Req(dict(body, restore=True), set_id="s", letter="B"))
    assert status == 200 and entries(st)[2]["image"] == "missing.png" and entries(st)[2]["tracks"]["FX"]["file"] == "missing.mp3"
    status, out = patch(st, {"restore": True, "image": "missing2.png"}, entry_id="gone0001")
    assert status == 200 and entries(st)[2]["image"] == "missing2.png"
    assert patch(st, {"restore": True, "image": "../x.png"}, entry_id="gone0001")[0] == 400       # names still checked


def test_detaching_tells_the_other_pages(st, job):
    j = job()
    st.sent.clear()
    call(server.api_job_detach, Req(job_id="j1"))
    assert st.sent == [("gen_update", j)] and j["target"] is None
    st.sent.clear()
    call(server.api_job_detach, Req(job_id="j1"))                    # nothing new: no event
    assert st.sent == []


def test_undo_restores_longer_text_an_import_allowed(st):
    long_prompt, long_desc = "p" * 301, "d" * 501
    body = {"id": "long0001", "word": "Bus", "_imageDesc": long_desc, "tracks": {"FX": {"prompt": long_prompt}}}
    assert call(server.api_add_entry, Req(dict(body), set_id="s", letter="B"))[0] == 400
    status, _ = call(server.api_add_entry, Req(dict(body, restore=True), set_id="s", letter="B"))
    assert status == 200 and entries(st)[2]["tracks"]["FX"]["prompt"] == long_prompt and entries(st)[2]["_imageDesc"] == long_desc
    too_long = "x" * (server.setbundle.MAX_TEXT + 1)
    assert patch(st, {"restore": True, "_imageDesc": too_long}, entry_id="long0001")[0] == 400


def test_an_uploaded_photo_is_scaled_down_not_kept_full_size(pics):
    from PIL import Image
    Image.new("RGB", (2000, 1500), (90, 120, 200)).save(pics / "images/photo.jpg")
    thumb(160, "photo.jpg")
    with Image.open(pics / "ai-generated/thumbs/160/photo.jpg.png") as im:
        assert max(im.size) == 160 and im.size == (160, 120)


def test_the_job_list_names_the_server_process(monkeypatch):
    monkeypatch.setattr(server, "gen_jobs", {"a": {"id": "a", "status": "generating"}})
    resp = asyncio.run(server.api_get_jobs(Req()))
    assert resp.headers["X-FK-Boot"] == server.BOOT_ID and json.loads(resp.body) == [{"id": "a", "status": "generating"}]
