"""File paths that come from requests stay inside their folder (FUNK-73).

These run the app's real routes (server.create_app) and send raw, still-encoded paths.
aiohttp is required here, never skipped: CI installs it for these tests.
"""
import asyncio
import errno
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiohttp  # noqa: E402,F401  (required: a missing aiohttp must fail, not skip)
import pytest  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import server  # noqa: E402

REAL_NORMALIZE = server.normalize_mp3


@pytest.fixture
def data(monkeypatch, tmp_path):
    d = tmp_path / "data"
    for sub in ("sounds", "images", "ai-generated/sounds", "ai-generated/images-original", "archive"):
        (d / sub).mkdir(parents=True)
    (tmp_path / "secret.txt").write_text("SECRET-CANARY")
    (d / "settings.json").write_text("{}")
    (d / "ai-generated/images-original/b_ball.png").write_bytes(b"png")
    (d / "sounds/b_ball.mp3").write_bytes(b"mp3")
    (d / "images/b_ball.png").write_bytes(b"img")
    monkeypatch.setattr(server, "DATA_DIR", str(d))
    monkeypatch.setattr(server, "SOUNDS_DIR", str(d / "sounds"))
    monkeypatch.setattr(server, "IMAGES_DIR", str(d / "images"))
    monkeypatch.setattr(server, "AI_DIR", str(d / "ai-generated"))
    monkeypatch.setattr(server, "AI_SOUNDS_DIR", str(d / "ai-generated/sounds"))
    monkeypatch.setattr(server, "SOUND_DIRS", (str(d / "sounds"), str(d / "ai-generated/sounds")))
    monkeypatch.setattr(server, "IMAGE_DIRS", (str(d / "images"),))
    monkeypatch.setattr(server, "settings", {"sets": {}})
    monkeypatch.setattr(server, "_issued_media_names", set())
    monkeypatch.setattr(server, "normalize_mp3", lambda src, dst: False)
    return d


def run(requests):
    """requests: (method, raw path, kwargs) against the real app; returns (status, body bytes)."""
    async def go():
        out = []
        async with TestClient(TestServer(server.create_app())) as c:
            for method, path, kw in requests:
                r = await c.request(method, path, allow_redirects=False, **kw)
                out.append((r.status, await r.read()))
        return out
    return asyncio.run(go())


# ── reading ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "/ai/..%2f..%2fsecret.txt", "/ai/%2E%2E%2F%2E%2E%2Fsecret.txt", "/ai/images-original/..%2f..%2f..%2fsecret.txt",
    "/ai/..%2fsettings.json", "/ai/%2Fetc%2Fhosts", "/ai/images-original", "/ai/a%00.png",
    "/sounds/..%2f..%2fsecret.txt", "/sounds/..%2fsettings.json", "/sounds/%2Fetc%2Fhosts", "/sounds/a%00.mp3",
    "/images/..%2f..%2fsecret.txt", "/images/%2fetc%2fhosts", "/images/..%2f",
])
def test_files_are_never_served_from_outside_their_folder(data, raw):
    ((status, body),) = run([("GET", raw, {})])
    assert status == 404 and b"CANARY" not in body and body != b"{}"


def test_a_symlink_out_of_the_folder_is_not_followed(data, tmp_path):
    os.symlink(tmp_path / "secret.txt", data / "sounds/link.mp3")
    ((status, body),) = run([("GET", "/sounds/link.mp3", {})])
    assert status == 404 and b"CANARY" not in body


def test_files_inside_are_served(data):
    got = run([("GET", "/ai/images-original/b_ball.png", {}), ("GET", "/sounds/b_ball.mp3", {}), ("GET", "/images/b_ball.png", {})])
    assert got == [(200, b"png"), (200, b"mp3"), (200, b"img")]


# ── uploading ────────────────────────────────────────────────────────────────

def upload(route, name, content=b"data"):
    # built by hand: aiohttp's own client would percent-encode the slashes an attacker sends raw
    body = (f'--XyZ\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n').encode() + content + b"\r\n--XyZ--\r\n"
    ((status, raw),) = run([("POST", route, {"data": body, "headers": {"Content-Type": "multipart/form-data; boundary=XyZ"}})])
    return status, json.loads(raw)


def leftovers(data):
    tmp = data / ".staging"
    return sorted(os.listdir(tmp)) if tmp.exists() else []


