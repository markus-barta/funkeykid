# funkeykid — Development Guide

**Version**: INSPR-CalVer3 (`version.json`; see the UI footer or `/api/version`, and DEPLOY.md § Versioning)
**Repo**: https://github.com/markus-barta/funkeykid
**Host**: hsb1 (Home Server Barta 1)
**Image**: ghcr.io/markus-barta/funkeykid:latest
**Task tracking**: [PPM](https://pm.barta.cm) — project `FUNK` (id 5). Issue keys like `FUNK-1` / `FUNK-2` referenced below resolve there. No `+pm/backlog/` in this repo.
**Related repos**: [pixdcon](https://github.com/markus-barta/pixdcon), the Pixoo scene renderer (separate codebase; funkeykid publishes MQTT into its funkeykid scene). nixcfg holds hsb1's host configuration: how the container runs.

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
        3. play_sound() → pactl set-sink-volume 100% → paplay via kiosk PipeWire → speakers
        4. display.publish_letter() → MQTT → pixdcon → Pixoo64
    → Web UI updates instantly via SSE EventSource

Keyboard connect/disconnect:
    → keyboard.py detects via evdev
    → display.publish_keyboard_status() → MQTT (retained) → pixdcon home scene
    → home.js: 3 green dots (connected) or 2 gray dots (disconnected) in header
```

---

## Host Setup (hsb1)

### Container and host configuration (nixcfg)

hsb1 runs the container from the nixcfg compose spec, `hosts/hsb1/docker/compose-spec.nix` (service `funkeykid`), plus the `spec` overlay in `hosts/hsb1/configuration.nix`. Together they are the source of truth:
- the `funkeykid` service: image, env, agenix env files, mounts;
- the configuration.nix overlay: the `FUNKEYKID_KEYBOARD` pin and the CA bundle.

The spec is rendered to `/etc/compose/hsb1/docker-compose.yml` and reconciled by `compose-hsb1.service`. There is no hand-edited compose file. DEPLOY.md § Production Setup has the summary, file paths and the locked roll. Change the host side only through nixcfg, with an OPS ticket.

### NixOS Module (`modules/funkeykid.nix` in nixcfg)

The options are `enable` (off: hsb1 uses Docker), `keyboardName`, `hardwareIsolation` and `bluetoothReconnect`. See DEPLOY.md § NixOS Module.

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
| `/run/agenix/hsb1-funkeykid-api-env` | API keys (ElevenLabs + OpenRouter), agenix from nixcfg `secrets/` | ✅ (in nixcfg) |
| `/run/agenix/hsb1-smarthome-env` | MQTT credentials, agenix from nixcfg `secrets/` | ✅ (in nixcfg) |

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
- Mount the kiosk runtime dir `/run/user/1001` (the whole dir, not just `pulse/`; see FAQ "No sound after reboot"), NOT `/run/user/1000` (mba)
- Set `XDG_RUNTIME_DIR=/run/user/1001` and `PULSE_SERVER=unix:/run/user/1001/pulse/native`
- paplay exits 0 on null sink but produces no audible output — always verify with real sink
- DO NOT break kiosk's PipeWire session — babycam depends on it

**Sink volume pinning (v2.5+):** Every `play_sound()` call runs
`pactl set-sink-volume @DEFAULT_SINK@ 100%` before `paplay`. This prevents
PipeWire sink drift (observed at 41%) from making sounds quiet. Safe because
babycam volume is controlled via VLC's internal telnet interface, not the sink.

---

## Keyboard (configurable, default ACME BK03 Bluetooth)

### Exclusive grab (v3.2.0, FUNK-59)

funkeykid owns the kids' keyboard **exclusively**: it opens every evdev node
of the configured device and takes an `EVIOCGRAB` on each one. A Bluetooth
keyboard usually registers several nodes for one device, e.g. `ACME BK03`,
`ACME BK03 Consumer Control` and `ACME BK03 System Control`, and the last one
carries the sleep/power keys. While grabbed, no key event reaches anything
else on the host: not X11/libinput (the kiosk session, VLC babycam hotkeys),
not logind (sleep/power), not the in-kernel console or SysRq.

- Nodes match by name: exactly the configured name, or that name plus a space
  and a suffix. Nodes of the same physical device (same Bluetooth address, or
  same USB parent) are included whatever their name.
- Always on, by design; there is no setting to turn it off. For emergency host
  input, plug in a USB keyboard (hsb1 has one attached).
- A (re)connected keyboard is picked up and grabbed within about 1 s (the old
  code polled every 5 s).
- If a grab fails (another process holds the node), the listener keeps
  working, logs a WARNING, reports `exclusive: false` (UI: red "Nicht
  exklusiv!" badge; Pixoo: warning state) and retries every 5 s.
- The host-side udev isolation in nixcfg (`modules/funkeykid.nix`) stays as
  the second layer for the time funkeykid is not running. It must name the
  same device.

### Choosing the keyboard

Precedence: env `FUNKEYKID_KEYBOARD` > `settings.keyboard_device` > `"ACME BK03"`.

- When the host sets `FUNKEYKID_KEYBOARD`, the Settings field is locked ("vom
  Host festgelegt"): the device and its host isolation are changed together
  in nixcfg.
- Otherwise the Settings field offers the connected input devices (from the
  diagnostics scan). A change hot-swaps the listener, and the old device's
  grabs are released first. No container restart is needed.

### Keyboard language (FUNK-64)

evdev reports **US QWERTY scancodes**. The keyboard language (`settings.language`: `de` or `us`) picks the map in `keyboard.py` from the code a key sends to the key funkeykid treats it as:

```python
KEYPAD = {"KP0": "0", …, "KP9": "9", "KPENTER": "ENTER"}   # both languages: pad digits play numbers
LAYOUTS = {
    "us": dict(KEYPAD),
    "de": {
        **KEYPAD,
        "Y": "Z",  # evdev KEY_Y (US top-row) = German Z (next to T)
        "Z": "Y",  # evdev KEY_Z (US bottom-row) = German Y
        "RIGHTBRACE": "EQUAL",  # + key
        "SLASH": "MINUS",       # - key
        "LEFTBRACE": "Ü", "SEMICOLON": "Ö", "APOSTROPHE": "Ä",   # umlauts are letters
    },
}
```

### BT Connection

1. NixOS `acme-bk03-reconnect.service` → 5× connect on boot
2. Keyboard → `/dev/input/eventN` when connected
3. Container needs the `/dev/input` **bind mount**, which makes nodes created after container start visible. The listener rescans every second; "Neu verbinden" restarts it.
4. The Bluetooth address is never hardcoded: diagnostics take it from the connected device (evdev `uniq`) or from `bluetoothctl devices` by name. MAC addresses belong only in encrypted `.age` files.
5. Battery: the kernel HID battery (`/sys/class/power_supply/hid-<addr>-battery`) first, then BlueZ `Battery Percentage` from `bluetoothctl info`. It is refreshed on connect and every 60 s. `null` when the keyboard reports nothing.
6. Low-battery warning: `settings.battery_low_threshold` (percent, default 20, 0 = off), set in the Settings tab. Rechargeable NiMH cells (1.2 V) report low levels early, so lower the threshold for them. Drives the UI badge and `battery_low` on MQTT.

### Startup Grace Period

3 seconds after container start, all keypresses are ignored. Prevents stale BT events from triggering sounds during container restart/weekly update.

### Key map (FUNK-62)

Every key of the kids' keyboard can do something. The pure logic is in `keymap.py`, with tests in `tests/test_keymap.py`. The UI is the Tastatur tab, section "Tastenbelegung".

- **Key id** = the code the keyboard sends: the evdev name without `KEY_`, such as `CUT`, `A`, or `Y` (the German Z). Ctrl, Alt and Meta held at the time are prefixed in a fixed order, as in `CTRL+X`. Shift is not part of the id: Shift + digit stays the favourite shortcut. Ids never depend on the language layout. `keyboard.py` tracks the held modifiers per input node and passes them to `handle_key(key, raw, mods)`.
- **Storage:** `settings["keymaps"][<profile>][<key id>] = action`, one map per keyboard profile (see below). `PUT /api/settings` never touches `keymaps`, `keyboard_profile` or `keyboard_profiles`.
- **Resolution:** an entry for the id with modifiers wins; then an entry for the plain key; otherwise the built-in behaviour, unchanged. Built-in: letters, digits, Space = Stopp, + and − = volume, Enter = play again, Tab = favourite, arrows = navigation. Any other key plays a random sound only if "Zufällige Sounds" is on.
- **Actions:**
  - `letter` (the content of a letter or digit);
  - `sound` (a file from `sounds/` and a volume);
  - `imgsound` (plus an image from `images/` and a word, which go to the Pixoo as `publish_letter("", word, image)`);
  - `func` (`keymap.FUNCTIONS`: volume ±, mute, stop, replay, favourite, next set, random sound, navigation);
  - `none` (silent).
- **Learn mode:** every press sends the `rawkey` SSE event with `id`, `matched`, `action`, `default` and `unassigned`. In learn mode the UI selects the pressed key.
- **Unassigned presses:** a press with neither an entry nor a built-in role is remembered in memory. It appears in "Gedrückt, aber ohne Wirkung" and in `/api/diagnostics` as `keymap_unassigned_seen`.
- **Virtual press:** `POST /api/press/{id}` presses a key by id, exactly like the keyboard. `?dry=1` only resolves it, so you can check a mapping without a sound. The virtual test keyboard (`/api/test/{letter}`) goes through the same path, via the inverse layout.
- **Shift** cannot be mapped (`LEFTSHIFT`, `RIGHTSHIFT`): Shift + digit is the favourite shortcut.

### Keyboard profiles (FUNK-63)

A profile describes what a keyboard looks like and what each key sends. The UI draws the keyboard from it, so a custom keyboard is a key list, not a picture. The pure logic is in `profiles.py`, with tests in `tests/test_profiles.py`.

- **Data:** rows of keys in key units (1 = one letter key). A key has a `slot` (its stable name in the profile), a `code` (the key id it sends, `""` = nothing reaches funkeykid, like fn), a width, legends (`label` in the middle, `shift` top left, `altgr` bottom left, `fn` in red), an optional `icon`, `guess` for a code that is assumed until a real press confirms it, and an optional `suggest` (a function, letter or "nothing" the UI offers). Rows can hold gaps and stacks (two half-height keys, like the BK03's ↑/↓). `profiles.layout()` turns this into absolute `x, y, w, h`.
- **Built-in:** `acme-bk03` (default, drawn from a photo: media keys on the top row with F1–F12 on fn, OS-mode switching on fn+Q/W/E; the top-row codes are guesses), `pc-de` (ISO, no number pad) and `mac-de`. They are read-only.
- **Custom:** a duplicate of any profile, stored validated in `settings["keyboard_profiles"][custom-<slug>]`; it starts with a copy of the source's key map. Its keys' legends, code, icon and width can be edited, including "take the code of the last pressed key" for a key the drawing gets wrong. Deleting a custom profile deletes its key map; if it was active, `acme-bk03` becomes active again.
- **Active profile:** `settings["keyboard_profile"]`, switched with `POST /api/keyboards/{id}/activate` (SSE `keyboard_profile`). An unknown or deleted id falls back to `acme-bk03`. The active profile's key map is what the real keyboard uses.
- **UI:** Tastatur tab. The drawing marks each key by what it does (letter content, sound, image + sound, function, nothing, or reserved), a press on the real keyboard lights its key up, and learn mode selects it. `/api/keymap` adds `defaults` (built-in label and kind per code of the active profile) and `seen_codes` (ids pressed since start, which confirm guessed codes).
- **"Standard"** (FUNK-64): `keyboard_profile = "default"` follows the language × PC/Mac (`keyboard_family`) × number pad (`keyboard_numpad`) and resolves to one of the generic built-ins `pc-de`, `pc-de-num`, `mac-de`, `mac-de-num`, `pc-us`, `pc-us-num`, `mac-us` or `mac-us-num`. Its key map is that profile's map. Changing the language can therefore switch the active profile, and the server announces that (SSE `keyboard_profile`).
- **Export / import** (FUNK-64): `GET /api/keyboards/{id}/export` downloads `{"format": "funkeykid.keyboard.v1", "profile", "keymap"}`, for built-ins too. `POST /api/keyboards/import` with `{"bundle", "name"?}` creates a new custom profile with that key map and never activates it. Everything is validated like a UI edit. Entries whose sound or image is missing on this machine are skipped and listed in `skipped`.
- **Not yet:** a phone or tablet keyboard (FUNK-65).

### Language migration (FUNK-64)

`settings.language` replaces `settings.keyboard_layout`. A missing `language` is taken from `keyboard_layout` (`profiles.language_of`), so an existing `de` or `us` setup keeps its letter mapping. Every key that had a role before keeps it. Only keys that had no built-in role gain one: the umlaut keys (and only if the set has content for them), the pad digits and pad Enter. `PUT /api/language` writes `keyboard_layout` in step, so a rollback to an older image still finds the same layout. `PUT /api/settings` never writes `keyboard_layout` any more.

Letters: for `de` the Letters tab also offers Ä, Ö and Ü.

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
| Einstellungen | Keyboard language, volume, display mode, random sounds |
| Dateien | Upload/preview sounds and images |

### Real-Time Updates (SSE)

The UI uses Server-Sent Events (`/api/events`) for instant updates:

| Event | Data | Trigger |
|-------|------|---------|
| `rawkey` | `{key, raw, timestamp}` | Every keypress (even unmapped) |
| `keypress` | `{letter, word, sound, image}` | Mapped letter press |
| `connection` | Full keyboard status (see `/api/status`) | Keyboard connect/disconnect (toast) |
| `volume` | `{volume}` | Volume change via +/- keys |
| `status` | Full status object | Initial connection; exclusive/grab or battery changes |

`/api/status` keyboard fields: `connected`, `exclusive`, `grabbed`, `nodes[{path,name,grabbed}]`,
`device_name`, `device_path`, `battery_level`, `battery_low`, `battery_low_threshold`,
`keyboard_env_pinned`, `searching`, `last_key*`.

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

Shows the version, build SHA (first 7 chars) and GitHub repo link.

- **Version display:** INSPR-CalVer3 (FUNK-60). One thin ES-module adapter at the end of `index.html` calls the shared `renderVersion` and `attachVersionInteraction` from `static/vendor/inspr-versioning/`. Pretty is the default, with SemVer selectable. The brand colour is resolved from the theme's primary colour.
- **Interaction:** the tooltip and accessible name carry the exact canonical version and the UTC time. A click or tap copies the canonical version (no `v`, `.0.0` kept).
- **Legacy and unknown schemes** render plain.
- **No runtime fetch:** the bundle is never fetched from inspr.at at runtime. Its bytes are pinned in `versioning_bundle.py` and verified by pytest and the Docker build.
- **`/api/version`** returns `version`, `version_scheme`, `release_channel`, `release_sequence`, `build`, `repo` and `build_time`.

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

**Important**: Hotfixed files are lost at the next recreate: a roll, a nixcfg switch that changes the spec, or the Saturday `compose-hsb1-update.timer`. Always commit, push and release.

### Full CI Deployment (release → locked roll)

A push to `main` only publishes `:<commit-sha>`. hsb1 runs `:latest`, which moves only with a release:
1. Run `scripts/reserve_version.py` and merge through a PR.
2. Tag `v<version>`. CI publishes `:<version>`, the release manifest, then `:latest`.
3. Roll only funkeykid on hsb1, under the stack lock, with a dry run first.

The exact commands are in DEPLOY.md § Versioning and § "Normal: Release → CI → locked roll on hsb1". Never run compose from `~/docker`; hsb1's compose file is rendered from nixcfg.

### After Container Recreate

The Dockerfile includes bluez + Pillow. After image pull, these are baked in. Manual `apt-get install` only needed when hotfixing the running container (which uses the old image).

### pixdcon Scenes

These scenes belong to the pixdcon repository; change them through a PIXD ticket. `pixdcon2-edge` (pixdcon v2) drives the displays and runs them from the shared scene mount; v1 `pixdcon` is the fallback.

**funkeykid scene** (`scenes/pixoo/funkeykid.js`) on pixoo-189:
- Subscribes to `home/hsb1/funkeykid/display` MQTT, and to the retained `home/hsb1/funkeykid/keyboard-info` for the start screen (PIXD-122)
- Loads images from `/app/assets/pixoo/funkeykid/`. That is funkeykid's `~/docker/mounts/funkeykid/images`, mounted read-only into both pixdcon containers by the nixcfg compose spec.
- Renders: bg image + letter (top-left, big font) + word (bottom-center on 50% darkened strip)
- Bottom 8px of bg image darkened by halving RGB values → readable text on any background
- Idle: shows last bg image after 10s
- Hot-reloads on file change (ScenesWatcher)

**home scene** (`scenes/pixoo/home.js`) on pixoo-159:
- Subscribes to `home/hsb1/funkeykid/keyboard-info` (retained)
- Header indicator at x=30,33,36 y=3: 3 green dots (connected), 2 gray dots (disconnected)

To update images, put them only into `~/docker/mounts/funkeykid/images/`; pixdcon sees the same directory through its mount. Then restart the instance that drives pixoo-189 (`docker restart pixdcon2-edge`), because pixdcon caches images per process.

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

## Arrow Key Navigation (v2.6+)

Four arrow keys provide sequential navigation through all sounds:

| Key | Action | Wraps? |
|-----|--------|--------|
| RIGHT | Next sound in flat playlist (A1→A2→...→A4→B1→B2→...) | Yes, Z last → A first |
| LEFT | Previous sound in flat playlist | Yes, A first → Z last |
| DOWN | Jump to next letter's first entry (A→B→C→...→Z→A) | Yes |
| UP | Jump to previous letter's first entry (Z→Y→...→A→Z) | Yes |

- Pressing a letter key syncs the flat playlist position
- Arrow keys bypass debounce for responsive rapid navigation
- All four directions wrap around — a child can hold RIGHT forever

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

## MQTT Topics

All topics use prefix from `settings.mqtt.topic_prefix` (default `home/hsb1/funkeykid`).

| Topic | Direction | Retained | Payload |
|-------|-----------|----------|---------|
| `…/display` | publish | no | `{letter, word, image, color, timestamp}` |
| `…/keyboard-info` | publish | **yes** (qos 1) | `{online, connected, exclusive, battery_level, battery_low, device_name, heartbeat_s, timestamp}` |
| `…/debug` | publish | no | `{timestamp, level, message}` |

**Consumers:**
- `…/display` → pixdcon `funkeykid.js` scene (Pixoo64 letter rendering)
- `…/keyboard-info` → pixdcon `home.js` scene (keyboard status dots in header) and the funkeykid scene's start screen (FUNK-58 contract, rendered since PIXD-122)

**keyboard-info contract (v3.2.0)**, also in the `display.py` docstring:
- Re-published on every change and as a **heartbeat every 10 s** (`heartbeat_s`). Treat 3 missed beats as offline.
- **MQTT last-will**: if funkeykid dies or loses the network, the broker replaces the retained message with `online: false` and everything else false/null. A clean stop publishes the same. The next (re)connect publishes `online: true` again.
- `exclusive: false` while `connected: true` means a grab failed and keys can reach the host: show it as a warning.
- `battery_low` is computed by funkeykid from `settings.battery_low_threshold`, so consumers don't need a threshold of their own.

---

## Known Gotchas

1. **Audio null sink**: mba (uid 1000) PipeWire has null sink. ALWAYS use kiosk (uid 1001)
2. **Docker /dev/input**: keep it a bind mount (`/dev/input:/dev/input`), which makes BT devices connecting after container start visible within ~1 s. "Neu verbinden" restarts the listener.
3. **pixdcon image cache**: `loadPixooImage` caches per process. Restart the driving instance (`docker restart pixdcon2-edge`) for new images
4. **Umlaut filenames**: NEVER use ä/ö/ü in filenames → use ae/oe/ue
5. **Hotfix volatility**: `docker cp` changes are lost at the next recreate. Always commit, push and release
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
| GET | `/api/keymap` | Key map of the active profile + functions + unassigned presses (FUNK-62) |
| PUT/DELETE | `/api/keymap/{id}` | Assign an action to a key id / back to built-in |
| POST | `/api/press/{id}` | Press a key by id like the keyboard (`?dry=1`: resolve only) |
| GET | `/api/keyboards` | Keyboard profiles (summary + silhouette) and the active one (FUNK-63) |
| GET | `/api/keyboards/{id}` | One profile with the absolute key layout |
| POST | `/api/keyboards/{id}/activate` | Make a profile active (its key map drives the keyboard) |
| POST | `/api/keyboards/{id}/duplicate` | Custom copy of a profile and its key map (`{"name"}` optional; `"activate_if_active": true` makes the copy active if the source still is) |
| PUT/DELETE | `/api/keyboards/{id}` | Rename / delete a custom profile |
| PUT | `/api/keyboards/{id}/keys/{slot}` | Edit a custom key: `label`, `name`, `shift`, `altgr`, `fn`, `icon`, `code`, `w` |
| GET | `/api/keyboards/{id}/export` | Profile + key map as a JSON file (FUNK-64) |
| POST | `/api/keyboards/import` | `{"bundle", "name"?}`: new custom profile from an export, never activated |
| GET/PUT | `/api/language` | Language (`de`, `us`), `family` (`pc`, `mac`), `numpad`; what "Standard" means |
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
  "language": "de",
  "keyboard_layout": "de",
  "keyboard_family": "pc",
  "keyboard_numpad": false,
  "keyboard_profile": "acme-bk03",
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
- **Entries**: Per-letter, each with own word + image + tracks + enabled flag
- **Cycling**: Same key pressed consecutively cycles through enabled entries
- **Auto-save**: Every change persisted immediately
- **Migration**: Old flat `letters` format auto-migrated to `sets` on load; single `sound` field migrated to `tracks` dict on v3 startup (see below)

---

## Multi-track audio model (v3.0.0)

Each entry carries a `tracks` dict keyed by kind. Kinds are defined in
`TRACK_KINDS = ("FX", "DE", "EN")` in `server.py`:

| Kind | Label     | Generator                           |
|------|-----------|-------------------------------------|
| FX   | Effekt    | ElevenLabs `sound-generation`       |
| DE   | Deutsch   | ElevenLabs `text-to-speech` (DE voice) |
| EN   | Englisch  | ElevenLabs `text-to-speech` (EN voice) |

**Entry shape (letter or number):**

```json
{
  "word": "Ameise",
  "image": "a_ameise.png",
  "enabled": true,
  "tracks": {
    "FX": {"file": "a_ameise.mp3",    "enabled": true, "prompt": "Ant crawling on dry leaves", "volume": 100},
    "DE": {"file": "a_ameise_de.mp3", "enabled": true, "prompt": "Ameise",                     "volume": 100},
    "EN": {"file": "a_ameise_en.mp3", "enabled": true, "prompt": "Ant",                        "volume": 120}
  },
  "_imageDesc": "Friendly cartoon ant"
}
```

**Set-level playback control:**

```json
{
  "name": "v2.0 Komplett",
  "track_order": [
    {"kind": "FX", "enabled": true},
    {"kind": "DE", "enabled": true},
    {"kind": "EN", "enabled": false}
  ],
  "letters": { ... },
  "numbers": { ... }
}
```

`track_order` controls playback order *and* per-kind global enable.

**Playback pipeline** (`play_sound_sequence` in `server.py`):

1. On keypress, `_collect_playable_files(entry)` iterates the active set's `track_order`, picks tracks that are present + enabled at both the set level and the track level, returns a list of `(path, volume_pct)` tuples.
2. A daemon thread runs `paplay` serially per file with a small gap (default 120 ms).
3. Per-track volume multiplies the global volume (`pa_vol = global * track / 100 * 65536`).
4. The global `_play_seq_abort` flag is raised by `stop_all_sounds()`; the thread checks it between tracks so the next keypress aborts mid-sequence.

**Migration (one-shot on startup)** — `_migrate_to_tracks_v2()`:

- Letter entries: legacy `sound` → `tracks.FX.file`; `_soundDesc` → `tracks.FX.prompt`; legacy keys stripped.
- Number entries: legacy `sound` → `tracks.DE.file` (number sounds historically were German TTS phrases).
- Each set gets `track_order` seeded with `[FX✓, DE✓, EN✗]` if missing.
- `settings.tts_voices` seeded with `{de: <default-voice>, en: <default-voice>}`.
- Idempotent: the marker check is "already has `tracks` dict and no `sound` field".

**API routing:**

- `POST /api/generate/track` — unified dispatcher, body `{kind, word, slug_prefix, prompt?}` → returns `{ok, job_id, filename}`. FX routes to SFX worker, DE/EN to TTS worker with kind-specific voice.
- `GET /api/sets/:id/letters/:L` — always returns normalized entries; additionally synthesizes a legacy `sound` alias from the first track with a file for back-compat.
- `PUT /api/sets/:id/letters/:L` — accepts either legacy or new shape; always persists new shape.
- `PUT /api/sets/:id` — accepts `track_order`.

---

## Loudness normalization (v3.1.0, FUNK-1)

Every sound landing in `/data/sounds` now passes through `ffmpeg loudnorm` so FX, TTS and parent recordings match in perceived loudness.

**Pipeline** — `normalize_mp3(in_path, out_path)` in `server.py`:

```
ffmpeg -y -i in.mp3 \
       -af loudnorm=I=-16:LRA=11:TP=-1.5 \
       -ar 44100 -b:a 128k -loglevel error \
       out.mp3
```

Single-pass (not two-pass): accuracy is fine for toy-fidelity, finishes in ~1 s per clip. Falls back to `shutil.copy2` when ffmpeg is absent, disabled, times out, or exits non-zero — the caller always gets a usable file.

**Wiring:**

- SFX + TTS workers (`_gen_sound_worker`, `_gen_tts_worker`) route their output through `normalize_mp3(ai_path, sounds_path)`. Originals stay in `/data/ai-generated/sounds`.
- Upload endpoint (`POST /api/sounds/upload`) writes to `ai-generated/sounds` first, then `normalize_mp3` into `/data/sounds`.
- `gen_jobs[job_id].normalized` boolean reflects whether the normalization actually ran (vs. fallback copy).

**Settings:**

- `settings.audio_normalize` — bool, default `true` on first run.
- `settings.audio_normalize_lufs` — float, default `-16`. UI slider range `-30 … -10` step `0.5`.

**Batch endpoint** — `POST /api/sounds/normalize-all`:

- Iterates `/data/sounds/*.mp3` in a daemon thread.
- First time a file is processed, the raw clip is backed up to `ai-generated/sounds/<name>_original.mp3` and a `<name>_normalized.marker` file is written. Subsequent runs skip files with a marker (idempotent).
- Streams `normalize_progress` SSE events (`{file, index, total, status: start|done|skipped|error}`) and a final `{status: complete, total, done, skipped, failed}`.
- The **"Bestehende Library normalisieren"** button in Settings triggers this; UI shows live progress from the SSE stream.

---

## Pixoo integration — volume VU-meter overlay (v3.0.0, FUNK-2)

`display.publish_volume(volume)` now publishes an enriched payload:

```json
{
  "letter": "80%",
  "word": "lautstaerke",
  "color": "#FFCC00",
  "bar": true,
  "percent": 80,
  "bars_total": 10,
  "bars_filled": 8,
  "timestamp": 1741180000
}
```

Rendering lives in **`pixdcon/scenes/pixoo/funkeykid.js`**:

- `bar:true` payloads touch only the overlay state (`_volumeBar`, `_volumeBarAt`) so the letter-render path isn't polluted with "80 %" text once the overlay expires.
- Overlay TTL **1.5 s**; a real letter/image payload cancels it instantly.
- Renders a **vertical tapered VU-meter**: 10 segments, narrow-green at the bottom → wide-red at the top, with simple 3-row 3D shading (lighter top row, darker bottom row), neutral-gray "VOLUME" label above, `NN%` in neutral gray below.
- Takes priority over the idle branch so volume changes show even on an otherwise-idle canvas.
