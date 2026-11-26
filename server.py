"""funkeykid v2 — aiohttp web server + keyboard handler with sets."""
import asyncio
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
gen_jobs = {}  # {job_id: {type, word, status, filename, error}}
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
    if quiz_engine.active:              # FUNK-39: the quiz owns the keyboard while it runs
        _quiz_key(key_name, action)
        return
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
                        record=_quiz_record, schedule=_quiz_schedule)


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


def _quiz_key(key_name, action):
    """A key press while the quiz runs: letters answer; Enter repeats, Space stops the
    sound, volume keys work; every other key stays silent (no noise over the question)."""
    quiz_engine.touch()
    kind = (action or {}).get("action")
    letter = None
    if kind == "letter":
        letter = action.get("letter")
    elif action is None and key_name in keymap.LETTERS:
        letter = key_name
    if letter is not None:
        if letter in keymap.LETTERS:
            print(f"[quiz] answer {letter}: {quiz_engine.answer(letter)}", flush=True)
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
    try:
        with open(SETTINGS_PATH, "rb") as f:
            before = f.read()
    except OSError:
        before = None
    sets[sid] = data
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
    # Clone entries for response so we can decorate with legacy aliases
    # without mutating the persisted in-memory state.
    out_entries = []
    for e in letter_cfg.get("entries", []):
        _normalize_letter_entry(e)
        import copy as _copy
        alias = _copy.deepcopy(e)
        _add_legacy_sound_alias(alias, "FX")
        out_entries.append(alias)
    return web.json_response({**letter_cfg, "entries": out_entries})


async def api_put_letter(request):
    """Replace all entries for a letter in a set.

    Accepts entries with either `tracks` dict (new shape) or legacy `sound`
    field (auto-migrated). Always persists the new shape.
    """
    set_id = request.match_info["set_id"]
    letter = keymap.norm_letter(request.match_info["letter"])
    data = await request.json()
    sets = settings.get("sets", {})
    if set_id not in sets:
        return web.json_response({"error": "Set not found"}, status=404)
    if "letters" not in sets[set_id]:
        sets[set_id]["letters"] = {}
    cleaned = []
    for e in data.get("entries", []):
        _normalize_letter_entry(e)
        _strip_legacy_sound(e)
        cleaned.append(e)
    sets[set_id]["letters"][letter] = {"entries": cleaned}
    save_settings()
    return web.json_response({"ok": True})


async def api_add_entry(request):
    """Add a new entry to a letter."""
    set_id = request.match_info["set_id"]
    letter = keymap.norm_letter(request.match_info["letter"])
    data = await request.json()
    sets = settings.get("sets", {})
    if set_id not in sets:
        return web.json_response({"error": "Set not found"}, status=404)
    letters = sets[set_id].setdefault("letters", {})
    letter_data = letters.setdefault(letter, {"entries": []})
    entry = {
        "word": data.get("word", ""),
        "sound": data.get("sound", ""),
        "image": data.get("image", ""),
        "enabled": data.get("enabled", True),
    }
    letter_data["entries"].append(entry)
    save_settings()
    return web.json_response({"ok": True, "index": len(letter_data["entries"]) - 1})


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
    return web.json_response({"ok": True})


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


async def api_upload_sound(request):
    """Upload a sound file. Normalizes the result (FUNK-1) unless disabled.

    Flow: write raw upload → ai-generated/sounds (or tmp if dir missing)
    → normalize_mp3 into /data/sounds. Keeps the original for reference.
    """
    os.makedirs(SOUNDS_DIR, exist_ok=True)
    os.makedirs(AI_SOUNDS_DIR, exist_ok=True)
    reader = await request.multipart()
    field = await reader.next()
    filename = field.filename
    raw_path = os.path.join(AI_SOUNDS_DIR, filename)
    with open(raw_path, "wb") as f:
        while True:
            chunk = await field.read_chunk()
            if not chunk:
                break
            f.write(chunk)
    normed = normalize_mp3(raw_path, os.path.join(SOUNDS_DIR, filename))
    return web.json_response({"ok": True, "file": filename, "normalized": normed})


async def api_get_images(request):
    os.makedirs(IMAGES_DIR, exist_ok=True)
    files = sorted(f for f in os.listdir(IMAGES_DIR) if f.endswith((".png", ".jpg", ".jpeg", ".gif")))
    return web.json_response(files)


async def api_upload_image(request):
    os.makedirs(IMAGES_DIR, exist_ok=True)
    reader = await request.multipart()
    field = await reader.next()
    filename = field.filename
    path = os.path.join(IMAGES_DIR, filename)
    with open(path, "wb") as f:
        while True:
            chunk = await field.read_chunk()
            if not chunk:
                break
            f.write(chunk)
    return web.json_response({"ok": True, "file": filename})


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


