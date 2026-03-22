#!/usr/bin/env python3
"""funkeykid — Educational keyboard toy for children.

Turns a dedicated Bluetooth keyboard into a learning tool:
- Plays language-appropriate sounds per letter
- Shows letters on Pixoo display via MQTT (pixdcon)
- Optionally speaks words via TTS (ElevenLabs)
"""
import evdev
import subprocess
import os
import random
import sys
import time
import threading
import json
from pathlib import Path

try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False
    print("Warning: paho-mqtt not available, MQTT disabled")

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False
    print("Warning: requests not available, TTS disabled")


# Global state
last_key_time = {}
last_any_key_time = 0
active_processes = []
mqtt_client = None
last_key_pressed = None


def load_config(config_path):
    """Load JSON config file."""
    if not os.path.exists(config_path):
        print(f"Error: Config file {config_path} not found")
        sys.exit(1)
    with open(config_path, 'r') as f:
        return json.load(f)


def load_language_pack(lang_dir, language):
    """Load language pack JSON."""
    lang_file = os.path.join(lang_dir, f"{language}.json")
    if not os.path.exists(lang_file):
        print(f"Error: Language pack {lang_file} not found")
        sys.exit(1)
    with open(lang_file, 'r') as f:
        return json.load(f)


def get_sound_files(sound_dir):
    """Get list of audio files in directory."""
    if not os.path.exists(sound_dir):
        print(f"Warning: Sound directory {sound_dir} not found")
        return []
    extensions = ('*.wav', '*.mp3', '*.ogg')
    files = []
    for ext in extensions:
        files.extend(Path(sound_dir).glob(ext))
    return files


def stop_all_sounds():
    """Stop all currently playing sounds."""
    global active_processes
    stopped = 0
    for proc in active_processes[:]:
        if proc.poll() is None:
            try:
                proc.terminate()
                stopped += 1
            except Exception:
                pass
        active_processes.remove(proc)
    if stopped > 0:
        mqtt_log(f"Stopped {stopped} sound(s)")


def play_sound(sound_file, device=None):
    """Play sound via PipeWire/paplay."""
    global active_processes
    if not os.path.exists(sound_file):
        mqtt_log(f"Sound file not found: {sound_file}", "error")
        return

    stop_all_sounds()

    proc = subprocess.Popen([
        'paplay',
        '--volume=45875',  # ~70% volume
        str(sound_file)
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    active_processes.append(proc)

    time.sleep(0.1)
    if proc.poll() is not None:
        stdout, stderr = proc.communicate()
        if stderr:
            mqtt_log(f"paplay error: {stderr.decode()}", "error")

    mqtt_log(f"Playing: {os.path.basename(sound_file)}")


def mqtt_log(message, level="info"):
    """Send debug log to MQTT."""
    global mqtt_client
    if mqtt_client and mqtt_client.is_connected():
        try:
            payload = json.dumps({
                "timestamp": time.time(),
                "level": level,
                "message": message
            })
            mqtt_client.publish(
                f"{mqtt_topic_prefix}/debug",
                payload, qos=0
            )
        except Exception as e:
            print(f"MQTT log error: {e}", flush=True)


def mqtt_publish_status(key_name=None, sound_file=None, word=None):
    """Publish status update to MQTT."""
    global mqtt_client, last_key_pressed
    if mqtt_client and mqtt_client.is_connected():
        try:
            payload = json.dumps({
                "timestamp": time.time(),
                "last_key": key_name or last_key_pressed,
                "word": word,
                "sound_playing": os.path.basename(sound_file) if sound_file else None
            })
            mqtt_client.publish(
                f"{mqtt_topic_prefix}/status",
                payload, qos=0, retain=True
            )
        except Exception as e:
            print(f"MQTT status error: {e}", flush=True)


def mqtt_publish_display(letter, word, color=None):
    """Publish letter to Pixoo display via MQTT (pixdcon picks this up)."""
    global mqtt_client
    if mqtt_client and mqtt_client.is_connected():
        try:
            payload = json.dumps({
                "letter": letter,
                "word": word,
                "color": color or random_color(),
                "timestamp": time.time()
            })
            mqtt_client.publish(
                pixoo_mqtt_topic,
                payload, qos=0
            )
        except Exception as e:
            print(f"MQTT display error: {e}", flush=True)


def random_color():
    """Generate a bright, child-friendly random color."""
    colors = [
        "#FF0000", "#00CC00", "#0066FF", "#FF6600",
        "#CC00CC", "#00CCCC", "#FFCC00", "#FF3399",
        "#6633FF", "#33CC33", "#FF6633", "#3399FF"
    ]
    return random.choice(colors)


def tts_speak(text, config):
    """Speak text via ElevenLabs TTS with caching."""
    if not REQUESTS_AVAILABLE:
        return

    tts_config = config.get("tts", {})
    cache_dir = tts_config.get("cache_dir", "/var/lib/funkeykid-sounds/tts-cache")
    os.makedirs(cache_dir, exist_ok=True)

    # Check cache first
    safe_name = text.replace(" ", "_").replace("/", "_")
    cache_file = os.path.join(cache_dir, f"{safe_name}.mp3")

    if os.path.exists(cache_file):
        play_sound(cache_file)
        return

    # Call ElevenLabs API
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        mqtt_log("ELEVENLABS_API_KEY not set, skipping TTS", "warning")
        return

    voice_id = tts_config.get("voice_id", "21m00Tcm4TlvDq8ikWAM")  # default: Rachel
    try:
        resp = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json"
            },
            json={
                "text": text,
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {
                    "stability": 0.75,
                    "similarity_boost": 0.75
                }
            },
            timeout=10
        )
        if resp.status_code == 200:
            with open(cache_file, 'wb') as f:
                f.write(resp.content)
            play_sound(cache_file)
            mqtt_log(f"TTS cached: {text}")
        else:
            mqtt_log(f"TTS API error {resp.status_code}: {resp.text[:100]}", "error")
    except Exception as e:
        mqtt_log(f"TTS error: {e}", "error")