@pytest.mark.parametrize("name", ["../../escape.mp3", "/tmp/escape.mp3", "sub/../../escape.mp3"])
def test_an_upload_name_is_only_its_last_part(data, tmp_path, name):
    status, out = upload("/api/sounds/upload", name)
    assert status == 200 and out["file"] == "escape.mp3"
    assert (data / "sounds/escape.mp3").read_bytes() == b"data"
    assert (data / "ai-generated/sounds/escape.mp3").read_bytes() == b"data"          # the original, as before
    assert not (tmp_path / "escape.mp3").exists() and not (data / "escape.mp3").exists() and leftovers(data) == []


def test_a_windows_style_name_never_escapes(data, tmp_path):
    # multipart reads a backslash as an escape: the name arrives as "....escape.mp3" (hidden: refused)
    status, out = upload("/api/sounds/upload", "..\\..\\escape.mp3")
    assert status in (200, 400) and not (tmp_path / "escape.mp3").exists() and not (data / "escape.mp3").exists()
    assert status == 400 or out["file"] == "escape.mp3"


@pytest.mark.parametrize("route,name", [("/api/sounds/upload", ".hidden.mp3"), ("/api/sounds/upload", "noext"),
                                        ("/api/sounds/upload", "x.exe"), ("/api/sounds/upload", "voice.m4a"),
                                        ("/api/images/upload", "x.svg"), ("/api/images/upload", "../"),
                                        ("/api/sounds/upload", "")])
def test_bad_upload_names_are_refused(data, route, name):
    status, out = upload(route, name)
    assert status == 400 and out["error"]
    assert sorted(os.listdir(data / "sounds")) == ["b_ball.mp3"] and sorted(os.listdir(data / "images")) == ["b_ball.png"]


def test_an_upload_never_overwrites_a_library_file(data):
    status, out = upload("/api/images/upload", "b_ball.png", b"new")
    assert status == 200 and out["file"] != "b_ball.png"
    assert (data / "images/b_ball.png").read_bytes() == b"img" and (data / "images" / out["file"]).read_bytes() == b"new"


def test_an_upload_never_writes_through_a_dangling_symlink(data, tmp_path):
    os.symlink(tmp_path / "outside.mp3", data / "sounds/foo.mp3")
    status, out = upload("/api/sounds/upload", "foo.mp3", b"new")
    assert status == 200 and out["file"] != "foo.mp3"
    assert not (tmp_path / "outside.mp3").exists() and os.path.islink(data / "sounds/foo.mp3")


def test_the_normalized_file_is_what_gets_published(data, monkeypatch):
    def norm(src, dst):
        with open(dst, "wb") as f:
            f.write(b"NORM")
        return True
    monkeypatch.setattr(server, "normalize_mp3", norm)
    status, out = upload("/api/sounds/upload", "b_biene.mp3", b"raw")
    assert status == 200 and out == {"ok": True, "file": "b_biene.mp3", "normalized": True}
    assert (data / "sounds/b_biene.mp3").read_bytes() == b"NORM" and leftovers(data) == []


def test_a_storage_error_while_normalizing_publishes_the_complete_raw_file_never_the_part(data, monkeypatch):
    def norm(src, dst):
        with open(dst, "wb") as f:
            f.write(b"PART")
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(server, "normalize_mp3", norm)
    status, out = upload("/api/sounds/upload", "b_biene.mp3", b"raw")
    assert status == 200 and out["normalized"] is False
    assert (data / "sounds/b_biene.mp3").read_bytes() == b"raw" and leftovers(data) == []


def test_a_full_disk_when_publishing_is_a_507_and_leaves_nothing(data, monkeypatch):
    def full(src, dst, follow_symlinks=True):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(server.os, "link", full)
    real_open = open

    def no_space_open(path, mode="r", *a, **k):
        if "x" in mode:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_open(path, mode, *a, **k)
    monkeypatch.setattr("builtins.open", no_space_open)
    monkeypatch.setattr(server, "_receive_upload", _staged_upload)
    status, out = upload("/api/sounds/upload", "b_biene.mp3", b"raw")
    assert status == 507 and "Speicher" in out["error"]
    assert sorted(os.listdir(data / "sounds")) == ["b_ball.mp3"]


async def _staged_upload(request, kind):
    """_receive_upload without reading the request (the test's open() refuses "x" writes)."""
    tmp = server._staging("mp3")
    with open(tmp, "wb") as f:
        f.write(b"raw")
    return tmp, "b_biene.mp3", None


def test_a_too_large_upload_is_refused_and_removed(data, monkeypatch):
    monkeypatch.setitem(server.UPLOAD_MAX_BYTES, "image", 10)
    status, out = upload("/api/images/upload", "big.png", b"x" * 100)
    assert status == 413 and sorted(os.listdir(data / "images")) == ["b_ball.png"] and leftovers(data) == []


