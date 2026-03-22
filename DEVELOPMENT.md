# funkeykid — Development Guide

**Version**: 2.4.0
**Repo**: https://github.com/markus-barta/funkeykid
**Host**: hsb1 (Home Server Barta 1)
**Image**: ghcr.io/markus-barta/funkeykid:latest

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│  funkeykid Docker container (Python 3.12 + aiohttp)  │
│  Port 8081 — ghcr.io/markus-barta/funkeykid:latest   │
│                                                       │
│  server.py ─── Web server + REST API + SSE            │
│      ├── keyboard.py ── evdev listener (BT keyboard)  │
│      ├── display.py ─── MQTT publish + Pixoo direct   │
│      ├── version.py ─── Version info                  │
│      └── static/index.html ── SPA (Alpine.js+DaisyUI) │
│                                                       │
│  /data/ (Docker volume mounts — ALL persistent)       │
│      ├── settings.json ── Config + sets + letters     │
│      ├── sounds/ ── Active .mp3 files                 │
│      ├── images/ ── Active .png files (64×64)         │
│      ├── ai-generated/ ── ALL AI output (preserved)   │
│      │   ├── sounds/ ── Generated sounds              │
│      │   ├── images-original/ ── Full-res (512×512)   │
│      │   └── images-resized/ ── Resized (64×64)       │
│      └── archive/ ── Archived/unused assets           │
│          ├── sounds/                                  │
│          └── images/                                  │
└──────────────┬───────────┬────────────────────────────┘
               │           │
       MQTT publish    Pixoo HTTP (optional)
               │
               ▼
┌─────────────────────────────┐
│  pixdcon              │
│  scenes/pixoo/funkeykid.js  │
│  Renders on Pixoo64 display │
│  (192.168.1.189)            │
└─────────────────────────────┘
```

### Data Flow: Keypress → Sound + Display

```
Physical keyboard (ACME BK03, Bluetooth)
    → /dev/input/eventN (evdev)
    → keyboard.py: scancode → layout map (QWERTZ Y↔Z) → letter
    → server.py handle_key():
        1. SSE broadcast "rawkey" (ALL keys, even unmapped)
        2. SSE broadcast "keypress" (letter + word + sound + image)
        3. play_sound() → paplay via kiosk PipeWire → speakers
        4. display.publish_letter() → MQTT → pixdcon → Pixoo64
    → Web UI updates instantly via SSE EventSource
```

---

## Host Setup (hsb1)

### Docker Compose

```yaml
funkeykid:
  image: ghcr.io/markus-barta/funkeykid:latest
  container_name: funkeykid
  network_mode: host
  restart: unless-stopped
  privileged: true           # Required for /dev/input access
  environment:
    - TZ=Europe/Vienna
    - XDG_RUNTIME_DIR=/run/user/1001      # kiosk user
    - PULSE_SERVER=unix:/run/user/1001/pulse/native
    - DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket
  env_file:
    - /home/mba/secrets/smarthome.env     # MQTT credentials
    - /home/mba/secrets/funkeykid-api.env # ELEVENLABS_API_KEY + OPENROUTER_API_KEY
  volumes:
    - ./mounts/funkeykid/settings.json:/data/settings.json
    - ./mounts/funkeykid/sounds:/data/sounds
    - ./mounts/funkeykid/images:/data/images
    - ./mounts/funkeykid/ai-generated:/data/ai-generated
    - /dev/input:/dev/input
    - /run/user/1001/pulse:/run/user/1001/pulse
    - /run/user/1001/pipewire-0:/run/user/1001/pipewire-0
    - /run/dbus:/run/dbus
