# funkeykid

Educational keyboard toy for children. A dedicated Bluetooth keyboard becomes a learning device that plays per-letter sounds, shows the letter + illustration on a Pixoo 64 display, and lets a parent author content via a web UI including AI generation.

**Host**: hsb1 · **Image**: `ghcr.io/markus-barta/funkeykid:latest` · **UI**: http://hsb1.lan:8081 · **Version**: INSPR-CalVer3, shown in the UI footer and at `/api/version` (`version.json`)

## Features

### Core
- **Per-letter content**: each key plays its entry's audio tracks and shows an illustration on the Pixoo 64. A-Z + digits 0-9.
- **Cycle on repeat**: pressing the same letter cycles through its enabled entries; position memory per letter.
- **Random-sounds fallback**: unmapped keys can play a random clip from the library (opt-in).
- **Key map** (FUNK-62): any key can be given its own sound, image + sound, a function (volume, mute, stop, next set, …), a letter's content, or nothing. This includes the keyboard's media keys, like the scissors. In learn mode (Tastatur tab) you press the key and assign it; keys pressed without any effect are listed. See DEVELOPMENT.md § Key map.
- **Keyboard profiles** (FUNK-63): the Tastatur tab draws the keyboard (ACME BK03, PC Deutsch, Mac Deutsch) with what every key does; click a key to assign it, or press it in learn mode and it lights up. Each keyboard has its own key map. Duplicate one to relabel keys or fix the code a key sends. See DEVELOPMENT.md § Keyboard profiles.
- **Language and number pad** (FUNK-64): the keyboard language (Deutsch or English US) decides the letters: Z/Y, and Ü/Ö/Ä/ß as letters for German (the ß key plays ß since FUNK-46; − and the volume keys change the volume). "Standard" means a PC or Mac keyboard of that language, with or without a number pad, and the pad digits play the numbers. Keyboards and their key maps can be exported and imported as JSON files.
- **Quiz** (FUNK-39): the box plays a word or a sound, the child presses its letter. Right: a jingle and the letter's entry; wrong: the question again, and after two tries the letter is shown. Start it in the Quiz tab or from a key ("Quiz starten/stoppen"). Each session is summarized; the last 20 are kept. See DEVELOPMENT.md § Quiz.
- **Progress per letter** (FUNK-40): each letter and digit card shows how often its key was pressed (the card gets more orange the more it was pressed), and how the quiz went. A letter is "gelernt" after a configurable number of quiz answers in a row that were right on the first try (default 3). Only the real keyboard counts. Settings → Fortschritt sets the number and resets everything. See DEVELOPMENT.md § Progress per letter.
- **Redesigned parent page** (FUNK-72):
  - **Navigation and layout:** sidebar navigation (a bottom bar on phones); the Buchstaben tiles show per letter how many words have a picture, a sound and a voice.
  - **Editor:** a word list plus one word in focus. Changes save by themselves, with undo. "So hört es dein Kind" plays a word the way the keyboard does. A Pixoo preview shows the 64×64 picture.
  - **AI generation:** never shows a broken picture. A new file fills an empty slot; when replacing, it is offered as "current vs new". Failures show their reason in plain German. An activity list shows every AI job. See DEVELOPMENT.md § Entry editor.
- **Übersicht** (FUNK-42): a dashboard tab for parents. Key presses, play time, quiz accuracy, learned letters and days played for today, 7 or 30 days; a 30-day chart; the keyboard as a heat map of the most-pressed keys; the top keys and the last play sessions; CSV export of the sessions (90 days) and the per-key totals. See DEVELOPMENT.md § Übersicht.
- **Set export / import** (FUNK-43): a set travels as one ZIP with all its sounds and pictures, for backups, siblings or another device. Importing shows a preview first, and if a file with the same name but other content already exists, you choose per file: rename, keep or overwrite.

### Multi-track audio model (v3.0.0)
- Each entry carries a `tracks` dict keyed by kind: `FX` (sound effect), `DE` (German TTS), `EN` (English TTS). Extensible later.
- Each set defines its own **playback order + per-kind enable** (Wiedergabe-Reihenfolge in the Sets tab) — e.g. "play FX then DE, skip EN".
- **Per-track volume** (0–200 %) trims quiet clips up and loud clips down independently of the global volume.
- Tracks are played back-to-back with a short gap. Any new keypress aborts the running sequence instantly.

