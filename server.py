"""funkeykid v2 — aiohttp web server + keyboard handler with sets."""
import asyncio
import errno
import json
import os
import subprocess
import random
import shutil
import re
import tempfile
import threading
import time
import uuid
import weakref
from pathlib import Path

from aiohttp import web

import atomicwrite
import keymap
import profiles
import progress
import quiz
import setbundle
from keyboard import KeyboardListener, is_battery_low
from display import Display, HEARTBEAT_SECONDS
from version import VERSION, VERSION_SCHEME, RELEASE_CHANNEL, RELEASE_SEQUENCE, BUILD, REPO

# SSE: connected clients
sse_clients = weakref.WeakSet()
_loop = None  # asyncio event loop ref for thread-safe SSE

DATA_DIR = os.environ.get("FUNKEYKID_DATA", "/data")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
SOUNDS_DIR = os.path.join(DATA_DIR, "sounds")
IMAGES_DIR = os.path.join(DATA_DIR, "images")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
PORT = int(os.environ.get("FUNKEYKID_PORT", "8081"))

# Global state
settings = {}
keyboard = None
display = None
active_processes = []
current_volume = 100
startup_time = time.time()
STARTUP_GRACE_SECONDS = 3  # Ignore keypresses for 3s after start (prevents stale BT events)
DEFAULT_KEYBOARD = "ACME BK03"
DEFAULT_BATTERY_LOW_THRESHOLD = 20


def _keyboard_env():
    """FUNKEYKID_KEYBOARD: the host pins the keyboard (its udev isolation must match)."""
    return (os.environ.get("FUNKEYKID_KEYBOARD") or "").strip()


def _keyboard_device_name():
    """Configured keyboard: env (host-pinned) > settings.keyboard_device > default."""
    return _keyboard_env() or (settings.get("keyboard_device") or "").strip() or DEFAULT_KEYBOARD


def _battery_low_threshold():
    """settings.battery_low_threshold in percent, 0 = warning off (FUNK-58).
    Rechargeable NiMH cells report low levels early, hence configurable."""
    try:
        return max(0, min(100, int(settings.get("battery_low_threshold", DEFAULT_BATTERY_LOW_THRESHOLD))))
    except (TypeError, ValueError):
        return DEFAULT_BATTERY_LOW_THRESHOLD


def _keyboard_status():
    """Keyboard status enriched with derived fields, for UI, SSE and MQTT."""
    status = keyboard.get_status() if keyboard else {
        "connected": False, "exclusive": False, "battery_level": None,
        "device_name": _keyboard_device_name()}
    status["battery_low_threshold"] = _battery_low_threshold()
    status["battery_low"] = is_battery_low(status.get("battery_level"), status["battery_low_threshold"])
    status["keyboard_env_pinned"] = bool(_keyboard_env())
    return status


def _publish_keyboard_status():
    if display:
        display.publish_keyboard_status(_keyboard_status())
# Per-letter cycling state: {letter: index} — persists across letter switches
cycle_index = {}
last_letter = None
# Per-digit background cycling state: {digit: bg_index}
number_cycle_index = {}
last_number = None
NUMBER_KEYS = ("0","1","2","3","4","5","6","7","8","9")
# Default number content — seeded into any set missing a `numbers` key on load
DEFAULT_NUMBERS = {
    "0": {"word": "Null",           "image_subject": "an empty wooden bowl, nothing inside, plain background"},
    "1": {"word": "Ein Apfel",      "image_subject": "one red apple"},
    "2": {"word": "Zwei Bananen",   "image_subject": "two yellow bananas"},
    "3": {"word": "Drei Enten",     "image_subject": "three ducks"},
    "4": {"word": "Vier Autos",     "image_subject": "four toy cars"},
    "5": {"word": "Fünf Sterne",    "image_subject": "five yellow stars"},
    "6": {"word": "Sechs Bälle",    "image_subject": "six colorful balls"},
    "7": {"word": "Sieben Fische",  "image_subject": "seven fish"},
    "8": {"word": "Acht Blumen",    "image_subject": "eight flowers"},
    "9": {"word": "Neun Marienkäfer","image_subject": "nine ladybugs"},
}
# Default ElevenLabs voice id for number TTS (overridable in settings.numbers_tts.voice_id)
DEFAULT_NUMBER_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"

# ── Multi-track audio model ────────────────────────────────────────────────
# Each entry (letter or number) has a `tracks` dict keyed by kind:
#   { "FX": {"file": "x.mp3", "enabled": true, "prompt": "..."}, "DE": {...} }
# Sets carry a `track_order` list describing playback order + per-kind enable:
#   [{"kind": "FX", "enabled": true}, {"kind": "DE", "enabled": true}, ...]
TRACK_KINDS = ("FX", "DE", "EN")
TRACK_KIND_META = {
    "FX": {"label": "Effekt",   "short": "FX", "generator": "sfx"},
    "DE": {"label": "Deutsch",  "short": "DE", "generator": "tts", "lang": "de"},
    "EN": {"label": "Englisch", "short": "EN", "generator": "tts", "lang": "en"},
}
DEFAULT_TRACK_ORDER = [
    {"kind": "FX", "enabled": True},
    {"kind": "DE", "enabled": True},
    {"kind": "EN", "enabled": False},
]
# Default voices per language. Can be overridden via settings.tts_voices.
DEFAULT_TTS_VOICES = {
    "de": DEFAULT_NUMBER_VOICE_ID,      # Rachel (existing default)
    "en": "pNInz6obpgDQGcFmaJgB",       # Adam
}


def _normalize_tracks(entry, legacy_kind):
    """Ensure entry has a valid `tracks` dict. Synthesize from legacy `sound` once.

    `legacy_kind` is the kind to migrate a legacy `.sound` file into:
      - letters: "FX" (historically sound effects)
      - numbers: "DE" (historically German TTS)
    Does not strip the legacy `sound` key (kept for back-compat reads until
    a full save writes the new shape back).
    """
    tracks = entry.get("tracks")
    if not isinstance(tracks, dict):
        tracks = {}
        entry["tracks"] = tracks
    legacy_sound = entry.get("sound")
    if legacy_sound and legacy_kind not in tracks:
        tracks[legacy_kind] = {
            "file": legacy_sound,
            "enabled": True,
            "prompt": entry.get("_soundDesc", "") or "",
            "volume": 100,
        }
    # Drop any unknown kinds / malformed entries silently
    for k in list(tracks.keys()):
        if k not in TRACK_KINDS:
            del tracks[k]
            continue
        t = tracks[k]
        if not isinstance(t, dict):
            del tracks[k]
            continue
        t.setdefault("file", "")
        t.setdefault("enabled", True)
        t.setdefault("prompt", "")
        # Volume: 0-200 percent. Defaults to 100 (neutral). Clamp silently.
        try:
            v = int(t.get("volume", 100))
        except (TypeError, ValueError):
            v = 100
        t["volume"] = max(0, min(200, v))
    return entry


def _normalize_letter_entry(e):
    return _normalize_tracks(e, "FX")


# ── Entry ids (FUNK-72) ──
# Every letter entry carries an id, unique within its letter. The editor and the generation
# jobs find an entry by it, never by word or position: a word can change or repeat, and the
# list can be reordered while a job runs.
_ENTRY_ID = re.compile(r"[A-Za-z0-9_-]{6,40}")


def _ensure_entry_ids(entries):
    """Give every entry a valid id that no other entry in the list has. True if any changed."""
    if not isinstance(entries, list):
        return False
    valid = [e.get("id") for e in entries
             if isinstance(e, dict) and isinstance(e.get("id"), str) and _ENTRY_ID.fullmatch(e["id"])]
    reserved = set(valid)
    taken, changed = set(), False
    for e in entries:
        if not isinstance(e, dict):
            continue
        eid = e.get("id")
        if not (isinstance(eid, str) and _ENTRY_ID.fullmatch(eid)) or eid in taken:
            eid = uuid.uuid4().hex[:10]
            while eid in reserved or eid in taken:
                eid = uuid.uuid4().hex[:10]
            e["id"] = eid
            changed = True
        taken.add(eid)
    return changed


def _ensure_all_entry_ids():
    """Ids for every letter entry of every set (startup, after an import). True if any changed."""
    changed = False
    for s in (settings.get("sets") or {}).values():
        if not isinstance(s, dict):
            continue
        for cfg in (s.get("letters") or {}).values():
            if isinstance(cfg, dict) and _ensure_entry_ids(cfg.get("entries")):
                changed = True
    return changed


def _normalize_number_entry(n):
    return _normalize_tracks(n, "DE")


def _add_legacy_sound_alias(entry, preferred_kind):
    """Back-compat: expose `sound` + `_soundDesc` keys synthesized from `tracks`
    so the pre-multi-track UI continues to work. Returns the entry.

    Prefers `preferred_kind` (FX for letters, DE for numbers). Falls back to
    any other track so a legacy client still sees *something* selected.
    """
    tracks = entry.get("tracks") or {}
    chosen = None
    if preferred_kind in tracks and tracks[preferred_kind].get("file"):
        chosen = tracks[preferred_kind]
    else:
        for k in TRACK_KINDS:
            if k in tracks and tracks[k].get("file"):
                chosen = tracks[k]
                break
    if chosen:
        entry["sound"] = chosen.get("file", "")
        if chosen.get("prompt"):
            entry["_soundDesc"] = chosen["prompt"]
    return entry


def _strip_legacy_sound(entry):
    """After tracks are in place, drop the legacy `sound` + `_soundDesc` keys."""
    entry.pop("sound", None)
    entry.pop("_soundDesc", None)
    return entry


def _normalize_track_order(order):
    """Ensure order is a list of {kind, enabled} with every known kind present."""
    if not isinstance(order, list):
        order = []
    seen = set()
    out = []
    for cfg in order:
        if not isinstance(cfg, dict):
            continue
        kind = cfg.get("kind")
        if kind in TRACK_KINDS and kind not in seen:
            seen.add(kind)
            out.append({"kind": kind, "enabled": bool(cfg.get("enabled", True))})
    for default in DEFAULT_TRACK_ORDER:
        if default["kind"] not in seen:
            out.append({"kind": default["kind"], "enabled": default["enabled"]})
    return out


def _get_active_set_cfg():
    """Return full active-set dict (metadata + letters + numbers + track_order)."""
    set_id = settings.get("active_set", "")
    sets = settings.get("sets", {})
    if set_id in sets:
        return sets[set_id]
    if sets:
        return next(iter(sets.values()))
    return {}


def _get_active_track_order():
    """Return normalized track_order of the active set."""
    return _normalize_track_order(_get_active_set_cfg().get("track_order"))


def _voice_for_kind(kind):
    """ElevenLabs voice id for a TTS kind (DE/EN). Returns None for non-TTS kinds."""
    meta = TRACK_KIND_META.get(kind, {})
    if meta.get("generator") != "tts":
        return None
    lang = meta.get("lang", "de")
    voices = settings.get("tts_voices") or {}
    return voices.get(lang) or DEFAULT_TTS_VOICES.get(lang, DEFAULT_NUMBER_VOICE_ID)


def _collect_playable_files(entry):
    """Resolve entry + active set's track_order into a list of (path, volume_pct)."""
    order = _get_active_track_order()
    tracks = entry.get("tracks") or {}
    files = []
    for cfg in order:
        if not cfg.get("enabled", True):
            continue
        t = tracks.get(cfg["kind"])
        if not t or not t.get("enabled", True):
            continue
        fname = t.get("file")
        if not fname:
            continue
        path = os.path.join(SOUNDS_DIR, fname)
        if os.path.exists(path):
            try:
                vol = int(t.get("volume", 100))
            except (TypeError, ValueError):
                vol = 100
            files.append((path, max(0, min(200, vol))))
    return files
# Flat playlist position for arrow-key navigation (index into _build_flat_playlist())
flat_pos = -1
# Favorites: list of {letter, entry_index} — up to 10
favorites = []
# AI generation jobs tracking
gen_jobs = {}  # {job_id: {type, kind, word, status, filename, started, finished, error, error_code, error_detail}}
BOOT_ID = uuid.uuid4().hex[:12]   # this server process: a page sees from it that the server restarted (jobs are in memory)
GEN_JOBS_KEEP = 300        # finished jobs kept for the activity list (FUNK-72); running ones always stay


def _register_job(job):
    """Add a generation job; forget the oldest finished ones beyond GEN_JOBS_KEEP."""
    gen_jobs[job["id"]] = job
    done = [j for j in gen_jobs.values() if j.get("status") in ("done", "error")]
    for j in sorted(done, key=lambda j: j.get("finished") or j.get("started") or 0)[:max(0, len(done) - GEN_JOBS_KEEP)]:
        gen_jobs.pop(j["id"], None)
    return job
# AI request/response log (circular buffer, last 50)
ai_log = []
AI_LOG_MAX = 50


def load_settings():
    global settings, current_volume
    if os.path.exists(SETTINGS_PATH):
        with open(SETTINGS_PATH) as f:
            settings = json.load(f)
    # Migrate old flat format → sets format
    if "letters" in settings and "sets" not in settings:
        _migrate_to_sets()
    _ensure_numbers_defaults()
    _migrate_to_tracks_v2()
    _migrate_sz_letter()
    if _ensure_all_entry_ids():                 # FUNK-72: entries are found by id
        try:
            save_settings()
            print("[migrate] letter entries got ids", flush=True)
        except Exception as e:
            print(f"[migrate] entry ids not saved: {e}", flush=True)
    current_volume = settings.get("volume", 100)
    return settings


def _migrate_sz_letter():
    """Move ß content stored under "SS" (ß.upper()) or "ẞ" before FUNK-46 to "ß".

    Only the API could write it there (the Letters tab had no ß card), but it
    would be unreachable now. Entries already under ß come first; a blacklist is
    merged. An older image ignores the "ß" key, so a rollback loses nothing.
    """
    active = settings.get("active_set")
    shift = {}   # legacy key → enabled ß entries before its own in the active set (favorites index those)
    for sid, s in (settings.get("sets") or {}).items():
        letters = s.get("letters") if isinstance(s, dict) else None
        if not isinstance(letters, dict):
            continue
        for legacy in keymap.LEGACY_SZ_KEYS:
            if legacy not in letters:
                continue
            old = letters.pop(legacy) or {}
            cur = letters.setdefault("ß", {"entries": []})
            if sid == active:
                shift[legacy] = sum(1 for e in cur.get("entries", []) if e.get("enabled", True))
            cur.setdefault("entries", []).extend(old.get("entries") or [])
            print(f"[migrate] set {sid}: {len(old.get('entries') or [])} ß entries moved from {legacy} to ß", flush=True)
    for fav in settings.get("favorites") or []:
        if isinstance(fav, dict) and fav.get("letter") in keymap.LEGACY_SZ_KEYS:
            if isinstance(fav.get("entry_index"), int):
                fav["entry_index"] += shift.get(fav["letter"], 0)
            fav["letter"] = "ß"
    blacklist = settings.get("blacklist")
    if isinstance(blacklist, dict):
        for legacy in keymap.LEGACY_SZ_KEYS:
            if legacy in blacklist:
                words = list(blacklist.get("ß") or [])
                words += [w for w in (blacklist.pop(legacy) or []) if w not in words]
                blacklist["ß"] = words


def _migrate_to_tracks_v2():
    """Migrate legacy {sound: "x.mp3"} entries → {tracks: {KIND: {...}}}.

    Letters migrate legacy sound → FX (sound effects).
    Numbers migrate legacy sound → DE (German TTS).
    Each set also gets a default track_order if missing.
    Runs at most once per entry — once `tracks` exists, this is a no-op.
    """
    changed = False
    sets = settings.get("sets", {})
    for set_id, set_data in sets.items():
        # Set-level: track_order
        if "track_order" not in set_data:
            set_data["track_order"] = [dict(c) for c in DEFAULT_TRACK_ORDER]
            changed = True
        # Letters
        for letter_cfg in set_data.get("letters", {}).values():
            for entry in letter_cfg.get("entries", []):
                had_tracks = "tracks" in entry
                _normalize_letter_entry(entry)
                if entry.get("sound"):
                    _strip_legacy_sound(entry)
                    changed = True
                elif not had_tracks:
                    # Blank entry, just added tracks:{} — record it but no data change
                    pass
        # Numbers
        for digit_cfg in set_data.get("numbers", {}).values():
            had_tracks = "tracks" in digit_cfg
            _normalize_number_entry(digit_cfg)
            if digit_cfg.get("sound"):
                _strip_legacy_sound(digit_cfg)
                changed = True
    # tts_voices default block
    if "tts_voices" not in settings:
        settings["tts_voices"] = dict(DEFAULT_TTS_VOICES)
        changed = True
    if changed:
        try:
            save_settings()
            print("[migrate] tracks v2: settings.json rewritten", flush=True)
        except Exception as e:
            print(f"[migrate] failed: {e}", flush=True)


def _ensure_numbers_defaults():
    """Seed `numbers` block into every set if missing. Ships metadata only — sound/image files are generated later via the web UI."""
    changed = False
    for set_data in settings.get("sets", {}).values():
        nums = set_data.get("numbers")
        if nums is None:
            set_data["numbers"] = {}
            nums = set_data["numbers"]
            changed = True
        for digit, default in DEFAULT_NUMBERS.items():
            if digit not in nums:
                nums[digit] = {
                    "word": default["word"],
                    "image_subject": default["image_subject"],
                    "sound": "",
                    "backgrounds": [],
                }
                changed = True
    if "numbers_tts" not in settings:
        settings["numbers_tts"] = {"voice_id": DEFAULT_NUMBER_VOICE_ID}
        changed = True
    if changed:
        try:
            save_settings()
        except Exception as e:
            print(f"[settings] failed to persist numbers defaults: {e}", flush=True)


def _migrate_to_sets():
    """Migrate old flat {letters: {A: {words, sounds, image}}} to sets format."""
    old = settings.pop("letters", {})
    new_letters = {}
    for letter, cfg in old.items():
        entries = []
        words = cfg.get("words", [])
        sounds = cfg.get("sounds", [])
        image = cfg.get("image", "")
        if words:
            for i, word in enumerate(words):
                entries.append({
                    "word": word,
                    "sound": sounds[i] if i < len(sounds) else (sounds[0] if sounds else ""),
                    "image": image,
                    "enabled": i == 0,
                })
        new_letters[letter] = {"entries": entries}
    settings["sets"] = {
        "default": {
            "name": "Standard",
            "description": "Migriert aus alter Konfiguration",
            "letters": new_letters,
        }
    }
    settings["active_set"] = "default"
    save_settings()


