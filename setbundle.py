"""Set export / import as ZIP (FUNK-43): backups, siblings, other devices.

A set file is a ZIP with exactly these members:

- ``funkeykid-set.json``: ``{"format": "funkeykid.set.v1", "set_id", "set", "missing"}``.
  The set is ``{name, description, letters, numbers, track_order}``, with every
  track's file, prompt, volume and enabled flag, and the numbers' backgrounds.
- ``sounds/<file>`` and ``images/<file>``: every file the set refers to and
  that existed at export time. Files that were missing are listed in ``missing``.

Import never trusts the file. Only those member paths are read, names must be
plain file names with a known extension, sizes are capped, members are read
one by one (never ``extractall``), and the set JSON is rebuilt from known
fields only. Importing creates a new set. Each file that is new is written,
an identical existing file is reused, and a conflict (same name, other
content) is resolved per file: ``rename`` (the default; references are
rewritten), ``keep`` (use the file already here) or ``overwrite``.

No server state is touched here; server.py owns settings, ids and activation.
Unit-tested in tests/test_setbundle.py.
"""
import copy
import hashlib
import io
import json
import os
import shutil
import struct
import threading
import uuid
import zipfile

FORMAT = "funkeykid.set.v1"
MANIFEST = "funkeykid-set.json"
SOUND_EXT = (".mp3", ".wav", ".ogg")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif")
TRACK_KINDS = ("FX", "DE", "EN")
NUMBER_KEYS = tuple("0123456789")
RESOLUTIONS = ("rename", "keep", "overwrite")

MAX_UPLOAD_BYTES = 400 * 1024 * 1024
MAX_MEMBERS = 3000
MAX_FILE_BYTES = 40 * 1024 * 1024
MAX_TOTAL_BYTES = 400 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_LETTERS = 64
MAX_ENTRIES = 300
MAX_BACKGROUNDS = 100
MAX_TEXT = 10000
WORD_MAX = 500
NAME_MAX = 200

_write_lock = threading.Lock()      # name allocation + writes of concurrent imports


class BundleError(ValueError):
    pass


# ── names and fields ─────────────────────────────────────────────────────────

def _utf8(value):
    """True if the text can be written as UTF-8 (JSON allows lone surrogates like "\\ud800")."""
    try:
        value.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def safe_name(name, kind):
    """A plain file name for sounds/ or images/ (no path, no hidden file, known extension)."""
    exts = SOUND_EXT if kind == "sound" else IMAGE_EXT
    if (not isinstance(name, str) or not 1 <= len(name) <= 200 or name in (".", "..") or name.startswith(".")
            or any(c in name for c in "/\\") or any(ord(c) < 32 for c in name) or not _utf8(name)
            or not name.lower().endswith(exts)):
        raise BundleError(f"invalid {kind} file name: {name!r}")
    return name


def _text(value, limit=MAX_TEXT):
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit or not _utf8(value):
        raise BundleError("text field too long, not text, or not valid Unicode")
    return value


def _file_ref(value, kind):
    """A file reference: "" (none) or a safe file name."""
    if value in (None, ""):
        return ""
    return safe_name(value, kind)


def _tracks(raw, legacy=None, legacy_kind=None, legacy_desc=""):
    """Tracks like server._normalize_tracks: known kinds, file, enabled, prompt, volume 0–200."""
    out = {}
    if isinstance(raw, dict):
        for kind, t in raw.items():
            if kind not in TRACK_KINDS or not isinstance(t, dict):
                continue
            vol = t.get("volume", 100)
            try:
                vol = int(vol) if not isinstance(vol, bool) else 100
            except (TypeError, ValueError):
                vol = 100
            out[kind] = {"file": _file_ref(t.get("file"), "sound"), "enabled": t.get("enabled", True) is not False,
                         "prompt": _text(t.get("prompt")), "volume": max(0, min(200, vol))}
    if legacy and legacy_kind not in out:              # pre-multi-track entries
        out[legacy_kind] = {"file": _file_ref(legacy, "sound"), "enabled": True,
                            "prompt": _text(legacy_desc), "volume": 100}
    return out


