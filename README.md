# funkeykid

Educational keyboard toy for children. Turns a dedicated Bluetooth keyboard into a learning tool.

## Features

- **Letter sounds**: Each key plays a language-appropriate sound (A = Apfel, K = Katze, ...)
- **Pixoo display**: Shows pressed letter on a Pixoo64 LED matrix via pixdcon MQTT
- **TTS**: Optionally speaks the word ("A wie Apfel") via ElevenLabs
- **Language packs**: Configurable per language (starting with de-AT)
- **Mode toggles**: Sound, display, and speak modes independently on/off

## Setup

```bash
cp config.example.json config.json
# Edit config.json with your device name, MQTT settings, paths
```

### Sound files

Sound files are NOT included in this repo (copyright protected). Deploy them to the target host:

```bash
rsync -avz ~/sounds/ mba@hsb1.lan:/var/lib/funkeykid-sounds/
```

### NixOS

This is designed to run as a NixOS service. The NixOS module lives in [nixcfg](https://github.com/markus-barta/nixcfg) at `modules/funkeykid.nix`.

### Development

```bash
nix develop  # Enter dev shell with Python + deps
python funkeykid.py  # Run locally (needs config.json + keyboard)
```

## Configuration

See `config.example.json` for all options. Key settings:

- `language`: Language pack to use (e.g., `de-AT`)
- `modes.sound`: Play sounds on keypress
- `modes.display`: Show letter on Pixoo via MQTT
- `modes.speak`: Speak word via TTS
- `tts.engine`: TTS provider (`elevenlabs`)

## Language packs

Language packs live in `lang/` as JSON files. Each maps letters to:
- A word in that language
- A sound file name
- TTS text to speak

See `lang/de-AT.json` for the format.

## Architecture

```
Keyboard (BT) → evdev → funkeykid.py
                              ├── paplay (sound)
                              ├── MQTT → pixdcon → Pixoo64 (display)
                              └── ElevenLabs API → cached mp3 (TTS)
```

## License

AGPL-3.0