def save_settings():
    """Write settings.json. Everything is serialized *before* the file is touched.

    A lone Unicode surrogate from some request cannot be written as UTF-8: the
    file then uses ASCII escapes, which json.load reads back identically
    (FUNK-43 review). The write itself is crash-safe where the mount allows it
    (see _write_settings_bytes, FUNK-67).
    """
    with _settings_write_lock:              # the asyncio thread and the keyboard thread both save
        try:
            payload = (json.dumps(settings, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        except UnicodeEncodeError:
            payload = (json.dumps(settings, indent=2, ensure_ascii=True) + "\n").encode("ascii")
        _write_settings_bytes(payload)


_settings_write_lock = threading.RLock()   # serialize + write as one step: the newest snapshot is written last
_settings_write_mode = None     # atomicwrite.RENAME or IN_PLACE: found on the first save, logged once


def _write_settings_bytes(payload):
    """Replace settings.json with payload, crash-safe where the mount allows it (FUNK-67).

    atomicwrite renames a fsynced temp file over it. On a single-file bind mount
    (hsb1 before FUNK-67) that is impossible, and it is rewritten in place as
    before; this process then stays in that mode.
    """
    global _settings_write_mode
    with _settings_write_lock:
        mode, why = atomicwrite.replace(SETTINGS_PATH, payload, in_place=_settings_write_mode == atomicwrite.IN_PLACE)
        if mode != _settings_write_mode:
            how = ("atomically (temp file + rename)" if mode == atomicwrite.RENAME
                   else f"in place: it cannot be renamed over ({why.strerror if why else 'single-file mount'})")
            print(f"[settings] settings.json is written {how}", flush=True)
        _settings_write_mode = mode


def get_active_set():
    """Return the active set's letters dict."""
    set_id = settings.get("active_set", "")
    sets = settings.get("sets", {})
    if set_id in sets:
        return sets[set_id].get("letters", {})
    # Fallback to first set
    if sets:
        return next(iter(sets.values())).get("letters", {})
    return {}


def get_enabled_entries(letter):
    """Get enabled entries for a letter in the active set."""
    letters = get_active_set()
    letter_cfg = letters.get(letter, {})
    return [e for e in letter_cfg.get("entries", []) if e.get("enabled", True)]


def _build_flat_playlist():
    """Build flat list of (letter, entry_index, entry) from A-Z, enabled entries only."""
    letters = get_active_set()
    playlist = []
    for letter in sorted(letters.keys()):
        for i, entry in enumerate(get_enabled_entries(letter)):
            playlist.append((letter, i, entry))
    return playlist


def _flat_pos_for(letter, entry_index=0):
    """Find flat playlist position for a given letter + entry index."""
    playlist = _build_flat_playlist()
    for i, (l, ei, _) in enumerate(playlist):
        if l == letter and ei == entry_index:
            return i
    # Letter not found — find first entry of this letter
    for i, (l, _, _) in enumerate(playlist):
        if l == letter:
            return i
    return 0


def _navigate_flat(delta):
    """Move through the flat playlist by delta (+1 = right, -1 = left). Wraps around."""
    global flat_pos, last_letter, cycle_index
    playlist = _build_flat_playlist()
    if not playlist:
        return

    # If no position yet, start at beginning (right) or end (left)
    if flat_pos < 0:
        flat_pos = 0 if delta > 0 else len(playlist) - 1

    new_pos = (flat_pos + delta) % len(playlist)
    flat_pos = new_pos
    letter, entry_index, entry = playlist[flat_pos]
    last_letter = letter
    cycle_index[letter] = entry_index
    _play_entry(letter, entry, entry_index)


def _navigate_letter(delta):
    """Jump to next (+1) or previous (-1) letter's first entry. Wraps A↔Z."""
    global flat_pos, last_letter, cycle_index
    playlist = _build_flat_playlist()
    if not playlist:
        return

    # Build ordered list of unique letters in the playlist
    seen = set()
    letter_order = []
    for l, _, _ in playlist:
        if l not in seen:
            seen.add(l)
            letter_order.append(l)

    if not letter_order:
        return

    # Find current letter's position in the letter list
    if last_letter in letter_order:
        cur = letter_order.index(last_letter)
    else:
        cur = 0 if delta > 0 else len(letter_order) - 1

    target_letter = letter_order[(cur + delta) % len(letter_order)]

    # Jump to first entry of that letter
    for i, (l, ei, entry) in enumerate(playlist):
        if l == target_letter:
            flat_pos = i
            last_letter = l
            cycle_index[l] = ei
            _play_entry(l, entry, ei)
            return


# Every stop and every new sequence bumps the generation: a sequence plays only while its
# generation is current. (A shared abort flag let a cut-short sequence play its next track
# once a newer sequence had reset the flag.) FUNK-39
_play_gen = 0
_play_lock = threading.Lock()


def stop_all_sounds():
    """Stop every sound. Returns the new generation: a sequence started right after owns it."""
    global _play_gen
    with _play_lock:
        _play_gen += 1
        gen = _play_gen
        procs = active_processes[:]
    for proc in procs:
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            active_processes.remove(proc)
        except ValueError:
            pass
    return gen


def play_sound_sequence(paths, gap_ms=120, on_done=None):
    """Play sound files back-to-back in a daemon thread. Stops when a newer sound starts.

    `paths` accepts either raw path strings (uses global volume) or
    (path, per_track_volume_pct) tuples — the per-track pct multiplies the
    global volume so users can trim quiet/loud clips individually.

    `gap_ms` is a small silence inserted between tracks so FX doesn't step on TTS.

    `on_done()` (FUNK-39) is called once when the sequence has ended, however it
    ended: played through, failed, or cut short by stop_all_sounds or a newer sound.
    """
    # the generation comes with the stop, atomically: two sequences started at once never share one
    gen = stop_all_sounds()
    # Normalize to list of (path, vol_pct) tuples
    norm = []
    for p in paths or ():
        if isinstance(p, (tuple, list)):
            norm.append((p[0], int(p[1]) if len(p) > 1 else 100))
        else:
            norm.append((p, 100))
    if not norm:
        if on_done:
            threading.Thread(target=on_done, daemon=True).start()
        return
    env = os.environ.copy()
    try:
        subprocess.run(
            ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "100%"],
            env=env, timeout=2, capture_output=True,
        )
    except Exception:
        pass

    def _runner(file_list):
        try:
            for i, (p, vol_pct) in enumerate(file_list):
                if _play_gen != gen:
                    return
                # global × per-track scaling. paplay --volume units: 65536 = 100%.
                # Go up to 200% per track (131072); paplay clamps safely.
                pa_vol = int(current_volume / 100 * vol_pct / 100 * 65536)
                try:
                    proc = subprocess.Popen(
                        ["paplay", f"--volume={pa_vol}", str(p)],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        env=env,
                    )
                except Exception as e:
                    print(f"[sound] sequence error on {p}: {e}", flush=True)
                    return
                with _play_lock:
                    current = _play_gen == gen
                    if current:
                        active_processes.append(proc)
                if not current:          # stopped between start and registration
                    proc.terminate()
                    return
                proc.wait()
                try:
                    active_processes.remove(proc)
                except ValueError:
                    pass
                if _play_gen != gen:
                    return
                if i < len(file_list) - 1 and gap_ms > 0:
                    time.sleep(gap_ms / 1000.0)
        finally:
            if on_done:
                try:
                    on_done()
                except Exception as e:
                    print(f"[sound] on_done error: {e}", flush=True)

    threading.Thread(target=_runner, args=(norm,), daemon=True).start()


def play_sound(sound_file):
    global active_processes, current_volume
    if not os.path.exists(sound_file):
        print(f"[sound] Not found: {sound_file}", flush=True)
        return
    stop_all_sounds()
    pa_vol = int(current_volume / 100 * 65536)
    env = os.environ.copy()
    # Pin PipeWire sink to 100% before every play — prevents drift from affecting output.
    # Node-RED babycam is unaffected (controls VLC internal volume via telnet, not the sink).
    try:
        subprocess.run(
            ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "100%"],
            env=env, timeout=2, capture_output=True,
        )
    except Exception:
        pass  # non-fatal — paplay still works, just at whatever sink level
    try:
        proc = subprocess.Popen(
            ["paplay", f"--volume={pa_vol}", str(sound_file)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env,
        )
        active_processes.append(proc)
        # Quick check if it failed immediately
        import time as _t
        _t.sleep(0.1)
        if proc.poll() is not None:
            _, stderr = proc.communicate()
            if stderr:
                print(f"[sound] paplay error: {stderr.decode()[:200]}", flush=True)
            else:
                print(f"[sound] paplay exited with code {proc.returncode}", flush=True)
    except Exception as e:
        print(f"[sound] Popen error: {e}", flush=True)


def change_volume(delta):
    global current_volume
    current_volume = max(0, min(100, current_volume + delta))
    settings["volume"] = current_volume
    save_settings()  # Persist volume changes from keyboard
    print(f"[volume] {current_volume}%", flush=True)
    sse_broadcast("volume", {"volume": current_volume})
    if display:
        display.publish_volume(current_volume)


def _play_entry(key_name, entry, entry_index=0):
    """Play an entry's tracks in the active set's configured order."""
    _normalize_letter_entry(entry)
    word = entry.get("word", key_name)
    image = entry.get("image", "")

    files = _collect_playable_files(entry)
    first_sound = os.path.basename(files[0][0]) if files else ""
    print(f"[key] {key_name} → {word} ({len(files)} tracks, image={image})", flush=True)

    try:
        sse_broadcast("keypress", {
            "letter": key_name, "word": word, "sound": first_sound, "image": image,
            "entry_index": entry_index, "timestamp": time.time(),
        })
    except Exception as e:
        print(f"[sse] broadcast error: {e}", flush=True)

    if files:
        play_sound_sequence(files)
    if display:
        display.publish_letter(key_name, word, image)


def _replay_last():
    """Replay the last played sound without advancing the cycle."""
    if not last_letter:
        return
    entries = get_enabled_entries(last_letter)
    if not entries:
        return
    idx = cycle_index.get(last_letter, 0) % len(entries)
    _play_entry(last_letter, entries[idx], idx)


def get_active_numbers():
    """Return the active set's numbers dict."""
    set_id = settings.get("active_set", "")
    return settings.get("sets", {}).get(set_id, {}).get("numbers", {})


def get_enabled_backgrounds(digit):
    """Return enabled backgrounds for a digit (cycled on repeated presses)."""
    cfg = get_active_numbers().get(digit, {})
    return [b for b in cfg.get("backgrounds", []) if b.get("enabled", True)]


def _play_number(digit):
    """Play a number entry — tracks in set order, cycling background on repeat."""
    global number_cycle_index, last_number
    cfg = get_active_numbers().get(digit)
    if not cfg:
        print(f"[num] {digit}: no config", flush=True)
        return
    _normalize_number_entry(cfg)
    word = cfg.get("word", digit)
    bgs = get_enabled_backgrounds(digit)
    if bgs:
        if digit == last_number:
            idx = (number_cycle_index.get(digit, 0) + 1) % len(bgs)
        else:
            idx = number_cycle_index.get(digit, 0) % len(bgs)
        number_cycle_index[digit] = idx
        image = bgs[idx].get("image", "")
    else:
        idx = 0
        image = ""
    last_number = digit
    files = _collect_playable_files(cfg)
    first_sound = os.path.basename(files[0][0]) if files else ""
    print(f"[num] {digit} → {word} ({len(files)} tracks, image={image})", flush=True)
    try:
        sse_broadcast("keypress", {
            "letter": digit, "word": word, "sound": first_sound, "image": image,
            "entry_index": idx, "timestamp": time.time(),
        })
    except Exception as e:
        print(f"[sse] broadcast error: {e}", flush=True)
    if files:
        play_sound_sequence(files)
    if display:
        display.publish_letter(digit, word, image)


# Validated custom profiles, rebuilt on demand. Key presses read it from the listener
# thread, so rebuilding and every write of settings["keyboard_profiles"] hold the same
# lock: a rebuild never sees a half-written dict, and a write always clears whatever a
# concurrent rebuild produced (FUNK-63 review).
_customs_lock = threading.Lock()
_customs_cache = None


def _custom_profiles():
    """Valid custom keyboard profiles from settings (FUNK-63): {id: profile}. Read-only: copy to change."""
    global _customs_cache
    with _customs_lock:
        if _customs_cache is None:
            _customs_cache = profiles.customs(settings.get("keyboard_profiles"))
        return _customs_cache


def _write_customs(change):
    """Apply change(stored_profiles_dict) under the lock and drop the cache."""
    global _customs_cache
    with _customs_lock:
        change(settings.setdefault("keyboard_profiles", {}))
        _customs_cache = None


def _language():
    """Keyboard language (FUNK-64): settings["language"], migrated from keyboard_layout."""
    return profiles.language_of(settings)


def _default_profile():
    """What "Standard" means: the generic profile of language × PC/Mac × number pad."""
    return profiles.default_id(_language(), profiles.family_of(settings), profiles.numpad_of(settings))


def _selected_profile():
    """The profile choice as stored: a profile id or "default" (FUNK-64)."""
    return settings.get("keyboard_profile") or keymap.DEFAULT_PROFILE


def _keymap_profile():
    """The active keyboard profile id; an unknown or deleted one falls back to the BK03."""
    pid = _selected_profile()
    if pid == profiles.DEFAULT_ALIAS:
        pid = _default_profile()
    return pid if profiles.get(pid, _custom_profiles()) else keymap.DEFAULT_PROFILE


def _active_keymap():
    """The key map of the active keyboard profile (FUNK-62): {key_id: action}."""
    return (settings.get("keymaps") or {}).get(_keymap_profile()) or {}


def _resolve_press(key_name, raw_key, mods=()):
    """(key_id, matched_id, action) for a press. Shift+digit stays the favourite shortcut."""
    kid = keymap.key_id(raw_key, mods)
    if key_name.startswith("SHIFT_"):      # Shift alone is ignored by keymap.resolve (RESERVED)
        return kid, None, None
    matched, action = keymap.resolve(_active_keymap(), raw_key, mods)
    return kid, matched, action


def handle_key(key_name, raw_key=None, mods=(), virtual=False):
    """Handle a key press: a key-map entry wins (FUNK-62), else the built-in behaviour.

    ``virtual`` presses (test keyboard, /api/press) behave the same, but never
    confirm a guessed key code: only the real keyboard can do that (FUNK-63).
    """
    raw_key = raw_key or key_name
    if time.time() - startup_time < STARTUP_GRACE_SECONDS:
        return
    kid, matched, action = _resolve_press(key_name, raw_key, mods)
    has_content = bool(get_enabled_entries(key_name)) if action is None and len(key_name) <= 2 else False
    unassigned = keymap.is_unassigned(key_name, raw_key, action, has_content)
    print(f"[key] handle_key({key_name}, raw={raw_key}, id={kid})"
          + (f" → keymap {matched}: {keymap.describe(action)}" if action else "")
          + (" (no function)" if unassigned else ""), flush=True)
    if not virtual:
        _remember(keyboard_seen, kid)
    if unassigned:
        _remember(keymap_seen_unassigned, kid)
    sse_broadcast("rawkey", {
        "key": key_name, "raw": raw_key, "id": kid, "mods": list(keymap.canonical_mods(mods)),
        "matched": matched, "action": action, "describe": keymap.describe(action),
        "default": keymap.default_label(key_name, has_content), "unassigned": unassigned,
        "virtual": virtual, "timestamp": time.time(),
    })
    if not virtual:                     # FUNK-42: any real key keeps the play session going
        _progress_key()
    if quiz_engine.active:              # FUNK-39: the quiz owns the keyboard while it runs
        _quiz_key(key_name, action, virtual)
        return
    if not virtual:                     # FUNK-40: the child's presses; test keys do not count
        _progress_press(key_name, action)
    if action is not None:
        _run_keymap_action(matched, action)
        return
    _handle_builtin(key_name)


def _handle_builtin(key_name):
    """Today's built-in key behaviour (unchanged by FUNK-62)."""
    global last_letter, cycle_index, favorites, flat_pos

    # Space = stop
    if key_name == "SPACE":
        stop_all_sounds()
        sse_broadcast("keypress", {"letter": "SPACE", "word": "Stop", "sound": "", "image": "", "entry_index": 0, "timestamp": time.time()})
        return

    # Volume
    if key_name in ("EQUAL", "KPPLUS"):
        change_volume(10)
        return
    if key_name in ("MINUS", "KPMINUS"):
        change_volume(-10)
        return

    # Enter = replay last sound
    if key_name == "ENTER":
        _replay_last()
        return

    # Tab = toggle favorite for last played letter
    if key_name == "TAB":
        _toggle_favorite()
        return

    # Arrow keys: LEFT/RIGHT = step through all sounds, UP/DOWN = jump by letter
    if key_name == "RIGHT":
        _navigate_flat(+1)
        return
    if key_name == "LEFT":
        _navigate_flat(-1)
        return
    if key_name == "DOWN":
        _navigate_letter(+1)
        return
    if key_name == "UP":
        _navigate_letter(-1)
        return

    # Shift+digit = play favorite (1..9 → 0..8, 0 → 9)
    if key_name.startswith("SHIFT_") and key_name[6:] in NUMBER_KEYS:
        d = key_name[6:]
        fav_idx = int(d) - 1 if d != "0" else 9
        _play_favorite(fav_idx)
        return

    # Digit alone = play number entry (word + sound + cycling bg)
    if key_name in NUMBER_KEYS:
        _play_number(key_name)
        return

    # Get enabled entries for this letter
    entries = get_enabled_entries(key_name)

    if not entries:
        if settings.get("random_sounds_enabled"):
            _play_random_sound()
        return

    # Position memory: advance index if same letter, keep position if returning
    if key_name == last_letter:
        idx = (cycle_index.get(key_name, 0) + 1) % len(entries)
    else:
        # Keep last position for this letter (don't reset!)
        idx = cycle_index.get(key_name, 0) % len(entries)
    cycle_index[key_name] = idx
    last_letter = key_name
    flat_pos = _flat_pos_for(key_name, idx)

    _play_entry(key_name, entries[idx], idx)


def _play_random_sound():
    """Play one random enabled track of the active set."""
    all_sounds = []
    for ldata in get_active_set().values():
        for e in ldata.get("entries", []):
            if not e.get("enabled"):
                continue
            _normalize_letter_entry(e)
            for t in (e.get("tracks") or {}).values():
                if t.get("enabled", True) and t.get("file"):
                    p = os.path.join(SOUNDS_DIR, t["file"])
                    if os.path.exists(p):
                        all_sounds.append(p)
    if all_sounds:
        play_sound_sequence([random.choice(all_sounds)])


# ── Key map actions (FUNK-62) ────────────────────────────────────────────────

keymap_seen_unassigned = {}   # key_id → last press time: pressed, but no function (in memory)
keyboard_seen = {}            # key_id → last physical press time: confirms guessed codes (FUNK-63)
SEEN_MAX = 300                # /api/press can invent ids: keep both maps bounded


def _remember(seen, kid):
    seen.pop(kid, None)
    seen[kid] = time.time()
    while len(seen) > SEEN_MAX:
        seen.pop(next(iter(seen)))
_muted_from = None            # volume before "Stumm" (func mute)


def _toggle_mute():
    global _muted_from
    if current_volume > 0:
        _muted_from = current_volume
        stop_all_sounds()          # a running paplay keeps its own volume: silence it now
        change_volume(-current_volume)
    else:
        change_volume((_muted_from or 50) - current_volume)
        _muted_from = None


def _activate_next_set():
    global cycle_index, last_letter
    ids = list(settings.get("sets", {}))
    if len(ids) < 2:
        return
    cur = settings.get("active_set")
    nxt = ids[(ids.index(cur) + 1) % len(ids)] if cur in ids else ids[0]
    settings["active_set"] = nxt
    cycle_index = {}
    last_letter = None
    save_settings()
    name = settings["sets"][nxt].get("name", nxt)
    print(f"[keymap] next set → {nxt} ({name})", flush=True)
    sse_broadcast("active_set", {"active_set": nxt, "name": name})
    sse_broadcast("keypress", {"letter": "★", "word": name, "sound": "", "image": "", "entry_index": 0, "timestamp": time.time()})


_KEYMAP_FUNCS = {
    "volup": lambda: change_volume(10),
    "voldown": lambda: change_volume(-10),
    "mute": _toggle_mute,
    "stop": lambda: _handle_builtin("SPACE"),
    "replay": lambda: _replay_last(),
    "fav": lambda: _toggle_favorite(),
    "nextset": _activate_next_set,
    "random": lambda: _play_random_sound(),
    "prevsound": lambda: _navigate_flat(-1),
    "nextsound": lambda: _navigate_flat(+1),
    "prevletter": lambda: _navigate_letter(-1),
    "nextletter": lambda: _navigate_letter(+1),
    "quiz": lambda: _quiz_toggle(),
}


def _run_keymap_action(kid, action):
    kind = action.get("action")
    if kind == "none":
        return
    if kind == "letter":
        _handle_builtin(action["letter"])
        return
    if kind == "func":
        fn = _KEYMAP_FUNCS.get(action.get("func"))
        if fn:
            fn()
        return
    if kind in ("sound", "imgsound"):
        path = os.path.join(SOUNDS_DIR, action.get("file", ""))
        image = action.get("image", "") if kind == "imgsound" else ""
        word = action.get("word", "") if kind == "imgsound" else ""
        if os.path.exists(path):
            play_sound_sequence([(path, action.get("volume", 100))])
        else:
            print(f"[keymap] {kid}: sound file missing: {action.get('file')}", flush=True)
        sse_broadcast("keypress", {"letter": "", "word": word or kid, "sound": action.get("file", ""),
                                   "image": image, "entry_index": 0, "timestamp": time.time()})
        if display and kind == "imgsound":
            display.publish_letter("", word, image)


# ── Progress per letter (FUNK-40) ────────────────────────────────────────────
# The counters live in progress.py (pure, own lock). This feeds them presses and
# quiz results from the real keyboard, keeps settings["progress"] in step and
# pushes changes to the parent page.

PROGRESS_SAVE_SECONDS = 10    # counts reach settings.json at most this long after a press
PROGRESS_SSE_SECONDS = 1      # the parent page gets changes at most this often

letter_progress = progress.Progress(keymap.LETTER_TARGETS)
_progress_saved_rev = 0       # the rev that settings.json holds


def _progress_key():
    """A key from the real keyboard, counted or not: the play session goes on (FUNK-42)."""
    try:
        letter_progress.key()
    except Exception as e:          # counting must never break a key press
        print(f"[progress] key not counted: {e}", flush=True)


def _progress_press(key_name, action):
    """Count a press outside the quiz: its letter or digit (a key-map letter key counts for
    the letter it plays). Other keys are not counted."""
    if action is None:
        letter = key_name
    elif action.get("action") == "letter":
        letter = action.get("letter")
    else:
        return
    try:
        letter_progress.press(letter)
    except Exception as e:          # counting must never break a key press
        print(f"[progress] press not counted: {e}", flush=True)


def _progress_quiz_result(letter, right, first_try, origin):
    """quiz.Quiz result hook: a question ended. Only answers from the real keyboard count."""
    if origin != "keyboard":
        return
    try:
        letter_progress.quiz_result(letter, right, first_try)
    except Exception as e:
        print(f"[progress] quiz result not counted: {e}", flush=True)


def _progress_save():
    """Write the counters to settings.json if they changed since the last write.

    Export, assign and write happen under the settings write lock, so of two
    writers the later one always writes the newer counts. Returns True if written.
    """
    global _progress_saved_rev
    with _settings_write_lock:
        rev, data = letter_progress.export()
        if rev == _progress_saved_rev:
            return False
        settings["progress"] = data
        save_settings()
        _progress_saved_rev = rev
        return True


def _progress_tick(state, day, now):
    """One round of the progress loop. Push a change to the parent page (SSE "progress"),
    also a new day, so an open page does not keep yesterday's "heute"; save at most every
    PROGRESS_SAVE_SECONDS. ``state`` carries what was sent and when it was saved."""
    letter_progress.roll_day()          # also while nobody plays: drop past days, new rev for the new day
    rev = letter_progress.rev
    if rev != state["sent"] or day != state["day"]:
        state["sent"], state["day"] = rev, day
        sse_broadcast("progress", letter_progress.snapshot())
    if rev != _progress_saved_rev and now - state["saved_at"] >= PROGRESS_SAVE_SECONDS:
        state["saved_at"] = now
        _progress_save()


def _progress_loop():
    state = {"sent": letter_progress.rev, "day": time.strftime("%Y-%m-%d"), "saved_at": time.monotonic()}
    while True:
        time.sleep(PROGRESS_SSE_SECONDS)
        try:
            _progress_tick(state, time.strftime("%Y-%m-%d"), time.monotonic())
        except Exception as e:      # a full disk must not end the loop: the next round retries
            print(f"[progress] not saved: {e}", flush=True)


def _progress_start():
    """At start: take over the saved counters and start the loop."""
    letter_progress.load(settings.get("progress"))
    settings["progress"] = letter_progress.export()[1]     # the key exists from now on (only replaced, never added)
    threading.Thread(target=_progress_loop, daemon=True, name="progress").start()


# ── Quiz (FUNK-39) ───────────────────────────────────────────────────────────
# The game logic lives in quiz.py (a pure state machine); this wires it to the
# active set, the speakers, the Pixoo, SSE and settings.json.

QUIZ_SOUNDS_DIR = os.path.join(tempfile.gettempdir(), "funkeykid-quiz")
QUIZ_COLORS = {"question": "#FFFFFF", "right": "#00CC44", "revealed": "#FF9900", "done": "#FFCC00"}
# built-in keys that keep a role during the quiz (layout-translated names → function)
QUIZ_BUILTIN_KEYS = {"ENTER": "replay", "SPACE": "stop", "EQUAL": "volup", "KPPLUS": "volup",
                     "MINUS": "voldown", "KPMINUS": "voldown"}


def _write_quiz_jingles():
    """The quiz's built-in jingles as WAV files (synthesized, so no assets are needed)."""
    os.makedirs(QUIZ_SOUNDS_DIR, exist_ok=True)
    for name in quiz.JINGLES:
        path = os.path.join(QUIZ_SOUNDS_DIR, f"{name}.wav")
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "wb") as f:
            f.write(quiz.jingle_wav(name))
        os.replace(tmp, path)


def _quiz_alphabet():
    """The letters a quiz may ask: A–Z, and Ä Ö Ü ß on a German keyboard."""
    return keymap.LETTERS if _language() == "de" else tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def _quiz_clip(tracks, kind):
    t = tracks.get(kind)
    if not isinstance(t, dict) or not t.get("enabled", True) or not t.get("file"):
        return None
    path = os.path.join(SOUNDS_DIR, t["file"])
    if not os.path.isfile(path):
        return None
    try:
        vol = max(0, min(200, int(t.get("volume", 100))))
    except (TypeError, ValueError):
        vol = 100
    return (path, vol)


def _quiz_entry(e):
    _normalize_letter_entry(e)
    tracks = e.get("tracks") or {}
    clips = {"word": _quiz_clip(tracks, "DE"), "sound": _quiz_clip(tracks, "FX")}
    files = _collect_playable_files(e)
    if not (clips["word"] or clips["sound"]) or not files:
        return None
    return {"word": e.get("word", ""), "image": e.get("image", ""), "clips": clips, "files": files}


def _quiz_pool(letters):
    """{letter: [entry]} of the active set: enabled entries with a word or sound file."""
    allowed = set(letters or ())
    pool = {}
    for letter in _quiz_alphabet():
        if allowed and letter not in allowed:
            continue
        entries = [x for x in (_quiz_entry(e) for e in get_enabled_entries(letter)) if x]
        if entries:
            pool[letter] = entries
    return pool


def _quiz_play(clips, on_done):
    paths = [(os.path.join(QUIZ_SOUNDS_DIR, f"{c}.wav"), 100) if isinstance(c, str) else c for c in clips]
    play_sound_sequence(paths, on_done=on_done)


def _quiz_show(kind, letter, word, image):
    # FUNK-41 adds the Pixoo reward animation here (kind == "right" / "done")
    print(f"[quiz] {kind}: {letter} {word}".rstrip(), flush=True)
    if display:
        try:
            display.publish_letter(letter, word, image or None, color=QUIZ_COLORS[kind])
        except Exception as e:
            print(f"[quiz] display error: {e}", flush=True)


def _quiz_record(summary):
    try:
        history = settings.get("quiz_history")
        settings["quiz_history"] = ([summary] + (history if isinstance(history, list) else []))[:quiz.HISTORY_MAX]
        save_settings()
    except Exception as e:      # the session is over either way
        print(f"[quiz] history not saved: {e}", flush=True)


def _quiz_schedule(seconds, fn, *args):
    t = threading.Timer(seconds, fn, args)
    t.daemon = True
    t.start()
    return t


quiz_engine = quiz.Quiz(pool=_quiz_pool, play=_quiz_play, show=_quiz_show,
                        broadcast=lambda snap: sse_broadcast("quiz", snap),
                        record=_quiz_record, schedule=_quiz_schedule, result=_progress_quiz_result)


def _quiz_config():
    """The saved quiz settings, completed with defaults."""
    try:
        return quiz.validate_config(settings.get("quiz"), keymap.LETTERS)
    except quiz.QuizError:
        return dict(quiz.DEFAULT_CONFIG)


def _quiz_toggle():
    """Key-map function "quiz": start with the saved settings, or stop."""
    if quiz_engine.active:
        quiz_engine.stop()
        return
    try:
        quiz_engine.start(_quiz_config())
    except quiz.QuizError as e:
        print(f"[quiz] not started: {e}", flush=True)


def _quiz_key(key_name, action, virtual=False):
    """A key press while the quiz runs: letters answer; Enter repeats, Space stops the
    sound, volume keys work; every other key stays silent (no noise over the question).
    Only answers from the real keyboard count for the progress (FUNK-40)."""
    quiz_engine.touch()
    kind = (action or {}).get("action")
    letter = None
    if kind == "letter":
        letter = action.get("letter")
    elif action is None and key_name in keymap.LETTERS:
        letter = key_name
    if letter is not None:
        if letter in keymap.LETTERS:
            result = quiz_engine.answer(letter, "test" if virtual else "keyboard")
            print(f"[quiz] answer {letter}: {result}", flush=True)
        return
    if kind == "func":
        func = action.get("func")
    elif action is None:
        func = QUIZ_BUILTIN_KEYS.get(key_name)
    else:
        func = None                     # sound, picture or "none" keys: silent during the quiz
    if func == "replay":
        quiz_engine.repeat()
    elif func == "stop":
        stop_all_sounds()
    elif func == "quiz":
        quiz_engine.stop()
    elif func in ("volup", "voldown", "mute"):
        _KEYMAP_FUNCS[func]()


def _toggle_favorite():
    """Toggle favorite for the last played letter+entry."""
    global favorites
    if not last_letter:
        return
    idx = cycle_index.get(last_letter, 0)
    fav = {"letter": last_letter, "entry_index": idx}
    # Check if already favorited
    for i, f in enumerate(favorites):
        if f["letter"] == last_letter and f["entry_index"] == idx:
            favorites.pop(i)
            _save_favorites()          # a removal used to last only until the next restart
            sse_broadcast("favorites", {"favorites": favorites})
            print(f"[fav] Removed favorite: {last_letter}[{idx}]", flush=True)
            return
    if len(favorites) >= 10:
        sse_broadcast("keypress", {"letter": "!", "word": "Max 10 Favoriten", "sound": "", "image": "", "entry_index": 0, "timestamp": time.time()})
        return
    favorites.append(fav)
    _save_favorites()
    sse_broadcast("favorites", {"favorites": favorites})
    entries = get_enabled_entries(last_letter)
    word = entries[idx]["word"] if idx < len(entries) else "?"
    print(f"[fav] Added favorite #{len(favorites)}: {last_letter} → {word}", flush=True)


def _play_favorite(fav_idx):
    """Play a favorite by number (0-9)."""
    if fav_idx >= len(favorites):
        return
    fav = favorites[fav_idx]
    entries = get_enabled_entries(fav["letter"])
    idx = fav["entry_index"]
    if idx < len(entries):
        _play_entry(fav["letter"], entries[idx], idx)


def _save_favorites():
    """Persist favorites to settings."""
    settings["favorites"] = favorites
    save_settings()


def _load_favorites():
    """Load favorites from settings."""
    global favorites
    favorites = settings.get("favorites", [])


# ── SSE (Server-Sent Events) ──────────────────────────────────────────────────