### Loudness normalization (v3.1.0, FUNK-1)
- `ffmpeg` loudnorm (EBU R128, default **–16 LUFS**) runs automatically on every generated or uploaded sound, so FX / TTS / parent recordings all sit at the same perceived loudness.
- Toggle + target LUFS slider in the Settings tab; "Bestehende Library normalisieren" batch-processes existing files with live progress (SSE) and backs up originals into `ai-generated/sounds`.

### Exclusive kids' keyboard (v3.2.0, FUNK-59)
- funkeykid **exclusively grabs every input node** of the configured keyboard (incl. its "System Control" node with sleep/power keys). No keypress reaches the host: not the kiosk session, not logind, not the console. This is always on.
- Picked up within ~1 s after a Bluetooth reconnect. A failed grab shows up as a red **"Nicht exklusiv!"** badge and `exclusive: false` on MQTT.
- Keyboard identity: env `FUNKEYKID_KEYBOARD` (host-pinned, locks the Settings field) > Settings picker > `ACME BK03`. Switching hot-swaps without a restart.
- **Akku-Warnung ab** (battery-low threshold, default 20 %, 0 = off) in Settings, because rechargeable NiMH cells report low levels early.

### AI content generation
- **ElevenLabs** for FX (`sound-generation`) and TTS (`text-to-speech` with `eleven_multilingual_v2`, separate voices per language).
- **OpenRouter** for images (default `google/gemini-3-pro-image-preview`, Pixar-style prompt baked in, Pillow-resized to 64×64) and for word suggestion.
- **Unified `/api/generate/track` endpoint** — FX / DE / EN dispatch behind one API call; returns a job id; completion streams over SSE.
- **KI-Vorschlag (word suggestion)** — expandable editable prompt panel. Server substitutes `{letter}` + `{excluded}`, auto-retries up to 3× on blacklist collisions, returns `{word, word_en, sound_description, image_description}`.
- **Auto-chain on suggest** — one click populates the entry and fires image + FX + DE + EN generations in parallel; the dropdowns and thumbnail fill in as each finishes.
- **Per-track "Generieren" + auto-play** — clicking Generieren on a single sound track plays the clip once when ready.
- **Fehlende Stimmen erzeugen (FUNK-75)** — one button on the Buchstaben tab makes the German voice for every word of the active set that has none. It runs on the server, word by word, with progress, "Anhalten" and a summary.

### Content editor
- **Letters tab** — 26-tile grid with first-entry thumbnail + FX/DE/EN presence dots.
- **Letter modal** — two-pane, responsive card grid; each entry gets image track + three sound tracks with per-track dropdown / prompt / volume / Generieren.
- **Header nav** — `◀ [A ▼] ▶` plus `Shift+←/→` to walk letters; auto-saves on jump.
- **Zahlen tab (FUNK-76)** — the same tiles and editor as Buchstaben: saves by itself, undo, voice/sound slots with "current vs new", plus a backgrounds strip (switch, remove, library, upload, KI-Hintergrund; the digit shows on an active background, pressing again switches).
- **Sets tab** — multiple content packs; Aktivieren / Duplizieren / Umbenennen / Löschen; active set exposes its Wiedergabe-Reihenfolge editor (reorder ↑↓ + per-kind toggle, auto-saves).
- **Blacklist per letter** — rejected KI suggestions remembered so they don't come back.
- **Editable AI prompts** — system prompt for word suggestion, sound description template, image style prompt, all in Settings.
- **Virtual keyboard** — QWERTZ + number row with ⌫ + Ü/Ö/Ä/ß + ↵ Enter + Tab ★ favorite + Space + −/+ volume + ◀▲▼▶ arrows. Clicking any key calls the same handler as the physical keyboard.