def _track_order(order):
    seen, out = set(), []
    for cfg in order if isinstance(order, list) else []:
        if isinstance(cfg, dict) and cfg.get("kind") in TRACK_KINDS and cfg["kind"] not in seen:
            seen.add(cfg["kind"])
            out.append({"kind": cfg["kind"], "enabled": cfg.get("enabled", True) is not False})
    for kind, enabled in (("FX", True), ("DE", True), ("EN", False)):
        if kind not in seen:
            out.append({"kind": kind, "enabled": enabled})
    return out


def clean_set(raw):
    """A set rebuilt from known fields only; raises BundleError for anything malformed."""
    if not isinstance(raw, dict):
        raise BundleError("the set must be an object")
    name = _text(raw.get("name"), NAME_MAX).strip()
    if not name:
        raise BundleError("the set needs a name")
    letters_in = raw.get("letters") or {}
    if not isinstance(letters_in, dict) or len(letters_in) > MAX_LETTERS:
        raise BundleError(f"letters must be an object with at most {MAX_LETTERS} letters")
    letters = {}
    for raw_letter, cfg in letters_in.items():
        # stored like the server stores them (upper case; ß stays ß), letters only
        letter = raw_letter if raw_letter == "ß" else (raw_letter.upper() if isinstance(raw_letter, str) else raw_letter)
        if (not isinstance(letter, str) or not 1 <= len(letter) <= 2 or not letter.isalpha()
                or not isinstance(cfg, dict)):
            raise BundleError(f"invalid letter {raw_letter!r}")
        if letter in letters:
            raise BundleError(f"letter {letter} appears twice")
        entries = cfg.get("entries") or []
        if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
            raise BundleError(f"letter {letter}: at most {MAX_ENTRIES} entries")
        out = []
        for e in entries:
            if not isinstance(e, dict):
                raise BundleError(f"letter {letter}: an entry must be an object")
            entry = {"word": _text(e.get("word"), WORD_MAX), "image": _file_ref(e.get("image"), "image"),
                     "enabled": e.get("enabled", True) is not False,
                     "tracks": _tracks(e.get("tracks"), e.get("sound"), "FX", e.get("_soundDesc"))}
            if e.get("_imageDesc"):
                entry["_imageDesc"] = _text(e["_imageDesc"])
            out.append(entry)
        letters[letter] = {"entries": out}
    numbers_in = raw.get("numbers") or {}
    if not isinstance(numbers_in, dict):
        raise BundleError("numbers must be an object")
    numbers = {}
    for digit, n in numbers_in.items():
        if digit not in NUMBER_KEYS or not isinstance(n, dict):
            raise BundleError(f"invalid number {digit!r}")
        bgs = n.get("backgrounds") or []
        if not isinstance(bgs, list) or len(bgs) > MAX_BACKGROUNDS:
            raise BundleError(f"number {digit}: at most {MAX_BACKGROUNDS} backgrounds")
        numbers[digit] = {
            "word": _text(n.get("word"), WORD_MAX), "image_subject": _text(n.get("image_subject")),
            "tracks": _tracks(n.get("tracks"), n.get("sound"), "DE", n.get("_soundDesc")),
            "backgrounds": [{"image": _file_ref(b.get("image"), "image"), "enabled": b.get("enabled", True) is not False}
                            for b in bgs if isinstance(b, dict)],
        }
    return {"name": name, "description": _text(raw.get("description"), 500),
            "letters": letters, "numbers": numbers, "track_order": _track_order(raw.get("track_order"))}


def referenced_files(set_data, unusable=None):
    """({sound names}, {image names}) a set refers to.

    With ``unusable`` (a list), names that are not plain file names are left out
    and recorded there instead of raising: export never fails on stored data.
    """
    sounds, images = set(), set()

    def add(target, name, kind):
        if not name:
            return
        if unusable is None:
            target.add(name)
            return
        try:
            target.add(safe_name(name, kind))
        except BundleError:
            unusable.append(f"{'sounds' if kind == 'sound' else 'images'}/{name}")

    for cfg in (set_data.get("letters") or {}).values():
        for e in (cfg or {}).get("entries") or []:
            add(images, e.get("image"), "image")
            for t in (e.get("tracks") or {}).values():
                add(sounds, (t or {}).get("file"), "sound")
    for n in (set_data.get("numbers") or {}).values():
        for t in ((n or {}).get("tracks") or {}).values():
            add(sounds, (t or {}).get("file"), "sound")
        for b in (n or {}).get("backgrounds") or []:
            add(images, (b or {}).get("image"), "image")
    return sounds, images