def sse_broadcast(event_type, data):
    """Broadcast an event to all connected SSE clients. Thread-safe."""
    if not _loop or not sse_clients:
        return
    msg = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    for q in list(sse_clients):
        try:
            _loop.call_soon_threadsafe(q.put_nowait, msg)
        except Exception:
            pass


async def api_sse(request):
    """SSE endpoint — streams real-time events to the web UI."""
    resp = web.StreamResponse()
    resp.content_type = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    await resp.prepare(request)

    q = asyncio.Queue()
    sse_clients.add(q)

    # Send initial status immediately
    status = _keyboard_status()
    status["volume"] = current_volume
    status["mqtt_connected"] = display._mqtt_connected if display else False
    await resp.write(f"event: status\ndata: {json.dumps(status)}\n\n".encode())

    try:
        while True:
            msg = await q.get()
            await resp.write(msg.encode())
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        sse_clients.discard(q)

    return resp


# ── Web API ──────────────────────────────────────────────────────────────────

async def api_get_status(request):
    status = _keyboard_status()
    status["volume"] = current_volume
    status["mqtt_connected"] = display._mqtt_connected if display else False
    status["active_set"] = settings.get("active_set", "")
    return web.json_response(status)


async def api_get_diagnostics(request):
    """Detailed keyboard + BT diagnostics for troubleshooting."""
    diag = keyboard.get_diagnostics() if keyboard else {"error": "no keyboard listener"}
    diag = dict(diag)
    km = _active_keymap()
    seen = [kid for kid, _ in sorted(keymap_seen_unassigned.items(), key=lambda kv: -kv[1]) if kid not in km]
    diag["keymap_unassigned_seen"] = seen     # pressed, but no function (FUNK-62)
    if seen and isinstance(diag.get("logs"), list):
        diag["logs"] = diag["logs"] + [f"Gedrückt ohne Wirkung: {', '.join(seen)}"]
    return web.json_response(diag)


async def api_reconnect_keyboard(request):
    """Restart the keyboard listener thread to pick up newly connected devices."""
    if keyboard:
        await asyncio.get_running_loop().run_in_executor(None, keyboard.restart)
        return web.json_response({"ok": True, "message": "Keyboard listener restarted"})
    return web.json_response({"error": "No keyboard listener"}, status=500)


async def api_get_settings(request):
    return web.json_response(settings)


async def api_put_settings(request):
    global settings
    data = await request.json()
    # Only update top-level config keys, not sets (those have own endpoints).
    # active_set changes only via /api/sets/{id}/activate or the key map: a stale
    # browser copy must never switch the set back (FUNK-62 review).
    # audio_normalize* were missing here since FUNK-1, so the UI toggle never persisted.
    # keyboard_layout follows the language (PUT /api/language, FUNK-64): never written here
    for k in ("keyboard_device", "volume", "random_sounds_enabled",
              "display_mode", "pixoo_ip", "debounce_seconds", "mqtt", "ai_prompts",
              "numbers_tts", "battery_low_threshold", "audio_normalize", "audio_normalize_lufs"):
        if k in data:
            settings[k] = data[k]
    save_settings()
    _reload_runtime()
    return web.json_response({"ok": True})


# ── Sets CRUD ────────────────────────────────────────────────────────────────

async def api_get_sets(request):
    """List all sets with metadata (not full letter data)."""
    sets = settings.get("sets", {})
    result = {}
    for sid, s in sets.items():
        letter_count = len(s.get("letters", {}))
        entry_count = sum(len(l.get("entries", [])) for l in s.get("letters", {}).values())
        result[sid] = {
            "name": s.get("name", sid),
            "description": s.get("description", ""),
            "letter_count": letter_count,
            "entry_count": entry_count,
            "active": sid == settings.get("active_set"),
        }
    return web.json_response(result)


async def api_get_set(request):
    """Get full set data including all letters, entries and track_order."""
    set_id = request.match_info["set_id"]
    sets = settings.get("sets", {})
    if set_id not in sets:
        return web.json_response({"error": "Set not found"}, status=404)
    s = sets[set_id]
    import copy as _copy
    # Normalize in-place, then deep-copy for output and decorate with legacy aliases.
    for letter_cfg in s.get("letters", {}).values():
        for e in letter_cfg.get("entries", []):
            _normalize_letter_entry(e)
    for n in s.get("numbers", {}).values():
        _normalize_number_entry(n)
    s["track_order"] = _normalize_track_order(s.get("track_order"))
    if any(_ensure_entry_ids(c.get("entries")) for c in list(s.get("letters", {}).values()) if isinstance(c, dict)):
        save_settings()                      # FUNK-72: the editor finds entries by id

    out = _copy.deepcopy(s)
    for letter_cfg in out.get("letters", {}).values():
        for e in letter_cfg.get("entries", []):
            _add_legacy_sound_alias(e, "FX")
    for n in out.get("numbers", {}).values():
        _add_legacy_sound_alias(n, "DE")
    return web.json_response(out)


async def api_create_set(request):
    data = await request.json()
    if "sets" not in settings:
        settings["sets"] = {}
    # a new id never replaces an existing set: before, "Neues Set" twice (or "Maße"
    # next to "Masse") gave the same id and silently emptied the first set
    set_id = data.get("id")
    if set_id is not None:
        if not isinstance(set_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,59}", set_id):
            return web.json_response({"error": "id: lower-case letters, digits and -"}, status=400)
        if set_id in settings["sets"]:
            return web.json_response({"error": f"set {set_id} exists"}, status=409)
    else:
        set_id = _new_set_id(data.get("name") or "new-set", settings["sets"])
    # Duplicate from existing set if specified
    source_id = data.get("duplicate_from")
    if source_id and source_id in settings["sets"]:
        import copy
        settings["sets"][set_id] = copy.deepcopy(settings["sets"][source_id])
        settings["sets"][set_id]["name"] = data.get("name", f"Kopie von {settings['sets'][source_id].get('name', source_id)}")
    else:
        settings["sets"][set_id] = {
            "name": data.get("name", "Neues Set"),
            "description": data.get("description", ""),
            "letters": {},
        }
    save_settings()
    return web.json_response({"ok": True, "id": set_id})


async def api_update_set(request):
    set_id = request.match_info["set_id"]
    data = await request.json()
    sets = settings.get("sets", {})
    if set_id not in sets:
        return web.json_response({"error": "Set not found"}, status=404)
    if "name" in data:
        sets[set_id]["name"] = data["name"]
    if "description" in data:
        sets[set_id]["description"] = data["description"]
    if "track_order" in data:
        sets[set_id]["track_order"] = _normalize_track_order(data["track_order"])
    save_settings()
    return web.json_response({"ok": True})


async def api_delete_set(request):
    set_id = request.match_info["set_id"]
    sets = settings.get("sets", {})
    if set_id not in sets:
        return web.json_response({"error": "Set not found"}, status=404)
    if len(sets) <= 1:
        return web.json_response({"error": "Cannot delete last set"}, status=400)
    del sets[set_id]
    if settings.get("active_set") == set_id:
        settings["active_set"] = next(iter(sets))
    save_settings()
    return web.json_response({"ok": True})


def _set_active(set_id):
    """The active-set state change, without saving."""
    global cycle_index, last_letter
    settings["active_set"] = set_id
    cycle_index = {}
    last_letter = None


def _announce_active(set_id):
    sse_broadcast("active_set", {"active_set": set_id, "name": settings["sets"][set_id].get("name", set_id)})


def _activate_set(set_id):
    """Make a set active (it must exist); saves and tells the browsers."""
    _set_active(set_id)
    save_settings()
    _announce_active(set_id)


async def api_activate_set(request):
    set_id = request.match_info["set_id"]
    if set_id not in settings.get("sets", {}):
        return web.json_response({"error": "Set not found"}, status=404)
    _activate_set(set_id)
    return web.json_response({"ok": True, "active_set": set_id})


# ── Set export / import as ZIP (FUNK-43) ─────────────────────────────────────

IMPORT_DIR = os.path.join(DATA_DIR, "import-staging")
IMPORT_TTL_SECONDS = 3600
MAX_PENDING_IMPORTS = 3                       # uploads running + previews waiting
MAX_STAGING_BYTES = 1024 * 1024 * 1024        # all staged uploads together
DISK_RESERVE_BYTES = 512 * 1024 * 1024        # never fill a disk below this
set_imports = {}      # token → {"path", "created", "set", "members"}: previews waiting for commit
_uploading = {}       # token → bytes received so far
_committing = {}      # token → staged ZIP of a commit still running (counted until it is removed)
_import_lock = asyncio.Lock()   # one commit at a time: publish, save and commit/rollback together


