# funkeykid — Development Guide

## Architecture

```
┌──────────────────────────────────────────────────────┐
│  funkeykid Docker container (Python 3.12)            │
│  Port 8081 — ghcr.io/markus-barta/funkeykid:latest   │
│                                                       │
│  server.py ─── aiohttp web server + API               │
│      ├── keyboard.py ── evdev listener (BT keyboard)  │
│      ├── display.py ─── MQTT publish + Pixoo direct   │
│      └── static/index.html ── SPA (Alpine.js+DaisyUI) │
│                                                       │
│  /data/ (Docker volume mount)                         │
│      ├── settings.json ── all config + sets + letters  │
│      ├── sounds/ ── .mp3 files                        │
│      └── images/ ── .png files (64×64)                │
└──────────────┬───────────┬────────────────────────────┘
               │           │
       MQTT publish    Pixoo HTTP (optional)
               │
               ▼
┌─────────────────────────────┐
│  pidicon-light              │
│  scenes/pixoo/funkeykid.js  │
│  Renders on Pixoo64 display │
└─────────────────────────────┘
```

## Host: hsb1

### Key Services

| Service | How | Purpose |
|---------|-----|---------|
| funkeykid | Docker container (`privileged`) | Keyboard listener, web UI, sound, MQTT |
| pidicon-light | Docker container | Pixoo display rendering |
| NixOS module | `modules/funkeykid.nix` | udev rules, BT reconnect, logind safety |

### NixOS Module (`modules/funkeykid.nix`)

Three independent toggles:

- **`enable`** (default `false`): Systemd Python service — disabled when using Docker
- **`hardwareIsolation`** (default `true`): udev rules that strip ACME BK03 from logind/X11. **MUST stay on** — without it, kid's keypresses type into host terminals and power keys shut down the system
- **`bluetoothReconnect`** (default `true`): Auto-connects ACME BK03 on boot (5 retries)

### Docker Compose Mounts

```yaml
funkeykid:
  privileged: true  # Required for /dev/input access
  volumes:
    - ./mounts/funkeykid/settings.json:/data/settings.json
    - ./mounts/funkeykid/sounds:/data/sounds
    - ./mounts/funkeykid/images:/data/images
    - /dev/input:/dev/input           # evdev keyboard access
    - /run/user/1001/pulse:/run/user/1001/pulse  # PipeWire audio (kiosk user!)
    - /run/user/1001/pipewire-0:/run/user/1001/pipewire-0
    - /run/dbus:/run/dbus             # bluetoothctl D-Bus access
  environment:
    - XDG_RUNTIME_DIR=/run/user/1001  # kiosk user's runtime
    - PULSE_SERVER=unix:/run/user/1001/pulse/native
    - DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket
```

## Audio Routing (CRITICAL)

The audio setup is non-obvious and must be understood to avoid breaking it:

```
kiosk user (uid 1001) owns PipeWire with the real audio hardware:
  → alsa_output.pci-0000_00_1b.0.analog-stereo (speakers)

mba user (uid 1000) has PipeWire with only a null/dummy sink:
  → auto_null (no audio output!)

babycam (VLC) runs as kiosk → plays through kiosk's PipeWire → speakers
funkeykid (Docker) → mounts kiosk's pulse socket → plays through same PipeWire → speakers
Both mix together via PipeWire.
```

**DO NOT switch to mba's PipeWire** — it has no audio hardware. Sound will "succeed" (exit 0) but produce no audible output.

**DO NOT break the kiosk PipeWire session** — the babycam depends on it.

## Keyboard (ACME BK03 Bluetooth)

### Layout: German QWERTZ

evdev reports **US QWERTY scancodes** regardless of keyboard locale:
- Physical German "Z" (next to T) → evdev `KEY_Y`
- Physical German "Y" (bottom row) → evdev `KEY_Z`

`keyboard.py` applies a layout map after evdev extraction:
```python
LAYOUTS = {
    "de": {"Y": "Z", "Z": "Y", "RIGHTBRACE": "EQUAL", "SLASH": "MINUS"},
}
```

### BT Connection Lifecycle

1. NixOS `acme-bk03-reconnect.service` runs on boot → tries 5× to connect
2. Keyboard appears as `/dev/input/eventN` when connected
3. Docker needs `/dev/input` bind mount — **new devices after container start require restart**
4. `keyboard.py` polls `evdev.list_devices()` every 5s looking for device name
5. Keyboard disconnect → error handler → retry loop

### Known Issue: Hot-plug

BT keyboard connecting after container start creates a new `/dev/input/eventN` that isn't visible inside the container (Docker device snapshot). Workaround: "Neu verbinden" button in web UI restarts the listener thread. For truly seamless hot-plug, the container would need a restart.

## Generating Sounds

All sounds generated via **ElevenLabs Sound Effects API**:

```bash
curl -X POST "https://api.elevenlabs.io/v1/sound-generation" \
  -H "xi-api-key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "happy puppy barking and tail wagging", "duration_seconds": 3, "prompt_influence": 0.6}' \
  -o sounds/h_hund.mp3
```