```

### NixOS Module (`modules/funkeykid.nix`)

Three independent toggles (all default true except `enable`):

| Option | Default | Purpose |
|--------|---------|---------|
| `enable` | `false` | Systemd Python service — OFF when using Docker |
| `hardwareIsolation` | `true` | udev rules: strip ACME BK03 from logind/X11 |
| `bluetoothReconnect` | `true` | Auto-connect ACME BK03 on boot (5 retries) |

**CRITICAL**: `hardwareIsolation` MUST stay `true` even with Docker. Without it:
- Child's keypresses type into host terminals
- Power/suspend keys shut down the system

### File Locations on hsb1

| Path | Purpose | Persistent |
|------|---------|-----------|
| `~/docker/mounts/funkeykid/settings.json` | All config, sets, letter entries | ✅ |
| `~/docker/mounts/funkeykid/sounds/` | Active sound files (.mp3) | ✅ |
| `~/docker/mounts/funkeykid/images/` | Active image files (.png, 64×64) | ✅ |
| `~/docker/mounts/funkeykid/ai-generated/sounds/` | AI-generated sounds (preserved) | ✅ |
| `~/docker/mounts/funkeykid/ai-generated/images-original/` | Full-res AI images (512×512) | ✅ |
| `~/docker/mounts/funkeykid/ai-generated/images-resized/` | Resized AI images (64×64) | ✅ |
| `~/docker/mounts/funkeykid/archive/sounds/` | Archived/unused sounds | ✅ |
| `~/docker/mounts/funkeykid/archive/images/` | Archived/unused images | ✅ |
| `~/docker/mounts/funkeykid/backup-v1.0/` | v1.0 backup (sounds + images + settings) | ✅ |
| `~/secrets/funkeykid-api.env` | API keys (ElevenLabs + OpenRouter) | ✅ |

---

## Audio Routing (CRITICAL)

```
kiosk user (uid 1001) owns PipeWire with real audio hardware:
  → alsa_output.pci-0000_00_1b.0.analog-stereo (speakers/HDMI)

mba user (uid 1000) has PipeWire with only a null/dummy sink:
  → auto_null (NO audio output!)