def _remove_quiet(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _purge_imports():
    """Drop previews older than an hour, and staged files no preview knows (e.g. from before a restart)."""
    now = time.time()
    for token, imp in list(set_imports.items()):
        if now - imp["created"] > IMPORT_TTL_SECONDS:
            set_imports.pop(token, None)
            _remove_quiet(imp["path"])
    known = {imp["path"] for imp in set_imports.values()} | set(_committing.values())
    try:
        for name in os.listdir(IMPORT_DIR):
            path = os.path.join(IMPORT_DIR, name)
            if path not in known and now - os.path.getmtime(path) > IMPORT_TTL_SECONDS:
                _remove_quiet(path)
    except OSError:
        pass


def _staged_bytes():
    total = sum(_uploading.values())
    for path in [imp["path"] for imp in set_imports.values()] + list(_committing.values()):
        try:
            total += os.path.getsize(path)
        except OSError:
            pass
    return total


def _pending_imports():
    return len(set_imports) + len(_uploading) + len(_committing)


def _settings_file_unchanged(before):
    """True if settings.json still holds exactly the bytes it had before a failed save."""
    try:
        with open(SETTINGS_PATH, "rb") as f:
            return f.read() == before
    except FileNotFoundError:
        return before is None
    except OSError:
        return False


def _restore_settings_file(before):
    """Put settings.json back exactly as it was."""
    if before is None:
        return False
    try:
        _write_settings_bytes(before)
        return True
    except OSError:
        return False


def _purge_import_dir_at_start():
    """Tokens live in memory: after a restart every staged upload is an orphan."""
    try:
        for name in os.listdir(IMPORT_DIR):
            _remove_quiet(os.path.join(IMPORT_DIR, name))
    except OSError:
        pass
    setbundle.purge_temp_files(SOUNDS_DIR, IMAGES_DIR)


async def _import_janitor():
    """Expire abandoned previews even when nobody starts another import."""
    while True:
        await asyncio.sleep(300)
        try:
            _purge_imports()
        except Exception as e:
            print(f"[sets] import cleanup failed: {e}", flush=True)


def _export_copy(set_data):
    """A normalized deep copy of a stored set, as the export writes it."""
    import copy as _copy
    out = _copy.deepcopy(set_data)
    for letter_cfg in out.get("letters", {}).values():
        for e in letter_cfg.get("entries", []):
            _normalize_letter_entry(e)
            _strip_legacy_sound(e)
    for n in out.get("numbers", {}).values():
        _normalize_number_entry(n)
        _strip_legacy_sound(n)
    out["track_order"] = _normalize_track_order(out.get("track_order"))
    return out


def _new_set_id(name, sets):
    """A free, URL-safe set id for a new or imported set."""
    base = re.sub(r"[^a-z0-9-]+", "-", _slugify(name)).strip("-") or "set"
    sid, n = base, 2
    while sid in sets:
        sid, n = f"{base}-{n}", n + 1
    return sid


async def api_export_set(request):
    set_id = request.match_info["set_id"]
    stored = settings.get("sets", {}).get(set_id)
    if stored is None:
        return web.json_response({"error": "Set not found"}, status=404)
    data = _export_copy(stored)               # snapshot now; the ZIP is built off the event loop
    body = await asyncio.get_running_loop().run_in_executor(
        None, setbundle.build_zip, set_id, data, SOUNDS_DIR, IMAGES_DIR, VERSION)
    fname = re.sub(r"[^A-Za-z0-9._-]+", "-", set_id)[:60] or "set"
    print(f"[sets] exported {set_id} ({len(body)} bytes)", flush=True)
    return web.Response(body=body, content_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="funkeykid-set-{fname}.zip"'})


async def api_import_set_preview(request):
    """Upload a set ZIP (multipart field "file"): check it and say what importing would do."""
    _purge_imports()
    try:
        reader = await request.multipart()
        field = await reader.next()
    except Exception:
        return web.json_response({"error": "upload the ZIP as multipart field 'file'"}, status=400)
    if field is None or field.name != "file":
        return web.json_response({"error": "upload the ZIP as multipart field 'file'"}, status=400)
    os.makedirs(IMPORT_DIR, exist_ok=True)
    if _pending_imports() >= MAX_PENDING_IMPORTS:
        return web.json_response({"error": "too many imports open at once: import or cancel one first"}, status=429)
    if shutil.disk_usage(IMPORT_DIR).free < setbundle.MAX_UPLOAD_BYTES + DISK_RESERVE_BYTES:
        return web.json_response({"error": "not enough free disk space for an import"}, status=507)
    token = uuid.uuid4().hex
    path = os.path.join(IMPORT_DIR, f"{token}.zip")
    loop = asyncio.get_running_loop()
    _uploading[token] = 0
    try:
        size = 0
        with open(path, "wb") as f:
            while True:
                chunk = await field.read_chunk(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                _uploading[token] = size
                if size > setbundle.MAX_UPLOAD_BYTES:
                    raise setbundle.BundleError("the file is too large")
                if _staged_bytes() > MAX_STAGING_BYTES:
                    raise setbundle.BundleError("too much import data waiting: import or cancel an open import first")
                f.write(chunk)
        set_data, members = await loop.run_in_executor(None, setbundle.read_zip, path)
        files = await loop.run_in_executor(None, setbundle.plan, path, set_data, members, SOUNDS_DIR, IMAGES_DIR)
    except setbundle.BundleError as e:
        _remove_quiet(path)
        return web.json_response({"error": str(e)}, status=400)
    except BaseException:
        _remove_quiet(path)
        raise
    finally:
        _uploading.pop(token, None)
    set_imports[token] = {"path": path, "created": time.time(), "set": set_data, "members": members}
    letters = set_data["letters"]
    print(f"[sets] import preview {token[:8]}: {set_data['name']} ({len(files)} files)", flush=True)
    return web.json_response({
        "token": token, "name": set_data["name"], "description": set_data["description"],
        "counts": {"letters": len(letters), "entries": sum(len(c["entries"]) for c in letters.values()),
                   "numbers": len(set_data["numbers"])},
        "files": files,
    })


async def api_import_set_commit(request):
    """Import a previewed set as a new set. Body: {"token", "name"?, "resolutions"?, "activate"?}."""
    try:
        body = await _json_object(request)
        token, name = body.get("token"), body.get("name")
        resolutions = body.get("resolutions") or {}
        if not isinstance(token, str):
            raise ValueError("token missing")
        if name is not None and (not isinstance(name, str) or len(name) > setbundle.NAME_MAX
                                 or not setbundle._utf8(name)):
            raise ValueError(f"name must be text of at most {setbundle.NAME_MAX} characters")
        if not isinstance(resolutions, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                         for k, v in resolutions.items()):
            raise ValueError("resolutions must map file paths to rename, keep or overwrite")
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    imp = set_imports.pop(token, None)        # claimed now: a second commit of the same preview finds nothing
    if imp is None:
        return web.json_response({"error": "this preview has expired or was already imported: upload the file again"},
                                 status=404)
    _committing[token] = imp["path"]          # still staged: counted and protected until removed
    try:
        async with _import_lock:              # publish, save and commit/rollback as one unit
            return await _commit_import(imp, name, resolutions, body.get("activate") is True)
    finally:
        _committing.pop(token, None)
        _remove_quiet(imp["path"])


async def _commit_import(imp, name, resolutions, activate):
    needed = sum(imp["members"].values())
    for base in (SOUNDS_DIR, IMAGES_DIR):
        os.makedirs(base, exist_ok=True)
        if shutil.disk_usage(base).free < needed + DISK_RESERVE_BYTES:
            return web.json_response({"error": "not enough free disk space for the set's files"}, status=507)
    try:
        data, summary, txn = await asyncio.get_running_loop().run_in_executor(
            None, setbundle.apply, imp["path"], imp["set"], imp["members"], SOUNDS_DIR, IMAGES_DIR, resolutions)
    except setbundle.BundleError as e:
        return web.json_response({"error": str(e)}, status=400)
    # no await from here on: choose the id, add the set and save in one step, or undo everything
    if name and name.strip():
        data["name"] = name.strip()
    sets = settings.setdefault("sets", {})
    sid = _new_set_id(data["name"], sets)
    previous_active = settings.get("active_set")
    # From reading "before" to a restore, no other thread may save: a restore would undo that
    # save, e.g. progress counts the keyboard thread had just written (FUNK-40 review).
    with _settings_write_lock:
        try:
            with open(SETTINGS_PATH, "rb") as f:
                before = f.read()
        except OSError:
            before = None
        sets[sid] = data
        _ensure_all_entry_ids()               # FUNK-72: imported entries get ids before the save
        try:
            if activate:
                _set_active(sid)
            save_settings()
        except Exception as e:
            sets.pop(sid, None)
            if activate:
                _set_active(previous_active)
            if _settings_file_unchanged(before) or _restore_settings_file(before):
                txn.rollback()                    # settings.json does not name the set: its files can go
                print(f"[sets] import of {sid} undone: settings not saved ({e})", flush=True)
                return web.json_response({"error": "the settings could not be saved: nothing was imported"}, status=500)
            txn.commit()                          # settings.json may name the set: keep its files
            print(f"[sets] import of {sid} failed and settings.json could not be restored ({e})", flush=True)
            return web.json_response({"error": "the settings could not be saved, and the previous settings could not be "
                                               "restored: check the Sets tab after a restart"}, status=500)
    txn.commit()
    if activate:
        _announce_active(sid)
    print(f"[sets] imported {sid}: {len(summary['written'])} written, {len(summary['renamed'])} renamed, "
          f"{len(summary['reused'])} reused, {len(summary['missing'])} missing", flush=True)
    return web.json_response({"ok": True, "id": sid, "name": data["name"], "active": activate, "summary": summary})


async def api_import_set_cancel(request):
    try:
        token = (await _json_object(request)).get("token")
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    imp = set_imports.pop(token, None) if isinstance(token, str) else None
    if imp:
        _remove_quiet(imp["path"])
    return web.json_response({"ok": True})


# ── Letter entries within a set ──────────────────────────────────────────────

async def api_get_letter(request):
    set_id = request.match_info["set_id"]
    letter = keymap.norm_letter(request.match_info["letter"])
    sets = settings.get("sets", {})
    if set_id not in sets:
        return web.json_response({"error": "Set not found"}, status=404)
    letter_cfg = sets[set_id].get("letters", {}).get(letter, {"entries": []})
    if _ensure_entry_ids(letter_cfg.get("entries")):
        save_settings()                      # FUNK-72
    # Clone entries for response so we can decorate with legacy aliases
    # without mutating the persisted in-memory state.
    out_entries = []
    for e in letter_cfg.get("entries", []):
        _normalize_letter_entry(e)
        import copy as _copy
        alias = _copy.deepcopy(e)
        _add_legacy_sound_alias(alias, "FX")
        out_entries.append(alias)
    return web.json_response({**letter_cfg, "entries": out_entries,
                              "track_order": _normalize_track_order(sets[set_id].get("track_order"))})


async def api_put_letter(request):
    """Replace all entries for a letter in a set.

    Accepts entries with either `tracks` dict (new shape) or legacy `sound`
    field (auto-migrated). Always persists the new shape.
    """
    set_id = request.match_info["set_id"]
    letter = keymap.norm_letter(request.match_info["letter"])
    try:
        data = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid JSON"}, status=400)
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        return web.json_response({"error": "entries must be a list of objects"}, status=400)
    sets = settings.get("sets", {})
    if set_id not in sets:
        return web.json_response({"error": "Set not found"}, status=404)
    if "letters" not in sets[set_id]:
        sets[set_id]["letters"] = {}
    cleaned = []
    for e in entries:
        _normalize_letter_entry(e)
        _strip_legacy_sound(e)
        cleaned.append(e)
    _ensure_entry_ids(cleaned)                # the editor sends its own ids; a bad or repeated one is replaced
    sets[set_id]["letters"][letter] = {"entries": cleaned}
    save_settings()
    _letter_changed(request, set_id, letter)
    return web.json_response({"ok": True, "ids": [e["id"] for e in cleaned]})


MEDIA_KINDS = ("IMG",) + tuple(TRACK_KINDS)
ENTRY_WORD_MAX, ENTRY_DESC_MAX, TRACK_PROMPT_MAX = 60, 500, 300


def _letter_entries(set_id, letter, create=False):
    """The entry list of a set's letter, or None (no such set, or no such letter and not create)."""
    sets = settings.get("sets", {})
    if set_id not in sets or not isinstance(sets[set_id], dict):
        return None
    letters = sets[set_id].setdefault("letters", {})
    cfg = letters.get(letter)
    if not isinstance(cfg, dict):
        if not create:
            return None
        cfg = letters[letter] = {"entries": []}
    if not isinstance(cfg.get("entries"), list):
        cfg["entries"] = []
    return cfg["entries"]


def _find_entry(entries, entry_id):
    for i, e in enumerate(entries or []):
        if isinstance(e, dict) and e.get("id") == entry_id:
            return i, e
    return -1, None


def _entry_patch(data, require_files=True):
    # an undo (require_files=False) restores what an entry already had: a set import allows longer
    # text (setbundle.WORD_MAX / MAX_TEXT), so a restore must too, or the word could not come back
    word_max = ENTRY_WORD_MAX if require_files else setbundle.WORD_MAX
    text_max = (ENTRY_DESC_MAX, TRACK_PROMPT_MAX) if require_files else (setbundle.MAX_TEXT, setbundle.MAX_TEXT)
    """Check the fields of an entry change (FUNK-72); returns them or raises ValueError.

    word, enabled, image ("" or a picture in the library), _imageDesc, and tracks:
    {KIND: {file ("" or a sound in the library), enabled, prompt, volume 0-200}}.
    """
    if not isinstance(data, dict):
        raise ValueError("body must be an object")
    unknown = set(data) - {"word", "enabled", "image", "_imageDesc", "tracks"}
    if unknown:
        raise ValueError(f"unknown field: {sorted(unknown)[0]}")
    out = {}
    if "word" in data:
        if not isinstance(data["word"], str) or len(data["word"]) > word_max:
            raise ValueError(f"word must be text (at most {word_max} characters)")
        out["word"] = data["word"]
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            raise ValueError("enabled must be true or false")
        out["enabled"] = data["enabled"]
    if "_imageDesc" in data:
        if not isinstance(data["_imageDesc"], str) or len(data["_imageDesc"]) > text_max[0]:
            raise ValueError(f"_imageDesc must be text (at most {text_max[0]} characters)")
        out["_imageDesc"] = data["_imageDesc"]
    if "image" in data:
        out["image"] = _library_name(data["image"], IMAGES_DIR, "image", require_files)
    if "tracks" in data:
        if not isinstance(data["tracks"], dict):
            raise ValueError("tracks must be an object")
        tracks = {}
        for kind, t in data["tracks"].items():
            if kind not in TRACK_KINDS or not isinstance(t, dict):
                raise ValueError(f"tracks: unknown kind {kind!r}")
            if set(t) - {"file", "enabled", "prompt", "volume"}:
                raise ValueError(f"tracks.{kind}: unknown field")
            tt = {}
            if "file" in t:
                tt["file"] = _library_name(t["file"], SOUNDS_DIR, f"tracks.{kind}.file", require_files)
            if "enabled" in t:
                if not isinstance(t["enabled"], bool):
                    raise ValueError(f"tracks.{kind}.enabled must be true or false")
                tt["enabled"] = t["enabled"]
            if "prompt" in t:
                if not isinstance(t["prompt"], str) or len(t["prompt"]) > text_max[1]:
                    raise ValueError(f"tracks.{kind}.prompt must be text (at most {text_max[1]} characters)")
                tt["prompt"] = t["prompt"]
            if "volume" in t:
                v = t["volume"]
                if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 200:
                    raise ValueError(f"tracks.{kind}.volume must be 0-200")
                tt["volume"] = v
            tracks[kind] = tt
        out["tracks"] = tracks
    return out


def _library_name(value, folder, what, require=True):
    """"" or the plain name of a file that exists in folder; else ValueError. An undo restores what an
    entry had (require=False): a plain name is enough there, the file may be missing (an import)."""
    if value == "":
        return ""
    if not isinstance(value, str) or not _MEDIA_NAME.fullmatch(value) or value.startswith("."):
        raise ValueError(f"{what}: invalid file name")
    if require and not os.path.isfile(os.path.join(folder, value)):
        raise ValueError(f"{what}: no such file in the library")
    return value


def _apply_entry_patch(entry, patch):
    _normalize_letter_entry(entry)
    for k in ("word", "enabled", "image", "_imageDesc"):
        if k in patch:
            entry[k] = patch[k]
    for kind, t in patch.get("tracks", {}).items():
        track = entry["tracks"].setdefault(kind, {"file": "", "enabled": True, "prompt": "", "volume": 100})
        track.update(t)
    _normalize_letter_entry(entry)


class _SaveFailed(Exception):
    pass


def _save_or_restore(entries, snapshot):
    """save_settings(), or put the entry list back as it was and raise _SaveFailed (FUNK-72):
    memory never keeps a change the file does not have (a retry would then wrongly succeed)."""
    try:
        save_settings()
    except Exception as e:
        entries[:] = snapshot
        print(f"[settings] save failed, change undone: {e}", flush=True)
        raise _SaveFailed() from e


_SAVE_FAILED = {"error": "Die Einstellungen konnten nicht gespeichert werden; nichts wurde geändert"}


def _drop_jobs_for_entry(set_id, letter, entry_id):
    """The entry is gone (removed or archived, FUNK-74): every job aiming at it, running or waiting for
    review, now only fills the library, and the pages drop its slot and its "Prüfen" offer."""
    for j in gen_jobs.values():
        t = j.get("target")
        if t and (t.get("set_id"), t.get("letter"), t.get("entry_id")) == (set_id, letter, entry_id):
            j["target"] = None
            sse_broadcast("gen_update", j)


def _detach_jobs_for(set_id, letter, entry_id, kinds):
    """An explicit choice for a slot, from any page: running jobs for that slot only fill the library."""
    for j in gen_jobs.values():
        t = j.get("target")
        if (t and j.get("status") in ("queued", "generating") and t["set_id"] == set_id and t["letter"] == letter
                and t["entry_id"] == entry_id and t["kind"] in kinds):
            j["target"] = None


def _letter_changed(request, set_id, letter, entry_id=None):
    """Tell every page a letter changed; origin = the page that changed it (it ignores its own echo)."""
    origin = (request.headers.get("X-FK-Client", "") if request is not None and hasattr(request, "headers") else "")[:40]
    data = {"set_id": set_id, "letter": letter, "origin": origin}
    if entry_id:
        data["entry_id"] = entry_id
    sse_broadcast("letter", data)


async def api_patch_entry(request):
    """Change some fields of one entry, found by id (FUNK-72): every edit in the editor is one
    of these, so two pages editing different words or fields never overwrite each other."""
    set_id = request.match_info["set_id"]
    letter = keymap.norm_letter(request.match_info["letter"])
    entry_id = request.match_info["entry_id"]
    try:
        data = await request.json()
        restore = isinstance(data, dict) and data.get("restore") is True
        if isinstance(data, dict):
            data = {k: v for k, v in data.items() if k != "restore"}
        patch = _entry_patch(data, require_files=not restore)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    entries = _letter_entries(set_id, letter)
    _, entry = _find_entry(entries, entry_id)
    if entry is None:
        return web.json_response({"error": "entry not found"}, status=404)
    import copy as _copy
    snapshot = _copy.deepcopy(entries)
    _apply_entry_patch(entry, patch)
    try:
        _save_or_restore(entries, snapshot)
    except _SaveFailed:
        return web.json_response(_SAVE_FAILED, status=500)
    chosen = (["IMG"] if "image" in patch else []) + [k for k, t in patch.get("tracks", {}).items() if "file" in t]
    if chosen:
        _detach_jobs_for(set_id, letter, entry_id, chosen)
    _letter_changed(request, set_id, letter, entry_id)
    return web.json_response({"ok": True, "entry": _copy.deepcopy(entry)})


async def api_delete_entry_by_id(request):
    """Remove one entry, found by id (FUNK-72); its files stay in the library."""
    set_id = request.match_info["set_id"]
    letter = keymap.norm_letter(request.match_info["letter"])
    entries = _letter_entries(set_id, letter)
    i, entry = _find_entry(entries, request.match_info["entry_id"])
    if entry is None:
        return web.json_response({"error": "entry not found"}, status=404)
    snapshot = list(entries)
    entries.pop(i)
    try:
        _save_or_restore(entries, snapshot)
    except _SaveFailed:
        return web.json_response(_SAVE_FAILED, status=500)
    _drop_jobs_for_entry(set_id, letter, entry["id"])
    _letter_changed(request, set_id, letter, entry["id"])
    return web.json_response({"ok": True, "index": i, "entry": entry})


def _names_used_elsewhere(skip):
    """Every string in the sets and key maps except inside the object skip."""
    out, stack = set(), [settings.get("sets"), settings.get("keymaps")]
    while stack:
        v = stack.pop()
        if v is skip:
            continue
        if isinstance(v, dict):
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
        elif isinstance(v, str):
            out.add(v)
    return out


async def api_archive_entry(request):
    """Archive one entry (FUNK-72): its files go to the archive (only those nothing else uses),
    then the entry goes. All or nothing: if a file cannot be archived, the ones already
    moved come back and the entry stays."""
    set_id = request.match_info["set_id"]
    letter = keymap.norm_letter(request.match_info["letter"])
    entries = _letter_entries(set_id, letter)
    i, entry = _find_entry(entries, request.match_info["entry_id"])
    if entry is None:
        return web.json_response({"error": "entry not found"}, status=404)
    _normalize_letter_entry(entry)
    files = ([("image", entry["image"])] if entry.get("image") else []) + \
            [("sound", t["file"]) for t in entry["tracks"].values() if t.get("file")]
    used = _names_used_elsewhere(entry)
    moved, kept = [], []
    for file_type, name in dict.fromkeys(files):
        folder = SOUNDS_DIR if file_type == "sound" else IMAGES_DIR
        if name in used or not os.path.isfile(os.path.join(folder, name)):
            kept.append(name)
            continue
        try:
            moved.append((file_type, name, _archive_one(file_type, name)))
        except _ArchiveError as e:
            for ft, n, dst in reversed(moved):
                _unarchive(ft, n, dst)
            return web.json_response({"error": f"{name}: {e.msg}"}, status=e.status)
    snapshot = list(entries)
    entries.pop(i)
    try:
        _save_or_restore(entries, snapshot)
    except _SaveFailed:
        for ft, n, dst in reversed(moved):                # the entry stays: its files come back too
            _unarchive(ft, n, dst)
        return web.json_response(_SAVE_FAILED, status=500)
    _drop_jobs_for_entry(set_id, letter, entry["id"])
    _letter_changed(request, set_id, letter, entry["id"])
    return web.json_response({"ok": True, "archived": [n for _, n, _ in moved], "kept": kept})


async def api_entry_media(request):
    """Put a finished file on one entry's picture or track, found by entry id (FUNK-72).

    Body: {"kind": "IMG"|"FX"|"DE"|"EN", "file": <name in the library>, "if_empty": bool}.
    A generation job's file is put on its entry only when the job is done, never before
    (a name that does not exist yet showed as a broken picture). With if_empty an occupied
    slot stays as it is (applied: false): a job that ends after the parent chose something
    else never overwrites that choice.
    """
    set_id = request.match_info["set_id"]
    letter = keymap.norm_letter(request.match_info["letter"])
    entry_id = request.match_info["entry_id"]
    try:
        data = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(data, dict):
        return web.json_response({"error": "body must be an object"}, status=400)
    kind, fname = data.get("kind"), data.get("file")
    if "if_empty" in data and not isinstance(data["if_empty"], bool):
        return web.json_response({"error": "if_empty must be true or false"}, status=400)
    if kind not in MEDIA_KINDS:
        return web.json_response({"error": f"kind must be one of {', '.join(MEDIA_KINDS)}"}, status=400)
    if not isinstance(fname, str) or not _MEDIA_NAME.fullmatch(fname) or fname.startswith("."):
        return web.json_response({"error": "invalid file name"}, status=400)
    if not os.path.isfile(os.path.join(IMAGES_DIR if kind == "IMG" else SOUNDS_DIR, fname)):
        return web.json_response({"error": "file not found"}, status=404)
    sets = settings.get("sets", {})
    if set_id not in sets:
        return web.json_response({"error": "Set not found"}, status=404)
    cfg = sets[set_id].get("letters", {}).get(letter)
    entries = cfg.get("entries") if isinstance(cfg, dict) else None
    entry = next((e for e in entries or [] if isinstance(e, dict) and e.get("id") == entry_id), None)
    if entry is None:
        return web.json_response({"error": "entry not found"}, status=404)
    _normalize_letter_entry(entry)
    if kind == "IMG":
        current = entry.get("image") or ""
    else:
        current = (entry["tracks"].get(kind) or {}).get("file") or ""
    if data.get("if_empty") is True and current:
        return web.json_response({"ok": True, "applied": False, "current": current})
    import copy as _copy
    snapshot = _copy.deepcopy(entries)
    if kind == "IMG":
        entry["image"] = fname
    else:
        track = entry["tracks"].setdefault(kind, {"file": "", "enabled": True, "prompt": "", "volume": 100})
        track["file"] = fname
        track["enabled"] = True
    try:
        _save_or_restore(entries, snapshot)
    except _SaveFailed:
        return web.json_response(_SAVE_FAILED, status=500)
    if data.get("if_empty") is not True:
        _detach_jobs_for(set_id, letter, entry_id, [kind])
    _letter_changed(request, set_id, letter, entry_id)
    return web.json_response({"ok": True, "applied": True})


async def api_add_entry(request):
    """Add an entry to a letter (FUNK-72): {id?, index?, word, enabled?, image?, _imageDesc?, tracks?}.

    The editor sends its own id (kept when valid and free, else a new one) and, to undo a
    removal, the position the entry had."""
    set_id = request.match_info["set_id"]
    letter = keymap.norm_letter(request.match_info["letter"])
    try:
        data = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(data, dict):
        return web.json_response({"error": "body must be an object"}, status=400)
    data = dict(data)
    eid, index = data.pop("id", None), data.pop("index", None)
    restore = data.pop("restore", None) is True             # an undo: the entry as it was, files may be missing
    if index is not None and (isinstance(index, bool) or not isinstance(index, int)):
        return web.json_response({"error": "index must be a number"}, status=400)
    try:
        patch = _entry_patch(data, require_files=not restore)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    entries = _letter_entries(set_id, letter, create=True)
    if entries is None:
        return web.json_response({"error": "Set not found"}, status=404)
    if isinstance(eid, str) and _ENTRY_ID.fullmatch(eid):
        i, have = _find_entry(entries, eid)
        if have is not None:                    # a repeat of an add whose answer got lost: nothing twice
            import copy as _copy
            return web.json_response({"ok": True, "id": eid, "index": i, "existed": True, "entry": _copy.deepcopy(have)})
    entry = {"word": "", "image": "", "enabled": True, "tracks": {}}
    if isinstance(eid, str) and _ENTRY_ID.fullmatch(eid):
        entry["id"] = eid
    _apply_entry_patch(entry, patch)
    i = len(entries) if index is None else max(0, min(index, len(entries)))
    snapshot = list(entries)
    entries.insert(i, entry)
    _ensure_entry_ids(entries)
    try:
        _save_or_restore(entries, snapshot)
    except _SaveFailed:
        return web.json_response(_SAVE_FAILED, status=500)
    _letter_changed(request, set_id, letter, entry["id"])
    import copy as _copy
    return web.json_response({"ok": True, "id": entry["id"], "index": i, "entry": _copy.deepcopy(entry)})


# ── Numbers (per set) ────────────────────────────────────────────────────────

async def api_get_numbers(request):
    """Return the numbers dict of a set."""
    set_id = request.match_info["set_id"]
    sets = settings.get("sets", {})
    if set_id not in sets:
        return web.json_response({"error": "Set not found"}, status=404)
    nums = sets[set_id].get("numbers", {})
    import copy as _copy
    out = {}
    for digit, n in nums.items():
        _normalize_number_entry(n)
        alias = _copy.deepcopy(n)
        _add_legacy_sound_alias(alias, "DE")
        out[digit] = alias
    return web.json_response(out)


async def api_put_number(request):
    """Replace the full config for a digit.

    Accepts legacy (with `sound` field) or new (`tracks` dict) shapes.
    Always persists the new shape.
    """
    set_id = request.match_info["set_id"]
    digit = request.match_info["digit"]
    if digit not in NUMBER_KEYS:
        return web.json_response({"error": "Invalid digit"}, status=400)
    data = await request.json()
    sets = settings.get("sets", {})
    if set_id not in sets:
        return web.json_response({"error": "Set not found"}, status=404)
    numbers = sets[set_id].setdefault("numbers", {})
    new_cfg = {
        "word": data.get("word", ""),
        "image_subject": data.get("image_subject", ""),
        "tracks": data.get("tracks", {}),
        "sound": data.get("sound", ""),  # transient — stripped below
        "backgrounds": [
            {"image": b.get("image", ""), "enabled": bool(b.get("enabled", True))}
            for b in data.get("backgrounds", [])
        ],
    }
    _normalize_number_entry(new_cfg)
    _strip_legacy_sound(new_cfg)
    numbers[digit] = new_cfg
    save_settings()
    for key in [k for k in _bg_tokens if k[:2] == (set_id, digit)]:
        _bg_tokens.pop(key, None)            # whatever is there now was not put there by an add (FUNK-76)
    return web.json_response({"ok": True})


# ── Numbers in the entry editor (FUNK-76) ───────────────────────────────────
# The Zahlen tab uses the letters' editor (FUNK-72): a digit is shown as one entry with the fixed id
# "number" (word; picture idea = image_subject; tracks), plus its backgrounds. Writes are small and
# by field, validated like the entries', and never kept when the save fails. Generation jobs target
# {set_id, letter: <digit>, entry_id: "number"}: a track fills only an empty slot, a picture job
# adds a background. The whole-digit PUT above stays for scripts/.
NUMBER_ENTRY_ID = "number"
NUMBER_BG_MAX = 60
_bg_ops = {}                # op id of an add, switch or removal → its first answer: a retry answers the same
BG_OPS_KEEP = 500
# (set_id, digit, image) → the token of the insertion that put that background there (in memory): an undo
# of an add deletes only that insertion, never one another page made after removing it
_bg_tokens = {}


def _bg_token_new(set_id, digit, image):
    tok = uuid.uuid4().hex[:12]
    _bg_tokens[(set_id, digit, image)] = tok
    return tok


def _number_entry_view(n):
    import copy as _copy
    _normalize_number_entry(n)
    return {"id": NUMBER_ENTRY_ID, "word": n.get("word") if isinstance(n.get("word"), str) else "",
            "enabled": True, "image": "",
            "_imageDesc": n.get("image_subject") if isinstance(n.get("image_subject"), str) else "",
            "tracks": _copy.deepcopy(n.get("tracks") or {})}


def _number_bgs(n):
    """The backgrounds as the editor shows them: plain names only, enabled true/false."""
    out = []
    for b in n.get("backgrounds") or [] if isinstance(n.get("backgrounds"), list) else []:
        if isinstance(b, dict) and isinstance(b.get("image"), str) and b["image"]:
            out.append({"image": b["image"], "enabled": b.get("enabled", True) is not False})
    return out


def _number_slot(set_id, digit):
    """(numbers dict of the set, the digit's config or None); (None, None) if the set does not exist."""
    sets = settings.get("sets", {})
    if digit not in NUMBER_KEYS or set_id not in sets or not isinstance(sets[set_id], dict):
        return None, None
    nums = sets[set_id].get("numbers")
    if not isinstance(nums, dict):
        nums = sets[set_id]["numbers"] = {}
    n = nums.get(digit)
    return nums, (n if isinstance(n, dict) else None)


def _number_for_write(set_id, digit):
    """(numbers dict, the digit's config, snapshot to restore); the config is created when missing."""
    import copy as _copy
    nums, n = _number_slot(set_id, digit)
    if nums is None:
        return None, None, None
    snapshot = _copy.deepcopy(n) if n is not None else None
    if n is None:
        n = nums[digit] = {"word": "", "image_subject": "", "tracks": {}, "backgrounds": []}
    _normalize_number_entry(n)
    _strip_legacy_sound(n)
    if not isinstance(n.get("backgrounds"), list):
        n["backgrounds"] = []
    return nums, n, snapshot


def _number_save_or_restore(nums, digit, snapshot):
    """save_settings(), or put the digit back as it was and raise _SaveFailed (like _save_or_restore)."""
    try:
        save_settings()
    except Exception as e:
        if snapshot is None:
            nums.pop(digit, None)
        else:
            nums[digit] = snapshot
        print(f"[settings] save failed, number change undone: {e}", flush=True)
        raise _SaveFailed() from e


def _number_route(request):
    set_id, digit = request.match_info["set_id"], request.match_info["digit"]
    if digit not in NUMBER_KEYS:
        return None, None, web.json_response({"error": "Invalid digit"}, status=400)
    if set_id not in settings.get("sets", {}):
        return None, None, web.json_response({"error": "Set not found"}, status=404)
    return set_id, digit, None


async def api_get_number(request):
    """One digit for the editor: {entries: [the digit as an entry], backgrounds, track_order}."""
    set_id, digit, err = _number_route(request)
    if err:
        return err
    _, n = _number_slot(set_id, digit)
    n = n if n is not None else {"word": "", "image_subject": "", "tracks": {}, "backgrounds": []}
    return web.json_response({"entries": [_number_entry_view(n)], "backgrounds": _number_bgs(n),
                              "track_order": _normalize_track_order(settings["sets"][set_id].get("track_order"))})


async def api_patch_number(request):
    """Change some fields of a digit (word, _imageDesc, tracks), like PATCH of an entry."""
    set_id, digit, err = _number_route(request)
    if err:
        return err
    if request.match_info["entry_id"] != NUMBER_ENTRY_ID:
        return web.json_response({"error": "entry not found"}, status=404)
    try:
        data = await request.json()
        restore = isinstance(data, dict) and data.get("restore") is True
        if isinstance(data, dict):
            data = {k: v for k, v in data.items() if k != "restore"}
        patch = _entry_patch(data, require_files=not restore)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    if "image" in patch or "enabled" in patch:
        return web.json_response({"error": "a number has backgrounds, not one picture, and is always on"}, status=400)
    nums, n, snapshot = _number_for_write(set_id, digit)
    if "word" in patch:
        n["word"] = patch["word"]
    if "_imageDesc" in patch:
        n["image_subject"] = patch["_imageDesc"]
    for kind, t in patch.get("tracks", {}).items():
        n["tracks"].setdefault(kind, {"file": "", "enabled": True, "prompt": "", "volume": 100}).update(t)
    _normalize_number_entry(n)
    try:
        _number_save_or_restore(nums, digit, snapshot)
    except _SaveFailed:
        return web.json_response(_SAVE_FAILED, status=500)
    chosen = [k for k, t in patch.get("tracks", {}).items() if "file" in t]
    if chosen:
        _detach_jobs_for(set_id, digit, NUMBER_ENTRY_ID, chosen)
    _letter_changed(request, set_id, digit, NUMBER_ENTRY_ID)
    return web.json_response({"ok": True, "entry": _number_entry_view(n)})


def _bg_index(n, image):
    for i, b in enumerate(n.get("backgrounds") or []):
        if isinstance(b, dict) and b.get("image") == image:
            return i
    return -1


async def api_number_bg_add(request):
    """Add a background by name: {image, enabled?, index?, restore?}. Already there → existed: true."""
    set_id, digit, err = _number_route(request)
    if err:
        return err
    try:
        data = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(data, dict) or set(data) - {"image", "enabled", "index", "restore", "op"}:
        return web.json_response({"error": "body must be {image, enabled?, index?, restore?, op?}"}, status=400)
    op = data.get("op")
    if op is not None and (not isinstance(op, str) or not 6 <= len(op) <= 40):
        return web.json_response({"error": "op must be an id"}, status=400)
    if op is not None and op in _bg_ops:
        return web.json_response(_bg_ops[op])          # the same add again (a retry): its first answer
    restore = data.get("restore") is True
    try:
        image = _library_name(data.get("image"), IMAGES_DIR, "image", require=not restore)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    enabled, index = data.get("enabled", True), data.get("index")
    if not image or not isinstance(enabled, bool) or (index is not None and (isinstance(index, bool) or not isinstance(index, int))):
        return web.json_response({"error": "image, enabled (true/false) and index (a number) please"}, status=400)
    nums, n, snapshot = _number_for_write(set_id, digit)
    if _bg_index(n, image) >= 0:
        return web.json_response(_bg_answer(op, {"ok": True, "existed": True}))
    if len(n["backgrounds"]) >= NUMBER_BG_MAX and not restore:   # an undo puts back what was just there
        return web.json_response({"error": f"Diese Zahl hat schon {NUMBER_BG_MAX} Hintergründe. Entferne zuerst einen."}, status=400)
    at = len(n["backgrounds"]) if index is None else max(0, min(index, len(n["backgrounds"])))
    n["backgrounds"].insert(at, {"image": image, "enabled": enabled})
    try:
        _number_save_or_restore(nums, digit, snapshot)
    except _SaveFailed:
        return web.json_response(_SAVE_FAILED, status=500)
    _letter_changed(request, set_id, digit, NUMBER_ENTRY_ID)
    return web.json_response(_bg_answer(op, {"ok": True, "index": at, "token": _bg_token_new(set_id, digit, image)}))


def _bg_answer(op, answer):
    if op is not None:
        _bg_ops[op] = answer
        while len(_bg_ops) > BG_OPS_KEEP:
            _bg_ops.pop(next(iter(_bg_ops)))
    return answer


async def api_number_bg_patch(request):
    """Switch a background on or off: {enabled, op?, if_enabled?, if_token?}. Answers whether it changed, what
    it was and the background's identity token; with if_enabled and if_token (an undo) it switches only if it
    is still that background (not a removed-and-added-again one) and still as that page left it."""
    set_id, digit, err = _number_route(request)
    if err:
        return err
    try:
        data = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if (not isinstance(data, dict) or not {"enabled"} <= set(data) <= {"enabled", "op", "if_enabled", "if_token"}
            or not isinstance(data["enabled"], bool) or not isinstance(data.get("if_enabled", True), bool)
            or not isinstance(data.get("if_token", ""), str)):
        return web.json_response({"error": "body must be {enabled: true|false, op?, if_enabled?, if_token?}"}, status=400)
    op = data.get("op")
    if op is not None and (not isinstance(op, str) or not 6 <= len(op) <= 40):
        return web.json_response({"error": "op must be an id"}, status=400)
    if op is not None and op in _bg_ops:
        return web.json_response(_bg_ops[op])          # a retry never re-applies a choice made since
    nums, n = _number_slot(set_id, digit)
    i = _bg_index(n, request.match_info["image"]) if n is not None else -1
    if i < 0:
        return web.json_response({"error": "background not found"}, status=404)
    image = request.match_info["image"]
    tok = _bg_tokens.get((set_id, digit, image)) or _bg_token_new(set_id, digit, image)   # its identity
    was = n["backgrounds"][i].get("enabled", True) is not False
    if (was == data["enabled"] or ("if_enabled" in data and was != data["if_enabled"])
            or ("if_token" in data and data["if_token"] != tok)):
        # nothing to do, or changed (or replaced) by someone else since: no write, nothing for an undo
        return web.json_response(_bg_answer(op, {"ok": True, "changed": False, "was": was, "token": tok}))
    nums, n, snapshot = _number_for_write(set_id, digit)
    n["backgrounds"][i]["enabled"] = data["enabled"]
    try:
        _number_save_or_restore(nums, digit, snapshot)
    except _SaveFailed:
        return web.json_response(_SAVE_FAILED, status=500)
    _letter_changed(request, set_id, digit, NUMBER_ENTRY_ID)
    return web.json_response(_bg_answer(op, {"ok": True, "changed": True, "was": was, "token": tok}))


async def api_number_bg_delete(request):
    """Take a background off the digit (the picture stays in the library). Not there → removed: false.
    Body (optional): {op, if_token}: a retry with the same op gets the first answer; with if_token
    (an undo of an add) only the insertion with that token is taken off."""
    set_id, digit, err = _number_route(request)
    if err:
        return err
    try:
        data = await request.json()
    except Exception:
        data = None
    op = data.get("op") if isinstance(data, dict) else None
    if op is not None and (not isinstance(op, str) or not 6 <= len(op) <= 40):
        return web.json_response({"error": "op must be an id"}, status=400)
    if op is not None and op in _bg_ops:
        return web.json_response(_bg_ops[op])
    if_token = data.get("if_token") if isinstance(data, dict) else None
    if if_token is not None and not isinstance(if_token, str):
        return web.json_response({"error": "if_token must be text"}, status=400)
    image = request.match_info["image"]
    nums, n = _number_slot(set_id, digit)
    i = _bg_index(n, image) if n is not None else -1
    if i < 0 or (if_token is not None and _bg_tokens.get((set_id, digit, image)) != if_token):
        return web.json_response(_bg_answer(op, {"ok": True, "removed": False}))
    nums, n, snapshot = _number_for_write(set_id, digit)
    gone = n["backgrounds"].pop(i)
    try:
        _number_save_or_restore(nums, digit, snapshot)
    except _SaveFailed:
        return web.json_response(_SAVE_FAILED, status=500)
    _bg_tokens.pop((set_id, digit, image), None)
    _letter_changed(request, set_id, digit, NUMBER_ENTRY_ID)
    return web.json_response(_bg_answer(op, {"ok": True, "removed": True, "index": i, "enabled": gone.get("enabled", True) is not False}))


async def api_delete_entry(request):
    """Delete an entry by index from a letter."""
    set_id = request.match_info["set_id"]
    letter = keymap.norm_letter(request.match_info["letter"])
    index = int(request.match_info["index"])
    sets = settings.get("sets", {})
    if set_id not in sets:
        return web.json_response({"error": "Set not found"}, status=404)
    entries = sets[set_id].get("letters", {}).get(letter, {}).get("entries", [])
    if 0 <= index < len(entries):
        entries.pop(index)
        save_settings()
        return web.json_response({"ok": True})
    return web.json_response({"error": "Index out of range"}, status=400)


# ── Files ────────────────────────────────────────────────────────────────────

async def api_get_sounds(request):
    os.makedirs(SOUNDS_DIR, exist_ok=True)
    files = sorted(f for f in os.listdir(SOUNDS_DIR) if f.endswith((".mp3", ".wav", ".ogg")))
    return web.json_response(files)


async def api_normalize_all_sounds(request):
    """Batch-normalize every mp3 in /data/sounds (FUNK-1).

    Run this once after enabling normalization on an existing library. Each
    file is backed up into /data/ai-generated/sounds/<name>_original.mp3 the
    first time it's processed (idempotent — detects the backup to avoid
    re-processing).

    Progress is streamed via SSE `normalize_progress` events; also returned
    as a summary when complete.
    """
    if not _normalize_enabled():
        return web.json_response({"error": "audio_normalize ist deaktiviert"}, status=400)
    try:
        proc = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        if proc.returncode != 0:
            return web.json_response({"error": "ffmpeg nicht verfügbar"}, status=500)
    except FileNotFoundError:
        return web.json_response({"error": "ffmpeg nicht installiert"}, status=500)

    def _run():
        import shutil as _sh
        os.makedirs(AI_SOUNDS_DIR, exist_ok=True)
        files = sorted(f for f in os.listdir(SOUNDS_DIR) if f.lower().endswith(".mp3"))
        total = len(files)
        done = 0
        skipped = 0
        failed = 0
        for i, fname in enumerate(files):
            sse_broadcast("normalize_progress", {"file": fname, "index": i, "total": total, "status": "start"})
            src = os.path.join(SOUNDS_DIR, fname)
            backup = os.path.join(AI_SOUNDS_DIR, fname.rsplit(".", 1)[0] + "_original.mp3")
            marker = os.path.join(AI_SOUNDS_DIR, fname.rsplit(".", 1)[0] + "_normalized.marker")
            if os.path.exists(marker):
                skipped += 1
                sse_broadcast("normalize_progress", {"file": fname, "index": i, "total": total, "status": "skipped"})
                continue
            # One-time backup of the pre-normalization original
            if not os.path.exists(backup):
                try: _sh.copy2(src, backup)
                except Exception: pass
            tmp = src + ".norm.mp3"
            ok = normalize_mp3(src, tmp)
            if ok and os.path.exists(tmp):
                try:
                    os.replace(tmp, src)
                    # leave a marker so we don't re-normalize a normalized file
                    with open(marker, "w") as m: m.write(str(int(time.time())))
                    done += 1
                    sse_broadcast("normalize_progress", {"file": fname, "index": i, "total": total, "status": "done"})
                except Exception as e:
                    failed += 1
                    sse_broadcast("normalize_progress", {"file": fname, "index": i, "total": total, "status": "error", "error": str(e)})
            else:
                try:
                    if os.path.exists(tmp): os.remove(tmp)
                except Exception: pass
                failed += 1
                sse_broadcast("normalize_progress", {"file": fname, "index": i, "total": total, "status": "error"})
        summary = {"total": total, "done": done, "skipped": skipped, "failed": failed}
        sse_broadcast("normalize_progress", {"status": "complete", **summary})
        print(f"[norm] batch complete: {summary}", flush=True)

    threading.Thread(target=_run, daemon=True).start()
    return web.json_response({"ok": True, "message": "Normalisierung läuft — siehe SSE normalize_progress."})


UPLOAD_EXTS = {"sound": ("mp3", "wav", "ogg"), "image": ("png", "jpg", "jpeg", "gif")}   # what the library lists
UPLOAD_MAX_BYTES = {"sound": 20 * 1024 * 1024, "image": 10 * 1024 * 1024}


class _UploadTooLarge(Exception):
    pass


def _upload_name(raw, kind):
    """The plain name an upload asks for, or None (FUNK-73).

    Only the last part of the browser's name counts (it was joined onto the data folder
    as is, "../" included); no hidden files and only the kind's extensions. The name it
    gets in the end is a free one (see _publish_new).
    """
    if not isinstance(raw, str):
        return None
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or name.startswith(".") or not _MEDIA_NAME.fullmatch(name):
        return None
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem or ext.lower() not in UPLOAD_EXTS[kind]:
        return None
    return name


def _staging(ext):
    """A fresh path in the private staging folder: on the data volume (links work), outside
    every folder a route serves (FUNK-73: /ai served the old upload temp files)."""
    folder = os.path.join(DATA_DIR, ".staging")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"{uuid.uuid4().hex}.{ext}")


def _publish_exact(src, dst):
    """Put a complete file at dst, never over an existing path and never through one.

    link() and open(..., "x") fail on any existing name, a dangling symlink included, and
    never follow it. Raises FileExistsError if dst is taken; a failed copy leaves nothing.
    """
    try:
        os.link(src, dst, follow_symlinks=False)
        return
    except FileExistsError:
        raise
    except NotImplementedError:
        pass
    except OSError as e:
        if e.errno not in (errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP):
            raise
    made = False
    try:
        with open(src, "rb") as fi, open(dst, "xb") as fo:
            made = True
            shutil.copyfileobj(fi, fo)
    except BaseException:
        if made:
            _remove_quiet(dst)
        raise


def _publish_new(src, folder, want, folders):
    """Put src into folder under a free name based on want; return that name (FUNK-73).

    Another writer (a set import) taking the name meanwhile just means the next free one.
    """
    stem, _, ext = want.rpartition(".")
    for _ in range(8):
        name = _claim_media_name(want, stem, ext, folders)
        try:
            _publish_exact(src, os.path.join(folder, name))
            return name
        except FileExistsError:
            continue
    raise FileExistsError(want)


def _normalize_staged(raw):
    """Normalize a staged sound into a second staged file. Returns (path to publish, normalized).

    The raw file is complete, so when normalization does not run or fails, the raw file is
    what gets published (FUNK-1: the caller always gets a usable file); a half-written
    normalized file is never used. Storage errors outside normalize_mp3 propagate.
    """
    out = _staging(raw.rpartition(".")[2])
    try:
        normed = normalize_mp3(raw, out)
    except OSError:                         # its own fallback copy failed (disk full): the raw file is complete
        _remove_quiet(out)
        return raw, False
    except BaseException:
        _remove_quiet(out)
        raise
    if normed and os.path.isfile(out) and os.path.getsize(out) > 0:
        return out, True
    _remove_quiet(out)
    return raw, False


def _store_generated_sound(audio_data, filename):
    """A generation job's sound: staged, normalized, published exclusively as /sounds/<filename>,
    the raw file kept next to the AI sounds (best effort). Returns whether it was normalized."""
    raw = _staging(filename.rpartition(".")[2] or "mp3")
    src = raw
    try:
        with open(raw, "xb") as f:
            f.write(audio_data)
        src, normed = _normalize_staged(raw)
        os.makedirs(SOUNDS_DIR, exist_ok=True)
        _publish_exact(src, os.path.join(SOUNDS_DIR, filename))
        try:
            os.makedirs(AI_SOUNDS_DIR, exist_ok=True)
            _publish_exact(raw, os.path.join(AI_SOUNDS_DIR, filename))
        except OSError:
            pass
        return normed
    finally:
        _remove_quiet(raw)
        if src != raw:
            _remove_quiet(src)


async def _receive_upload(request, kind):
    """The request's first file field, read into a private temp file.

    Returns (temp path, wanted name, None) or (None, None, error response). The temp
    file is removed on every failure, a cancelled request included.
    """
    try:
        reader = await request.multipart()
        field = await reader.next()
    except Exception:
        return None, None, web.json_response({"error": "multipart upload expected"}, status=400)
    want = _upload_name(getattr(field, "filename", None), kind) if field is not None else None
    if not want:
        allowed = ", ".join(UPLOAD_EXTS[kind])
        return None, None, web.json_response({"error": f"Dateiname ungültig (erlaubt: {allowed})"}, status=400)
    tmp, size, ok = None, 0, False
    try:
        tmp = _staging(want.rpartition(".")[2])
        with open(tmp, "xb") as f:
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                size += len(chunk)
                if size > UPLOAD_MAX_BYTES[kind]:
                    raise _UploadTooLarge()
                f.write(chunk)
        ok = True
    except _UploadTooLarge:
        mb = UPLOAD_MAX_BYTES[kind] // (1024 * 1024)
        return None, None, web.json_response({"error": f"Datei zu groß (höchstens {mb} MB)"}, status=413)
    except OSError:
        return None, None, web.json_response({"error": "Die Datei konnte nicht gespeichert werden (Speicher voll?)"}, status=507)
    finally:
        if not ok and tmp:
            _remove_quiet(tmp)
    return tmp, want, None


async def api_upload_sound(request):
    """Upload a sound file. Normalizes the result (FUNK-1) unless disabled.

    The upload is staged privately, normalized there, and only a complete file is
    published into /data/sounds under a free name (FUNK-73): use the response. The
    original stays next to the AI sounds for reference (best effort, never partial).
    """
    tmp, want, err = await _receive_upload(request, "sound")
    if err:
        return err
    fut = asyncio.get_running_loop().run_in_executor(None, _normalize_staged, tmp)
    try:
        src, normed = await asyncio.shield(fut)
    except asyncio.CancelledError:
        # the worker keeps running: it owns tmp until it ends; clean both up then
        def _after(f):
            _remove_quiet(tmp)
            if not f.cancelled() and f.exception() is None and f.result()[0] != tmp:
                _remove_quiet(f.result()[0])
        fut.add_done_callback(_after)
        raise
    except OSError:
        _remove_quiet(tmp)
        return web.json_response({"error": "Die Datei konnte nicht gespeichert werden (Speicher voll?)"}, status=507)
    try:
        os.makedirs(SOUNDS_DIR, exist_ok=True)
        name = _publish_new(src, SOUNDS_DIR, want, SOUND_DIRS)
        try:
            os.makedirs(AI_SOUNDS_DIR, exist_ok=True)
            _publish_exact(tmp, os.path.join(AI_SOUNDS_DIR, name))
        except OSError:
            pass
    except OSError:
        return web.json_response({"error": "Die Datei konnte nicht gespeichert werden (Speicher voll?)"}, status=507)
    finally:
        _remove_quiet(tmp)
        if src != tmp:
            _remove_quiet(src)
    return web.json_response({"ok": True, "file": name, "normalized": normed})


async def api_get_images(request):
    os.makedirs(IMAGES_DIR, exist_ok=True)
    files = sorted(f for f in os.listdir(IMAGES_DIR) if f.endswith((".png", ".jpg", ".jpeg", ".gif")))
    return web.json_response(files)


async def api_upload_image(request):
    """Upload a picture into the library under a plain, free name (FUNK-73): use the response."""
    tmp, want, err = await _receive_upload(request, "image")
    if err:
        return err
    try:
        os.makedirs(IMAGES_DIR, exist_ok=True)
        name = _publish_new(tmp, IMAGES_DIR, want, IMAGE_DIRS)
    except OSError:
        return web.json_response({"error": "Die Datei konnte nicht gespeichert werden (Speicher voll?)"}, status=507)
    finally:
        _remove_quiet(tmp)
    return web.json_response({"ok": True, "file": name})


async def api_test_key(request):
    letter = keymap.norm_letter(request.match_info["letter"])
    handle_key(letter, _raw_for_key(letter), virtual=True)
    return web.json_response({"ok": True, "letter": letter})


def _raw_for_key(key_name):
    """The code a physical key sends for a (layout-translated) key name: inverse layout.

    The main block wins over the number pad: the test key "1" is the digit-row 1
    (not KP1), and German "+" is the + key (RIGHTBRACE), as before FUNK-64.
    """
    from keyboard import LAYOUTS
    layout = LAYOUTS.get(_language(), {})
    for raw, key in layout.items():
        if key == key_name and not raw.startswith("KP"):
            return raw
    return key_name


# ── Quiz API (FUNK-39) ───────────────────────────────────────────────────────

def _quiz_payload():
    pool = _quiz_pool([])
    history = settings.get("quiz_history")
    return {
        **quiz_engine.snapshot(),
        "config": _quiz_config(),
        "history": history[:quiz.HISTORY_MAX] if isinstance(history, list) else [],
        "alphabet": list(_quiz_alphabet()),
        "available": {letter: len(entries) for letter, entries in pool.items()},
        "options": {"modes": quiz.MODES, "questions": list(quiz.QUESTION_COUNTS),
                    "remind_seconds": list(quiz.REMIND_CHOICES), "max_tries": quiz.MAX_TRIES},
    }


async def _quiz_body(request):
    if not request.can_read_body:
        return {}
    data = await request.json()
    if not isinstance(data, dict):
        raise quiz.QuizError("body must be an object")
    return data


async def api_quiz_get(request):
    return web.json_response(_quiz_payload())


async def api_quiz_config(request):
    try:
        cfg = quiz.validate_config(await _quiz_body(request), keymap.LETTERS)
    except (quiz.QuizError, ValueError) as e:
        return web.json_response({"error": str(e)}, status=400)
    settings["quiz"] = cfg
    save_settings()
    return web.json_response(_quiz_payload())


async def api_quiz_start(request):
    """Start with the settings sent along (they are saved), else the saved ones."""
    try:
        body = await _quiz_body(request)
        cfg = quiz.validate_config(body["config"], keymap.LETTERS) if "config" in body else _quiz_config()
    except (quiz.QuizError, ValueError) as e:
        return web.json_response({"error": str(e)}, status=400)
    if "config" in body:
        settings["quiz"] = cfg
        save_settings()
    try:
        # playback starts processes (pactl): off the event loop
        await asyncio.get_running_loop().run_in_executor(None, quiz_engine.start, cfg)
    except quiz.QuizError as e:
        return web.json_response({**_quiz_payload(), "error": str(e)}, status=409)
    return web.json_response(_quiz_payload())


async def api_quiz_stop(request):
    await asyncio.get_running_loop().run_in_executor(None, quiz_engine.stop)
    return web.json_response(_quiz_payload())


async def api_quiz_repeat(request):
    await asyncio.get_running_loop().run_in_executor(None, quiz_engine.repeat)
    return web.json_response(_quiz_payload())


# ── Progress API (FUNK-40) ───────────────────────────────────────────────────

async def api_progress_get(request):
    return web.json_response(letter_progress.snapshot())


async def api_dashboard_get(request):
    """The parent dashboard (FUNK-42): days, periods, latest sessions."""
    return web.json_response(letter_progress.dashboard())


def _csv_response(text, name):
    """A CSV download: UTF-8 with a byte-order mark, so spreadsheet apps read the umlauts."""
    filename = f"funkeykid-{name}-{time.strftime('%Y-%m-%d')}.csv"
    return web.Response(body=("\ufeff" + text).encode("utf-8"), content_type="text/csv", charset="utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"',
                                 "Cache-Control": "no-store"})


async def api_progress_sessions_csv(request):
    return _csv_response(letter_progress.sessions_csv(), "sitzungen")


async def api_progress_letters_csv(request):
    return _csv_response(letter_progress.letters_csv(), "tasten")


async def _progress_saved_snapshot():
    """Save now (off the event loop), tell every parent page, answer with the new state."""
    await asyncio.get_running_loop().run_in_executor(None, _progress_save)
    snap = letter_progress.snapshot()
    sse_broadcast("progress", snap)
    return web.json_response(snap)


async def api_progress_config(request):
    try:
        data = await request.json()
        if not isinstance(data, dict) or "mastery" not in data:
            raise progress.ProgressError("body must be {\"mastery\": n}")
        letter_progress.set_mastery(data["mastery"])
    except ValueError as e:         # ProgressError, and a body that is not JSON
        return web.json_response({"error": str(e)}, status=400)
    return await _progress_saved_snapshot()


async def api_progress_reset(request):
    letter_progress.reset()
    print("[progress] reset by the parent page", flush=True)
    return await _progress_saved_snapshot()


# ── Key map API (FUNK-62) ────────────────────────────────────────────────────

def _key_name(raw):
    """The (layout-translated) key name for a code the keyboard sends."""
    from keyboard import LAYOUTS
    return LAYOUTS.get(_language(), {}).get(raw, raw)


def _builtin_for(raw):
    """(label, kind) of what a code does without a key-map entry; (None, None) if nothing."""
    key_name = _key_name(raw)
    has_content = bool(get_enabled_entries(key_name)) if len(key_name) <= 2 else False
    return keymap.default_label(key_name, has_content), keymap.default_kind(key_name, has_content)


def _keymap_payload():
    km = _active_keymap()
    pid = _keymap_profile()
    prof = profiles.get(pid, _custom_profiles())
    seen = sorted(keymap_seen_unassigned.items(), key=lambda kv: -kv[1])
    defaults = {}
    for code in profiles.codes(prof):
        raw, _ = keymap.parse_key_id(code) if code not in profiles.SHIFT_CODES else (code, ())
        label, kind = _builtin_for(raw)
        defaults[code] = {"label": label, "kind": kind}
    return {
        "profile": pid,
        "profile_name": prof["name"],
        "map": km,
        "describe": {kid: keymap.describe(a) for kid, a in km.items()},
        "functions": keymap.FUNCTIONS,
        "letters": list(keymap.LETTER_TARGETS),
        "seen_unassigned": [{"id": kid, "at": at} for kid, at in seen if kid not in km],
        "seen_codes": list(keyboard_seen),
        "defaults": defaults,
        # the state these defaults belong to: the UI installs a map only next to the same
        # language and "Standard" in its profile list (FUNK-64 review)
        "language": _language(), "default": _default_profile(), "selected": _selected_shown(),
    }


async def api_get_keymap(request):
    return web.json_response(_keymap_payload())


def _profile_mismatch(request):
    """409 if the request names a profile that is no longer the active one (it was switched meanwhile)."""
    wanted = request.query.get("profile")
    if wanted and wanted != _keymap_profile():
        return web.json_response({"error": "the keyboard profile was switched: reload", "profile": _keymap_profile()},
                                 status=409)
    return None


async def api_put_keymap(request):
    kid = request.match_info["key_id"]
    try:
        keymap.parse_key_id(kid)
        action = keymap.validate(await request.json())
    except (keymap.KeymapError, ValueError) as e:
        return web.json_response({"error": str(e)}, status=400)
    # checked after the body is read: no await between here and the write
    mismatch = _profile_mismatch(request)
    if mismatch:
        return mismatch
    if action["action"] in ("sound", "imgsound") and not os.path.isfile(os.path.join(SOUNDS_DIR, action["file"])):
        return web.json_response({"error": f"sound not found: {action['file']}"}, status=400)
    if action["action"] == "imgsound" and not os.path.isfile(os.path.join(IMAGES_DIR, action["image"])):
        return web.json_response({"error": f"image not found: {action['image']}"}, status=400)
    maps = settings.setdefault("keymaps", {})
    maps.setdefault(_keymap_profile(), {})[kid] = action
    save_settings()
    print(f"[keymap] {kid} = {keymap.describe(action)}", flush=True)
    return web.json_response(_keymap_payload())


async def api_delete_keymap(request):
    kid = request.match_info["key_id"]
    try:
        keymap.parse_key_id(kid, allow_reserved=True)   # an old Shift entry can still be removed
    except keymap.KeymapError as e:
        return web.json_response({"error": str(e)}, status=400)
    mismatch = _profile_mismatch(request)
    if mismatch:
        return mismatch
    removed = (settings.get("keymaps") or {}).get(_keymap_profile(), {}).pop(kid, None)
    if removed is not None:
        save_settings()
        print(f"[keymap] {kid} back to built-in behaviour", flush=True)
    return web.json_response(_keymap_payload())


async def api_press(request):
    """Press a key by id, like the physical keyboard (virtual input; ?dry=1 only resolves)."""
    kid = request.match_info["key_id"]
    try:
        raw, mods = keymap.parse_key_id(kid)
    except keymap.KeymapError as e:
        return web.json_response({"error": str(e)}, status=400)
    key_name = _key_name(raw)
    _, matched, action = _resolve_press(key_name, raw, mods)
    result = {"id": kid, "key": key_name, "matched": matched, "action": action,
              "describe": keymap.describe(action), "default": _builtin_for(raw)[0]}
    if request.query.get("dry") not in ("1", "true"):
        handle_key(key_name, raw, mods, virtual=True)
        result["pressed"] = True
    return web.json_response(result)


# ── Keyboard profiles API (FUNK-63) ──────────────────────────────────────────

def _selected_shown():
    """The choice to show in the UI: "default", a profile id, or what is really active for an unknown one."""
    selected = _selected_profile()
    if selected != profiles.DEFAULT_ALIAS and not profiles.get(selected, _custom_profiles()):
        return _keymap_profile()
    return selected


def _keyboards_payload():
    custom = _custom_profiles()
    default = _default_profile()
    return {"active": _keymap_profile(), "selected": _selected_shown(),
            "default": default, "default_name": profiles.BUILTIN[default]["name"],
            "language": _language(), "family": profiles.family_of(settings), "numpad": profiles.numpad_of(settings),
            "languages": profiles.LANGUAGES, "families": profiles.FAMILIES,
            "icons": list(profiles.ICONS),
            "profiles": [profiles.summary(p) for p in profiles.all_profiles(custom)]}


async def _json_object(request, optional=False):
    """The request body as a dict; raises ValueError with a readable message."""
    if optional and not request.can_read_body:
        return {}
    try:
        body = await request.json()
    except ValueError:
        raise ValueError("body must be JSON") from None
    if body is None and optional:
        return {}
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    return body


def _custom_or_error(pid):
    """(profile, None) for a custom profile, else (None, error response)."""
    prof = _custom_profiles().get(pid)
    if prof:
        return prof, None
    if pid in profiles.BUILTIN:
        return None, web.json_response({"error": "built-in profiles are read-only: duplicate it first"}, status=400)
    return None, web.json_response({"error": "profile not found"}, status=404)


def _store_custom(prof):
    _write_customs(lambda stored: stored.__setitem__(prof["id"], prof))
    save_settings()


def _profile_switched():
    pid = _keymap_profile()
    sse_broadcast("keyboard_profile", {"profile": pid, "selected": _selected_shown(),
                                       "language": _language(), "default": _default_profile(), "name": profiles.get(pid, _custom_profiles())["name"]})


async def api_get_keyboards(request):
    return web.json_response(_keyboards_payload())


async def api_get_keyboard(request):
    pid = request.match_info["profile_id"]
    prof = profiles.get(_default_profile() if pid == profiles.DEFAULT_ALIAS else pid, _custom_profiles())
    if not prof:
        return web.json_response({"error": "profile not found"}, status=404)
    return web.json_response(profiles.render(prof))


async def api_activate_keyboard(request):
    pid = request.match_info["profile_id"]
    if pid != profiles.DEFAULT_ALIAS and not profiles.get(pid, _custom_profiles()):
        return web.json_response({"error": "profile not found"}, status=404)
    settings["keyboard_profile"] = pid        # "default" follows language × PC/Mac × number pad
    save_settings()
    print(f"[keyboard] profile → {pid}", flush=True)
    _profile_switched()
    return web.json_response(_keyboards_payload())


async def api_duplicate_keyboard(request):
    try:
        body = await _json_object(request, optional=True)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    # from here on no await: read, allocate the id and write in one step (FUNK-63 review)
    custom = _custom_profiles()
    source = profiles.get(request.match_info["profile_id"], custom)
    if not source:
        return web.json_response({"error": "profile not found"}, status=404)
    if len(settings.get("keyboard_profiles") or {}) >= profiles.MAX_CUSTOM:
        return web.json_response({"error": f"at most {profiles.MAX_CUSTOM} custom keyboards: delete one first"}, status=400)
    try:
        name = body.get("name") or profiles.copy_name(source["name"])
        if not isinstance(name, str):
            raise ValueError("name must be text")
        prof = profiles.duplicate(source, profiles.new_id(name, set(custom) | set(settings.get("keyboard_profiles") or {})), name)
    except ValueError as e:      # ProfileError is a ValueError
        return web.json_response({"error": str(e)}, status=400)
    # "activate_if_active": the copy becomes active only if its source still is, decided
    # here in the same step as the copy (another browser may have switched meanwhile)
    follow = body.get("activate_if_active") is True and _keymap_profile() == source["id"]
    _store_custom(prof)
    # the copy starts with the source's key map
    maps = settings.setdefault("keymaps", {})
    maps[prof["id"]] = json.loads(json.dumps(maps.get(source["id"], {})))
    if follow:
        settings["keyboard_profile"] = prof["id"]
    save_settings()
    print(f"[keyboard] profile {source['id']} duplicated → {prof['id']}" + (" (active)" if follow else ""), flush=True)
    if follow:
        _profile_switched()
    return web.json_response(dict(_keyboards_payload(), id=prof["id"], activated=follow))


async def api_put_keyboard(request):
    try:
        body = await _json_object(request)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    prof, err = _custom_or_error(request.match_info["profile_id"])   # current state, after the await
    if err:
        return err
    try:
        prof = profiles.rename(prof, name=body.get("name"), note=body.get("note"))
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    _store_custom(prof)
    if prof["id"] == _keymap_profile():
        _profile_switched()
    return web.json_response(_keyboards_payload())


async def api_put_keyboard_key(request):
    try:
        patch = await _json_object(request)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    prof, err = _custom_or_error(request.match_info["profile_id"])   # current state, after the await
    if err:
        return err
    try:
        prof = profiles.edit_key(prof, request.match_info["slot"], patch)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    _store_custom(prof)
    print(f"[keyboard] {prof['id']}: key {request.match_info['slot']} edited", flush=True)
    return web.json_response(profiles.render(prof))


async def api_delete_keyboard(request):
    pid = request.match_info["profile_id"]
    _, err = _custom_or_error(pid)
    if err:
        return err
    was_active = pid == _keymap_profile()
    _write_customs(lambda stored: stored.pop(pid, None))
    (settings.get("keymaps") or {}).pop(pid, None)
    if was_active:
        settings["keyboard_profile"] = keymap.DEFAULT_PROFILE
    save_settings()
    print(f"[keyboard] profile {pid} deleted", flush=True)
    if was_active:
        _profile_switched()
    return web.json_response(_keyboards_payload())


# ── Language, export / import (FUNK-64) ──────────────────────────────────────

async def api_get_language(request):
    return web.json_response(_keyboards_payload())


async def api_put_language(request):
    """Set language, PC/Mac and number pad. Body: any of {"language", "family", "numpad"}."""
    try:
        body = await _json_object(request)
        lang = body.get("language", _language())
        family = body.get("family", profiles.family_of(settings))
        numpad = body.get("numpad", profiles.numpad_of(settings))
        if not isinstance(lang, str) or lang not in profiles.LANGUAGES:
            raise ValueError(f"language must be one of {', '.join(profiles.LANGUAGES)}")
        if not isinstance(family, str) or family not in profiles.FAMILIES:
            raise ValueError(f"family must be one of {', '.join(profiles.FAMILIES)}")
        if not isinstance(numpad, bool):
            raise ValueError("numpad must be true or false")
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    # no await from here on: read, write and announce in one step
    from keyboard import LAYOUTS
    settings["language"] = lang
    settings["keyboard_layout"] = lang        # kept in step: an older image (rollback) reads keyboard_layout
    settings["keyboard_family"] = family
    settings["keyboard_numpad"] = numpad
    if keyboard:
        keyboard.layout = LAYOUTS.get(lang, {})
    save_settings()
    print(f"[keyboard] language {lang}, standard = {_default_profile()}", flush=True)
    _profile_switched()                       # letters, "Standard" and maybe the active profile changed
    return web.json_response(_keyboards_payload())


async def api_export_keyboard(request):
    pid = request.match_info["profile_id"]
    prof = profiles.get(_default_profile() if pid == profiles.DEFAULT_ALIAS else pid, _custom_profiles())
    if not prof:
        return web.json_response({"error": "profile not found"}, status=404)
    bundle = profiles.export_bundle(prof, (settings.get("keymaps") or {}).get(prof["id"], {}))
    return web.json_response(bundle, headers={
        "Content-Disposition": f'attachment; filename="funkeykid-{prof["id"]}.json"'})


async def api_import_keyboard(request):
    """Import an exported keyboard as a new custom profile with its key map (never activates it)."""
    try:
        body = await _json_object(request)
        name = body.get("name")
        if name is not None and not isinstance(name, str):
            raise ValueError("name must be text")
        bundle = body.get("bundle")
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    # no await from here on: validate, allocate the id and write in one step
    if len(settings.get("keyboard_profiles") or {}) >= profiles.MAX_CUSTOM:
        return web.json_response({"error": f"at most {profiles.MAX_CUSTOM} custom keyboards: delete one first"}, status=400)
    custom = _custom_profiles()
    src = bundle.get("profile") if isinstance(bundle, dict) else None
    src_name = src.get("name") if isinstance(src, dict) else None
    wanted = (name or (src_name if isinstance(src_name, str) else "")).strip() or "Importierte Tastatur"
    try:
        prof, entries = profiles.import_bundle(
            bundle, profiles.new_id(wanted, set(custom) | set(settings.get("keyboard_profiles") or {})), wanted)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    # sounds and images must exist here; entries pointing elsewhere are skipped, not fatal
    kept, skipped = {}, []
    for kid, action in entries.items():
        missing = False
        if action["action"] in ("sound", "imgsound"):
            missing = not os.path.isfile(os.path.join(SOUNDS_DIR, action["file"]))
        if action["action"] == "imgsound":
            missing = missing or not os.path.isfile(os.path.join(IMAGES_DIR, action["image"]))
        if missing:
            skipped.append(kid)
        else:
            kept[kid] = action
    _store_custom(prof)
    settings.setdefault("keymaps", {})[prof["id"]] = kept
    save_settings()
    print(f"[keyboard] imported {prof['id']} ({len(kept)} key-map entries, {len(skipped)} skipped)", flush=True)
    return web.json_response(dict(_keyboards_payload(), id=prof["id"], imported=len(kept), skipped=skipped))


async def api_get_layout(request):
    from keyboard import LAYOUTS
    name = request.match_info["name"]
    return web.json_response(LAYOUTS.get(name, {}))


def _safe_file(folder, name):
    """The path of an existing regular file inside folder, or None (FUNK-73).

    name comes from a request: aiohttp decodes %2f inside a route's {filename} too, so
    "..%2f..%2fproc%2fself%2fenviron" arrives as a path. The resolved path (symlinks
    followed) must stay inside folder; a NUL or an invalid path is simply not found.
    """
    if not isinstance(name, str) or not name or "\x00" in name:
        return None
    try:
        base = os.path.realpath(folder)
        path = os.path.realpath(os.path.join(base, name))
    except (ValueError, OSError):
        return None
    if path == base or os.path.commonpath([base, path]) != base or not os.path.isfile(path):
        return None
    return path


async def serve_sound(request):
    path = _safe_file(SOUNDS_DIR, request.match_info["filename"])
    if not path:
        return web.Response(status=404)
    return web.FileResponse(path)


async def serve_image(request):
    path = _safe_file(IMAGES_DIR, request.match_info["filename"])
    if not path:
        return web.Response(status=404)
    return web.FileResponse(path)


THUMB_SIZES = (160, 480)


def _make_thumb(src, dst, size, pixel):
    """Write dst: an AI original scaled down to fit size×size (JPEG), or a library picture as PNG:
    a small one (the Pixoo's 64×64) scaled up by whole pixels, so it stays sharp; a big one
    (an uploaded photo) scaled down to fit."""
    from PIL import Image
    with Image.open(src) as im:
        if pixel:
            im = im.convert("RGBA")
            if max(im.size) > size:
                im.thumbnail((size, size), Image.LANCZOS)
            else:
                n = max(1, size // max(im.size))
                im = im.resize((im.width * n, im.height * n), Image.NEAREST)
            fmt, opts = "PNG", {"optimize": True}
        else:
            im = im.convert("RGB")
            im.thumbnail((size, size), Image.LANCZOS)
            fmt, opts = "JPEG", {"quality": 84, "optimize": True}
        tmp = f"{dst}.{uuid.uuid4().hex}.tmp"
        try:
            im.save(tmp, fmt, **opts)
            os.replace(tmp, dst)
        finally:
            _remove_quiet(tmp)


async def serve_thumb(request):
    """A picture sized for the parent page (FUNK-72): the AI original scaled down when there is
    one (a 1 MB original per tile was too heavy), else the library picture scaled up sharp.
    Cached under ai-generated/thumbs; rebuilt when the picture is newer."""
    try:
        size = int(request.match_info["size"])
    except ValueError:
        return web.Response(status=404)
    name = request.match_info["filename"]
    if size not in THUMB_SIZES or not isinstance(name, str) or not _MEDIA_NAME.fullmatch(name) or name.startswith("."):
        return web.Response(status=404)
    orig, lib = _safe_file(AI_IMAGES_ORIG_DIR, name), _safe_file(IMAGES_DIR, name)   # FUNK-73 containment
    if orig and lib and os.path.getmtime(lib) - os.path.getmtime(orig) > 60:
        orig = None                             # the library picture was replaced later (a set import): it wins
    if orig:
        src, pixel = orig, False
    elif lib:
        src, pixel = lib, True
    else:
        return web.Response(status=404)
    dst = os.path.join(AI_DIR, "thumbs", str(size), name + (".png" if pixel else ".jpg"))
    try:
        fresh = os.path.isfile(dst) and os.path.getmtime(dst) >= os.path.getmtime(src)
        if not fresh:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            await asyncio.get_running_loop().run_in_executor(None, _make_thumb, src, dst, size, pixel)
    except Exception as e:                       # not a picture PIL reads: the file itself, as it is
        print(f"[thumbs] {name}: {e}", flush=True)
        return web.FileResponse(src)
    resp = web.FileResponse(dst)
    resp.headers["Cache-Control"] = "no-cache"   # revalidated each time (304 if unchanged): a replaced picture shows at once
    return resp


async def serve_index(request):
    resp = web.FileResponse(os.path.join(STATIC_DIR, "index.html"))
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


async def api_version(request):
    from version import build_time
    # Machine-facing: canonical version + explicit scheme (never inferred from shape).
    return web.json_response({
        "version": VERSION,
        "version_scheme": VERSION_SCHEME,
        "release_channel": RELEASE_CHANNEL,
        "release_sequence": RELEASE_SEQUENCE,
        "build": BUILD,
        "repo": REPO,
        "build_time": build_time(),
    })


# ── AI Generation ───────────────────────────────────────────────────────────

AI_DIR = os.path.join(DATA_DIR, "ai-generated")
AI_SOUNDS_DIR = os.path.join(AI_DIR, "sounds")
AI_IMAGES_ORIG_DIR = os.path.join(AI_DIR, "images-original")
AI_IMAGES_RESIZED_DIR = os.path.join(AI_DIR, "images-resized")
SOUND_DIRS = (SOUNDS_DIR, AI_SOUNDS_DIR)          # where a generated sound lands
IMAGE_DIRS = (IMAGES_DIR, AI_IMAGES_ORIG_DIR, AI_IMAGES_RESIZED_DIR)


# ── Loudness normalization (FUNK-1) ─────────────────────────────────────────
# EBU R128 loudnorm applied to every sound that lands in /data/sounds, so FX,
# TTS and parent recordings all sit at the same perceived loudness.
# Pipeline: raw clip → ffmpeg loudnorm → /data/sounds/<name>.mp3
# The untouched AI original stays in /data/ai-generated/sounds so re-generation
# and A/B comparison remain possible.

def _normalize_enabled():
    return bool(settings.get("audio_normalize", True))


def _normalize_target_lufs():
    try:
        return float(settings.get("audio_normalize_lufs", -16.0))
    except (TypeError, ValueError):
        return -16.0


def normalize_mp3(in_path, out_path):
    """Apply EBU R128 loudnorm with ffmpeg. Returns True on success.

    Falls back to `shutil.copy2` if ffmpeg is absent, normalization is
    disabled, or the encode fails — the caller always gets a usable file.
    """
    import shutil
    if not _normalize_enabled():
        shutil.copy2(in_path, out_path)
        return False
    target = _normalize_target_lufs()
    try:
        # Two-pass would be more accurate but slow. Single-pass with loudnorm
        # is sufficient for kid-toy fidelity and finishes in ~1s per clip.
        proc = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(in_path),
                "-af", f"loudnorm=I={target}:LRA=11:TP=-1.5",
                "-ar", "44100",  # keep sample rate consistent
                "-b:a", "128k",
                "-loglevel", "error",
                str(out_path),
            ],
            capture_output=True, timeout=60,
        )
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace")[:300]
            print(f"[norm] ffmpeg failed on {in_path}: {err}", flush=True)
            shutil.copy2(in_path, out_path)
            return False
        return True
    except FileNotFoundError:
        print("[norm] ffmpeg not installed — copying raw", flush=True)
        shutil.copy2(in_path, out_path)
        return False
    except subprocess.TimeoutExpired:
        print(f"[norm] ffmpeg timeout on {in_path}", flush=True)
        try:
            shutil.copy2(in_path, out_path)
        except Exception:
            pass
        return False
    except Exception as e:
        print(f"[norm] unexpected error on {in_path}: {e}", flush=True)
        try:
            shutil.copy2(in_path, out_path)
        except Exception:
            pass
        return False

