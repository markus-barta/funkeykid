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
import re
import threading
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

def safe_name(name, kind):
    """A plain file name for sounds/ or images/ (no path, no hidden file, known extension)."""
    exts = SOUND_EXT if kind == "sound" else IMAGE_EXT
    if (not isinstance(name, str) or not 1 <= len(name) <= 200 or name in (".", "..") or name.startswith(".")
            or any(c in name for c in "/\\") or any(ord(c) < 32 for c in name)
            or not name.lower().endswith(exts)):
        raise BundleError(f"invalid {kind} file name: {name!r}")
    return name


def _text(value, limit=MAX_TEXT):
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise BundleError("text field too long or not text")
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

def _member_kind(name):
    """("sound"|"image", file name) for an allowed member path, else raise."""
    folder, _, fname = name.partition("/")
    if folder == "sounds":
        return "sound", safe_name(fname, "sound")
    if folder == "images":
        return "image", safe_name(fname, "image")
    raise BundleError(f"unexpected file in the ZIP: {name!r}")


def read_zip(path):
    """(cleaned set, {member path: size}) from an uploaded set ZIP. Raises BundleError."""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise BundleError("not a ZIP file") from None
    with z:
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
    with z.open(info) as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise BundleError(f"{info.filename} is too large")
    return data


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
    """
    sounds, images = referenced_files(set_data)
    files = []
    with zipfile.ZipFile(path) as z:
        for kind, names, base, folder in (("sound", sounds, sounds_dir, "sounds"), ("image", images, images_dir, "images")):
            for name in sorted(names):
                member = f"{folder}/{name}"
                here = os.path.join(base, name)
                if member not in members:
                    files.append({"path": member, "kind": kind, "name": name,
                                  "status": "local" if os.path.isfile(here) else "missing", "size": 0})
                    continue
                status = "new"
                if os.path.isfile(here):
                    h = hashlib.sha256()
                    with z.open(member) as f:
                        for chunk in iter(lambda: f.read(1 << 20), b""):
                            h.update(chunk)
                    status = "same" if h.hexdigest() == _sha256_file(here) else "conflict"
                files.append({"path": member, "kind": kind, "name": name, "status": status, "size": members[member]})
    return files


def _free_name(base, name, taken):
    stem, ext = os.path.splitext(name)
    n = 2
    candidate = f"{stem}-{n}{ext}"
    while candidate in taken or os.path.exists(os.path.join(base, candidate)):
        n += 1
        candidate = f"{stem}-{n}{ext}"
    return candidate


def apply(path, set_data, members, sounds_dir, images_dir, resolutions=None):
    """Write the set's files per plan and resolutions; return (set with rewritten names, summary).

    Statuses are computed again here (a file may have appeared since the
    preview). Every write goes through a temp file and os.replace.
    """
    resolutions = resolutions or {}
    for res in resolutions.values():
        if res not in RESOLUTIONS:
            raise BundleError(f"resolution must be one of {', '.join(RESOLUTIONS)}")
    data = copy.deepcopy(set_data)
    summary = {"written": [], "reused": [], "renamed": {}, "overwritten": [], "kept": [], "missing": []}
    maps = {"sound": {}, "image": {}}
    with _write_lock, zipfile.ZipFile(path) as z:
        taken = set()
        for item in plan(path, data, members, sounds_dir, images_dir):
            base = sounds_dir if item["kind"] == "sound" else images_dir
            if item["status"] == "missing":
                summary["missing"].append(item["path"])
                continue
            if item["status"] in ("local", "same"):
                summary["reused"].append(item["path"])
                continue
            target = item["name"]
            if item["status"] == "conflict":
                res = resolutions.get(item["path"], "rename")
                if res == "keep":
                    summary["kept"].append(item["path"])
                    continue
                if res == "rename":
                    target = _free_name(base, item["name"], taken)
                    maps[item["kind"]][item["name"]] = target
                    summary["renamed"][item["path"]] = target
                else:
                    summary["overwritten"].append(item["path"])
            taken.add(target)
            info = z.getinfo(item["path"])
            os.makedirs(base, exist_ok=True)
            tmp = os.path.join(base, f".import-{os.getpid()}-{threading.get_ident()}.tmp")
            written = 0
            with z.open(info) as src, open(tmp, "wb") as dst:
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    written += len(chunk)
                    if written > MAX_FILE_BYTES:
                        dst.close()
                        os.remove(tmp)
                        raise BundleError(f"{item['path']} is too large")
                    dst.write(chunk)
            os.replace(tmp, os.path.join(base, target))
            if item["status"] != "conflict" or target == item["name"]:
                summary["written"].append(item["path"])
    _rewrite(data, maps["sound"], maps["image"])
    return data, summary