def test_a_cancelled_upload_leaves_nothing(data):
    class Field:
        filename = "slow.png"
        n = 0

        async def read_chunk(self):
            self.n += 1
            if self.n > 1:
                raise asyncio.CancelledError()
            return b"first"

    class Reader:
        async def next(self):
            return Field()

    class Req:
        async def multipart(self):
            return Reader()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(server._receive_upload(Req(), "image"))
    assert leftovers(data) == []


def test_a_name_taken_meanwhile_means_the_next_free_one(data, monkeypatch):
    src = data / "src.png"
    src.write_bytes(b"new")
    real = server._claim_media_name
    raced = []

    def claim(requested, base, ext, folders):
        name = real(requested, base, ext, folders)
        if not raced:                                  # a set import writes this name right after the claim
            raced.append(name)
            (data / "images" / name).write_bytes(b"import")
        return name
    monkeypatch.setattr(server, "_claim_media_name", claim)
    name = server._publish_new(str(src), str(data / "images"), "b_baum.png", server.IMAGE_DIRS)
    assert name != raced[0] and (data / "images" / raced[0]).read_bytes() == b"import"
    assert (data / "images" / name).read_bytes() == b"new"


# ── archiving ────────────────────────────────────────────────────────────────

def archive(body):
    ((status, raw),) = run([("POST", "/api/archive", {"json": body})])
    return status, json.loads(raw)


@pytest.mark.parametrize("name", ["../settings.json", "../../secret.txt", "..", ".hidden", "a/b.mp3", "", "a\x00.mp3"])
def test_archive_takes_only_a_library_name(data, tmp_path, name):
    status, out = archive({"type": "sound", "filename": name})
    assert status in (400, 404) and out["error"]
    assert (data / "settings.json").exists() and (tmp_path / "secret.txt").exists()


def test_archive_moves_and_never_overwrites(data):
    assert archive({"type": "sound", "filename": "b_ball.mp3"})[0] == 200
    (data / "sounds/b_ball.mp3").write_bytes(b"two")
    assert archive({"type": "sound", "filename": "b_ball.mp3"})[0] == 200
    arch = data / "archive/sounds"
    assert (arch / "b_ball.mp3").read_bytes() == b"mp3" and (arch / "b_ball-2.mp3").read_bytes() == b"two"
    assert not (data / "sounds/b_ball.mp3").exists()


# ── round 2: staging, real normalization, generation, archive races, export ──

def test_staging_is_outside_every_served_folder(data):
    path = server._staging("mp3")
    open(path, "wb").write(b"staged")
    for folder in (server.AI_DIR, server.SOUNDS_DIR, server.IMAGES_DIR):
        assert os.path.commonpath([os.path.realpath(folder), os.path.realpath(path)]) != os.path.realpath(folder)
    name = os.path.basename(path)
    assert [s for s, _ in run([("GET", "/ai/..%2f.staging%2f" + name, {}), ("GET", "/sounds/..%2f.staging%2f" + name, {})])] == [404, 404]


