"""Crash-safe replacement of a small state file such as settings.json (FUNK-67).

replace() writes a temp file beside the target, fsyncs it, renames it over
the target and fsyncs the directory. A crash or power cut then leaves the old
file or the new one, never a truncated one.

If the directory cannot be fsynced after the rename, replace() raises: the
new content is in place, but not yet durable, and the caller must not report
success. Only a file system that does not support a directory fsync at all
(EINVAL, ENOTSUP) is accepted silently.

A file that is itself a bind mount (a single-file Docker volume) cannot be
renamed over: Linux answers EBUSY (or EXDEV). replace() then rewrites it in
place with one write and fsync, and reports that, so the caller can stay in
that mode and say so once.
"""
import errno
import os
import stat
import uuid

TMP_SUFFIX = ".tmp"
RENAME, IN_PLACE = "rename", "in place"


def tmp_prefix(path):
    return "." + os.path.basename(path) + "."


def replace(path, payload, in_place=False):
    """Write payload (bytes) to path. Returns (mode, OSError that forced in place or None)."""
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    why = None
    if not in_place:
        tmp = os.path.join(folder, f"{tmp_prefix(path)}{uuid.uuid4().hex}{TMP_SUFFIX}")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, "wb") as f:
                _match_owner(path, f.fileno())
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError as e:
            _remove(tmp)
            if e.errno not in (errno.EBUSY, errno.EXDEV):
                raise
            why = e
        else:
            _fsync_dir(folder)
            return RENAME, None
    with open(path, "wb") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    return IN_PLACE, why


def purge_temp_files(path):
    """Remove temp files a crash left between write and rename (call when nothing writes)."""
    folder, prefix = os.path.dirname(path) or ".", tmp_prefix(path)
    try:
        names = os.listdir(folder)
    except OSError:
        return 0
    removed = 0
    for name in names:
        if name.startswith(prefix) and name.endswith(TMP_SUFFIX):
            removed += _remove(os.path.join(folder, name))
    return removed


def _match_owner(path, fd):
    """Give the temp file the target's mode and owner (best effort: only root may chown)."""
    try:
        st = os.stat(path)
    except OSError:
        return
    try:
        os.fchmod(fd, stat.S_IMODE(st.st_mode))
    except OSError:
        pass
    try:
        os.fchown(fd, st.st_uid, st.st_gid)
    except OSError:
        pass


_NO_DIR_FSYNC = {errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL), getattr(errno, "EOPNOTSUPP", errno.EINVAL)}


def _fsync_dir(folder):
    """Make the rename durable. Raises on failure, except where directory fsync is unsupported."""
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError as e:
        if e.errno not in _NO_DIR_FSYNC:
            raise
    finally:
        os.close(fd)


def _remove(path):
    try:
        os.remove(path)
        return 1
    except OSError:
        return 0