### Pixoo 64 display (via pixdcon, separate repo)
- `display.publish_letter(letter, word, image)` → scene draws the bg image + letter + word.
- `display.publish_volume(volume)` → scene renders a **vertical VU-meter** overlay (FUNK-2): 10 tapered segments from narrow-green at the bottom to wide-red at the top, with "VOLUME" label and `NN%` in neutral gray. TTL 1.5 s; any letter press cancels the overlay.
- `display.publish_keyboard_status(status)` → retained `keyboard-info` (`online`, `connected`, `exclusive`, `battery_level`, `battery_low`, …), re-published every 10 s, with an MQTT last-will that flips it to `online: false` if funkeykid dies. It feeds the home scene's status dots and the funkeykid scene's start screen (PIXD-122). Contract: `display.py` docstring / DEVELOPMENT.md § MQTT Topics.

### Ops / platform
- **Docker** image on hsb1 with `host` networking, privileged, volume-mounted `/data`. How hsb1 runs it (compose spec, agenix secrets, keyboard pin) is declared in nixcfg (`hosts/hsb1/docker/compose-spec.nix`). See DEPLOY.md § Production Setup.
- **GitHub Actions CI → GHCR**: `:<commit-sha>` on every push to `main`; on a release tag, `:<version>` first, then `:latest` (BUILD_SHA + BUILD_TIME baked in). hsb1 runs `:latest` and is rolled with a locked single-service compose roll.
- **SSE live updates** for keypresses, gen jobs, AI log, normalize progress, volume, favorites, keyboard connection.
- **Versioning**: INSPR Calendar Versioning 3 (`inspr-calver-3`, `YYMMDDhhmmss.0.0`, UTC), authoritative in `version.json`. It was migrated from legacy 3.2.0 in FUNK-60. A release = `scripts/reserve_version.py` → PR → tag `v<version>` → CI publishes the immutable `:<version>` and `:latest` plus a release-set manifest. The footer renders the version through the shared INSPR Pretty/SemVer renderer, vendored and pinned. See DEPLOY.md § Versioning.
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
├── settings.json            # All config: sets, letters, numbers, track_order, AI prompts, voices, quiz, progress
├── sounds/                  # Active mp3s (normalized, served to paplay + browser preview)
├── images/                  # Active 64×64 pngs (served to pixdcon + letters UI)
├── ai-generated/
│   ├── sounds/              # Pre-normalization originals + _original backups
│   ├── images-original/     # Raw AI output (512×512)
│   └── images-resized/      # Resized 64×64 twins
└── archive/                 # Archived (unused) assets kept for rollback
```

## Deploy

Normal path: **release, then roll**. A push to `main` alone does not reach hsb1.

1. Reserve the version: `python3 scripts/reserve_version.py`, which updates `version.json`. Merge it through a PR with green CI.
2. Tag `v<version>` on the merge commit and push the tag. CI publishes `:<version>`, the GitHub release with its manifest, then `:latest`.
3. Roll **only** funkeykid on hsb1 with the locked compose roll (pull, dry run, `up -d --no-deps funkeykid`).
4. Verify that `/api/version` and the running digest match the release manifest.

The exact commands are in **DEPLOY.md** § Versioning → "Release procedure" and § "Normal: Release → CI → locked roll on hsb1". hsb1's compose file comes from nixcfg; never run compose from `~/docker`.

Static-only hotfix (lost at the next recreate; don't skip the commit and release):

```bash
scp static/index.html mba@hsb1.lan:/tmp/fk.html
ssh mba@hsb1.lan "docker cp /tmp/fk.html funkeykid:/app/static/index.html"
```

See **[DEPLOY.md](DEPLOY.md)** for the full ops guide, volume mounts, NixOS module, and audio routing.

## Docs

- **[DEVELOPMENT.md](DEVELOPMENT.md)** — Architecture, data model (multi-track), APIs, AI integrations, audio pipeline.
- **[DEPLOY.md](DEPLOY.md)** — Docker, NixOS module, hardware isolation, audio routing via kiosk PipeWire.
- **[FAQ.md](FAQ.md)** — Troubleshooting (no sound, keyboard issues, volume).

## Tasks / backlog

All task tracking — backlog, in-flight work, epics, bug reports — lives in **PPM** (pm.barta.cm) under project **`FUNK`** (id 5). There is no `+pm/backlog/` directory in this repo; funkeykid skipped that legacy phase. Issue keys referenced in code and docs (e.g. `FUNK-1` for loudness normalization, `FUNK-2` for the VU-meter overlay) link back to PPM.

## License

AGPL-3.0