def _rewrite(set_data, sound_map, image_map):
    """Rename file references in a cleaned set (in place)."""
    for cfg in set_data.get("letters", {}).values():
        for e in cfg.get("entries", []):
            e["image"] = image_map.get(e.get("image"), e.get("image"))
            for t in e.get("tracks", {}).values():
                t["file"] = sound_map.get(t.get("file"), t.get("file"))
    for n in set_data.get("numbers", {}).values():
        for t in n.get("tracks", {}).values():
            t["file"] = sound_map.get(t.get("file"), t.get("file"))
        for b in n.get("backgrounds", []):
            b["image"] = image_map.get(b.get("image"), b.get("image"))
    return set_data


# ── export ───────────────────────────────────────────────────────────────────

def build_zip(set_id, set_data, sounds_dir, images_dir, version=""):
    """The ZIP bytes for a set: manifest plus every referenced file that exists.

    The set is written as stored (the server passes a normalized copy); export
    never fails on it. Import is where the file is checked strictly.
    """
    missing = []
    sounds, images = referenced_files(set_data, unusable=missing)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for folder, names, base in (("sounds", sounds, sounds_dir), ("images", images, images_dir)):
            for name in sorted(names):
                path = os.path.join(base, name)
                if os.path.isfile(path):
                    z.write(path, f"{folder}/{name}", compress_type=zipfile.ZIP_STORED)   # already compressed media
                else:
                    missing.append(f"{folder}/{name}")
        manifest = {"format": FORMAT, "set_id": set_id, "funkeykid": version, "set": set_data, "missing": missing}
        z.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=1), compress_type=zipfile.ZIP_DEFLATED)
    return buf.getvalue()


# ── import ───────────────────────────────────────────────────────────────────

MAX_CD_BYTES = MAX_MEMBERS * 600     # central directory: ~46 bytes per entry + name + extras


def _check_central_directory(path):
    """Refuse a ZIP whose central directory is too large *before* zipfile reads it.

    zipfile allocates every entry of the central directory when it opens a
    file, and a small upload can declare a huge one. This reads only the end
    record, with zipfile's own reader (_EndRecData, ZIP64 included). The check
    therefore sees exactly the entry count and directory size that zipfile
    will use (FUNK-43 review).
    """
    try:
        with open(path, "rb") as f:
            endrec = zipfile._EndRecData(f)
    except (OSError, zipfile.BadZipFile, struct.error):
        raise BundleError("not a ZIP file") from None
    if not endrec:
        raise BundleError("not a ZIP file")
    entries, cd_size = endrec[zipfile._ECD_ENTRIES_TOTAL], endrec[zipfile._ECD_SIZE]
    if entries > MAX_MEMBERS + 1 or cd_size > MAX_CD_BYTES:
        raise BundleError(f"too many files in the ZIP (at most {MAX_MEMBERS})")


def _member_kind(name):
    """("sound"|"image", file name) for an allowed member path, else raise."""
    folder, _, fname = name.partition("/")
    if folder == "sounds":
        return "sound", safe_name(fname, "sound")
    if folder == "images":
        return "image", safe_name(fname, "image")
    raise BundleError(f"unexpected file in the ZIP: {name!r}")


def _open_zip(path):
    _check_central_directory(path)
    try:
        return zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError, ValueError):
        raise BundleError("not a ZIP file") from None