FALLBACK_SOUND_PROMPT = "Clear, distinct {word} sound effect, high quality, recognizable for children"
FALLBACK_IMAGE_PROMPT = "Pixar-style 3D cartoon of {description}. Vibrant saturated colors, soft lighting, rounded friendly shapes, big expressive eyes. Simple composition, recognizable at 64x64 pixels. Studio quality children's animation style."
FALLBACK_SUGGEST_PROMPT = """Schlage EIN deutsches Wort vor, das mit dem Buchstaben {letter} beginnt.

Anforderungen:
- Sprache: Deutsch (Österreich)
- Zielgruppe: Kinder 2-5 Jahre
- Das Wort muss ein konkretes Ding/Tier/Objekt sein (kein abstraktes Konzept)
- Es muss ein dazu passendes, klar erkennbares Geräusch geben
- Nicht anstößig, kindgerecht
- VERBOTEN — diese Wörter dürfen NICHT vorgeschlagen werden: {excluded}
- Wenn du eines dieser verbotenen Wörter vorschlägst, ist die Antwort FALSCH

Antworte NUR mit einem JSON-Objekt (keine Erklärung):
{{"word": "Wort", "sound_description": "Geräusch auf Englisch", "image_description": "Bildbeschreibung auf Englisch"}}"""


