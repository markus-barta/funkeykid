# funkeykid

Educational keyboard toy for children. Turns a dedicated Bluetooth keyboard into a learning tool with sounds, Pixoo64 display, and AI-generated content.

**Host**: hsb1 · **Image**: `ghcr.io/markus-barta/funkeykid:latest` · **UI**: http://hsb1.lan:8081

## Features

- **Letter sounds**: Each key plays a language-appropriate sound (A = Apfel, K = Katze, ...)
- **Arrow navigation**: RIGHT/LEFT step through all sounds sequentially; UP/DOWN jump by letter
- **Pixoo display**: Shows letter + word on Pixoo64 via pixdcon MQTT (50% darkened text strip)
- **Keyboard status**: Connection state published to MQTT → shown as dots in pixdcon home scene
- **AI generation**: Create sounds (ElevenLabs) and images (Gemini) from the web UI
- **Sets**: Multiple letter/word collections, switchable from the UI
- **Web UI**: Full management at port 8081 (letters, sets, files, settings, AI tools)

## Architecture

```
ACME BK03 (Bluetooth keyboard)
  → evdev → keyboard.py (QWERTZ layout mapping)
  → server.py handle_key()
      ├── pactl set-sink-volume 100% → paplay → kiosk PipeWire → speakers
      ├── MQTT display topic → pixdcon funkeykid.js → Pixoo64 (letter+image)
      └── MQTT keyboard-info → pixdcon home.js → Pixoo64 (status dots)
```

## Keyboard Controls

| Key | Action |
|-----|--------|
| A-Z | Play letter sound, cycle through entries on repeat |
| RIGHT/LEFT | Step through all sounds sequentially (wraps) |
| UP/DOWN | Jump to previous/next letter (wraps) |
| ENTER | Replay last sound |
| SPACE | Stop all sounds |
| +/- | Volume up/down (10% steps) |
| TAB | Toggle favorite on current entry |
| 1-0 | Play favorite #1-#10 |

## Deploy

```bash
# CI path (normal)
git push → gh run watch → ssh hsb1 "cd ~/docker && docker compose pull funkeykid && docker compose up -d funkeykid"

# Hotfix (no CI, lost on next pull)
scp server.py mba@hsb1.lan:/tmp/fk.py
ssh mba@hsb1.lan "docker cp /tmp/fk.py funkeykid:/app/server.py && docker restart funkeykid"
```

## Docs

- **[DEVELOPMENT.md](DEVELOPMENT.md)** — Full dev guide (architecture, audio routing, APIs, AI generation)
- **[DEPLOY.md](DEPLOY.md)** — Deployment guide (Docker, NixOS, audio, file locations)
- **[FAQ.md](FAQ.md)** — Troubleshooting (no sound, keyboard issues, volume)

## License

AGPL-3.0
