# funkeykid — Deployment Guide

## Production Setup (hsb1)

**Server**: hsb1 (Mac Mini, 192.168.1.101, NixOS 25.11)
**Container**: `ghcr.io/markus-barta/funkeykid:latest`
**Web UI**: http://hsb1.lan:8081
**CI**: GitHub Actions → GHCR on push to main

### What Runs Where

```
hsb1 (NixOS)
├── Docker: funkeykid container
│   ├── server.py (web UI + keyboard listener + sound player)
│   ├── keyboard.py (evdev → ACME BK03 via /dev/input)
│   ├── display.py (MQTT publish → pixdcon)
│   └── paplay → kiosk PipeWire → 3.5mm speakers
│
├── Docker: pixdcon container
│   ├── funkeykid.js scene (pixoo-189) — letter + image display
│   └── home.js scene (pixoo-159) — keyboard status dots in header
│
├── NixOS systemd (always active, even with Docker):
│   ├── acme-bk03-reconnect.service — BT auto-connect on boot
│   └── udev rules — isolate keyboard from X11/logind
│
└── kiosk user (uid 1001)
    ├── PipeWire session — owns real audio hardware
    ├── VLC babycam — controlled via Node-RED MQTT (separate volume)
    └── Openbox + X11 — kiosk display
```

### Docker Compose

In `~/docker/docker-compose.yml` on hsb1:

```yaml
funkeykid:
  image: ghcr.io/markus-barta/funkeykid:latest
  container_name: funkeykid
  network_mode: host
  restart: unless-stopped
  privileged: true
  environment:
    - TZ=Europe/Vienna
    - XDG_RUNTIME_DIR=/run/user/1001
    - PULSE_SERVER=unix:/run/user/1001/pulse/native
    - DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket
  env_file:
    - /home/mba/secrets/smarthome.env
    - /home/mba/secrets/funkeykid-api.env
  volumes:
    - ./mounts/funkeykid/settings.json:/data/settings.json
    - ./mounts/funkeykid/sounds:/data/sounds
    - ./mounts/funkeykid/images:/data/images
    - ./mounts/funkeykid/ai-generated:/data/ai-generated
    - /dev/input:/dev/input
    - /run/user/1001/pulse:/run/user/1001/pulse
    - /run/dbus:/run/dbus
```

### File Locations on hsb1

| Path | Purpose |
|------|---------|
| `~/docker/mounts/funkeykid/settings.json` | All config, sets, letter entries |
| `~/docker/mounts/funkeykid/sounds/` | Active sound files (.mp3) |
| `~/docker/mounts/funkeykid/images/` | Active image files (.png, 64×64) |
| `~/docker/mounts/funkeykid/ai-generated/` | AI-generated assets (preserved) |
| `~/secrets/smarthome.env` | MQTT credentials |
| `~/secrets/funkeykid-api.env` | ELEVENLABS_API_KEY + OPENROUTER_API_KEY |

### pixdcon Integration

funkeykid images are also mounted into the pixdcon container:

```yaml
# In pixdcon service volumes:
- ./mounts/funkeykid/images:/app/assets/pixoo/funkeykid:ro
```

When updating images, copy to both:
```bash
~/docker/mounts/funkeykid/images/          # funkeykid reads these
~/docker/mounts/pixdcon/assets/pixoo/funkeykid/  # NOT needed — mounted from above
```

#### Updating the Pixoo scene (pixdicon repo)

The `funkeykid.js` scene on the Pixoo lives in the separate **pixdicon** repo. Its scene files are host-mounted and hot-reloaded by pixdicon — no container rebuild needed for scene tweaks:

```bash
# From pixdicon working copy:
scp scenes/pixoo/funkeykid.js mba@hsb1.lan:~/docker/mounts/pixdcon/scenes/pixoo/funkeykid.js
# ScenesWatcher logs "Changed: funkeykid.js" and reinitializes the scene.
```

Core pixdicon code changes (src/, lib/, package.json) still go via its own CI → GHCR → `docker compose pull pixdcon`.

See `pixdicon/docs/DEPLOY.md` for the full pixdicon deploy guide.

---

## Versioning

Version is in `version.py`. Format: `MAJOR.MINOR.PATCH`

**Every deploy bumps at least the patch version.** No exceptions.

