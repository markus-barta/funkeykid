"""funkeykid v2 — aiohttp web server + keyboard handler with sets."""
import asyncio
import json
import os
import subprocess
import random
import threading
import time
import uuid
import weakref
from pathlib import Path

from aiohttp import web

from keyboard import KeyboardListener
from display import Display
from version import VERSION, BUILD, REPO

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
    """Resolve entry + active set's track_order into a list of absolute sound paths."""
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
            files.append(path)
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
    current_volume = settings.get("volume", 100)
    return settings


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
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write("\n")


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


_play_seq_abort = False


def stop_all_sounds():
    global active_processes, _play_seq_abort
    _play_seq_abort = True
    for proc in active_processes[:]:
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            active_processes.remove(proc)
        except ValueError:
            pass


def play_sound_sequence(paths, gap_ms=120):
    """Play sound files back-to-back in a daemon thread. Aborts on stop_all_sounds.

    `gap_ms` is a small silence inserted between tracks so FX doesn't step on TTS.
    """
    global _play_seq_abort
    stop_all_sounds()
    if not paths:
        return
    _play_seq_abort = False
    env = os.environ.copy()
    # Pin the sink once per sequence (cheap, prevents external volume drift).
    try:
        subprocess.run(
            ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "100%"],
            env=env, timeout=2, capture_output=True,
        )
    except Exception:
        pass

    def _runner(file_list):
        global _play_seq_abort
        pa_vol = int(current_volume / 100 * 65536)
        for i, p in enumerate(file_list):
            if _play_seq_abort:
                return
            try:
                proc = subprocess.Popen(
                    ["paplay", f"--volume={pa_vol}", str(p)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=env,
                )
                active_processes.append(proc)
                proc.wait()
                try:
                    active_processes.remove(proc)
                except ValueError:
                    pass
            except Exception as e:
                print(f"[sound] sequence error on {p}: {e}", flush=True)
                return
            if _play_seq_abort:
                return
            if i < len(file_list) - 1 and gap_ms > 0:
                time.sleep(gap_ms / 1000.0)

    threading.Thread(target=_runner, args=(list(paths),), daemon=True).start()


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
    first_sound = os.path.basename(files[0]) if files else ""
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
    first_sound = os.path.basename(files[0]) if files else ""
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


def handle_key(key_name, raw_key=None):
    """Handle a key press — cycles through enabled entries per letter."""
    global last_letter, cycle_index, favorites, flat_pos
    raw_key = raw_key or key_name
    if time.time() - startup_time < STARTUP_GRACE_SECONDS:
        return
    print(f"[key] handle_key({key_name}, raw={raw_key})", flush=True)

    sse_broadcast("rawkey", {"key": key_name, "raw": raw_key, "timestamp": time.time()})

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
    status = keyboard.get_status() if keyboard else {"connected": False}
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
    status = keyboard.get_status() if keyboard else {"connected": False}
    status["volume"] = current_volume
    status["mqtt_connected"] = display._mqtt_connected if display else False
    status["active_set"] = settings.get("active_set", "")
    return web.json_response(status)


async def api_get_diagnostics(request):
    """Detailed keyboard + BT diagnostics for troubleshooting."""
    diag = keyboard.get_diagnostics() if keyboard else {"error": "no keyboard listener"}
    return web.json_response(diag)


async def api_reconnect_keyboard(request):
    """Restart the keyboard listener thread to pick up newly connected devices."""
    global keyboard
    if keyboard:
        keyboard.stop()
        time.sleep(1)
        keyboard.start()
        return web.json_response({"ok": True, "message": "Keyboard listener restarted"})
    return web.json_response({"error": "No keyboard listener"}, status=500)


async def api_get_settings(request):
    return web.json_response(settings)


async def api_put_settings(request):
    global settings
    data = await request.json()
    # Only update top-level config keys, not sets (those have own endpoints)
    for k in ("keyboard_layout", "keyboard_device", "volume", "random_sounds_enabled",
              "display_mode", "pixoo_ip", "debounce_seconds", "active_set", "mqtt", "ai_prompts",
              "numbers_tts"):
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
    set_id = data.get("id") or _slugify(data.get("name", "new-set"))
    if "sets" not in settings:
        settings["sets"] = {}
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


async def api_activate_set(request):
    set_id = request.match_info["set_id"]
    if set_id not in settings.get("sets", {}):
        return web.json_response({"error": "Set not found"}, status=404)
    settings["active_set"] = set_id
    global cycle_index, last_letter
    cycle_index = {}
    last_letter = None
    save_settings()
    return web.json_response({"ok": True, "active_set": set_id})


# ── Letter entries within a set ──────────────────────────────────────────────

async def api_get_letter(request):
    set_id = request.match_info["set_id"]
    letter = request.match_info["letter"].upper()
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
    letter = request.match_info["letter"].upper()
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
    letter = request.match_info["letter"].upper()
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
    letter = request.match_info["letter"].upper()
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


async def api_upload_sound(request):
    os.makedirs(SOUNDS_DIR, exist_ok=True)
    reader = await request.multipart()
    field = await reader.next()
    filename = field.filename
    path = os.path.join(SOUNDS_DIR, filename)
    with open(path, "wb") as f:
        while True:
            chunk = await field.read_chunk()
            if not chunk:
                break
            f.write(chunk)
    return web.json_response({"ok": True, "file": filename})


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
    letter = request.match_info["letter"].upper()
    handle_key(letter)
    return web.json_response({"ok": True, "letter": letter})


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
    return web.json_response({"version": VERSION, "build": BUILD, "repo": REPO, "build_time": build_time()})


# ── AI Generation ───────────────────────────────────────────────────────────

AI_DIR = os.path.join(DATA_DIR, "ai-generated")
AI_SOUNDS_DIR = os.path.join(AI_DIR, "sounds")
AI_IMAGES_ORIG_DIR = os.path.join(AI_DIR, "images-original")
AI_IMAGES_RESIZED_DIR = os.path.join(AI_DIR, "images-resized")

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
        shutil.copy2(outpath, os.path.join(SOUNDS_DIR, filename))
        gen_jobs[job_id].update({"status": "done", "size": len(audio_data), "path": f"/sounds/{filename}"})
        _ai_log_entry("sound", "elevenlabs/sfx", prompt, f"OK: {len(audio_data)} bytes → {filename}", "ok")
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
    filename = data.get("filename", _slugify(word) + ".mp3")
    if not os.environ.get("ELEVENLABS_API_KEY"):
        return web.json_response({"error": "ELEVENLABS_API_KEY nicht gesetzt"}, status=500)
    job_id = f"snd_{uuid.uuid4().hex[:8]}"
    gen_jobs[job_id] = {"id": job_id, "type": "sound", "word": word, "filename": filename, "status": "queued"}
    sse_broadcast("gen_update", gen_jobs[job_id])
    import threading
    threading.Thread(target=_gen_sound_worker, args=(job_id, word, prompt, duration, filename), daemon=True).start()
    return web.json_response({"ok": True, "job_id": job_id})


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
    filename = data.get("filename", _slugify(word) + ".png")
    model = data.get("model")
    if not os.environ.get("OPENROUTER_API_KEY"):
        return web.json_response({"error": "OPENROUTER_API_KEY nicht gesetzt"}, status=500)
    job_id = f"img_{uuid.uuid4().hex[:8]}"
    gen_jobs[job_id] = {"id": job_id, "type": "image", "word": word, "filename": filename, "status": "queued"}
    sse_broadcast("gen_update", gen_jobs[job_id])
    import threading
    threading.Thread(target=_gen_image_worker, args=(job_id, word, prompt, filename, model), daemon=True).start()
    return web.json_response({"ok": True, "job_id": job_id})


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
        shutil.copy2(outpath, os.path.join(SOUNDS_DIR, filename))
        gen_jobs[job_id].update({"status": "done", "size": len(audio_data), "path": f"/sounds/{filename}"})
        _ai_log_entry("tts", f"elevenlabs/tts/{voice_id}", text, f"OK: {len(audio_data)}b → {filename}", "ok")
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
    filename = data.get("filename") or (_slugify(text) + ".mp3")
    word = data.get("word", text)
    if not os.environ.get("ELEVENLABS_API_KEY"):
        return web.json_response({"error": "ELEVENLABS_API_KEY nicht gesetzt"}, status=500)
    job_id = f"tts_{uuid.uuid4().hex[:8]}"
    gen_jobs[job_id] = {"id": job_id, "type": "sound", "word": word, "filename": filename, "status": "queued"}
    sse_broadcast("gen_update", gen_jobs[job_id])
    import threading
    threading.Thread(target=_gen_tts_worker, args=(job_id, text, voice_id, filename), daemon=True).start()
    return web.json_response({"ok": True, "job_id": job_id})


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
    filename = data.get("filename") or (base + ".mp3")

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
    letter = data.get("letter", "A").upper()

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
        'Antwort NUR als JSON: {{"word": "...", "sound_description": "... (Englisch)", "image_description": "... (Englisch)"}}'
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

    system_msg = f"""Du bist ein Kinderwort-Generator. Du schlägst deutsche Wörter vor die mit einem bestimmten Buchstaben beginnen.
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
    letter = request.match_info["letter"].upper()
    words = settings.get("blacklist", {}).get(letter, [])
    return web.json_response(words)


async def api_put_blacklist(request):
    """Set blacklisted words for a letter."""
    letter = request.match_info["letter"].upper()
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


def _slugify(text):
    return text.lower().replace(" ", "-").replace("&", "und").replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")[:30]


def _reload_runtime():
    global current_volume
    current_volume = settings.get("volume", 100)
    if keyboard:
        from keyboard import LAYOUTS
        keyboard.layout = LAYOUTS.get(settings.get("keyboard_layout", "de"), {})
        keyboard.debounce_seconds = settings.get("debounce_seconds", 0.8)
    if display:
        display.reload_settings(settings)


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
    app.router.add_get("/api/images", api_get_images)
    app.router.add_post("/api/images/upload", api_upload_image)
    # Test
    app.router.add_post("/api/test/{letter}", api_test_key)
    app.router.add_get("/api/layout/{name}", api_get_layout)
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

    settings = load_settings()
    _load_favorites()
    set_count = len(settings.get("sets", {}))
    active = settings.get("active_set", "?")
    print(f"[funkeykid] Settings loaded: {set_count} set(s), active={active}, {len(favorites)} favorites", flush=True)

    # Display
    display = Display(settings)
    display.connect()

    # Keyboard
    device_name = settings.get("keyboard_device", "ACME BK03")
    layout = settings.get("keyboard_layout", "de")
    debounce = settings.get("debounce_seconds", 0.8)
    keyboard = KeyboardListener(device_name, layout=layout, debounce_seconds=debounce)
    keyboard.on_key(handle_key)
    def _on_keyboard_connection(connected, status):
        sse_broadcast("connection", status)
        if display:
            display.publish_keyboard_status(status)
    keyboard.on_connection_change(_on_keyboard_connection)
    keyboard.start()
    print(f"[funkeykid] Keyboard: {device_name} (layout={layout})", flush=True)

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
        print(f"[funkeykid] Server running on http://0.0.0.0:{PORT}", flush=True)
        # Keep running forever
        await asyncio.Event().wait()

    asyncio.run(start_app())


if __name__ == "__main__":
    main()
