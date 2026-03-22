"""funkeykid v2 — aiohttp web server + keyboard handler with sets."""
import asyncio
import json
import os
import subprocess
import random
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
# Favorites: list of {letter, entry_index} — up to 10
favorites = []
# AI generation jobs tracking
gen_jobs = {}  # {job_id: {type, word, status, filename, error}}


def load_settings():
    global settings, current_volume
    if os.path.exists(SETTINGS_PATH):
        with open(SETTINGS_PATH) as f:
            settings = json.load(f)
    # Migrate old flat format → sets format
    if "letters" in settings and "sets" not in settings:
        _migrate_to_sets()
    current_volume = settings.get("volume", 100)
    return settings


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


def stop_all_sounds():
    global active_processes
    for proc in active_processes[:]:
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        active_processes.remove(proc)


def play_sound(sound_file):
    global active_processes, current_volume
    if not os.path.exists(sound_file):
        print(f"[sound] Not found: {sound_file}", flush=True)
        return
    stop_all_sounds()
    pa_vol = int(current_volume / 100 * 65536)
    env = os.environ.copy()
    # Use container's PULSE_SERVER (set via docker-compose, points to kiosk's PipeWire)
    # Don't override if already set correctly
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
    """Play a specific entry — sound + display + SSE broadcast."""
    word = entry.get("word", key_name)
    sound = entry.get("sound", "")
    image = entry.get("image", "")

    print(f"[key] {key_name} → {word} (sound={sound}, image={image})", flush=True)

    try:
        sse_broadcast("keypress", {
            "letter": key_name, "word": word, "sound": sound, "image": image,
            "entry_index": entry_index, "timestamp": time.time(),
        })
    except Exception as e:
        print(f"[sse] broadcast error: {e}", flush=True)

    if sound:
        sound_path = os.path.join(SOUNDS_DIR, sound)
        print(f"[key] Playing: {sound_path}", flush=True)
        play_sound(sound_path)
    if display:
        display.publish_letter(key_name, word, image)


def handle_key(key_name, raw_key=None):
    """Handle a key press — cycles through enabled entries per letter."""
    global last_letter, cycle_index, favorites
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

    # Tab = toggle favorite for last played letter
    if key_name == "TAB":
        _toggle_favorite()
        return

    # Number keys 1-0 = play favorite
    if key_name in ("1","2","3","4","5","6","7","8","9","0"):
        fav_idx = int(key_name) - 1 if key_name != "0" else 9
        _play_favorite(fav_idx)
        return

    # Get enabled entries for this letter
    entries = get_enabled_entries(key_name)

    if not entries:
        if settings.get("random_sounds_enabled"):
            all_sounds = []
            for ldata in get_active_set().values():
                for e in ldata.get("entries", []):
                    if e.get("enabled") and e.get("sound"):
                        p = os.path.join(SOUNDS_DIR, e["sound"])
                        if os.path.exists(p):
                            all_sounds.append(p)
            if all_sounds:
                play_sound(random.choice(all_sounds))
        return

    # Position memory: advance index if same letter, keep position if returning
    if key_name == last_letter:
        idx = (cycle_index.get(key_name, 0) + 1) % len(entries)
    else:
        # Keep last position for this letter (don't reset!)
        idx = cycle_index.get(key_name, 0) % len(entries)
    cycle_index[key_name] = idx
    last_letter = key_name

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
              "display_mode", "pixoo_ip", "debounce_seconds", "active_set", "mqtt"):
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
    """Get full set data including all letters and entries."""
    set_id = request.match_info["set_id"]
    sets = settings.get("sets", {})
    if set_id not in sets:
        return web.json_response({"error": "Set not found"}, status=404)
    return web.json_response(sets[set_id])


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
    letters = sets[set_id].get("letters", {})
    return web.json_response(letters.get(letter, {"entries": []}))


async def api_put_letter(request):
    """Replace all entries for a letter in a set."""
    set_id = request.match_info["set_id"]
    letter = request.match_info["letter"].upper()
    data = await request.json()
    sets = settings.get("sets", {})
    if set_id not in sets:
        return web.json_response({"error": "Set not found"}, status=404)
    if "letters" not in sets[set_id]:
        sets[set_id]["letters"] = {}
    sets[set_id]["letters"][letter] = data
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
    return web.FileResponse(os.path.join(STATIC_DIR, "index.html"))


async def api_version(request):
    return web.json_response({"version": VERSION, "build": BUILD, "repo": REPO})


# ── AI Generation ───────────────────────────────────────────────────────────

AI_DIR = os.path.join(DATA_DIR, "ai-generated")
AI_SOUNDS_DIR = os.path.join(AI_DIR, "sounds")
AI_IMAGES_ORIG_DIR = os.path.join(AI_DIR, "images-original")
AI_IMAGES_RESIZED_DIR = os.path.join(AI_DIR, "images-resized")