def test_a_staging_folder_that_cannot_be_made_is_a_507(data, monkeypatch):
    real = os.makedirs

    def full(path, *a, **k):
        if path.endswith(".staging"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(path, *a, **k)
    monkeypatch.setattr(server.os, "makedirs", full)
    status, out = upload("/api/images/upload", "foo.png")
    assert status == 507 and "Speicher" in out["error"]


@pytest.mark.parametrize("enabled", [False, True])
def test_the_real_normalizer_publishes_a_complete_file(data, monkeypatch, enabled):
    monkeypatch.setattr(server, "normalize_mp3", REAL_NORMALIZE)
    monkeypatch.setattr(server, "_normalize_enabled", lambda: enabled)

    def no_ffmpeg(*a, **k):                           # enabled but ffmpeg missing: raw copy fallback
        raise FileNotFoundError("ffmpeg")
    monkeypatch.setattr(server.subprocess, "run", no_ffmpeg)
    status, out = upload("/api/sounds/upload", "b_biene.mp3", b"raw-audio")
    assert status == 200 and out["normalized"] is False
    assert (data / "sounds/b_biene.mp3").read_bytes() == b"raw-audio" and leftovers(data) == []


def test_a_cancel_during_normalization_cleans_up_when_the_worker_ends(data, monkeypatch):
    import threading
    started, release = threading.Event(), threading.Event()

    def slow(src, dst):
        started.set()
        release.wait(5)
        with open(dst, "wb") as f:
            f.write(b"NORM")
        return True
    monkeypatch.setattr(server, "normalize_mp3", slow)

    class Field:
        filename = "slow.mp3"
        sent = False

        async def read_chunk(self):
            if self.sent:
                return b""
            self.sent = True
            return b"raw"

    class Reader:
        async def next(self):
            return Field()

    class Req:
        async def multipart(self):
            return Reader()

    async def go():
        task = asyncio.ensure_future(server.api_upload_sound(Req()))
        for _ in range(500):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        for _ in range(200):
            if not leftovers(data):
                break
            await asyncio.sleep(0.02)
    asyncio.run(go())
    assert leftovers(data) == [] and not (data / "sounds/slow.mp3").exists()


def test_a_free_name_skips_a_dangling_symlink(data, tmp_path):
    os.symlink(tmp_path / "outside.mp3", data / "sounds/foo.mp3")
    assert server._claim_media_name(None, "foo", "mp3", server.SOUND_DIRS) == "foo-2.mp3"


def test_a_generated_sound_never_goes_through_a_link_placed_after_the_claim(data, tmp_path):
    os.symlink(tmp_path / "outside.mp3", data / "sounds/late.mp3")
    with pytest.raises(FileExistsError):
        server._store_generated_sound(b"x" * 2000, "late.mp3")
    assert not (tmp_path / "outside.mp3").exists() and leftovers(data) == []


def test_a_generated_sound_is_published_with_its_original(data):
    assert server._store_generated_sound(b"y" * 2000, "gen.mp3") is False
    assert (data / "sounds/gen.mp3").read_bytes() == b"y" * 2000
    assert (data / "ai-generated/sounds/gen.mp3").read_bytes() == b"y" * 2000 and leftovers(data) == []


def test_archive_refuses_a_symlink_and_keeps_its_target(data, tmp_path):
    os.symlink("b_ball.mp3", data / "sounds/alias.mp3")
    os.symlink(tmp_path / "secret.txt", data / "sounds/out.mp3")
    assert archive({"type": "sound", "filename": "alias.mp3"})[0] == 404
    assert archive({"type": "sound", "filename": "out.mp3"})[0] == 404
    assert (data / "sounds/b_ball.mp3").read_bytes() == b"mp3" and (tmp_path / "secret.txt").exists()


def test_archive_never_deletes_a_file_written_after_it_took_the_old_one(data, monkeypatch):
    real = os.rename

    def then_import(a, b):
        real(a, b)
        if a.endswith("b_ball.mp3"):
            with open(a, "wb") as f:                     # a set import writes the name right after
                f.write(b"imported")
    monkeypatch.setattr(server.os, "rename", then_import)
    assert archive({"type": "sound", "filename": "b_ball.mp3"})[0] == 200
    assert (data / "archive/sounds/b_ball.mp3").read_bytes() == b"mp3"
    assert (data / "sounds/b_ball.mp3").read_bytes() == b"imported" and leftovers(data) == []


def test_set_export_never_reads_through_a_symlink(data, tmp_path):
    import io
    import zipfile
    import setbundle
    os.symlink(tmp_path / "secret.txt", data / "sounds/leak.mp3")
    os.symlink("../settings.json", data / "images/leak.png")
    st = {"letters": {"B": {"entries": [{"word": "Ball", "image": "leak.png",
                                         "tracks": {"FX": {"file": "leak.mp3"}, "DE": {"file": "b_ball.mp3"}}}]}}}
    raw = setbundle.build_zip("s", st, str(data / "sounds"), str(data / "images"))
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = z.namelist()
        assert "sounds/b_ball.mp3" in names and "sounds/leak.mp3" not in names and "images/leak.png" not in names
        assert all(b"CANARY" not in z.read(n) for n in names)


@pytest.mark.parametrize("enabled", [False, True])
def test_a_full_disk_during_the_normalizers_own_copy_still_publishes_the_upload(data, monkeypatch, enabled):
    monkeypatch.setattr(server, "normalize_mp3", REAL_NORMALIZE)
    monkeypatch.setattr(server, "_normalize_enabled", lambda: enabled)

    def no_ffmpeg(*a, **k):
        raise FileNotFoundError("ffmpeg")
    monkeypatch.setattr(server.subprocess, "run", no_ffmpeg)
    real_copy = server.shutil.copy2

    def full(src, dst, *a, **k):                      # the fallback copy into staging hits a full disk
        if ".staging" in str(dst):
            with open(dst, "wb") as f:
                f.write(b"PART")
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_copy(src, dst, *a, **k)
    monkeypatch.setattr(server.shutil, "copy2", full)
    status, out = upload("/api/sounds/upload", "b_biene.mp3", b"raw-audio")
    assert status == 200 and out == {"ok": True, "file": "b_biene.mp3", "normalized": False}
    assert (data / "sounds/b_biene.mp3").read_bytes() == b"raw-audio" and leftovers(data) == []