async def serve_sound(request):
    filename = request.match_info["filename"]
    path = os.path.join(SOUNDS_DIR, filename)
    if not os.path.exists(path):
        return web.Response(status=404)
    return web.FileResponse(path)


async def serve_image(request):
    filename = request.match_info["filename"]
    path = os.path.join(IMAGES_DIR, filename)
    if not os.path.exists(path):
        return web.Response(status=404)
    return web.FileResponse(path)


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


def _gen_sound_worker(job_id, word, prompt, duration, filename):
    """Background worker for sound generation."""
    import urllib.request, shutil
    gen_jobs[job_id]["status"] = "generating"
    sse_broadcast("gen_update", gen_jobs[job_id])
    api_key = os.environ.get("ELEVENLABS_API_KEY", "")
    os.makedirs(AI_SOUNDS_DIR, exist_ok=True)
    _ai_log_entry("sound", "elevenlabs/sfx", prompt, None, "sending")
    try:
        payload = json.dumps({"text": prompt, "duration_seconds": duration, "prompt_influence": 0.6}).encode()
        req = urllib.request.Request("https://api.elevenlabs.io/v1/sound-generation",
            data=payload, headers={"xi-api-key": api_key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            audio_data = resp.read()
        if len(audio_data) < 1000:
            raise ValueError("Generierung fehlgeschlagen (zu klein)")
        outpath = os.path.join(AI_SOUNDS_DIR, filename)
        with open(outpath, "wb") as f:
            f.write(audio_data)
        normed = normalize_mp3(outpath, os.path.join(SOUNDS_DIR, filename))
        gen_jobs[job_id].update({"status": "done", "size": len(audio_data), "path": f"/sounds/{filename}", "normalized": normed})
        _ai_log_entry("sound", "elevenlabs/sfx", prompt,
                      f"OK: {len(audio_data)} bytes → {filename}" + (" (normalized)" if normed else ""),
                      "ok")
    except Exception as e:
        gen_jobs[job_id].update({"status": "error", "error": str(e)})
        _ai_log_entry("sound", "elevenlabs/sfx", prompt, str(e), "error")
    sse_broadcast("gen_update", gen_jobs[job_id])


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
    gen_jobs[job_id] = {"id": job_id, "type": "sound", "word": word, "filename": filename, "status": "queued"}
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
        # Save original
        with open(os.path.join(AI_IMAGES_ORIG_DIR, filename), "wb") as f:
            f.write(img_data)
        # Resize
        resized_path = os.path.join(AI_IMAGES_RESIZED_DIR, filename)
        try:
            from PIL import Image
            from io import BytesIO
            img = Image.open(BytesIO(img_data))
            img = img.resize((64, 64), Image.LANCZOS)
            img.save(resized_path)
        except ImportError:
            with open(resized_path, "wb") as f:
                f.write(img_data)
        shutil.copy2(resized_path, os.path.join(IMAGES_DIR, filename))
        gen_jobs[job_id].update({"status": "done", "size": len(img_data), "path": f"/images/{filename}"})
        _ai_log_entry("image", model, prompt, f"OK: {len(img_data)}b → {filename}", "ok")
    except Exception as e:
        gen_jobs[job_id].update({"status": "error", "error": str(e)})
        _ai_log_entry("image", model, prompt, str(e), "error")
    sse_broadcast("gen_update", gen_jobs[job_id])


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
    if not os.environ.get("OPENROUTER_API_KEY"):
        return web.json_response({"error": "OPENROUTER_API_KEY nicht gesetzt"}, status=500)
    job_id = f"img_{uuid.uuid4().hex[:8]}"
    gen_jobs[job_id] = {"id": job_id, "type": "image", "word": word, "filename": filename, "status": "queued"}
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
    os.makedirs(AI_SOUNDS_DIR, exist_ok=True)
    _ai_log_entry("tts", f"elevenlabs/tts/{voice_id}", text, None, "sending")
    try:
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
        outpath = os.path.join(AI_SOUNDS_DIR, filename)
        with open(outpath, "wb") as f:
            f.write(audio_data)
        normed = normalize_mp3(outpath, os.path.join(SOUNDS_DIR, filename))
        gen_jobs[job_id].update({"status": "done", "size": len(audio_data), "path": f"/sounds/{filename}", "normalized": normed})
        _ai_log_entry("tts", f"elevenlabs/tts/{voice_id}", text,
                      f"OK: {len(audio_data)}b → {filename}" + (" (normalized)" if normed else ""),
                      "ok")
    except Exception as e:
        gen_jobs[job_id].update({"status": "error", "error": str(e)})
        _ai_log_entry("tts", f"elevenlabs/tts/{voice_id}", text, str(e), "error")
    sse_broadcast("gen_update", gen_jobs[job_id])


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
    gen_jobs[job_id] = {"id": job_id, "type": "sound", "word": word, "filename": filename, "status": "queued"}
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
    filename = _claim_media_name(data.get("filename"), base, "mp3", SOUND_DIRS)
    if not filename:
        return web.json_response({"error": "invalid filename"}, status=400)

    if meta["generator"] == "sfx":
        if not os.environ.get("ELEVENLABS_API_KEY"):
            return web.json_response({"error": "ELEVENLABS_API_KEY nicht gesetzt"}, status=500)
        prompt = data.get("prompt") or _get_sound_prompt().format(word=word)
        duration = data.get("duration", 4)
        job_id = f"trk_{uuid.uuid4().hex[:8]}"
        gen_jobs[job_id] = {"id": job_id, "type": "sound", "word": word, "filename": filename, "kind": kind, "status": "queued"}
        sse_broadcast("gen_update", gen_jobs[job_id])
        threading.Thread(target=_gen_sound_worker, args=(job_id, word, prompt, duration, filename), daemon=True).start()
        return web.json_response({"ok": True, "job_id": job_id, "filename": filename})

    if meta["generator"] == "tts":
        if not os.environ.get("ELEVENLABS_API_KEY"):
            return web.json_response({"error": "ELEVENLABS_API_KEY nicht gesetzt"}, status=500)
        # For TTS the `prompt` IS the text to speak. If caller didn't pass one,
        # fall back to the word (works for DE; EN callers should send an English word).
        text = (data.get("prompt") or word).strip()
        voice_id = data.get("voice_id") or _voice_for_kind(kind)
        job_id = f"trk_{uuid.uuid4().hex[:8]}"
        gen_jobs[job_id] = {"id": job_id, "type": "sound", "word": word, "filename": filename, "kind": kind, "status": "queued"}
        sse_broadcast("gen_update", gen_jobs[job_id])
        threading.Thread(target=_gen_tts_worker, args=(job_id, text, voice_id, filename), daemon=True).start()
        return web.json_response({"ok": True, "job_id": job_id, "filename": filename})

    return web.json_response({"error": f"Generator nicht implementiert: {meta['generator']}"}, status=500)


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
            _ai_log_entry("suggest", model, user_msg, str(e), "error")
            return web.json_response({"error": str(e)}, status=500)

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


async def api_archive_file(request):
    """Move a sound or image to the archive folder."""
    data = await request.json()
    file_type = data.get("type")  # "sound" or "image"
    filename = data.get("filename", "")

    if not filename or file_type not in ("sound", "image"):
        return web.json_response({"error": "type und filename erforderlich"}, status=400)

    archive_dir = os.path.join(DATA_DIR, "archive", f"{file_type}s")
    os.makedirs(archive_dir, exist_ok=True)

    if file_type == "sound":
        src = os.path.join(SOUNDS_DIR, filename)
    else:
        src = os.path.join(IMAGES_DIR, filename)

    if not os.path.exists(src):
        return web.json_response({"error": f"Datei nicht gefunden: {filename}"}, status=404)

    import shutil
    dst = os.path.join(archive_dir, filename)
    shutil.move(src, dst)
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
    """Serve files from ai-generated directory."""
    subpath = request.match_info["path"]
    path = os.path.join(AI_DIR, subpath)
    if not os.path.exists(path):
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
    while name in taken or any(os.path.exists(os.path.join(d, name)) for d in folders):
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
    app.router.add_delete("/api/sets/{set_id}/letters/{letter}/entries/{index}", api_delete_entry)
    # Numbers within a set
    app.router.add_get("/api/sets/{set_id}/numbers", api_get_numbers)
    app.router.add_put("/api/sets/{set_id}/numbers/{digit}", api_put_number)
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
    app.router.add_post("/api/suggest-word", api_suggest_word)
    app.router.add_get("/api/blacklist/{letter}", api_get_blacklist)
    app.router.add_put("/api/blacklist/{letter}", api_put_blacklist)
    app.router.add_get("/api/jobs", lambda r: web.json_response(list(gen_jobs.values())))
    app.router.add_get("/api/ai-log", lambda r: web.json_response(ai_log))
    app.router.add_get("/api/integrations/status", api_integrations_status)
    app.router.add_get("/api/favorites", lambda r: web.json_response(favorites))
    app.router.add_post("/api/archive", api_archive_file)
    app.router.add_get("/api/archive", api_list_archive)
    app.router.add_get("/ai/{path:.+}", serve_ai_file)
    # File serving
    app.router.add_get("/sounds/{filename}", serve_sound)
    app.router.add_get("/images/{filename}", serve_image)
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