babycam (VLC) runs as kiosk → kiosk PipeWire → speakers
funkeykid (Docker) → mounts kiosk pulse socket → same PipeWire → speakers
Both audio streams mix via PipeWire.
```

**Rules:**
- Mount `/run/user/1001/pulse` (kiosk), NOT `/run/user/1000/pulse` (mba)
- Set `XDG_RUNTIME_DIR=/run/user/1001` and `PULSE_SERVER=unix:/run/user/1001/pulse/native`
- paplay exits 0 on null sink but produces no audible output — always verify with real sink
- DO NOT break kiosk's PipeWire session — babycam depends on it

---

## Keyboard (ACME BK03 Bluetooth)

### QWERTZ Layout

evdev reports **US QWERTY scancodes**. keyboard.py applies layout map:

```python
LAYOUTS = {
    "de": {
        "Y": "Z",  # evdev KEY_Y (US top-row) = German Z (next to T)
        "Z": "Y",  # evdev KEY_Z (US bottom-row) = German Y
        "RIGHTBRACE": "EQUAL",  # + key
        "SLASH": "MINUS",       # - key
    },
}
```

### BT Connection

1. NixOS `acme-bk03-reconnect.service` → 5× connect on boot
2. Keyboard → `/dev/input/eventN` when connected
3. Container needs `/dev/input` bind mount
4. **Hot-plug issue**: New devices after container start not visible → use "Neu verbinden" button or restart container
5. MAC address: `20:73:00:04:21:4F`
6. Battery read from sysfs (if available)

### Startup Grace Period

3 seconds after container start, all keypresses are ignored. Prevents stale BT events from triggering sounds during container restart/weekly update.

---

## Sound Generation (ElevenLabs)

### API

```
POST https://api.elevenlabs.io/v1/sound-generation
Header: xi-api-key: $ELEVENLABS_API_KEY
Body: {"text": "prompt", "duration_seconds": 3-6, "prompt_influence": 0.6}
Response: Binary MP3 audio (application/octet-stream)
```

### Cost

| Method | Credits |
|--------|---------|
| Auto duration | 100 credits/generation |
| Manual duration | 20 credits/second |

### Prompt Tips

- Be specific: "happy puppy barking and tail wagging" > "dog sound"
- Include action: "guitar strumming four cheerful chords" > "guitar"
- For longer sounds, specify duration: `duration_seconds: 5-6`
- Use `prompt_influence: 0.6-0.8` for consistent results

### File Naming

`{letter}_{word}.mp3` — lowercase, umlauts normalized: ä→ae, ö→oe, ü→ue

### Storage

1. Generated → `/data/ai-generated/sounds/{filename}` (preserved forever)
2. Copied → `/data/sounds/{filename}` (active, used by player)
3. Archived → `/data/archive/sounds/{filename}` (via archive button)

---

## Image Generation (OpenRouter + Gemini)

### API

```
POST https://openrouter.ai/api/v1/chat/completions
Header: Authorization: Bearer $OPENROUTER_API_KEY
Body: {
  "model": "google/gemini-2.5-flash-image",
  "messages": [{"role": "user", "content": "Generate a 512x512 image: ..."}]
}
Response: JSON with choices[0].message.images[0].image_url.url (base64 data URI)
```

### Image Extraction

```python
images = response["choices"][0]["message"]["images"]
b64_data = images[0]["image_url"]["url"].split(",", 1)[1]
raw_png = base64.b64decode(b64_data)
```

### Style Prompt Template

```
Pixar-style 3D cartoon of {description}. Vibrant saturated colors,
soft lighting, rounded friendly shapes, big expressive eyes.
Simple composition, recognizable at 64x64 pixels.
Studio quality children's animation style.
```

### File Naming

`{letter}_{word}.png` — same convention as sounds

### Storage & Resize

1. Generated (512×512) → `/data/ai-generated/images-original/{filename}` (preserved forever)
2. Resized (64×64) → `/data/ai-generated/images-resized/{filename}`
3. Copied (64×64) → `/data/images/{filename}` (active, used by display)
4. Resize: Pillow `Image.resize((64, 64), Image.LANCZOS)` in container
5. macOS resize: `sips -z 64 64 file.png`

### Original Images

96 original full-resolution images from v2.0 are preserved at:
`~/docker/mounts/funkeykid/ai-generated/images-original/`

These are the "master" copies. Never delete this directory.

---

## AI Word Suggestion

### API

```
POST /api/suggest-word
Body: {"letter": "B"}
Response: {
  "word": "Boot",
  "sound_description": "Splashing water sound",
  "image_description": "A small wooden boat floating on a lake"
}
```

Uses OpenRouter (gpt-4.1-nano) with a German-language prompt that:
- Targets ages 2-5
- Requires concrete objects/animals (not abstract concepts)
- Requires identifiable sounds
- Excludes already-used words in all sets
- Returns structured JSON with sound/image descriptions for downstream generation

---

## Archive System

### How It Works

1. User clicks 📦 on an entry in the edit modal
2. Sound file moved: `/data/sounds/{file}` → `/data/archive/sounds/{file}`
3. Image file moved: `/data/images/{file}` → `/data/archive/images/{file}`
4. Entry removed from the letter's entries list
5. Files are preserved forever in archive (never auto-deleted)

### API

```
POST /api/archive    Body: {"type": "sound", "filename": "x.mp3"}
GET  /api/archive    Returns: {"sounds": [...], "images": [...]}
```

---

## Web UI

**URL**: http://hsb1.lan:8081

### Tabs

| Tab | Purpose |
|-----|---------|
| Buchstaben | 26 letter cards, click to edit entries |
| Sets | Create, duplicate, rename, delete, activate sets |
| Tastatur | Virtual QWERTZ keyboard for testing |
| Einstellungen | Layout, volume, display mode, random sounds |
| Dateien | Upload/preview sounds and images |

### Real-Time Updates (SSE)

The UI uses Server-Sent Events (`/api/events`) for instant updates:

| Event | Data | Trigger |
|-------|------|---------|
| `rawkey` | `{key, raw, timestamp}` | Every keypress (even unmapped) |
| `keypress` | `{letter, word, sound, image}` | Mapped letter press |
| `connection` | `{connected, device_path, battery}` | Keyboard connect/disconnect |
| `volume` | `{volume}` | Volume change via +/- keys |
| `status` | Full status object | Initial connection |

Fallback: 10s polling for robustness.

### Letter Edit Modal

Each entry has:
- Toggle (enable/disable)
- Word input
- Sound picker (searchable, type-to-filter)
- Image picker (searchable, thumbnail grid)
- 🔊 Sound generieren → ElevenLabs API
- 🖼️ Bild generieren → OpenRouter/Gemini API
- ⚙️ Custom AI instructions field
- 📦 Archive button (moves files to archive)
- ✕ Delete button

**✨ KI-Vorschlag**: AI suggests a fitting word + auto-generates sound + image

### Settings Auto-Save

All settings changes are auto-saved with 300ms debounce. No manual save button needed. Toast notification "Gespeichert ✓" confirms each save.

Volume changes from the physical keyboard (+/- keys) are also persisted to settings.json.

### Footer

Shows version, build SHA (first 7 chars), and GitHub repo link.

---

## Deployment

### Quick Hotfix (no CI)

```bash
# Python files (requires restart)
scp server.py mba@hsb1.lan:/tmp/fk.py
ssh mba@hsb1.lan "docker cp /tmp/fk.py funkeykid:/app/server.py && docker restart funkeykid"

