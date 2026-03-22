"""funkeykid v2 — aiohttp web server + keyboard handler."""
import asyncio
import json
import os
import subprocess
import random
import time
from pathlib import Path

from aiohttp import web

from keyboard import KeyboardListener
from display import Display

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
last_letter = None
word_index = 0


def load_settings():
    global settings, current_volume
    if os.path.exists(SETTINGS_PATH):
        with open(SETTINGS_PATH) as f:
            settings = json.load(f)
    current_volume = settings.get("volume", 100)
    return settings


def save_settings():
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write("\n")


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
    proc = subprocess.Popen(
        ["paplay", f"--volume={pa_vol}", str(sound_file)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    active_processes.append(proc)


def change_volume(delta):
    global current_volume
    current_volume = max(0, min(100, current_volume + delta))
    settings["volume"] = current_volume
    print(f"[volume] {current_volume}%", flush=True)
    if display:
        display.publish_volume(current_volume)


def handle_key(key_name):
    """Handle a key press from keyboard or test mode."""
    global last_letter, word_index
    print(f"[key] handle_key({key_name})", flush=True)

    # Space = stop
    if key_name == "SPACE":
        stop_all_sounds()
        return

    # Volume
    if key_name in ("EQUAL", "KPPLUS"):
        change_volume(10)
        return
    if key_name in ("MINUS", "KPMINUS"):
        change_volume(-10)
        return

    # Letter lookup
    letters = settings.get("letters", {})
    letter_cfg = letters.get(key_name)
    print(f"[key] letter_cfg for {key_name}: {'found' if letter_cfg else 'NOT FOUND'}", flush=True)

    if not letter_cfg:
        if settings.get("random_sounds_enabled"):
            # Play random sound from any letter
            all_sounds = []
            for lc in letters.values():
                for s in lc.get("sounds", []):
                    p = os.path.join(SOUNDS_DIR, s)
                    if os.path.exists(p):
                        all_sounds.append(p)
            if all_sounds:
                play_sound(random.choice(all_sounds))
        return

    # Word cycling
    words = letter_cfg.get("words", [key_name])
    if key_name == last_letter:
        word_index = (word_index + 1) % len(words)
    else:
        word_index = 0
        last_letter = key_name

    word = words[word_index] if words else key_name
    image = letter_cfg.get("image")

    # Play sound
    sounds = letter_cfg.get("sounds", [])
    if sounds:
        sound_file = os.path.join(SOUNDS_DIR, sounds[0])
        print(f"[key] Playing: {sound_file}", flush=True)
        play_sound(sound_file)

    # Display
    if display:
        print(f"[key] Publishing: {key_name} → {word} (mqtt_connected={display._mqtt_connected})", flush=True)
        display.publish_letter(key_name, word, image)
    else:
        print("[key] WARNING: display is None!", flush=True)
        display.log(f"Key: {key_name} → {word}")


# ── Web API ──────────────────────────────────────────────────────────────────

async def api_get_settings(request):
    return web.json_response(settings)


async def api_put_settings(request):
    global settings
    data = await request.json()
    settings.update(data)
    save_settings()
    # Hot-reload keyboard and display
    if keyboard:
        from keyboard import LAYOUTS
        keyboard.layout = LAYOUTS.get(settings.get("keyboard_layout", "de"), {})
        keyboard.debounce_seconds = settings.get("debounce_seconds", 0.8)
    if display:
        display.reload_settings(settings)
    return web.json_response({"ok": True})


async def api_get_status(request):
    """Live keyboard status — polled by web UI every 1-2s."""
    status = keyboard.get_status() if keyboard else {"connected": False}
    status["volume"] = current_volume
    status["mqtt_connected"] = display._mqtt_connected if display else False
    return web.json_response(status)


async def api_get_letters(request):
    return web.json_response(settings.get("letters", {}))


async def api_put_letter(request):
    letter = request.match_info["letter"].upper()
    data = await request.json()
    if "letters" not in settings:
        settings["letters"] = {}
    settings["letters"][letter] = data
    save_settings()
    return web.json_response({"ok": True, "letter": letter})


async def api_get_sounds(request):
    os.makedirs(SOUNDS_DIR, exist_ok=True)
    files = sorted(
        f for f in os.listdir(SOUNDS_DIR)
        if f.endswith((".mp3", ".wav", ".ogg"))
    )
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
    files = sorted(
        f for f in os.listdir(IMAGES_DIR)
        if f.endswith((".png", ".jpg", ".jpeg", ".gif"))
    )
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
    layout = LAYOUTS.get(name, {})
    return web.json_response(layout)


async def serve_sound(request):
    """Serve sound files for web preview."""
    filename = request.match_info["filename"]
    path = os.path.join(SOUNDS_DIR, filename)
    if not os.path.exists(path):
        return web.Response(status=404)
    return web.FileResponse(path)


async def serve_image(request):
    """Serve image files for web preview."""
    filename = request.match_info["filename"]
    path = os.path.join(IMAGES_DIR, filename)
    if not os.path.exists(path):
        return web.Response(status=404)
    return web.FileResponse(path)


async def serve_index(request):
    """Serve the SPA."""
    return web.FileResponse(os.path.join(STATIC_DIR, "index.html"))


def create_app():
    app = web.Application()
    # API
    app.router.add_get("/api/status", api_get_status)
    app.router.add_get("/api/settings", api_get_settings)
    app.router.add_put("/api/settings", api_put_settings)
    app.router.add_get("/api/letters", api_get_letters)
    app.router.add_put("/api/letters/{letter}", api_put_letter)
    app.router.add_get("/api/sounds", api_get_sounds)
    app.router.add_post("/api/sounds/upload", api_upload_sound)
    app.router.add_get("/api/images", api_get_images)
    app.router.add_post("/api/images/upload", api_upload_image)
    app.router.add_post("/api/test/{letter}", api_test_key)
    app.router.add_get("/api/layout/{name}", api_get_layout)
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
    print(f"[funkeykid] Settings loaded: {len(settings.get('letters', {}))} letters", flush=True)

    # Display (MQTT + Pixoo)
    display = Display(settings)
    display.connect()

    # Keyboard
    device_name = settings.get("keyboard_device", "ACME BK03")
    layout = settings.get("keyboard_layout", "de")
    debounce = settings.get("debounce_seconds", 0.8)
    keyboard = KeyboardListener(device_name, layout=layout, debounce_seconds=debounce)
    keyboard.on_key(handle_key)
    keyboard.start()
    print(f"[funkeykid] Keyboard listener started: {device_name} (layout={layout})", flush=True)

    # Web server
    app = create_app()
    print(f"[funkeykid] Web UI: http://0.0.0.0:{PORT}", flush=True)
    web.run_app(app, host="0.0.0.0", port=PORT, print=None)


if __name__ == "__main__":
    main()