def _get_sound_prompt():
    return settings.get("ai_prompts", {}).get("sound", FALLBACK_SOUND_PROMPT)


def _get_image_prompt():
    return settings.get("ai_prompts", {}).get("image", FALLBACK_IMAGE_PROMPT)


def _get_suggest_prompt():
    return settings.get("ai_prompts", {}).get("suggest", FALLBACK_SUGGEST_PROMPT)


def _ai_log_entry(action, model, prompt_sent, response_text, status="ok"):
    """Log an AI request/response for the debug tab."""
    entry = {
        "timestamp": time.time(),
        "action": action,
        "model": model,
        "prompt": prompt_sent[:500],
        "response": response_text[:500] if response_text else "",
        "status": status,
    }
    ai_log.append(entry)
    if len(ai_log) > AI_LOG_MAX:
        ai_log.pop(0)
    sse_broadcast("ai_log", entry)


_PROVIDER_NAMES = {"elevenlabs": "ElevenLabs", "openrouter": "OpenRouter"}


def _provider_error(provider, exc):
    """A failed AI call as (message for the parent, HTTP status or None, the provider's own words).

    FUNK-72: the page showed "HTTP Error 402: Payment Required". The message says what
    happened and what to do, in German; the provider's text stays for the log and the
    details line. An error body is read at most 4 KiB and never echoed in full.
    """
    import urllib.error
    name = _PROVIDER_NAMES.get(provider, provider)
    if isinstance(exc, urllib.error.HTTPError):
        code, detail = exc.code, ""
        try:
            raw = exc.read(4096).decode("utf-8", "replace")
        except Exception:
            raw = ""
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        if isinstance(body, dict):
            d, err = body.get("detail"), body.get("error")
            if isinstance(d, dict):
                detail = str(d.get("message") or d.get("status") or "")
            elif isinstance(d, str):
                detail = d
            elif isinstance(err, dict):
                detail = str(err.get("message") or "")
            elif isinstance(err, str):
                detail = err
        elif raw and not raw.lstrip().startswith("<"):
            detail = raw
        detail = " ".join(detail.split())[:300]
        low = detail.lower()
        if provider == "elevenlabs" and "quota" in low:
            msg = (f"Das {name}-Kontingent ist aufgebraucht. Unter KI & Dienste siehst du den Stand; "
                   "es füllt sich zum nächsten Abrechnungszeitraum wieder auf.")
        elif code == 401:
            msg = f"{name} hat den Schlüssel abgelehnt (ungültig oder abgelaufen). Prüfe ihn unter KI & Dienste."
        elif code == 402 and provider == "openrouter":
            msg = "Das OpenRouter-Guthaben reicht nicht. Lade es auf openrouter.ai auf."
        elif code == 402:
            msg = (f"{name} verlangt dafür ein bezahltes Abo (zum Beispiel für diese Stimme). "
                   "Prüfe unter KI & Dienste, zu welchem Konto der Schlüssel von funkeykid gehört.")
        elif code == 403:
            msg = f"{name} erlaubt diesem Schlüssel das nicht. Prüfe seine Berechtigungen bei {name}."
        elif code == 429:
            msg = f"{name} bekommt gerade zu viele Anfragen. Versuch es in einer Minute nochmal."
        elif 500 <= code < 600:
            msg = f"{name} hat gerade ein Problem (Fehler {code}). Versuch es gleich nochmal."
        elif code in (400, 422):
            msg = f"{name} hat die Anfrage abgelehnt" + (f": {detail}" if detail else ".")
        else:
            msg = f"{name} hat mit Fehler {code} geantwortet."
        return msg, code, detail
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return f"Keine Verbindung zu {name}. Ist das Internet erreichbar?", None, str(exc)[:300]
    if isinstance(exc, ValueError):                 # our own checks: empty result, no image data
        return f"{name}: {exc}", None, str(exc)[:300]
    return f"{name}: unerwarteter Fehler ({type(exc).__name__}).", None, str(exc)[:300]


def _job_failed(job_id, provider, exc, action, model, prompt):
    """Mark a generation job failed with the parent-facing reason, and log the provider's words."""
    msg, code, detail = _provider_error(provider, exc)
    gen_jobs[job_id].update({"status": "error", "error": msg, "error_code": code,
                             "error_detail": detail, "finished": time.time()})
    _ai_log_entry(action, model, prompt, f"{code or ''} {detail or exc}".strip(), "error")


class _TargetError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status, self.msg = status, msg


def _job_target(raw, kind):
    """Where a job's file goes when it is done (FUNK-72): {set_id, letter, entry_id} → with kind.

    Checked when the job starts (the entry must exist); applied by _apply_job_target only if
    that slot is still empty then. None when the request names no target."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or not all(isinstance(raw.get(k), str) for k in ("set_id", "letter", "entry_id")):
        raise _TargetError(400, "target must be {set_id, letter, entry_id}")
    letter = keymap.norm_letter(raw["letter"])
    if _slot_entry(raw["set_id"], letter, raw["entry_id"]) is None:
        raise _TargetError(404, "entry not found")
    if letter in NUMBER_KEYS and kind == "IMG":
        kind = "BG"                          # a number's picture job adds a background (FUNK-76)
        bgs = _slot_entry(raw["set_id"], letter, raw["entry_id"]).get("backgrounds")
        if isinstance(bgs, list) and len(bgs) >= NUMBER_BG_MAX:
            raise _TargetError(400, f"Diese Zahl hat schon {NUMBER_BG_MAX} Hintergründe. Entferne zuerst einen.")
    return {"set_id": raw["set_id"], "letter": letter, "entry_id": raw["entry_id"], "kind": kind}


def _slot_entry(set_id, letter, entry_id):
    """The entry a job target names: a letter's entry by id, or a digit's config (id "number")."""
    if letter in NUMBER_KEYS:
        if entry_id != NUMBER_ENTRY_ID:
            return None
        return _number_slot(set_id, letter)[1]
    return _find_entry(_letter_entries(set_id, letter), entry_id)[1]


def _job_finished(job_id):
    """A worker's last word on its job (from its thread). A successful job becomes "done" only on
    the event loop (_finalize_job), where every settings write runs: until then it is still
    "generating" and a parent's explicit choice can detach it (FUNK-72). A failed job is announced."""
    job = gen_jobs.get(job_id)
    if job is not None and (job.get("_done") or job.get("status") == "done"):
        if _loop is not None:
            _loop.call_soon_threadsafe(_finalize_job, job_id)
        else:
            _finalize_job(job_id)
        return
    sse_broadcast("gen_update", job)


def _finalize_job(job_id):
    """On the event loop: mark the job done and, if it still has a target, put its file on that
    entry when the slot is empty; then tell the pages, with applied true/false."""
    job = gen_jobs.get(job_id)
    if job is None:
        return
    job.pop("_done", None)
    job["status"] = "done"
    t, applied = job.get("target"), False       # a detached job has no target any more
    if t:
        e = _slot_entry(t["set_id"], t["letter"], t["entry_id"])
        fname, kind = job.get("filename"), t["kind"]
        folder = IMAGES_DIR if kind in ("IMG", "BG") else SOUNDS_DIR
        if e is not None and job.get("batch") and (not _fill_wanted(e, kind) or _fill_text(e, kind) != job.get("text")):
            # FUNK-75: a fill run's voice for a word that changed meanwhile (renamed, new spoken text,
            # switched off or filled): it only goes to the library, and no page offers it for review
            job["target"] = None
        elif e is not None and fname and os.path.isfile(os.path.join(folder, fname)):
            if t["letter"] in NUMBER_KEYS:
                _normalize_number_entry(e)
                if not isinstance(e.get("backgrounds"), list):
                    e["backgrounds"] = []
            else:
                _normalize_letter_entry(e)
            if kind == "BG":                 # a background is added, never replaces one (FUNK-76)
                # already there: done; the digit full: it waits for review ("current vs new")
                current = fname if _bg_index(e, fname) >= 0 else ("full" if len(e["backgrounds"]) >= NUMBER_BG_MAX else "")
            else:
                current = e.get("image") if kind == "IMG" else (e["tracks"].get(kind) or {}).get("file")
            if not current:
                before = json.dumps(e)
                if kind == "BG":
                    e["backgrounds"].append({"image": fname, "enabled": True})
                    _bg_token_new(t["set_id"], t["letter"], fname)
                elif kind == "IMG":
                    e["image"] = fname
                else:
                    track = e["tracks"].setdefault(kind, {"file": "", "enabled": True, "prompt": "", "volume": 100})
                    track["file"], track["enabled"] = fname, True
                try:
                    save_settings()
                    applied = True
                except Exception as ex:
                    e.clear()
                    e.update(json.loads(before))
                    job["save_failed"] = True      # its file waits for review; a fill run stops (FUNK-75)
                    print(f"[gen] {job_id}: could not save the file on its entry: {ex}", flush=True)
                if applied:
                    sse_broadcast("letter", {"set_id": t["set_id"], "letter": t["letter"], "entry_id": t["entry_id"], "origin": "job"})
            elif kind == "BG" and current == fname:
                applied = True               # that background is already on the digit
    if "target" in job:
        job["applied"] = applied
    job["finished"] = job.get("finished") or time.time()
    sse_broadcast("gen_update", job)


_apply_job_target = _finalize_job              # the name the tests and the docs used first


async def api_get_jobs(request):
    """Every generation job this server process knows; X-FK-Boot names the process (FUNK-72)."""
    resp = web.json_response(list(gen_jobs.values()))
    resp.headers["X-FK-Boot"] = BOOT_ID
    return resp


async def api_job_detach(request):
    """The parent chose something else for the slot (FUNK-72): the running job keeps going, but its
    file only lands in the library. Runs on the event loop like _apply_job_target: never both at once."""
    job = gen_jobs.get(request.match_info["job_id"])
    if job is None:
        return web.json_response({"error": "job not found"}, status=404)
    running = job.get("status") in ("queued", "generating")
    had = job.get("target") is not None
    # finished: the parent took or discarded its file ("reviewed"), so no page offers it again
    job["target"] = None
    if had:
        sse_broadcast("gen_update", job)
    if running:
        return web.json_response({"ok": True, "detached": True})
    return web.json_response({"ok": True, "detached": False, "status": job.get("status")})


def _gen_sound_worker(job_id, word, prompt, duration, filename):
    """Background worker for sound generation."""
    import urllib.request, shutil
    gen_jobs[job_id]["status"] = "generating"
    sse_broadcast("gen_update", gen_jobs[job_id])
    api_key = os.environ.get("ELEVENLABS_API_KEY", "")
    _ai_log_entry("sound", "elevenlabs/sfx", prompt, None, "sending")
    try:
        os.makedirs(AI_SOUNDS_DIR, exist_ok=True)      # inside: a failure ends the job (FUNK-75)
        payload = json.dumps({"text": prompt, "duration_seconds": duration, "prompt_influence": 0.6}).encode()
        req = urllib.request.Request("https://api.elevenlabs.io/v1/sound-generation",
            data=payload, headers={"xi-api-key": api_key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            audio_data = resp.read()
        if len(audio_data) < 1000:
            raise ValueError("Generierung fehlgeschlagen (zu klein)")
        normed = _store_generated_sound(audio_data, filename)      # FUNK-73: staged, published exclusively
        gen_jobs[job_id].update({"_done": True, "size": len(audio_data), "path": f"/sounds/{filename}",
                                 "normalized": normed, "finished": time.time()})
        _ai_log_entry("sound", "elevenlabs/sfx", prompt,
                      f"OK: {len(audio_data)} bytes → {filename}" + (" (normalized)" if normed else ""),
                      "ok")
    except Exception as e:
        _job_failed(job_id, "elevenlabs", e, "sound", "elevenlabs/sfx", prompt)
    _job_finished(job_id)


async def api_generate_sound(request):
    """Start async sound generation — returns job ID immediately."""
    data = await request.json()
    word = data.get("word", "")
    prompt = data.get("prompt", _get_sound_prompt().format(word=word))
    duration = data.get("duration", 4)
    filename = _claim_media_name(data.get("filename"), _slugify(word), "mp3", SOUND_DIRS)
    if not filename:
        return web.json_response({"error": "invalid filename"}, status=400)
    if not os.environ.get("ELEVENLABS_API_KEY"):
        return web.json_response({"error": "ELEVENLABS_API_KEY nicht gesetzt"}, status=500)
    job_id = f"snd_{uuid.uuid4().hex[:8]}"
    _register_job({"id": job_id, "type": "sound", "word": word, "filename": filename, "status": "queued", "started": time.time()})
    sse_broadcast("gen_update", gen_jobs[job_id])
    import threading
    threading.Thread(target=_gen_sound_worker, args=(job_id, word, prompt, duration, filename), daemon=True).start()
    return web.json_response({"ok": True, "job_id": job_id, "filename": filename})


def _gen_image_worker(job_id, word, prompt, filename, model=None):
    """Background worker for image generation."""
    import urllib.request, base64, shutil
    gen_jobs[job_id]["status"] = "generating"
    sse_broadcast("gen_update", gen_jobs[job_id])
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    os.makedirs(AI_IMAGES_ORIG_DIR, exist_ok=True)
    os.makedirs(AI_IMAGES_RESIZED_DIR, exist_ok=True)
    model = model or settings.get("ai_prompts", {}).get("image_model", "google/gemini-3-pro-image-preview")
    _ai_log_entry("image", model, prompt, None, "sending")
    try:
        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": f"Generate a 512x512 image: {prompt}"}],
        }).encode()
        req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions",
            data=payload, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=90) as resp:
            d = json.load(resp)
        images = d["choices"][0]["message"].get("images", [])
        if not images:
            raise ValueError("Keine Bilddaten erhalten")
        b64 = images[0]["image_url"]["url"].split(",", 1)[1]
        img_data = base64.b64decode(b64)
        # FUNK-73: staged first, then published exclusively (never over a file, never through a link)
        orig = _staging(filename.rpartition(".")[2] or "png")
        small = _staging(filename.rpartition(".")[2] or "png")
        try:
            with open(orig, "xb") as f:
                f.write(img_data)
            try:
                from PIL import Image
                from io import BytesIO
                img = Image.open(BytesIO(img_data))
                img = img.resize((64, 64), Image.LANCZOS)
                img.save(small, format=img.format or "PNG")
            except ImportError:
                with open(small, "wb") as f:
                    f.write(img_data)
            os.makedirs(IMAGES_DIR, exist_ok=True)
            _publish_exact(small, os.path.join(IMAGES_DIR, filename))
            for src, dst in ((orig, AI_IMAGES_ORIG_DIR), (small, AI_IMAGES_RESIZED_DIR)):
                try:
                    _publish_exact(src, os.path.join(dst, filename))
                except OSError:
                    pass
        finally:
            _remove_quiet(orig)
            _remove_quiet(small)
        gen_jobs[job_id].update({"_done": True, "size": len(img_data), "path": f"/images/{filename}",
                                 "finished": time.time()})
        _ai_log_entry("image", model, prompt, f"OK: {len(img_data)}b → {filename}", "ok")
    except Exception as e:
        _job_failed(job_id, "openrouter", e, "image", model, prompt)
    _job_finished(job_id)