| Change | Bump | Example |
|--------|------|---------|
| Bug fix, hotfix, styling tweak | PATCH | 3.1.0 → 3.1.1 |
| New feature, UI rework, new API | MINOR | 3.1.0 → 3.2.0 |
| Breaking change, major rearchitecture | MAJOR | 3.1.0 → 4.0.0 |

Current: **3.1.0** (ffmpeg loudnorm pipeline). **3.0.0** was the multi-track refactor.

Before pushing:
```bash
# Edit version.py
VERSION = "X.Y.Z"

# Commit with the version in the message
git add -A && git commit -m "feat: description (vX.Y.Z)"
```

The CI build bakes `BUILD_SHA` into the Docker image via `--build-arg`. The footer shows version + commit hash linked to GitHub.

---

## Deploy Workflows

### Normal: Push → CI → Pull

```bash
git add . && git commit -m "..." && git push
gh run watch --exit-status
ssh mba@hsb1.lan "cd ~/docker && docker compose pull funkeykid && docker compose up -d funkeykid"
```

### Hotfix: Direct Docker CP (no CI)

```bash
# Python files (requires restart)
scp server.py mba@hsb1.lan:/tmp/fk.py
ssh mba@hsb1.lan "docker cp /tmp/fk.py funkeykid:/app/server.py && docker restart funkeykid"

# Static files (no restart — aiohttp serves from disk)
scp static/index.html mba@hsb1.lan:/tmp/fk.html
ssh mba@hsb1.lan "docker cp /tmp/fk.html funkeykid:/app/static/index.html"
```

**Warning**: Hotfixed files are lost on next `docker compose up`. Always commit + push.

### Settings / Sounds / Images (no deploy needed)

These are volume-mounted. Edit directly on hsb1 or via web UI:

```bash
# Edit settings
ssh mba@hsb1.lan "vi ~/docker/mounts/funkeykid/settings.json"

# Upload sounds
scp new_sound.mp3 mba@hsb1.lan:~/docker/mounts/funkeykid/sounds/

# Upload images
scp new_image.png mba@hsb1.lan:~/docker/mounts/funkeykid/images/
```

---

## NixOS Module

In `nixcfg/modules/funkeykid.nix`. Three independent toggles:

| Option | Default | Purpose |
|--------|---------|---------|
| `enable` | `false` | Systemd Python service — OFF when using Docker |
| `hardwareIsolation` | `true` | udev rules: strip ACME BK03 from logind/X11 |
| `bluetoothReconnect` | `true` | Auto-connect ACME BK03 on boot (5 retries) |

**CRITICAL**: `hardwareIsolation` MUST stay `true` even with Docker. Without it, child keypresses type into host terminals and power keys shut down the system.

---

## Audio Routing

```
funkeykid container → paplay → PULSE_SERVER socket → kiosk PipeWire → ALSA → speakers
babycam VLC         → (runs as kiosk natively)    → kiosk PipeWire → ALSA → speakers
```

- Both streams share the same PipeWire session (kiosk, uid 1001)
- funkeykid pins sink volume to 100% before every `paplay` call
- babycam volume controlled independently via VLC telnet (Node-RED MQTT)
- funkeykid volume controlled via `paplay --volume=N` (per-stream, 0-65536)

### Volume Stack

```
settings.json "volume": 100
  → pa_vol = int(volume / 100 * 65536)     # 100% = 65536 = 0dB
  → pactl set-sink-volume @DEFAULT_SINK@ 100%  # pin sink (prevents drift)
  → paplay --volume=65536 sound.mp3         # per-stream volume
  → PipeWire sink (pinned 100%)
  → ALSA → 3.5mm → speakers
```

---

## Monitoring

```bash
# Container logs
ssh mba@hsb1.lan "docker logs -f funkeykid"

# Keyboard status via MQTT
mosquitto_sub -h hsb1.lan -u smarthome -P '***' -t 'home/hsb1/funkeykid/keyboard-info' -v

# Check PipeWire sink volume
ssh mba@hsb1.lan "sudo -u kiosk XDG_RUNTIME_DIR=/run/user/1001 pactl get-sink-volume @DEFAULT_SINK@"

# Restart
ssh mba@hsb1.lan "cd ~/docker && docker compose restart funkeykid"
```