DEFAULT_SOUND_PROMPT = "Clear, distinct {word} sound effect, high quality, recognizable for children"
DEFAULT_IMAGE_PROMPT = "Pixar-style 3D cartoon of {description}. Vibrant saturated colors, soft lighting, rounded friendly shapes, big expressive eyes. Simple composition, recognizable at 64x64 pixels. Studio quality children's animation style."


def _gen_sound_worker(job_id, word, prompt, duration, filename):
    """Background worker for sound generation."""
    import urllib.request, shutil
    gen_jobs[job_id]["status"] = "generating"
    sse_broadcast("gen_update", gen_jobs[job_id])
    api_key = os.environ.get("ELEVENLABS_API_KEY", "")
    os.makedirs(AI_SOUNDS_DIR, exist_ok=True)
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
    except Exception as e:
        gen_jobs[job_id].update({"status": "error", "error": str(e)})
    sse_broadcast("gen_update", gen_jobs[job_id])


async def api_generate_sound(request):
    """Start async sound generation — returns job ID immediately."""
    data = await request.json()
    word = data.get("word", "")
    prompt = data.get("prompt", DEFAULT_SOUND_PROMPT.format(word=word))
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


def _gen_image_worker(job_id, word, prompt, filename):
    """Background worker for image generation."""
    import urllib.request, base64, shutil
    gen_jobs[job_id]["status"] = "generating"
    sse_broadcast("gen_update", gen_jobs[job_id])
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    os.makedirs(AI_IMAGES_ORIG_DIR, exist_ok=True)
    os.makedirs(AI_IMAGES_RESIZED_DIR, exist_ok=True)
    try:
        payload = json.dumps({
            "model": "google/gemini-2.5-flash-image",
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
    except Exception as e:
        gen_jobs[job_id].update({"status": "error", "error": str(e)})
    sse_broadcast("gen_update", gen_jobs[job_id])


async def api_generate_image(request):
    """Start async image generation — returns job ID immediately."""
    data = await request.json()
    word = data.get("word", "")
    description = data.get("description", word)
    prompt = data.get("prompt", DEFAULT_IMAGE_PROMPT.format(description=description))
    filename = data.get("filename", _slugify(word) + ".png")
    if not os.environ.get("OPENROUTER_API_KEY"):
        return web.json_response({"error": "OPENROUTER_API_KEY nicht gesetzt"}, status=500)
    job_id = f"img_{uuid.uuid4().hex[:8]}"
    gen_jobs[job_id] = {"id": job_id, "type": "image", "word": word, "filename": filename, "status": "queued"}
    sse_broadcast("gen_update", gen_jobs[job_id])
    import threading
    threading.Thread(target=_gen_image_worker, args=(job_id, word, prompt, filename), daemon=True).start()
    return web.json_response({"ok": True, "job_id": job_id})


async def api_suggest_word(request):
    """AI-suggest a fitting German word for a letter, not already used."""
    data = await request.json()
    letter = data.get("letter", "A").upper()
    language = data.get("language", "de-AT")

    # Collect already-used words for this letter
    used_words = set()
    for s in settings.get("sets", {}).values():
        for entry in s.get("letters", {}).get(letter, {}).get("entries", []):
            used_words.add(entry.get("word", "").lower())

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return web.json_response({"error": "OPENROUTER_API_KEY nicht gesetzt"}, status=500)

    used_list = ", ".join(sorted(used_words)) if used_words else "keine"
    prompt = f"""Schlage EIN deutsches Wort vor, das mit dem Buchstaben {letter} beginnt.

Anforderungen:
- Sprache: Deutsch (Österreich)
- Zielgruppe: Kinder 2-5 Jahre
- Das Wort muss ein konkretes Ding/Tier/Objekt sein (kein abstraktes Konzept)
- Es muss ein dazu passendes, klar erkennbares Geräusch geben
- Nicht anstößig, kindgerecht
- NICHT eines dieser bereits verwendeten Wörter: {used_list}

Antworte NUR mit einem JSON-Objekt in diesem Format (keine Erklärung):
{{"word": "Wort", "sound_description": "Beschreibung des Geräuschs auf Englisch für Sound-KI", "image_description": "Beschreibung des Bildes auf Englisch für Bild-KI"}}"""

    try:
        import urllib.request
        payload = json.dumps({
            "model": "openai/gpt-4.1-nano",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.9,
        }).encode()
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            d = json.load(resp)

        content = d["choices"][0]["message"]["content"]
        # Parse JSON from response (might have markdown wrapping)
        import re
        json_match = re.search(r'\{[^}]+\}', content)
        if json_match:
            suggestion = json.loads(json_match.group())
            return web.json_response(suggestion)
        return web.json_response({"error": "KI-Antwort nicht parsebar", "raw": content[:200]}, status=500)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


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
    app.router.add_post("/api/suggest-word", api_suggest_word)
    app.router.add_get("/api/jobs", lambda r: web.json_response(list(gen_jobs.values())))
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
    keyboard.on_connection_change(
        lambda connected, status: sse_broadcast("connection", status)
    )
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