async def api_generate_image(request):
    """Start async image generation — returns job ID immediately."""
    data = await request.json()
    word = data.get("word", "")
    description = data.get("description", word)
    prompt = data.get("prompt", _get_image_prompt().format(description=description))
    filename = _claim_media_name(data.get("filename"), _slugify(word), "png", IMAGE_DIRS)
    if not filename:
        return web.json_response({"error": "invalid filename"}, status=400)
    model = data.get("model")
    try:
        target = _job_target(data.get("target"), "IMG")
    except _TargetError as e:
        return web.json_response({"error": e.msg}, status=e.status)
    if not os.environ.get("OPENROUTER_API_KEY"):
        return web.json_response({"error": "OPENROUTER_API_KEY nicht gesetzt"}, status=500)
    job_id = f"img_{uuid.uuid4().hex[:8]}"
    _register_job({"id": job_id, "type": "image", "kind": "IMG", "word": word, "filename": filename,
                   "status": "queued", "started": time.time(), "target": target})
    sse_broadcast("gen_update", gen_jobs[job_id])
    import threading
    threading.Thread(target=_gen_image_worker, args=(job_id, word, prompt, filename, model), daemon=True).start()
    return web.json_response({"ok": True, "job_id": job_id, "filename": filename})


def _gen_tts_worker(job_id, text, voice_id, filename):
    """Background worker for ElevenLabs TTS (text-to-speech) — used for number words."""
    import urllib.request, shutil
    gen_jobs[job_id]["status"] = "generating"
    sse_broadcast("gen_update", gen_jobs[job_id])
    api_key = os.environ.get("ELEVENLABS_API_KEY", "")
    _ai_log_entry("tts", f"elevenlabs/tts/{voice_id}", text, None, "sending")
    try:
        os.makedirs(AI_SOUNDS_DIR, exist_ok=True)      # inside: a failure ends the job (FUNK-75)
        payload = json.dumps({
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "style": 0.0, "use_speaker_boost": True},
        }).encode()
        req = urllib.request.Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            data=payload,
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            audio_data = resp.read()
        if len(audio_data) < 1000:
            raise ValueError("TTS-Ausgabe zu klein")
        normed = _store_generated_sound(audio_data, filename)      # FUNK-73: staged, published exclusively
        gen_jobs[job_id].update({"_done": True, "size": len(audio_data), "path": f"/sounds/{filename}",
                                 "normalized": normed, "finished": time.time()})
        _ai_log_entry("tts", f"elevenlabs/tts/{voice_id}", text,
                      f"OK: {len(audio_data)}b → {filename}" + (" (normalized)" if normed else ""),
                      "ok")
    except Exception as e:
        _job_failed(job_id, "elevenlabs", e, "tts", f"elevenlabs/tts/{voice_id}", text)
    _job_finished(job_id)


async def api_generate_tts(request):
    """Start TTS generation — returns job ID immediately. Uses ElevenLabs multilingual v2."""
    data = await request.json()
    text = (data.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "text fehlt"}, status=400)
    voice_id = (data.get("voice_id")
                or settings.get("numbers_tts", {}).get("voice_id")
                or DEFAULT_NUMBER_VOICE_ID)
    filename = _claim_media_name(data.get("filename"), _slugify(text), "mp3", SOUND_DIRS)
    if not filename:
        return web.json_response({"error": "invalid filename"}, status=400)
    word = data.get("word", text)
    if not os.environ.get("ELEVENLABS_API_KEY"):
        return web.json_response({"error": "ELEVENLABS_API_KEY nicht gesetzt"}, status=500)
    job_id = f"tts_{uuid.uuid4().hex[:8]}"
    _register_job({"id": job_id, "type": "sound", "word": word, "filename": filename, "status": "queued", "started": time.time()})
    sse_broadcast("gen_update", gen_jobs[job_id])
    import threading
    threading.Thread(target=_gen_tts_worker, args=(job_id, text, voice_id, filename), daemon=True).start()
    return web.json_response({"ok": True, "job_id": job_id, "filename": filename})


async def api_generate_track(request):
    """Dispatch generation for a single track based on its `kind`.

    Body:
      kind: "FX" | "DE" | "EN"  — track kind
      word: entry word (used for filename slug + default prompts)
      slug_prefix: optional "a" (letter) or "3" (digit) so files are grouped
      prompt: optional override. For FX → sound description; for TTS → text to speak
      filename: optional explicit filename (else auto-generated)
      duration: optional (FX only)

    Returns {ok, job_id, filename} — generation runs in background, listen to SSE.
    """
    data = await request.json()
    kind = (data.get("kind") or "").upper()
    if kind not in TRACK_KINDS:
        return web.json_response({"error": f"Unbekannte Art: {kind!r}"}, status=400)
    meta = TRACK_KIND_META[kind]
    word = (data.get("word") or "").strip()
    if not word:
        return web.json_response({"error": "word fehlt"}, status=400)
    prefix = (data.get("slug_prefix") or "").strip().lower()
    slug = _slugify(word)
    base = f"{prefix}_{slug}_{kind.lower()}" if prefix else f"{slug}_{kind.lower()}"
    try:
        target = _job_target(data.get("target"), kind)
    except _TargetError as e:
        return web.json_response({"error": e.msg}, status=e.status)
    # what the job says (TTS) or describes (FX)
    if meta["generator"] == "sfx":
        want = data.get("prompt") or _get_sound_prompt().format(word=word)
    else:
        want = (data.get("prompt") or word).strip()
    if target:
        # a job already making the same for this slot (a fill run, another page): that one, never a
        # second paid one (FUNK-75). Another text gets its own job: the parent asked for that one.
        running = next((j for j in _slot_jobs(target["set_id"], target["letter"], target["entry_id"], kind)
                        if j.get("text") == want), None)
        if running is not None:
            return web.json_response({"ok": True, "job_id": running["id"], "filename": running.get("filename"), "existing": True})
    filename = _claim_media_name(data.get("filename"), base, "mp3", SOUND_DIRS)
    if not filename:
        return web.json_response({"error": "invalid filename"}, status=400)

    if meta["generator"] == "sfx":
        if not os.environ.get("ELEVENLABS_API_KEY"):
            return web.json_response({"error": "ELEVENLABS_API_KEY nicht gesetzt"}, status=500)
        prompt = want
        duration = data.get("duration", 4)
        job_id = f"trk_{uuid.uuid4().hex[:8]}"
        _register_job({"id": job_id, "type": "sound", "word": word, "filename": filename, "kind": kind,
                       "status": "queued", "started": time.time(), "target": target, "text": want})
        sse_broadcast("gen_update", gen_jobs[job_id])
        threading.Thread(target=_gen_sound_worker, args=(job_id, word, prompt, duration, filename), daemon=True).start()
        return web.json_response({"ok": True, "job_id": job_id, "filename": filename})

    if meta["generator"] == "tts":
        if not os.environ.get("ELEVENLABS_API_KEY"):
            return web.json_response({"error": "ELEVENLABS_API_KEY nicht gesetzt"}, status=500)
        # For TTS the `prompt` IS the text to speak. If caller didn't pass one,
        # fall back to the word (works for DE; EN callers should send an English word).
        text = want
        voice_id = data.get("voice_id") or _voice_for_kind(kind)
        job_id = f"trk_{uuid.uuid4().hex[:8]}"
        _register_job({"id": job_id, "type": "sound", "word": word, "filename": filename, "kind": kind,
                       "status": "queued", "started": time.time(), "target": target, "text": want})
        sse_broadcast("gen_update", gen_jobs[job_id])
        threading.Thread(target=_gen_tts_worker, args=(job_id, text, voice_id, filename), daemon=True).start()
        return web.json_response({"ok": True, "job_id": job_id, "filename": filename})

    return web.json_response({"error": f"Generator nicht implementiert: {meta['generator']}"}, status=500)


# ── Fill missing voices (FUNK-75) ───────────────────────────────────────────
# "Fehlende Stimmen erzeugen": one run at a time makes the German voice for every word of a set
# whose DE slot is empty, word by word, on the event loop. Each word is an ordinary generation job
# (a target, filled only while the slot is still empty: FUNK-72 _finalize_job), marked with the
# run's id, so pages neither autoplay it nor toast it. Before each word the run checks again that
# the entry is there, its slot empty and no other job filling it. The run lives in memory like the
# jobs: after a restart it is gone, and starting again only makes what is still missing.
FILL_KINDS = ("DE",)
FILL_STOP_CODES = (401, 402, 403)          # the key or the plan: every further word would fail too
FILL_RETRY_429 = (10, 30)                  # seconds before the first and the second retry of a 429
FILL_MAX_FAILS_IN_ROW = 3
FILL_FAILED_WORDS_MAX = 50
FILL_SAVE_FAILED = ("Die Stimme ist erzeugt, konnte aber nicht beim Wort gespeichert werden (ist der Speicher voll?). "
                    "Im Wort kannst du sie prüfen und übernehmen.")
fill_run = None                            # the current or the last run
# one order for every fill state the pages see, across runs and restarts (wall-clock ms, then +1):
# a late answer (a GET sent before a run ended, or before a restart) never replaces a newer state
_fill_seq = int(time.time() * 1000)


def _fill_next_seq():
    global _fill_seq
    _fill_seq = max(_fill_seq + 1, int(time.time() * 1000))
    return _fill_seq


def _fill_public(run):
    return None if run is None else {k: v for k, v in run.items() if not k.startswith("_")}


def _fill_broadcast(run):
    run["seq"] = _fill_next_seq()
    sse_broadcast("fill_update", _fill_public(run))


def _fill_text(e, kind):
    """What a voice of kind says for entry e: its spoken text if set, else its word ("" if neither)."""
    if not isinstance(e, dict):
        return ""
    track = (e.get("tracks") or {}).get(kind) if isinstance(e.get("tracks"), dict) else None
    prompt = track.get("prompt") if isinstance(track, dict) else ""
    prompt = prompt.strip() if isinstance(prompt, str) else ""
    word = e.get("word").strip() if isinstance(e.get("word"), str) else ""
    return prompt or word


def _slot_file(e, kind):
    tracks = e.get("tracks") if isinstance(e.get("tracks"), dict) else {}
    track = tracks.get(kind)
    return track.get("file") if isinstance(track, dict) else ""


def _fill_wanted(e, kind):
    """True if a run should make e's voice: something to say, nothing in the slot, and the parent
    did not switch that track off for this word (a track without a file is on by default)."""
    if not isinstance(e, dict) or not _fill_text(e, kind) or _slot_file(e, kind):
        return False
    track = (e.get("tracks") or {}).get(kind) if isinstance(e.get("tracks"), dict) else None
    return not (isinstance(track, dict) and track.get("enabled") is False)


def _slot_jobs(set_id, letter, entry_id, kind):
    """The jobs still running (or waiting for _finalize_job) that mean to fill this slot."""
    out = []
    for j in gen_jobs.values():
        t = j.get("target")
        if (t and j.get("status") in ("queued", "generating")
                and (t.get("set_id"), t.get("letter"), t.get("entry_id"), t.get("kind")) == (set_id, letter, entry_id, kind)):
            out.append(j)
    return out


def _slot_busy(set_id, letter, entry_id, kind):
    return bool(_slot_jobs(set_id, letter, entry_id, kind))


def _fill_missing(set_id, kind):
    """(letter, entry_id) of every entry of the set whose voice of kind a run should make (_fill_wanted)."""
    letters = (settings.get("sets", {}).get(set_id) or {}).get("letters")
    out = []
    if not isinstance(letters, dict):
        return out
    for letter in sorted(letters):
        cfg = letters[letter]
        if not isinstance(cfg, dict) or not isinstance(cfg.get("entries"), list):
            continue
        for e in cfg["entries"]:
            if isinstance(e, dict) and isinstance(e.get("id"), str) and _fill_wanted(e, kind):
                out.append((letter, e["id"]))
    return out


def _fill_saved_result(set_id, letter, entry_id, kind, text):
    """A finished, unreviewed job for this slot and text whose file exists but could not be saved on
    the word (save_failed): a run attaches that file instead of paying for the same voice again."""
    for j in gen_jobs.values():
        t = j.get("target")
        if (t and j.get("status") == "done" and j.get("save_failed") and j.get("text") == text and j.get("filename")
                and (t.get("set_id"), t.get("letter"), t.get("entry_id"), t.get("kind")) == (set_id, letter, entry_id, kind)
                and os.path.isfile(os.path.join(SOUNDS_DIR, j["filename"]))):
            return j
    return None


def _fill_attach(set_id, letter, entry_id, kind, job):
    """Put a save_failed job's file on its (still empty, still wanting) slot. True if saved; the job
    then counts as reviewed (no page offers it again). Raises _SaveFailed if the save fails again."""
    import copy as _copy
    entries = _letter_entries(set_id, letter)
    _, e = _find_entry(entries, entry_id)
    snapshot = _copy.deepcopy(entries)
    _normalize_letter_entry(e)
    track = e["tracks"].setdefault(kind, {"file": "", "enabled": True, "prompt": "", "volume": 100})
    track["file"], track["enabled"] = job["filename"], True
    _save_or_restore(entries, snapshot)
    job.pop("save_failed", None)
    job["target"], job["applied"] = None, True
    sse_broadcast("letter", {"set_id": set_id, "letter": letter, "entry_id": entry_id, "origin": "job"})
    sse_broadcast("gen_update", job)
    return True


def _fill_fatal(job):
    code = job.get("error_code")
    return code in FILL_STOP_CODES or "quota" in (job.get("error_detail") or "").lower()


async def _fill_one(run, letter, entry_id, word, text, voice_id):
    """One word of a run: an ordinary DE job with a target, run in a worker thread; its end state."""
    kind = run["kind"]
    filename = _claim_media_name(None, f"{_slugify(letter)}_{_slugify(word)}_{kind.lower()}", "mp3", SOUND_DIRS)
    job_id = f"trk_{uuid.uuid4().hex[:8]}"
    _register_job({"id": job_id, "type": "sound", "word": word, "filename": filename, "kind": kind,
                   "status": "queued", "started": time.time(), "batch": run["id"], "text": text,
                   "target": {"set_id": run["set_id"], "letter": letter, "entry_id": entry_id, "kind": kind}})
    sse_broadcast("gen_update", gen_jobs[job_id])
    try:
        await asyncio.get_running_loop().run_in_executor(None, _gen_tts_worker, job_id, text, voice_id, filename)
    except Exception as ex:                 # escaped the worker: the job still ends, so its slot is free again
        job = gen_jobs.get(job_id)
        if job is not None and job.get("status") not in ("done", "error"):
            if not job.get("_done"):
                _job_failed(job_id, "elevenlabs", ex, "tts", f"elevenlabs/tts/{voice_id}", text)
            _job_finished(job_id)           # failed: announced; stored: finalized on the event loop
    # a successful job becomes "done" on the event loop (_finalize_job, scheduled by the worker)
    for _ in range(400):
        job = gen_jobs.get(job_id)
        if job is None or job.get("status") in ("done", "error"):
            break
        await asyncio.sleep(0.05)
    job = gen_jobs.get(job_id)
    if job is None or job.get("status") not in ("done", "error"):
        return {"status": "error", "error": "Der Auftrag ist verloren gegangen.", "error_code": None, "error_detail": ""}
    return job


async def _fill_worker(run, items):
    kind, voice_id = run["kind"], _voice_for_kind(run["kind"])
    fails_in_row, ended_early = 0, False
    try:
        for letter, entry_id in items:
            if run["stopping"]:
                ended_early = True
                break
            waits = list(FILL_RETRY_429)
            while True:
                _, e = _find_entry(_letter_entries(run["set_id"], letter), entry_id)
                if e is None or not _fill_wanted(e, kind) or _slot_busy(run["set_id"], letter, entry_id, kind):
                    job = None                      # removed, filled or switched off meanwhile, or another job fills it
                    break
                text = _fill_text(e, kind)
                word = e["word"].strip() if isinstance(e.get("word"), str) and e["word"].strip() else text
                made = _fill_saved_result(run["set_id"], letter, entry_id, kind, text)
                if made is not None:            # made before, not saved then: attached now, never paid twice
                    try:
                        _fill_attach(run["set_id"], letter, entry_id, kind, made)
                        job = {"status": "done", "applied": True}
                    except _SaveFailed:
                        job = {"status": "done", "save_failed": True}
                    break
                run["current"], run["current_letter"] = word, letter
                _fill_broadcast(run)
                job = await _fill_one(run, letter, entry_id, word, text, voice_id)
                if job.get("status") == "error" and job.get("error_code") == 429 and waits and not run["stopping"]:
                    await asyncio.sleep(waits.pop(0))
                    if not run["stopping"]:
                        continue                    # checked again from the top: the slot may be filled now
                break
            if job is None:
                run["skipped"] += 1
            elif job.get("status") == "done" and job.get("save_failed"):
                # the voice exists but could not be put on its word: every further word would fail the same
                run["failed"] += 1
                if len(run["failed_words"]) < FILL_FAILED_WORDS_MAX:
                    run["failed_words"].append(word)
                run["status"], run["error"] = "error", FILL_SAVE_FAILED
                ended_early = True
                break
            elif job.get("status") == "done":
                fails_in_row = 0
                if job.get("applied"):
                    run["done"] += 1
                else:                               # the slot was filled meanwhile: offered for review
                    run["skipped"] += 1
            else:
                run["failed"] += 1
                fails_in_row += 1
                if len(run["failed_words"]) < FILL_FAILED_WORDS_MAX:
                    run["failed_words"].append(word)
                if _fill_fatal(job) or fails_in_row >= FILL_MAX_FAILS_IN_ROW:
                    run["status"], run["error"] = "error", job.get("error") or "Unbekannter Fehler"
                    ended_early = True
                    break
            run["current"], run["current_letter"] = "", ""
            _fill_broadcast(run)
    except Exception as ex:                         # never a run that says "running" forever
        print(f"[fill] {run['id']}: {type(ex).__name__}: {ex}", flush=True)
        run["status"], run["error"] = "error", f"Unerwarteter Fehler ({type(ex).__name__})."
    finally:
        if run["status"] == "running":
            run["status"] = "stopped" if ended_early else "done"
        run["current"], run["current_letter"] = "", ""
        run["finished"] = time.time()
        _fill_broadcast(run)


async def api_fill_get(request):
    """The current or last fill run, or null; X-FK-Boot names the process (a run does not survive it)."""
    resp = web.json_response({"run": _fill_public(fill_run), "seq": _fill_seq})
    resp.headers["X-FK-Boot"] = BOOT_ID
    return resp


async def api_fill_start(request):
    """Start "Fehlende Stimmen erzeugen" for a set. Body (optional): {kind: "DE"}."""
    global fill_run
    set_id = request.match_info["set_id"]
    try:
        data = await request.json()
    except Exception:
        data = {}
    kind = str((data if isinstance(data, dict) else {}).get("kind") or "DE").upper()
    if kind not in FILL_KINDS:
        return web.json_response({"error": f"Nur diese Stimmen: {', '.join(FILL_KINDS)}"}, status=400)
    sets = settings.get("sets", {})
    if set_id not in sets or not isinstance(sets[set_id], dict):
        return web.json_response({"error": "Set nicht gefunden"}, status=404)
    if fill_run is not None and fill_run["status"] == "running":
        return web.json_response({"error": "Es werden schon Stimmen erzeugt.", "run": _fill_public(fill_run),
                                  "seq": _fill_seq}, status=409)
    if not os.environ.get("ELEVENLABS_API_KEY"):
        return web.json_response({"error": "ELEVENLABS_API_KEY nicht gesetzt"}, status=500)
    items = _fill_missing(set_id, kind)
    run = {"id": f"fill_{uuid.uuid4().hex[:8]}", "set_id": set_id, "set_name": sets[set_id].get("name") or set_id,
           "kind": kind, "status": "running" if items else "done", "stopping": False,
           "total": len(items), "done": 0, "skipped": 0, "failed": 0, "failed_words": [],
           "current": "", "current_letter": "", "error": "", "started": time.time(), "finished": None, "seq": 0}
    fill_run = run
    if items:
        run["_task"] = asyncio.get_running_loop().create_task(_fill_worker(run, items))
    else:
        run["finished"] = time.time()
    _fill_broadcast(run)
    return web.json_response({"ok": True, "run": _fill_public(run), "seq": run["seq"]})


async def api_fill_stop(request):
    """Stop the running fill after the word it is on (that word still lands)."""
    run = fill_run
    if run is not None and run["status"] == "running" and not run["stopping"]:
        run["stopping"] = True
        _fill_broadcast(run)
    return web.json_response({"ok": True, "run": _fill_public(run), "seq": _fill_seq})


async def api_suggest_word(request):
    """AI-suggest a fitting German word for a letter, not already used."""
    data = await request.json()
    letter = keymap.norm_letter(data.get("letter") or "A")

    # Collect ALL used words for this letter (from all sets)
    used_words = set()
    for s in settings.get("sets", {}).values():
        for entry in s.get("letters", {}).get(letter, {}).get("entries", []):
            used_words.add(entry.get("word", ""))
    # Also include blacklisted words
    blacklist = list(settings.get("blacklist", {}).get(letter, []))
    all_excluded = sorted(set(list(used_words) + blacklist))

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return web.json_response({"error": "OPENROUTER_API_KEY nicht gesetzt"}, status=500)

    excluded_str = ", ".join(all_excluded) if all_excluded else "keine"

    # Build forbidden block — super explicit
    forbidden_lines = "\n".join(f"- NICHT {w}" for w in all_excluded) if all_excluded else "- (keine)"

    model = settings.get("ai_prompts", {}).get("suggest_model", "openai/gpt-4.1-mini")

    # User prompt: from request body > settings.ai_prompts.suggest > hardcoded default.
    # Supports {letter} and {excluded} placeholders.
    default_user_tpl = (
        'Schlage EIN deutsches Wort vor das mit "{letter}" beginnt.\n'
        '- Zielgruppe: Kinder 2-5 Jahre, Deutsch (Österreich)\n'
        '- Konkretes Ding/Tier/Objekt mit erkennbarem Geräusch\n'
        '- Kindgerecht\n'
        '- NICHT: {excluded}\n\n'
        'Antwort NUR als JSON (ein einzelnes englisches Wort bei word_en, '
        'das dem deutschen Wort entspricht — z.B. Ameise → Ant):\n'
        '{{"word": "...", "word_en": "...", '
        '"sound_description": "... (Englisch)", "image_description": "... (Englisch)"}}'
    )
    user_tpl = (
        data.get("user_prompt")
        or settings.get("ai_prompts", {}).get("suggest")
        or default_user_tpl
    )
    try:
        user_msg = user_tpl.replace("{letter}", letter).replace("{excluded}", excluded_str)
    except Exception:
        user_msg = default_user_tpl.replace("{letter}", letter).replace("{excluded}", excluded_str)
    rule = "die mit einem bestimmten Buchstaben beginnen"
    if letter == "ß":
        # no German word starts with ß (FUNK-46): ask for one that contains it, whatever the template says
        user_msg += ("\n\nWICHTIG: Kein deutsches Wort beginnt mit ß. Schlage ein Wort vor, "
                     "in dem ein ß vorkommt (z.B. Fuß, Straße, Strauß), und das Kinder kennen.")
        rule = "in denen ein ß vorkommt"

    system_msg = f"""Du bist ein Kinderwort-Generator. Du schlägst deutsche Wörter vor, {rule}.
STRENG VERBOTEN sind diese Wörter — du darfst sie NIEMALS vorschlagen:
{forbidden_lines}

Wenn du eines der verbotenen Wörter vorschlägst, wird deine Antwort verworfen."""

    _ai_log_entry("suggest", model, f"SYSTEM: {system_msg[:200]}...\nUSER: {user_msg}", None, "sending")

    # Auto-retry up to 3 times if model returns excluded word
    import urllib.request, re
    for attempt in range(3):
        try:
            payload = json.dumps({
                "model": model,
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
                "temperature": 1.0 + attempt * 0.1,  # increase randomness on retry
            }).encode()
            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/chat/completions",
                data=payload,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                d = json.load(resp)

            content = d["choices"][0]["message"]["content"]
            json_match = re.search(r'\{[^}]+\}', content)
            if not json_match:
                _ai_log_entry("suggest", model, user_msg, content[:200], "parse_error")
                continue

            suggestion = json.loads(json_match.group())
            suggested_word = suggestion.get("word", "")

            if suggested_word.lower() in [w.lower() for w in all_excluded]:
                _ai_log_entry("suggest", model, user_msg, f"REJECTED #{attempt+1}: {suggested_word}", "rejected")
                continue  # auto-retry

            _ai_log_entry("suggest", model, user_msg, f"OK: {suggested_word} (attempt {attempt+1})", "ok")
            return web.json_response(suggestion)

        except Exception as e:
            msg, code, detail = _provider_error("openrouter", e)
            _ai_log_entry("suggest", model, user_msg, f"{code or ''} {detail or e}".strip(), "error")
            return web.json_response({"error": msg, "error_code": code, "error_detail": detail}, status=502)

    # All 3 attempts returned excluded words
    _ai_log_entry("suggest", model, user_msg, f"FAILED after 3 attempts", "error")
    return web.json_response({
        "error": f"KI konnte nach 3 Versuchen kein neues Wort für {letter} finden. Evtl. zu viele ausgeschlossene Wörter.",
    })


