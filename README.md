# funkeykid

Educational keyboard toy for children. A dedicated Bluetooth keyboard becomes a learning device that plays per-letter sounds, shows the letter + illustration on a Pixoo 64 display, and lets a parent author content via a web UI including AI generation.

**Host**: hsb1 · **Image**: `ghcr.io/markus-barta/funkeykid:latest` · **UI**: http://hsb1.lan:8081 · **Version**: 3.1.0

## Features

### Core
- **Per-letter content**: each key plays its entry's audio tracks and shows an illustration on the Pixoo 64. A-Z + digits 0-9.
- **Cycle on repeat**: pressing the same letter cycles through its enabled entries; position memory per letter.
- **Random-sounds fallback**: unmapped keys can play a random clip from the library (opt-in).

### Multi-track audio model (v3.0.0)
- Each entry carries a `tracks` dict keyed by kind: `FX` (sound effect), `DE` (German TTS), `EN` (English TTS). Extensible later.
- Each set defines its own **playback order + per-kind enable** (Wiedergabe-Reihenfolge in the Sets tab) — e.g. "play FX then DE, skip EN".
- **Per-track volume** (0–200 %) trims quiet clips up and loud clips down independently of the global volume.
- Tracks are played back-to-back with a short gap. Any new keypress aborts the running sequence instantly.

### Loudness normalization (v3.1.0, FKID-1)
- `ffmpeg` loudnorm (EBU R128, default **–16 LUFS**) runs automatically on every generated or uploaded sound, so FX / TTS / parent recordings all sit at the same perceived loudness.
- Toggle + target LUFS slider in the Settings tab; "Bestehende Library normalisieren" batch-processes existing files with live progress (SSE) and backs up originals into `ai-generated/sounds`.

### AI content generation
- **ElevenLabs** for FX (`sound-generation`) and TTS (`text-to-speech` with `eleven_multilingual_v2`, separate voices per language).
- **OpenRouter** for images (default `google/gemini-3-pro-image-preview`, Pixar-style prompt baked in, Pillow-resized to 64×64) and for word suggestion.
- **Unified `/api/generate/track` endpoint** — FX / DE / EN dispatch behind one API call; returns a job id; completion streams over SSE.
- **KI-Vorschlag (word suggestion)** — expandable editable prompt panel. Server substitutes `{letter}` + `{excluded}`, auto-retries up to 3× on blacklist collisions, returns `{word, word_en, sound_description, image_description}`.
- **Auto-chain on suggest** — one click populates the entry and fires image + FX + DE + EN generations in parallel; the dropdowns and thumbnail fill in as each finishes.
- **Per-track "Generieren" + auto-play** — clicking Generieren on a single sound track plays the clip once when ready.

### Content editor
- **Letters tab** — 26-tile grid with first-entry thumbnail + FX/DE/EN presence dots.
- **Letter modal** — two-pane, responsive card grid; each entry gets image track + three sound tracks with per-track dropdown / prompt / volume / Generieren.
- **Header nav** — `◀ [A ▼] ▶` plus `Shift+←/→` to walk letters; auto-saves on jump.
- **Numbers tab + editor** — same multi-track treatment plus the background-image gallery (cycling on repeat-press).
- **Sets tab** — multiple content packs; Aktivieren / Duplizieren / Umbenennen / Löschen; active set exposes its Wiedergabe-Reihenfolge editor (reorder ↑↓ + per-kind toggle, auto-saves).
- **Blacklist per letter** — rejected KI suggestions remembered so they don't come back.
- **Editable AI prompts** — system prompt for word suggestion, sound description template, image style prompt, all in Settings.
- **Virtual keyboard** — QWERTZ + number row with ⌫ + Ü/Ö/Ä/ß + ↵ Enter + Tab ★ favorite + Space + −/+ volume + ◀▲▼▶ arrows. Clicking any key calls the same handler as the physical keyboard.

### Pixoo 64 display (via pixdicon, separate repo)
- `display.publish_letter(letter, word, image)` → scene draws the bg image + letter + word.
- `display.publish_volume(volume)` → scene renders a **vertical VU-meter** overlay (FKID-2): 10 tapered segments from narrow-green at the bottom to wide-red at the top, with "VOLUME" label and `NN%` in neutral gray. TTL 1.5 s; any letter press cancels the overlay.
- `display.publish_keyboard_status(status)` → retained heartbeat for the home scene's status dots.

### Ops / platform
- **Docker** image on hsb1 with `host` networking, privileged, volume-mounted `/data`.
- **GitHub Actions CI → GHCR** on push to `main` (build + BUILD_SHA + BUILD_TIME baked in). `docker compose pull && up -d` on hsb1 to roll forward.
- **SSE live updates** for keypresses, gen jobs, AI log, normalize progress, volume, favorites, keyboard connection.
- **Versioning**: every deploy bumps `version.py` at minimum by patch. UI footer shows version linked to the exact commit.
- **Cache-Control: no-cache** on `index.html` so clients pick up new builds immediately.
- **AI log** (rolling last 50) + ElevenLabs / OpenRouter credit checks in the "KI & Dienste" tab.

## Keyboard controls

| Key | Action |
|-----|--------|
| `A–Z` | Play letter entry; repeat-press cycles through the letter's enabled entries |
| `0–9` | Play digit entry; repeat-press cycles through its backgrounds |
| `←` / `→` | Step through the flat playlist (all enabled entries, A→Z) |
| `↑` / `↓` | Jump to the previous / next letter's first entry |
| `ENTER` | Replay the last played sound |
| `SPACE` | Stop all sounds |
| `+` / `−` | Volume up / down (10 % steps) — publishes VU-meter overlay to the Pixoo |
| `TAB` | Toggle favorite for the last-played entry |
| `SHIFT + 0–9` | Play favorite slot (1→slot 0, …, 0→slot 9) |

## Data layout

```
/data/
├── settings.json            # All config: sets, letters, numbers, track_order, AI prompts, voices
├── sounds/                  # Active mp3s (normalized, served to paplay + browser preview)
├── images/                  # Active 64×64 pngs (served to pixdcon + letters UI)
├── ai-generated/
│   ├── sounds/              # Pre-normalization originals + _original backups
│   ├── images-original/     # Raw AI output (512×512)
│   └── images-resized/      # Resized 64×64 twins
└── archive/                 # Archived (unused) assets kept for rollback
```

## Deploy

Normal path:

```bash
git add . && git commit -m "..."
git push
gh run watch --exit-status
ssh mba@hsb1.lan "cd ~/docker && docker compose pull funkeykid && docker compose up -d funkeykid"
```

Static-only hotfix (lost on next pull — don't skip the commit):

```bash
scp static/index.html mba@hsb1.lan:/tmp/fk.html
ssh mba@hsb1.lan "docker cp /tmp/fk.html funkeykid:/app/static/index.html"
```

See **[DEPLOY.md](DEPLOY.md)** for the full ops guide, volume mounts, NixOS module, and audio routing.

## Docs

- **[DEVELOPMENT.md](DEVELOPMENT.md)** — Architecture, data model (multi-track), APIs, AI integrations, audio pipeline.
- **[DEPLOY.md](DEPLOY.md)** — Docker, NixOS module, hardware isolation, audio routing via kiosk PipeWire.
- **[FAQ.md](FAQ.md)** — Troubleshooting (no sound, keyboard issues, volume).

## License

AGPL-3.0
