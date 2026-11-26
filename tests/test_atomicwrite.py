"""Crash-safe settings.json writes (FUNK-67).

Run: python -m pytest tests/ (no Docker needed; the real single-file bind mount
was checked in a Linux container, see the ticket).
"""
import errno
import os
import stat
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import atomicwrite as aw  # noqa: E402


@pytest.fixture
def target(tmp_path):
    path = tmp_path / "settings.json"
    path.write_bytes(b'{"old": true}\n')
    os.chmod(path, 0o640)
    return str(path)


def _leftovers(path):
    folder = os.path.dirname(path)
    return [n for n in os.listdir(folder) if n.startswith(aw.tmp_prefix(path))]


def test_rename_replaces_the_file_atomically(target):
    before = os.stat(target)
    mode, why = aw.replace(target, b'{"new": true}\n')
    after = os.stat(target)
    assert (mode, why) == (aw.RENAME, None)
    assert open(target, "rb").read() == b'{"new": true}\n'
    assert after.st_ino != before.st_ino                 # a new file took its place: never half-written
    assert stat.S_IMODE(after.st_mode) == 0o640           # mode kept (and owner, where allowed)
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    assert _leftovers(target) == []


def test_a_new_file_is_created(tmp_path):
    path = str(tmp_path / "sub" / "settings.json")
    assert aw.replace(path, b"{}\n")[0] == aw.RENAME
    assert open(path, "rb").read() == b"{}\n"


@pytest.mark.parametrize("code", [errno.EBUSY, errno.EXDEV])
def test_a_single_file_mount_falls_back_to_writing_in_place(target, monkeypatch, code):
    """Linux refuses to rename over a bind-mounted file with EBUSY (EXDEV elsewhere)."""
    def refuse(src, dst):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(aw.os, "replace", refuse)
    ino = os.stat(target).st_ino
    mode, why = aw.replace(target, b'{"new": true}\n')
    assert mode == aw.IN_PLACE and why.errno == code
    assert open(target, "rb").read() == b'{"new": true}\n'
    assert os.stat(target).st_ino == ino                  # the same file, as a mount needs it
    assert _leftovers(target) == []


def test_in_place_mode_skips_the_temp_file(target, monkeypatch):
    monkeypatch.setattr(aw.os, "replace", lambda *a: pytest.fail("no rename in place mode"))
    assert aw.replace(target, b"x", in_place=True) == (aw.IN_PLACE, None)
    assert open(target, "rb").read() == b"x"


def test_any_other_failure_leaves_the_old_file_intact(target, monkeypatch):
    real_fsync = os.fsync

    def disk_full(fd):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(aw.os, "fsync", disk_full)
    with pytest.raises(OSError) as e:
        aw.replace(target, b'{"new": true}\n')
    monkeypatch.setattr(aw.os, "fsync", real_fsync)
    assert e.value.errno == errno.ENOSPC
    assert open(target, "rb").read() == b'{"old": true}\n'
    assert _leftovers(target) == []


def test_purge_removes_only_its_own_temp_files(target):
    folder = os.path.dirname(target)
    for name in (".settings.json.abc.tmp", ".settings.json.def.tmp", ".other.tmp", "settings.json.tmp", ".import-x.tmp"):
        open(os.path.join(folder, name), "wb").close()
    assert aw.purge_temp_files(target) == 2
    assert sorted(os.listdir(folder)) == [".import-x.tmp", ".other.tmp", "settings.json", "settings.json.tmp"]