async def api_integrations_status(request):
    """Check health and credits for all AI integrations."""
    import urllib.request
    loop = asyncio.get_event_loop()

    def _check_elevenlabs():
        api_key = os.environ.get("ELEVENLABS_API_KEY", "")
        if not api_key:
            return {"status": "not_configured", "error": "ELEVENLABS_API_KEY nicht gesetzt"}
        try:
            req = urllib.request.Request("https://api.elevenlabs.io/v1/user/subscription",
                headers={"xi-api-key": api_key})
            with urllib.request.urlopen(req, timeout=8) as resp:
                d = json.load(resp)
            used = d.get("character_count", 0)
            limit = d.get("character_limit", 0)
            remaining = limit - used
            status = "warning" if (limit > 0 and remaining / limit < 0.05) else "ok"
            return {
                "status": status,
                "tier": d.get("tier", "unknown"),
                "characters_used": used,
                "characters_limit": limit,
                "characters_remaining": remaining,
                "next_reset": d.get("next_character_count_reset_unix"),
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def _check_openrouter():
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not api_key:
            return {"status": "not_configured", "error": "OPENROUTER_API_KEY nicht gesetzt"}
        try:
            req = urllib.request.Request("https://openrouter.ai/api/v1/auth/key",
                headers={"Authorization": f"Bearer {api_key}"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                d = json.load(resp)
            data = d.get("data", {})
            limit = data.get("limit")
            usage = data.get("usage", 0)
            remaining = data.get("limit_remaining")
            if limit is None:
                # Unlimited key (pay-as-you-go with balance)
                return {
                    "status": "ok",
                    "credits_used": round(usage, 4),
                    "credits_limit": None,
                    "credits_remaining": round(remaining, 4) if remaining is not None else None,
                    "label": data.get("label", ""),
                }
            remaining = limit - usage if remaining is None else remaining
            status = "warning" if (limit > 0 and remaining / limit < 0.10) else "ok"
            return {
                "status": status,
                "credits_used": round(usage, 4),
                "credits_limit": round(limit, 4),
                "credits_remaining": round(remaining, 4),
                "label": data.get("label", ""),
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    el, orr = await asyncio.gather(
        loop.run_in_executor(None, _check_elevenlabs),
        loop.run_in_executor(None, _check_openrouter),
    )
    return web.json_response({"elevenlabs": el, "openrouter": orr})


async def api_get_blacklist(request):
    """Get blacklisted words for a letter."""
    letter = keymap.norm_letter(request.match_info["letter"])
    words = settings.get("blacklist", {}).get(letter, [])
    return web.json_response(words)


async def api_put_blacklist(request):
    """Set blacklisted words for a letter."""
    letter = keymap.norm_letter(request.match_info["letter"])
    data = await request.json()
    if "blacklist" not in settings:
        settings["blacklist"] = {}
    settings["blacklist"][letter] = data.get("words", [])
    save_settings()
    return web.json_response({"ok": True})


class _ArchiveError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status, self.msg = status, msg


def _archive_one(file_type, filename):
    """Move one library file into the archive; returns the archive path (FUNK-73).

    A plain name only, never a symlink; the file is taken away atomically first, so a set
    import writing the name right after keeps its new file; an archived file is never
    overwritten. Raises _ArchiveError(status, message).
    """
    import stat
    if not isinstance(filename, str) or not _MEDIA_NAME.fullmatch(filename) or filename.startswith("."):
        raise _ArchiveError(400, "ungültiger Dateiname")
    archive_dir = os.path.join(DATA_DIR, "archive", f"{file_type}s")
    os.makedirs(archive_dir, exist_ok=True)
    src = os.path.join(SOUNDS_DIR if file_type == "sound" else IMAGES_DIR, filename)
    try:
        st = os.lstat(src)
    except (OSError, ValueError):
        st = None
    if st is None or not stat.S_ISREG(st.st_mode):
        raise _ArchiveError(404, f"Datei nicht gefunden: {filename}")
    try:
        held = _staging(filename.rpartition(".")[2] or "bin")
        os.rename(src, held)
    except FileNotFoundError:
        raise _ArchiveError(404, f"Datei nicht gefunden: {filename}")
    except OSError:
        raise _ArchiveError(507, "Archivieren fehlgeschlagen")
    stem, dot, ext = filename.rpartition(".")
    dst = None
    try:
        for n in range(1, 1000):
            cand = os.path.join(archive_dir, filename if n == 1 else (f"{stem}-{n}.{ext}" if dot and stem else f"{filename}-{n}"))
            try:
                _publish_exact(held, cand)
                dst = cand
                break
            except FileExistsError:
                continue
    except OSError:
        dst = None
    if dst is None:                                         # put it back where it was, if that name is still free
        try:
            _publish_exact(held, src)
        except OSError:
            raise _ArchiveError(507, "Archivieren fehlgeschlagen; die Datei liegt in /data/.staging")
        _remove_quiet(held)
        raise _ArchiveError(507, "Archivieren fehlgeschlagen")
    _remove_quiet(held)
    return dst


def _unarchive(file_type, filename, dst):
    """Undo _archive_one (best effort): the archived file back under its library name."""
    try:
        _publish_exact(dst, os.path.join(SOUNDS_DIR if file_type == "sound" else IMAGES_DIR, filename))
        os.unlink(dst)
    except OSError as e:
        print(f"[archive] could not restore {filename}: {e}", flush=True)


# ── What uses which file (FUNK-78) ──────────────────────────────────────────
FILE_USES_MAX = 50          # per file: enough to show and to say who uses it


def _file_usage():
    """{"sounds": {name: [use]}, "images": {name: [use]}} from every set and key map. A use is
    {set_id, set, key, entry_id, word, kind} for a letter's entry (IMG, FX, DE, EN) or a digit
    (FX/DE/EN, BG), or {profile, key, kind: "KEY", word} for a key of a key map."""
    sounds, images = {}, {}

    def add(index, name, use):
        if isinstance(name, str) and name:
            uses = index.setdefault(name, [])
            if len(uses) < FILE_USES_MAX:
                uses.append(use)

    def tracks(e, base, legacy_kind):
        ts = e.get("tracks") if isinstance(e.get("tracks"), dict) else {}
        for kind, t in ts.items():
            if isinstance(t, dict):
                add(sounds, t.get("file"), {**base, "kind": kind})
        if e.get("sound") and not (isinstance(ts.get(legacy_kind), dict) and ts[legacy_kind].get("file")):
            add(sounds, e.get("sound"), {**base, "kind": legacy_kind})       # before multi-track

    for sid, s in (settings.get("sets") or {}).items():
        if not isinstance(s, dict):
            continue
        sname = s.get("name") if isinstance(s.get("name"), str) else sid
        for letter, cfg in (s.get("letters") or {}).items():
            for e in (cfg.get("entries") if isinstance(cfg, dict) else None) or []:
                if not isinstance(e, dict):
                    continue
                base = {"set_id": sid, "set": sname, "key": letter, "entry_id": e.get("id") or "",
                        "word": e.get("word") if isinstance(e.get("word"), str) else ""}
                add(images, e.get("image"), {**base, "kind": "IMG"})
                tracks(e, base, "FX")
        for digit, n in (s.get("numbers") or {}).items():
            if not isinstance(n, dict):
                continue
            base = {"set_id": sid, "set": sname, "key": digit, "entry_id": NUMBER_ENTRY_ID,
                    "word": n.get("word") if isinstance(n.get("word"), str) else ""}
            tracks(n, base, "DE")
            for b in n.get("backgrounds") or [] if isinstance(n.get("backgrounds"), list) else []:
                if isinstance(b, dict):
                    add(images, b.get("image"), {**base, "kind": "BG"})
    for pid, km in (settings.get("keymaps") or {}).items():
        for kid, a in (km.items() if isinstance(km, dict) else []):
            if isinstance(a, dict):
                use = {"profile": pid, "key": kid, "kind": "KEY", "word": a.get("word") if isinstance(a.get("word"), str) else ""}
                add(sounds, a.get("file"), use)
                add(images, a.get("image"), use)
    return {"sounds": sounds, "images": images}


async def api_file_usage(request):
    """What uses each sound and picture (FUNK-78): sets (letters, digits) and key maps."""
    return web.json_response(_file_usage())


def _use_label(u):
    kinds = {"IMG": "Bild", "BG": "Hintergrund", "FX": "Geräusch", "DE": "Stimme", "EN": "Englisch"}
    if u.get("kind") == "KEY":
        return f"Taste {u.get('key')}"
    return f"{u.get('key')} · {u.get('word') or '—'} ({kinds.get(u.get('kind'), u.get('kind'))})"


async def api_archive_file(request):
    """Move a sound or image to the archive folder; never one that a set or key map still uses (FUNK-78)."""
    try:
        data = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(data, dict):
        return web.json_response({"error": "type und filename erforderlich"}, status=400)
    file_type = data.get("type")  # "sound" or "image"
    filename = data.get("filename", "")
    if not filename or file_type not in ("sound", "image"):
        return web.json_response({"error": "type und filename erforderlich"}, status=400)
    if isinstance(filename, str) and filename in _referenced_names():
        # an entry, a digit or a key still points at it: archiving would leave it pointing at nothing
        uses = _file_usage()["sounds" if file_type == "sound" else "images"].get(filename, [])
        who = ", ".join(_use_label(u) for u in uses[:3]) + (" …" if len(uses) > 3 else "")
        return web.json_response({"error": "Wird noch benutzt" + (f": {who}" if who else "") + ". Erst dort entfernen.",
                                  "uses": uses}, status=409)
    try:
        dst = _archive_one(file_type, filename)
    except _ArchiveError as e:
        return web.json_response({"error": e.msg}, status=e.status)
    return web.json_response({"ok": True, "archived": filename, "to": dst})


async def api_list_archive(request):
    """List archived files."""
    archive_dir = os.path.join(DATA_DIR, "archive")
    result = {"sounds": [], "images": []}
    for sub in ("sounds", "images"):
        d = os.path.join(archive_dir, sub)
        if os.path.exists(d):
            result[sub] = sorted(os.listdir(d))
    return web.json_response(result)


async def serve_ai_file(request):
    """Serve a file from the ai-generated folder, and nothing outside it.

    FUNK-73: aiohttp decodes %2f after routing, so /ai/..%2f..%2fetc reached any file the
    process can read (/proc/self/environ holds the API keys). The resolved path must stay
    inside the folder.
    """
    path = _safe_file(AI_DIR, request.match_info["path"])
    if not path:
        return web.Response(status=404)
    return web.FileResponse(path)


def _free_media_name(base, ext, folders):
    """base.ext, or base-2.ext …: a name no file in folders has, no job was given
    since start and nothing in the settings refers to.

    A generated file never replaces another one: "Busse" and "Buße" share a slug
    since ß is spelled ss (FUNK-46), and the same word in two sets shares one too.
    Called with no await before the job is registered.
    """
    # a default base comes from a word: never a path, never hidden
    base = re.sub(r"[/\\\x00]+", "-", base).lstrip(".") or "file"
    taken = _issued_media_names | _referenced_names()

    def candidate(n):   # at most MEDIA_NAME_MAX characters, suffix and extension included
        suffix = f"-{n}" if n > 1 else ""
        return f"{base[:MEDIA_NAME_MAX - len(suffix) - len(ext) - 1]}{suffix}.{ext}"

    n = 1
    name = candidate(n)
    while name in taken or any(os.path.lexists(os.path.join(d, name)) for d in folders):   # a dangling link counts (FUNK-73)
        n += 1
        name = candidate(n)
    _issued_media_names.add(name)
    return name


# Every name a job was given since start, whatever became of the job: an entry may already
# reference the name of a job that failed or is still running, so it is never given twice.
_issued_media_names = set()


def _referenced_names():
    """Every string in the sets and key maps: any file name an entry, track or key refers to."""
    out = set()
    stack = [settings.get("sets"), settings.get("keymaps")]
    while stack:
        v = stack.pop()
        if isinstance(v, dict):
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
        elif isinstance(v, str):
            out.add(v)
    return out


MEDIA_NAME_MAX = 200     # what set export/import and key maps accept (setbundle, keymap)
_MEDIA_NAME = re.compile(r"[^/\\\x00]{1,%d}" % MEDIA_NAME_MAX)


def _claim_media_name(requested, base, ext, folders):
    """The file a generation job writes, or None if ``requested`` is not a plain file name.

    The requested name (the UI picks one) when it is free, else a free variant of
    it; without one, base.ext or a free variant (see _free_media_name). A job
    never writes over a file, another job's name or a name the settings refer
    to: two words with one slug ("Busse", "Buße"), the same word generated twice
    at once, or a rerun of a script. Responses carry the name: callers use it.
    """
    if requested:
        if (not isinstance(requested, str) or not _MEDIA_NAME.fullmatch(requested)
                or requested.startswith(".")):
            return None
        stem, dot, req_ext = requested.rpartition(".")
        base, ext = (stem, req_ext) if dot and stem else (requested, ext)
        if not 1 <= len(ext) <= 10:
            return None
    return _free_media_name(base, ext, folders)


def _slugify(text):
    return (text.lower().replace(" ", "-").replace("&", "und")
            .replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss"))[:30]


def _reload_runtime():
    global current_volume
    current_volume = settings.get("volume", 100)
    if keyboard:
        from keyboard import LAYOUTS
        keyboard.layout = LAYOUTS.get(_language(), {})
        keyboard.debounce_seconds = settings.get("debounce_seconds", 0.8)
        # Switch devices without a container restart. set_device() is
        # non-blocking and latest-wins; the restart it triggers releases the
        # old device's grabs before the new device is grabbed.
        wanted = _keyboard_device_name()
        if wanted != keyboard.device_name:
            print(f"[funkeykid] Keyboard device requested: {keyboard.device_name} → {wanted}", flush=True)
        keyboard.set_device(wanted)
    if display:
        display.reload_settings(settings)
    # battery_low may change with the threshold: tell the UI and MQTT now
    sse_broadcast("status", _keyboard_status())
    _publish_keyboard_status()


def create_app():
    app = web.Application()
    # Status
    app.router.add_get("/api/events", api_sse)
    app.router.add_get("/api/status", api_get_status)
    app.router.add_get("/api/diagnostics", api_get_diagnostics)
    app.router.add_post("/api/reconnect", api_reconnect_keyboard)
    # Settings
    app.router.add_get("/api/settings", api_get_settings)
    app.router.add_put("/api/settings", api_put_settings)
    # Sets CRUD
    app.router.add_get("/api/sets", api_get_sets)
    app.router.add_post("/api/sets", api_create_set)
    app.router.add_get("/api/sets/{set_id}", api_get_set)
    app.router.add_put("/api/sets/{set_id}", api_update_set)
    app.router.add_delete("/api/sets/{set_id}", api_delete_set)
    app.router.add_post("/api/sets/{set_id}/activate", api_activate_set)
    app.router.add_get("/api/sets/{set_id}/export", api_export_set)
    app.router.add_post("/api/sets/import/preview", api_import_set_preview)
    app.router.add_post("/api/sets/import/commit", api_import_set_commit)
    app.router.add_post("/api/sets/import/cancel", api_import_set_cancel)
    # Letter entries within a set
    app.router.add_get("/api/sets/{set_id}/letters/{letter}", api_get_letter)
    app.router.add_put("/api/sets/{set_id}/letters/{letter}", api_put_letter)
    app.router.add_post("/api/sets/{set_id}/letters/{letter}/entries", api_add_entry)
    app.router.add_post("/api/sets/{set_id}/letters/{letter}/entries/{entry_id}/media", api_entry_media)
    app.router.add_patch("/api/sets/{set_id}/letters/{letter}/entries/{entry_id}", api_patch_entry)
    app.router.add_delete("/api/sets/{set_id}/letters/{letter}/entries/id/{entry_id}", api_delete_entry_by_id)
    app.router.add_post("/api/sets/{set_id}/letters/{letter}/entries/{entry_id}/archive", api_archive_entry)
    app.router.add_post("/api/jobs/{job_id}/detach", api_job_detach)
    app.router.add_delete("/api/sets/{set_id}/letters/{letter}/entries/{index}", api_delete_entry)
    # Numbers within a set
    app.router.add_get("/api/sets/{set_id}/numbers", api_get_numbers)
    app.router.add_put("/api/sets/{set_id}/numbers/{digit}", api_put_number)
    app.router.add_get("/api/sets/{set_id}/numbers/{digit}", api_get_number)
    app.router.add_patch("/api/sets/{set_id}/numbers/{digit}/entries/{entry_id}", api_patch_number)
    app.router.add_post("/api/sets/{set_id}/numbers/{digit}/backgrounds", api_number_bg_add)
    app.router.add_patch("/api/sets/{set_id}/numbers/{digit}/backgrounds/{image}", api_number_bg_patch)
    app.router.add_delete("/api/sets/{set_id}/numbers/{digit}/backgrounds/{image}", api_number_bg_delete)
    # Files
    app.router.add_get("/api/sounds", api_get_sounds)
    app.router.add_post("/api/sounds/upload", api_upload_sound)
    app.router.add_post("/api/sounds/normalize-all", api_normalize_all_sounds)
    app.router.add_get("/api/images", api_get_images)
    app.router.add_post("/api/images/upload", api_upload_image)
    # Test
    app.router.add_post("/api/test/{letter}", api_test_key)
    app.router.add_get("/api/layout/{name}", api_get_layout)
    app.router.add_get("/api/keymap", api_get_keymap)
    app.router.add_put("/api/keymap/{key_id}", api_put_keymap)
    app.router.add_delete("/api/keymap/{key_id}", api_delete_keymap)
    app.router.add_get("/api/quiz", api_quiz_get)
    app.router.add_put("/api/quiz/config", api_quiz_config)
    app.router.add_post("/api/quiz/start", api_quiz_start)
    app.router.add_post("/api/quiz/stop", api_quiz_stop)
    app.router.add_post("/api/quiz/repeat", api_quiz_repeat)
    app.router.add_get("/api/progress", api_progress_get)
    app.router.add_get("/api/dashboard", api_dashboard_get)
    app.router.add_get("/api/progress/sessions.csv", api_progress_sessions_csv)
    app.router.add_get("/api/progress/letters.csv", api_progress_letters_csv)
    app.router.add_put("/api/progress/config", api_progress_config)
    app.router.add_post("/api/progress/reset", api_progress_reset)
    app.router.add_post("/api/press/{key_id}", api_press)
    app.router.add_get("/api/keyboards", api_get_keyboards)
    app.router.add_post("/api/keyboards/import", api_import_keyboard)
    app.router.add_get("/api/language", api_get_language)
    app.router.add_put("/api/language", api_put_language)
    app.router.add_get("/api/keyboards/{profile_id}", api_get_keyboard)
    app.router.add_put("/api/keyboards/{profile_id}", api_put_keyboard)
    app.router.add_delete("/api/keyboards/{profile_id}", api_delete_keyboard)
    app.router.add_post("/api/keyboards/{profile_id}/activate", api_activate_keyboard)
    app.router.add_post("/api/keyboards/{profile_id}/duplicate", api_duplicate_keyboard)
    app.router.add_put("/api/keyboards/{profile_id}/keys/{slot}", api_put_keyboard_key)
    app.router.add_get("/api/keyboards/{profile_id}/export", api_export_keyboard)
    # Version
    app.router.add_get("/api/version", api_version)
    # AI Generation
    app.router.add_post("/api/generate/sound", api_generate_sound)
    app.router.add_post("/api/generate/image", api_generate_image)
    app.router.add_post("/api/generate/tts", api_generate_tts)
    app.router.add_post("/api/generate/track", api_generate_track)
    app.router.add_get("/api/fill", api_fill_get)
    app.router.add_post("/api/fill/stop", api_fill_stop)
    app.router.add_post("/api/sets/{set_id}/fill-voices", api_fill_start)
    app.router.add_post("/api/suggest-word", api_suggest_word)
    app.router.add_get("/api/blacklist/{letter}", api_get_blacklist)
    app.router.add_put("/api/blacklist/{letter}", api_put_blacklist)
    app.router.add_get("/api/jobs", api_get_jobs)
    app.router.add_get("/api/ai-log", lambda r: web.json_response(ai_log))
    app.router.add_get("/api/integrations/status", api_integrations_status)
    app.router.add_get("/api/favorites", lambda r: web.json_response(favorites))
    app.router.add_post("/api/archive", api_archive_file)
    app.router.add_get("/api/archive", api_list_archive)
    app.router.add_get("/api/files/usage", api_file_usage)
    app.router.add_get("/ai/{path:.+}", serve_ai_file)
    # File serving
    app.router.add_get("/sounds/{filename}", serve_sound)
    app.router.add_get("/images/{filename}", serve_image)
    app.router.add_get("/thumbs/{size}/{filename}", serve_thumb)
    # SPA
    app.router.add_get("/", serve_index)
    app.router.add_static("/static/", STATIC_DIR)
    return app


def main():
    global settings, keyboard, display

    os.makedirs(SOUNDS_DIR, exist_ok=True)
    os.makedirs(IMAGES_DIR, exist_ok=True)
    _purge_import_dir_at_start()             # FUNK-43: staged uploads and temp files from before a restart
    try:
        _write_quiz_jingles()                # FUNK-39
    except OSError as e:
        print(f"[quiz] jingles not written: {e}", flush=True)
    atomicwrite.purge_temp_files(SETTINGS_PATH)   # FUNK-67: a temp file a crash left before its rename

    settings = load_settings()
    _load_favorites()
    _progress_start()                        # FUNK-40
    set_count = len(settings.get("sets", {}))
    active = settings.get("active_set", "?")
    print(f"[funkeykid] Settings loaded: {set_count} set(s), active={active}, {len(favorites)} favorites", flush=True)

    # Display (MQTT). Every (re)connect re-publishes keyboard-info, which also
    # clears a retained last-will "offline" message.
    display = Display(settings)
    display.on_connected = _publish_keyboard_status
    display.connect()

    # Keyboard — exclusive grab of every node of the configured device (FUNK-59)
    device_name = _keyboard_device_name()
    layout = _language()      # FUNK-64: language, migrated from keyboard_layout
    debounce = settings.get("debounce_seconds", 0.8)
    keyboard = KeyboardListener(device_name, layout=layout, debounce_seconds=debounce)
    keyboard.on_key(handle_key)

    def _on_keyboard_connection(connected, status):
        status = _keyboard_status()
        sse_broadcast("connection", status)
        if display:
            display.publish_keyboard_status(status)

    def _on_keyboard_status(status):
        status = _keyboard_status()
        sse_broadcast("status", status)
        if display:
            display.publish_keyboard_status(status)

    keyboard.on_connection_change(_on_keyboard_connection)
    keyboard.on_status_change(_on_keyboard_status)
    keyboard.start()
    source = "env FUNKEYKID_KEYBOARD" if _keyboard_env() else "settings"
    print(f"[funkeykid] Keyboard: {device_name} (from {source}, layout={layout}, exclusive grab)", flush=True)

    # Heartbeat: re-publish keyboard-info so subscribers can detect a hung process
    def _keyboard_heartbeat():
        while True:
            time.sleep(HEARTBEAT_SECONDS)
            try:
                _publish_keyboard_status()
            except Exception as e:
                print(f"[funkeykid] keyboard-info heartbeat failed: {e}", flush=True)

    threading.Thread(target=_keyboard_heartbeat, daemon=True, name="kb-heartbeat").start()

    # Web server
    app = create_app()
    print(f"[funkeykid] Web UI: http://0.0.0.0:{PORT}", flush=True)

    # Capture the event loop for thread-safe SSE broadcasts
    async def start_app():
        global _loop
        _loop = asyncio.get_event_loop()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        asyncio.create_task(_import_janitor())
        print(f"[funkeykid] Server running on http://0.0.0.0:{PORT}", flush=True)
        # Keep running forever
        await asyncio.Event().wait()

    asyncio.run(start_app())


if __name__ == "__main__":
    main()