- **Cost**: 100 credits per generation (auto duration), 20 credits/second (manual duration)
- **API key**: stored in agenix on hsb0 (`hsb0-elevenlabs-api-key`)
- **Naming**: `{letter}_{word}.mp3` (lowercase, umlauts → ascii: ä→ae, ö→oe, ü→ue)
- **Duration**: 3 seconds per sound
- **Prompt tips**: Be specific and descriptive. "happy puppy barking" > "dog sound"

### Batch Generation

See `scripts/gen_sounds_v2.py` (runs on hsb0 where the API key lives).

## Generating Images

All images generated via **Google Gemini 2.5 Flash Image** on OpenRouter:

```bash
curl -X POST "https://openrouter.ai/api/v1/chat/completions" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "google/gemini-2.5-flash-image", "messages": [{"role": "user", "content": "Generate a 512x512 image: a happy golden retriever puppy. Pixar-style 3D cartoon, vibrant colors, soft lighting. Must be recognizable at 64x64."}]}'
```

Response contains `choices[0].message.images[0].image_url.url` as base64 data URI.

- **Model**: `google/gemini-2.5-flash-image` (also available: `gpt-5-image-mini`)
- **API key**: OpenRouter key in agenix on hsb0 (`hsb0-openclaw-openrouter-key`)
- **Resize**: Generated at 512×512, resized to 64×64 via `sips -z 64 64` (macOS) or Pillow
- **Naming**: `{letter}_{word}.png` (same convention as sounds)
- **Style prompt**: Always include "Pixar-style 3D cartoon, vibrant colors, simple composition, recognizable at 64x64"
- **Filename encoding**: No umlauts! chamäleon → chamaeleon

### Batch Generation

See `scripts/gen_images_v2.py` (runs on hsb0).

## Deployment

### Quick Hotfix (no CI wait)

```bash
# Update Python files directly in running container
scp server.py mba@hsb1.lan:/tmp/fk.py
ssh mba@hsb1.lan "docker cp /tmp/fk.py funkeykid:/app/server.py && docker restart funkeykid"

# Static files (no restart needed)
scp static/index.html mba@hsb1.lan:/tmp/fk.html
ssh mba@hsb1.lan "docker cp /tmp/fk.html funkeykid:/app/static/index.html"
```

**Important**: Hotfixed files are lost on `docker compose up --force-recreate` or image pull. Always commit + push so CI builds a permanent image.

### Full Deployment (via CI)

```bash
git add . && git commit -m "..." && git push
# Wait for GitHub Actions CI
gh run watch --exit-status
# Pull new image on hsb1
ssh mba@hsb1.lan "cd ~/docker && docker compose pull funkeykid && docker compose up -d funkeykid"
```

### After Container Recreate

The `bluez` package (for BT diagnostics) is baked into the Dockerfile. No manual `apt-get install` needed after CI builds.

## Settings Format

```json
{
  "keyboard_layout": "de",
  "active_set": "v2-komplett",
  "sets": {
    "v2-komplett": {
      "name": "v2.0 Komplett",
      "letters": {
        "A": { "entries": [
          { "word": "Affe", "sound": "a_affe.mp3", "image": "a_affe.png", "enabled": true },
          { "word": "Apfel", "sound": "a_apfel.mp3", "image": "a_apfel.png", "enabled": true }
        ]}
      }
    }
  }
}
```

- **Sets**: Named collections, switchable from navbar
- **Entries**: Per-letter, each with own word + sound + image + enabled flag
- **Cycling**: Same key pressed consecutively cycles through enabled entries
- **CRUD**: Full set management via web UI + REST API

## Web UI

`http://hsb1.lan:8081`

- **Buchstaben**: 26 letter cards, click to edit entries
- **Sets**: Create, duplicate, rename, delete, activate
- **Tastatur**: Virtual QWERTZ keyboard for testing
- **Einstellungen**: Layout, volume, display mode, random sounds
- **Dateien**: Upload/preview sounds and images
- **Status bar**: Always visible — last key, "ago" timer, stop button
- **Diagnostics**: Click keyboard badge — shows evdev devices, BT info, system status
- **SSE**: Real-time updates via Server-Sent Events (no polling delay)

## API

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | /api/events | SSE stream (keypress, connection, volume) |
| GET | /api/status | Keyboard + system status |
| GET | /api/diagnostics | Full BT + device diagnostics |
| POST | /api/reconnect | Restart keyboard listener |
| GET/PUT | /api/settings | Global settings |
| GET/POST | /api/sets | List/create sets |
| GET/PUT/DELETE | /api/sets/{id} | Set CRUD |
| POST | /api/sets/{id}/activate | Switch active set |
| GET/PUT | /api/sets/{id}/letters/{L} | Letter entries |
| POST | /api/test/{letter} | Simulate keypress |
| GET/POST | /api/sounds | List/upload sounds |
| GET/POST | /api/images | List/upload images |