def should_process_key(key_name, debounce_seconds):
    """Check if key press should be processed (debouncing)."""
    global last_key_time, last_any_key_time
    current_time = time.time()

    if current_time - last_any_key_time < debounce_seconds:
        return False
    if key_name in last_key_time:
        if current_time - last_key_time[key_name] < debounce_seconds:
            return False

    last_key_time[key_name] = current_time
    last_any_key_time = current_time
    return True


def find_device_by_name(device_name):
    """Find input device by name."""
    devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
    for device in devices:
        if device.name == device_name:
            return device.path
    return None


# Module-level config (set in main)
mqtt_topic_prefix = "home/hsb1/funkeykid"
pixoo_mqtt_topic = "home/hsb1/funkeykid/display"


def main():
    global mqtt_client, mqtt_topic_prefix, pixoo_mqtt_topic

    # Load configuration
    config_path = os.getenv('FUNKEYKID_CONFIG', '/etc/funkeykid/config.json')
    config = load_config(config_path)

    # Load language pack
    lang_dir = os.getenv('FUNKEYKID_LANG_DIR', os.path.join(os.path.dirname(__file__), 'lang'))
    language = config.get("language", "de-AT")
    lang_pack = load_language_pack(lang_dir, language)

    device_name = config.get("keyboard_device", "ACME BK03")
    sound_dir = config.get("sound_dir", "/var/lib/funkeykid-sounds")
    debounce = config.get("debounce_seconds", 1.0)
    modes = config.get("modes", {"sound": True, "display": True, "speak": False})

    mqtt_config = config.get("mqtt", {})
    mqtt_topic_prefix = mqtt_config.get("topic_prefix", "home/hsb1/funkeykid")
    pixoo_mqtt_topic = config.get("pixoo", {}).get("mqtt_topic", "home/hsb1/funkeykid/display")

    letters = lang_pack.get("letters", {})
    special_keys = lang_pack.get("special_keys", {})

    sound_files = get_sound_files(sound_dir)

    print(f"funkeykid starting...", flush=True)
    print(f"  Language: {language}", flush=True)
    print(f"  Device: {device_name}", flush=True)
    print(f"  Sound dir: {sound_dir} ({len(sound_files)} files)", flush=True)
    print(f"  Modes: sound={modes.get('sound')}, display={modes.get('display')}, speak={modes.get('speak')}", flush=True)

    # Connect MQTT
    if MQTT_AVAILABLE:
        try:
            mqtt_client = mqtt.Client()
            mqtt_host = mqtt_config.get("host", os.getenv("MOSQUITTO_HOST", "localhost"))
            mqtt_user = mqtt_config.get("user", os.getenv("MOSQUITTO_USER", "smarthome"))
            mqtt_pass = os.getenv("MOSQUITTO_PASS")
            if mqtt_pass:
                mqtt_client.username_pw_set(mqtt_user, mqtt_pass)
            mqtt_port = mqtt_config.get("port", 1883)
            mqtt_client.connect_async(mqtt_host, mqtt_port, 60)
            mqtt_client.loop_start()
            print(f"  MQTT: {mqtt_host}:{mqtt_port}", flush=True)
            time.sleep(0.5)
        except Exception as e:
            print(f"  MQTT failed: {e}", flush=True)
            mqtt_client = None

    # Main connection loop
    while True:
        device_path = None
        if device_name.startswith('/'):
            if os.path.exists(device_name):
                device_path = device_name
        else:
            device_path = find_device_by_name(device_name)

        if not device_path:
            print(f"Waiting for keyboard '{device_name}'...", flush=True)
            mqtt_log(f"Waiting for keyboard '{device_name}'")
            time.sleep(5)
            continue

        try:
            device = evdev.InputDevice(device_path)
            print(f"Opened: {device.name} at {device_path}", flush=True)
            mqtt_log(f"Opened device: {device.name}")

            # Event loop
            for event in device.read_loop():
                if event.type != evdev.ecodes.EV_KEY:
                    continue

                key_event = evdev.categorize(event)
                if key_event.keystate != evdev.KeyEvent.key_down:
                    continue

                key_name = key_event.keycode
                if isinstance(key_name, list):
                    key_name = key_name[0]
                if key_name.startswith('KEY_'):
                    key_name = key_name[4:]

                global last_key_pressed
                last_key_pressed = key_name
                mqtt_log(f"Key: {key_name}")

                # SPACE always stops sounds
                if key_name == 'SPACE':
                    stop_all_sounds()
                    continue

                if not should_process_key(key_name, debounce):
                    continue

                # Check special keys first
                if key_name in special_keys:
                    action = special_keys[key_name]
                    if action == "stop_all":
                        stop_all_sounds()
                    elif action == "random" and sound_files and modes.get("sound"):
                        play_sound(random.choice(sound_files))
                    continue

                # Letter handling
                letter_info = letters.get(key_name)

                if letter_info:
                    word = letter_info.get("word", "")
                    sound_name = letter_info.get("sound", "")
                    tts_text = letter_info.get("tts", "")

                    # Play sound
                    if modes.get("sound") and sound_name:
                        sound_path = os.path.join(sound_dir, sound_name)
                        if os.path.exists(sound_path):
                            play_sound(sound_path)
                        elif sound_files:
                            play_sound(random.choice(sound_files))

                    # Show on Pixoo display
                    if modes.get("display"):
                        mqtt_publish_display(key_name, word)

                    # Speak word via TTS
                    if modes.get("speak") and tts_text:
                        threading.Thread(
                            target=tts_speak,
                            args=(tts_text, config),
                            daemon=True
                        ).start()

                    mqtt_publish_status(key_name, sound_name, word)

                else:
                    # Unknown key: play random sound
                    if modes.get("sound") and sound_files:
                        play_sound(random.choice(sound_files))

        except (OSError, Exception) as e:
            print(f"Device error: {e}", flush=True)
            mqtt_log(f"Device error: {e}", "warning")
            stop_all_sounds()
            time.sleep(5)
            continue
        except KeyboardInterrupt:
            print("\nStopping...", flush=True)
            mqtt_log("Service stopping")
            break

    if mqtt_client:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()


if __name__ == '__main__':
    main()