def read_zip(path):
    """(cleaned set, {member path: size}) from an uploaded set ZIP. Raises BundleError."""
    with _open_zip(path) as z:
        infos = z.infolist()
        if len(infos) > MAX_MEMBERS:
            raise BundleError(f"too many files in the ZIP (at most {MAX_MEMBERS})")
        members, total, manifest = {}, 0, None
        for info in infos:
            if info.is_dir():
                continue
            if info.flag_bits & 0x1:
                raise BundleError("encrypted ZIP files are not supported")
            if info.filename == MANIFEST:
                if manifest is not None:
                    raise BundleError(f"{MANIFEST} appears twice")
                if info.file_size > MAX_MANIFEST_BYTES:
                    raise BundleError("the set description is too large")
                manifest = _read_member(z, info, MAX_MANIFEST_BYTES)
                continue
            _member_kind(info.filename)
            if info.file_size > MAX_FILE_BYTES:
                raise BundleError(f"{info.filename} is too large")
            if info.filename in members:
                raise BundleError(f"{info.filename} appears twice")
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise BundleError("the ZIP unpacks to too much data")
            members[info.filename] = info.file_size
    if manifest is None:
        raise BundleError(f"{MANIFEST} is missing: not a funkeykid set")
    try:
        data = json.loads(manifest.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BundleError(f"{MANIFEST} is not valid JSON") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise BundleError(f"not a funkeykid set file (format {FORMAT} expected)")
    return clean_set(data.get("set")), members


def _read_member(z, info, limit):
    """A member's bytes, never more than limit (the declared size can lie)."""
    try:
        with z.open(info) as f:
            data = f.read(limit + 1)
    except Exception as e:                  # bad CRC, broken deflate stream …
        raise BundleError(f"{info.filename} is damaged") from e
    if len(data) > limit:
        raise BundleError(f"{info.filename} is too large")
    return data


def _hash_member(z, name):
    """sha256 of a member, read completely: this also checks its CRC and its real size."""
    h, n = hashlib.sha256(), 0
    try:
        with z.open(name) as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                n += len(chunk)
                if n > MAX_FILE_BYTES:
                    raise BundleError(f"{name} is too large")
                h.update(chunk)
    except BundleError:
        raise
    except Exception as e:
        raise BundleError(f"{name} is damaged") from e
    return h.hexdigest()


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def plan(path, set_data, members, sounds_dir, images_dir):
    """Per referenced file: what importing would do, and what is missing.

    Status: "new" (not here yet), "same" (identical file here, reused),
    "conflict" (same name, different content: needs a resolution),
    "local" (not in the ZIP but already here), "missing" (nowhere).
    Every member is read completely, so a damaged ZIP fails here, at preview.
    """
    sounds, images = referenced_files(set_data)
    files = []
    with _open_zip(path) as z:
        for kind, names, base, folder in (("sound", sounds, sounds_dir, "sounds"), ("image", images, images_dir, "images")):
            for name in sorted(names):
                member = f"{folder}/{name}"
                here = os.path.join(base, name)
                if member not in members:
                    files.append({"path": member, "kind": kind, "name": name,
                                  "status": "local" if os.path.isfile(here) else "missing", "size": 0})
                    continue
                digest = _hash_member(z, member)
                status = "new"
                if os.path.isfile(here):
                    status = "same" if digest == _sha256_file(here) else "conflict"
                files.append({"path": member, "kind": kind, "name": name, "status": status, "size": members[member]})
    return files


def _free_name(base, name, taken, kind):
    """name-2.ext, name-3.ext …: not taken, not on disk, and still a valid name (stem shortened if needed)."""
    stem, ext = os.path.splitext(name)
    n = 2
    while True:
        suffix = f"-{n}"
        candidate = safe_name(stem[:200 - len(suffix) - len(ext)] + suffix + ext, kind)
        if candidate not in taken and not os.path.exists(os.path.join(base, candidate)):
            return candidate
        n += 1


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


class Transaction:
    """What an import published, so it can be undone if the set cannot be saved.

    ``commit()`` once the set is saved (drops the backups of overwritten files);
    ``rollback()`` removes created files and puts overwritten ones back.
    """

    def __init__(self):
        self.created = []           # files that did not exist before
        self.replaced = []          # (file, backup) for overwritten files

    def rollback(self):
        for path in reversed(self.created):
            _remove(path)
        for final, backup in reversed(self.replaced):
            try:
                os.replace(backup, final)
            except OSError:
                pass
        self.created, self.replaced = [], []

    def commit(self):
        for _, backup in self.replaced:
            _remove(backup)
        self.created, self.replaced = [], []


def _stage(z, member, base):
    """Copy a member completely into a hidden temp file beside its target (CRC and size checked)."""
    tmp = os.path.join(base, f".import-{uuid.uuid4().hex}.tmp")
    written = 0
    try:
        with z.open(member) as src, open(tmp, "wb") as dst:
            for chunk in iter(lambda: src.read(1 << 20), b""):
                written += len(chunk)
                if written > MAX_FILE_BYTES:
                    raise BundleError(f"{member} is too large")
                dst.write(chunk)
    except BundleError:
        _remove(tmp)
        raise
    except Exception as e:
        _remove(tmp)
        raise BundleError(f"{member} is damaged") from e
    return tmp


def _create_exclusive(tmp, final):
    """Publish tmp as final only if final does not exist (raises FileExistsError): never overwrites."""
    try:
        os.link(tmp, final)
        return
    except FileExistsError:
        raise
    except OSError:
        pass                                # no hard links on this file system: copy instead
    fd = os.open(final, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "wb") as dst, open(tmp, "rb") as src:
            shutil.copyfileobj(src, dst)
    except BaseException:
        _remove(final)
        raise


def _backup(final):
    backup = os.path.join(os.path.dirname(final), f".import-backup-{uuid.uuid4().hex}.bak")
    try:
        os.link(final, backup)
    except OSError:
        shutil.copy2(final, backup)
    return backup


def apply(path, set_data, members, sounds_dir, images_dir, resolutions=None):
    """Write the set's files; return (set with rewritten names, summary, Transaction).

    Two phases, so a failure changes nothing: every needed member is first read
    completely into hidden temp files (CRC and size checked); only then are the
    files published. New and renamed files are created exclusively and never
    overwrite a file that appeared meanwhile (an identical one is reused,
    another gets the next free name). "overwrite" keeps a backup until the
    caller commits the transaction. Statuses are computed again here.
    """
    resolutions = resolutions or {}
    for res in resolutions.values():
        if res not in RESOLUTIONS:
            raise BundleError(f"resolution must be one of {', '.join(RESOLUTIONS)}")
    data = copy.deepcopy(set_data)
    summary = {"written": [], "reused": [], "renamed": {}, "overwritten": [], "kept": [], "missing": []}
    maps = {"sound": {}, "image": {}}
    txn = Transaction()
    staged = []
    with _write_lock:
        try:
            items = plan(path, data, members, sounds_dir, images_dir)
            with _open_zip(path) as z:
                for item in items:
                    status = item["status"]
                    if status == "missing":
                        summary["missing"].append(item["path"])
                        continue
                    if status in ("local", "same"):
                        summary["reused"].append(item["path"])
                        continue
                    action = "new" if status == "new" else resolutions.get(item["path"], "rename")
                    if action == "keep":
                        summary["kept"].append(item["path"])
                        continue
                    base = sounds_dir if item["kind"] == "sound" else images_dir
                    os.makedirs(base, exist_ok=True)
                    staged.append((item, action, _stage(z, item["path"], base), base))
            # every name the set refers to is reserved, even a missing one: a renamed file
            # must never take a name another reference already means (FUNK-43 review)
            ref_sounds, ref_images = referenced_files(data)
            taken = {"sound": set(ref_sounds), "image": set(ref_images)}
            for item, action, tmp, base in staged:
                name = item["name"]
                if action == "overwrite":
                    final = os.path.join(base, name)
                    backup = _backup(final) if os.path.exists(final) else None
                    os.replace(tmp, final)
                    if backup:
                        txn.replaced.append((final, backup))
                    else:
                        txn.created.append(final)
                    summary["overwritten"].append(item["path"])
                    continue
                kind = item["kind"]
                target = name if action == "new" else _free_name(base, name, taken[kind], kind)
                while True:
                    final = os.path.join(base, target)
                    try:
                        _create_exclusive(tmp, final)
                    except FileExistsError:
                        if target == name and _sha256_file(tmp) == _sha256_file(final):
                            target = None           # the very same file appeared meanwhile: reuse it
                            break
                        target = _free_name(base, name, taken[kind], kind)
                        continue
                    txn.created.append(final)
                    taken[kind].add(target)
                    break
                if target is None:
                    summary["reused"].append(item["path"])
                elif target == name:
                    summary["written"].append(item["path"])
                else:
                    maps[item["kind"]][name] = target
                    summary["renamed"][item["path"]] = target
        except BaseException:
            txn.rollback()
            raise
        finally:
            for _, _, tmp, _ in staged:
                _remove(tmp)
    _rewrite(data, maps["sound"], maps["image"])
    return data, summary, txn


def purge_temp_files(*dirs):
    """Remove temp files a crashed import left behind (call at startup, when no import runs)."""
    for base in dirs:
        try:
            for name in os.listdir(base):
                if name.startswith(".import-") and name.endswith(".tmp"):
                    _remove(os.path.join(base, name))
        except OSError:
            pass