# Static files (no restart — aiohttp serves from disk)
scp static/index.html mba@hsb1.lan:/tmp/fk.html
ssh mba@hsb1.lan "docker cp /tmp/fk.html funkeykid:/app/static/index.html"
```

**Important**: Hotfixed files lost on `docker compose up --force-recreate`. Always commit + push.

### Full CI Deployment

```bash
git add . && git commit -m "..." && git push
gh run watch --exit-status  # Wait for GitHub Actions
ssh mba@hsb1.lan "cd ~/docker && docker compose pull funkeykid && docker compose up -d funkeykid"
```

### After Container Recreate

The Dockerfile includes bluez + Pillow. After image pull, these are baked in. Manual `apt-get install` only needed when hotfixing the running container (which uses the old image).

### pixdcon Scene

The funkeykid scene (`scenes/pixoo/funkeykid.js`) in pixdcon:
- Subscribes to `home/hsb1/funkeykid/display` MQTT
- Loads images from `/app/assets/pixoo/funkeykid/` (host-mounted)
- Renders: bg image + letter (top-left, shadow) + word (bottom-center, shadow)
- Idle: shows last bg image after 10s
- Hot-reloads on file change (ScenesWatcher)

To update images for pixdcon, copy to both:
```bash
# Active images (funkeykid reads these)
~/docker/mounts/funkeykid/images/
# pixdcon assets (scene reads these)
~/docker/mounts/pixdcon/assets/pixoo/funkeykid/
```

---

## Favorites (v2.2+)

- **Tab key**: Toggles favorite on the currently active letter+entry
- **Number keys 1-0**: Play favorite #1-#10 directly
- Up to 10 favorites, persisted in `settings.json`
- Favorites display in web UI (SSE `favorites` event)

---

## Position Memory (v2.2+)

Switching letters does NOT reset the cycle position. Example:
- Press W → Wasser (idx 0)
- Press W → Wal (idx 1)
- Press A → Affe
- Press W → Wind (idx 2, continues from where you left off)

Implemented via `cycle_index = {}` dict — never reset on letter change.

---

## Blacklist (v2.3+)

Rejected AI word suggestions are blacklisted per letter:
- Persisted in `settings.json` under `blacklist: {U: ["Uhu", "Unke"]}`
- Excluded words sent in AI prompt as system message with explicit "NICHT X" lines
- **Server-side validation**: Even if the AI returns a blacklisted word, the server rejects it and auto-retries (up to 3 attempts, increasing temperature)
- Editable in the letter edit modal (collapsed at bottom)

---

## KI-Log (v2.3.5+)

Debug tab showing all AI requests and responses:
- Circular buffer of last 50 entries
- Real-time updates via SSE `ai_log` event
- Each entry: action, model, prompt (truncated), response, status badge
- Expandable details per entry

---

## Configurable AI Models & Prompts (v2.3.4+)

Under Settings → KI-Generierung:
- **Sound-Prompt**: Template with `{word}` placeholder
- **Bild-Prompt**: Template with `{description}` placeholder
- **Wort-Vorschlag-Prompt**: Template with `{letter}` and `{excluded}` placeholders
- **Vorschlag-Modell**: Dropdown with free/cheap/frontier models (Nemotron, GPT-4.1/5, Claude, Gemini)

---

## Async AI Generation (v2.2+)

Sound and image generation runs in background threads:
- API returns job ID immediately
- SSE `gen_update` events notify UI on status changes (queued → generating → done/error)
- Generation jobs panel in header shows active/failed jobs
- File lists auto-refresh on completion

---

## Known Gotchas

1. **Audio null sink**: mba (uid 1000) PipeWire has null sink. ALWAYS use kiosk (uid 1001)
2. **Docker /dev/input**: BT devices connecting after container start need restart or "Neu verbinden"
3. **pixdcon image cache**: `loadPixooImage` caches forever — restart pixdcon for new images
4. **Umlaut filenames**: NEVER use ä/ö/ü in filenames → use ae/oe/ue
5. **Hotfix volatility**: `docker cp` changes lost on image pull — always commit+push
6. **Startup sounds**: 3s grace period prevents stale BT events at container restart
7. **Small AI models**: Ignore exclusion lists — use system message + server-side validation + auto-retry

---

## API Reference

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/api/version` | Version + build + repo |
| GET | `/api/events` | SSE stream |
| GET | `/api/status` | Keyboard + system status |
| GET | `/api/diagnostics` | BT + device diagnostics |
| POST | `/api/reconnect` | Restart keyboard listener |
| GET/PUT | `/api/settings` | Global settings |
| GET/POST | `/api/sets` | List/create sets |
| GET/PUT/DELETE | `/api/sets/{id}` | Set CRUD |
| POST | `/api/sets/{id}/activate` | Switch active set |
| GET/PUT | `/api/sets/{id}/letters/{L}` | Letter entries |
| POST | `/api/sets/{id}/letters/{L}/entries` | Add entry |
| DELETE | `/api/sets/{id}/letters/{L}/entries/{i}` | Delete entry |
| POST | `/api/test/{letter}` | Simulate keypress |
| POST | `/api/suggest-word` | AI word suggestion |
| POST | `/api/generate/sound` | Generate sound (ElevenLabs) |
| POST | `/api/generate/image` | Generate image (Gemini) |
| POST | `/api/archive` | Archive a file |
| GET | `/api/archive` | List archived files |
| GET/PUT | `/api/blacklist/{letter}` | Get/set blacklisted words |
| GET | `/api/jobs` | List generation jobs |
| GET | `/api/ai-log` | AI request/response log (last 50) |
| GET | `/api/favorites` | List favorites |
| GET/POST | `/api/sounds` | List/upload sounds |
| GET/POST | `/api/images` | List/upload images |
| GET | `/ai/{path}` | Serve ai-generated files |
| GET | `/sounds/{file}` | Serve sound file |
| GET | `/images/{file}` | Serve image file |

---

## Settings Format

```json
{
  "keyboard_layout": "de",
  "keyboard_device": "ACME BK03",
  "volume": 100,
  "random_sounds_enabled": false,
  "display_mode": "mqtt",
  "pixoo_ip": "192.168.1.189",
  "debounce_seconds": 0.8,
  "mqtt": { "host": "localhost", "port": 1883, "user": "smarthome" },
  "active_set": "v2-komplett",
  "sets": {
    "v2-komplett": {
      "name": "v2.0 Komplett",
      "description": "96 Einträge, 3-5 Wörter pro Buchstabe",
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

### Key Concepts

- **Sets**: Named collections, switchable from navbar dropdown
- **Entries**: Per-letter, each with own word + sound + image + enabled flag
- **Cycling**: Same key pressed consecutively cycles through enabled entries
- **Auto-save**: Every change persisted immediately
- **Migration**: Old flat format auto-migrated to sets format on load
