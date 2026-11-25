# funkeykid — Deployment Guide

## Production Setup (hsb1)

**Server**: hsb1 (Mac Mini, 192.168.1.101, NixOS)
**Container**: `ghcr.io/markus-barta/funkeykid:latest`, which moves only with a release (see [Versioning](#versioning))
**Web UI**: http://hsb1.lan:8081
**CI**: GitHub Actions → GHCR: `:<commit-sha>` on every push to main; `:<version>`, then `:latest`, on a release tag

### Source of truth: nixcfg

This repository owns the **image**. How hsb1 **runs** it is declared in the host configuration repository, nixcfg, and is changed only there, by PR. From funkeykid, propose a change with an OPS ticket.

| What | Where in nixcfg |
|------|-----------------|
| The container: image, environment, env files, mounts, labels | `hosts/hsb1/docker/compose-spec.nix`, service `funkeykid` |
| Host additions to the container: the keyboard pin `FUNKEYKID_KEYBOARD`, and the system CA bundle mounted over certifi | `hosts/hsb1/configuration.nix`, the `spec` overlay |
| Keyboard isolation, Bluetooth reconnect, the keyboard name | `modules/funkeykid.nix` (see [NixOS Module](#nixos-module)) |
| Host runbook | `hosts/hsb1/docs/RUNBOOK.md`, `hosts/hsb1/docs/FUNKEYKID.md` |

The spec is written in Nix. It is rendered into the NixOS closure as `/etc/compose/hsb1/docker-compose.yml`, and `compose-hsb1.service` reconciles it at every switch. The compose project is `docker`, and the stack lock is `/run/lock/compose-hsb1.lock`.

The old hand-edited compose file and the env files in the home directory are **retired**. `~/docker/mounts/` holds runtime data only.

### What Runs Where

```
hsb1 (NixOS)
├── Docker: funkeykid container            (compose-spec.nix service `funkeykid`)
│   ├── server.py (web UI + keyboard listener + sound player)
│   ├── keyboard.py (evdev: exclusive grab of every node of the configured keyboard)
│   ├── display.py (MQTT publish → pixdcon scenes)
│   └── paplay → kiosk PipeWire → 3.5mm speakers
│
├── Docker: pixdcon2-edge container (pixdcon v2, UI :8082) — drives the displays
│   ├── funkeykid.js scene (pixoo-189) — start screen + letter/image display
│   └── home.js scene (pixoo-159) — keyboard status dots in header
├── Docker: pixdcon container (v1, UI :8080) — fallback, same scene files
│
├── NixOS (modules/funkeykid.nix, always active, also with Docker):
│   ├── udev 72-funkeykid.rules — hide every node of the keyboard from X11/libinput/logind
│   ├── logind — power/suspend/hibernate/reboot keys ignored
│   └── acme-bk03-reconnect.service — Bluetooth auto-connect on boot (by name)
│
└── kiosk user (uid 1001)
    ├── PipeWire session — owns real audio hardware
    ├── VLC babycam — controlled via Node-RED MQTT (separate volume)
    └── Openbox + X11 — kiosk display
```

Which pixdcon instance drives a display is set per device in the pixdcon2-edge `config.json` (`output`). As of 2026-11, v2 drives all of them and v1 is the fallback. Details are in the pixdcon project.

### The container

This is a summary of the `funkeykid` service in `hosts/hsb1/docker/compose-spec.nix` (nixcfg main, 2026-11-25). The file itself is authoritative.

| Setting | Value |
|---------|-------|
| image | `ghcr.io/markus-barta/funkeykid:latest` |
| network / restart | `network_mode: host`, `restart: unless-stopped`, `privileged: true` (for `/dev/input`) |
| environment | `TZ=Europe/Vienna`, `XDG_RUNTIME_DIR=/run/user/1001`, `PULSE_SERVER=unix:/run/user/1001/pulse/native`, `DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket`; plus `FUNKEYKID_KEYBOARD=<keyboardName>` from the configuration.nix overlay |
| env_file (agenix) | `/run/agenix/hsb1-smarthome-env` (MQTT credentials), `/run/agenix/hsb1-funkeykid-api-env` (`ELEVENLABS_API_KEY`, `OPENROUTER_API_KEY`) |
| volumes | `~/docker/mounts/funkeykid/{settings.json,sounds,images,ai-generated}` → `/data/…`; `/dev/input`; `/run/user/1001` (the whole runtime dir; see FAQ "No sound after reboot"); `/run/dbus`; plus the system CA bundle over certifi (OPS-430) |
| labels | `com.centurylinklabs.watchtower.*`, leftovers only: watchtower is retired (OPS-125) |

- **Keyboard pin:** because nixcfg sets `FUNKEYKID_KEYBOARD`, the web UI shows the keyboard as "vom Host festgelegt" and locks the device picker. udev and the container always use the same name.
- **Secrets** live in nixcfg as `secrets/hsb1-*-env.age` and are decrypted by agenix to `/run/agenix/`. Changing one is an operator task in nixcfg, never an edit on the host.
- **Updates:** `compose-hsb1-update.timer` (Saturdays 05:00, with jitter) pulls every service and reconciles the stack. Since `:latest` only moves with a release, it only ever picks up released funkeykid images.

### File Locations on hsb1

| Path | Purpose |
|------|---------|
| `~/docker/mounts/funkeykid/settings.json` | All config, sets, letter entries |
| `~/docker/mounts/funkeykid/sounds/` | Active sound files (.mp3) |
| `~/docker/mounts/funkeykid/images/` | Active image files (.png, 64×64), also mounted into pixdcon |
| `~/docker/mounts/funkeykid/ai-generated/` | AI-generated assets (preserved) |
| `/run/agenix/hsb1-smarthome-env` | MQTT credentials (agenix; never read or print it) |
| `/run/agenix/hsb1-funkeykid-api-env` | `ELEVENLABS_API_KEY` + `OPENROUTER_API_KEY` (agenix) |
| `/etc/compose/hsb1/docker-compose.yml` | The rendered compose file (read-only, from the closure) |

### pixdcon Integration

Both pixdcon containers mount funkeykid's image directory read-only (nixcfg compose spec):

```
~/docker/mounts/funkeykid/images → /app/assets/pixoo/funkeykid:ro    # pixdcon2-edge and pixdcon
```

Put images only into `~/docker/mounts/funkeykid/images/`; there is no second copy. pixdcon caches images per process (`lib/pixoo-image.js`), so after replacing an image restart the instance that drives pixoo-189: `ssh mba@hsb1.lan "docker restart pixdcon2-edge"`.

#### The Pixoo scene (pixdcon repo)

The `funkeykid.js` scene (`scenes/pixoo/funkeykid.js`: start screen, letters, images) lives in the separate **pixdcon** repository and belongs to the pixdcon project. From funkeykid, propose scene changes as a PIXD ticket.

- **Deploy:** pixdcon's `docs/DEPLOY.md` § "Scene hot-reload". Scene files go to `~/docker/mounts/pixdcon/scenes/`. pixdcon2-edge reads that directory read-only, and both instances hot-reload a changed scene with their ScenesWatcher, so no restart is needed.
- **Core pixdcon changes** go through pixdcon's own CI. The images are digest-pinned in nixcfg and updated only by PR.

**Contract funkeykid → scene:** MQTT `home/hsb1/funkeykid/display` (letter events) and the retained `home/hsb1/funkeykid/keyboard-info` (start screen, keyboard status). See DEVELOPMENT.md § MQTT.

---

## Versioning

funkeykid uses **INSPR Calendar Versioning 3** (`inspr-calver-3`, label `INSPR-CalVer3`) since FUNK-60. A version is the UTC reservation second, `YYMMDDhhmmss.0.0`, for example `261124195854.0.0`. It is SemVer-syntactic but not SemVer-semantic: a larger number only means "later", and `.0.0` never changes.

- **Authoritative source:** `version.json` (`version`, `version_scheme`, `release_channel`, `release_sequence`, `migration`). `version.py` loads and validates it, and fails closed on any error. Build nodes never derive a version from their clock.
- **Migration anchor:** the last legacy (`MAJOR.MINOR.PATCH`) release is **3.2.0**; the first calendar release is **261124195854.0.0**, stable #1. Legacy tags (v2.x) and images stay exactly as published. Order between eras comes only from this anchor (see `versioning.py`).
- **Every release reserves a new coordinate.** A changed artifact never reuses a published one, and a published `:<version>` image is never overwritten (CI refuses it).
- **Display:** the footer renders the version through the shared INSPR Pretty/SemVer renderer, vendored offline in `static/vendor/inspr-versioning/`. The pins are in `versioning_bundle.py`, verified in tests and in every Docker build. `/api/version` and logs carry the canonical string plus `version_scheme`.

### Release procedure

```bash
python3 scripts/reserve_version.py        # current UTC second, strictly later; release_sequence + 1
git add version.json && git commit -m "release: reserve <version> (inspr-calver-3) for FUNK-n"
# PR → review gate → merge, then tag the merge commit:
git tag -a v<version> <merge-sha> -m "funkeykid <version>" && git push origin v<version>
gh run watch                              # release job: tag == version.json → :<version> + :latest (same build) + GitHub release with release-manifest.json
```

- **Main pushes** build and publish only `:<commit-sha>`. **`:latest` moves only with a release**, so hsb1 (which runs `:latest`) only ever receives released artifacts.
- **Release job order:**
  1. Releases are serialized (one publisher at a time, never cancelled).
  2. The tag must equal `version.json`, and `version.json` must succeed the released history: a strictly later coordinate, `release_sequence` + 1, the same channel and an unchanged anchor (`scripts/check_release_history.py`).
  3. The immutable `:<version>` is published. An existing one is never overwritten; a rerun verifies its labels and recovers without rebuilding.
  4. `release-manifest.json` is written to the GitHub release. It records scheme, channel, sequence, anchor, the image index and per-platform manifest digests, base image, `requirements.lock` digest, per-platform pip/dpkg provenance, bundle pins and the validation run.
  5. **Only then** is that exact artifact promoted to `:latest`, never backward.
- **Reservations are immutable and claimed on a shared ledger.**
  - `reserve_version.py` claims the sequence atomically on the forge as the annotated tag `reservation/stable/<N>`. A second preparer's push is rejected, so two clones can never reserve the same sequence.
  - Only after the claim does it write `version.json`, under a lock and atomically.
  - It refuses to replace a reserved-but-unreleased coordinate.
  - It refuses if any earlier `v<version>` tag is not a **completed** release, meaning a published, non-draft GitHub release that carries its manifest.
  - CI checks that `version.json` is exactly the ledger's coordinate for its sequence.
  - **Interrupted reservation** (push accepted but response lost, or stopped before `version.json` was written): rerun with `--recover`. It restores exactly the claimed coordinate and sequence, with no new timestamp and no ledger change.
  - A previous coordinate more than 5 s in the future (clock skew) fails instead of waiting.
- **Only the `stable` channel** is published, with one immutable migration anchor. Another channel would need its own aliases first.
- **Interrupted releases are recovered:**
  - A rerun publishes a draft release, uploads a missing manifest, and never replaces an existing one.
  - Promotion to `:latest` happens only after the published (non-draft) release verifiably describes the artifact.
- **Local builds go through the build gate:** `python3 scripts/source_manifest.py && docker build .`. The Docker build requires `.source-manifest.json` and fails without it. It verifies that the vendored bundle equals the git-tracked, committed bytes recorded there, because the build context itself has no `.git`.
- **Dependencies:**
  - The base image is pinned by digest (`ARG BASE_IMAGE` in the Dockerfile).
  - Python packages install from `requirements.lock`, which has hashes and is generated from `requirements.in` with `pip-compile --generate-hashes --allow-unsafe --strip-extras` on Python 3.12.
  - Refresh either one deliberately, in a release PR.
- **Presentation pin review** at each release prep: compare `static/vendor/inspr-versioning/manifest.json` with inspr-at/inspr, and re-vendor only if the approved display config or renderer changed (procedure in the `versioning_bundle.py` docstring).
- **Rollback:** deploy a previous release by exact version and digest. For a calendar release, take them from its `release-manifest.json`. For the legacy 3.2.0, use the anchor's `last_legacy_artifact` in `version.json` (`ghcr.io/markus-barta/funkeykid@sha256:bb7818fa…`); CI's `rollback-gate` restores it by digest on every run and checks that it serves 3.2.0. Steps: `docker pull <image>@sha256:<digest>`, re-tag it as `:latest` on hsb1, then the locked single-service `up` (see Deploy Workflows). Rollback never decrements or reuses a version. The fix is a new, later release.

---

## Deploy Workflows

### Normal: Release → CI → locked roll on hsb1

Release first (see "Release procedure" above), then roll **only** funkeykid on hsb1. Use the closure-pinned compose file under the stack lock, with a dry run first. Never operate compose from `~/docker`; hsb1's RUNBOOK forbids it. hsb1's login shell is fish, so pass scripts via `bash -s`:

```bash
ssh mba@hsb1.lan 'bash -s' <<'EOS'
set -euo pipefail
C="docker compose -p docker -f /etc/compose/hsb1/docker-compose.yml --project-directory /home/mba/Code/nixcfg/hosts/hsb1/docker"
docker images --digests ghcr.io/markus-barta/funkeykid          # record the rollback anchor first
sudo flock -w 120 /run/lock/compose-hsb1.lock $C pull funkeykid
sudo $C --dry-run up -d --no-deps funkeykid                      # expect only funkeykid Recreate
sudo flock -w 120 /run/lock/compose-hsb1.lock $C up -d --no-deps funkeykid
EOS
curl -s http://hsb1.lan:8081/api/version                         # version + version_scheme must match the release
```

Verify that the running image digest equals `release-manifest.json`, and that the container stays `Up` for ≥ 2 min. The weekly `compose-hsb1-update.timer` (Saturdays 05:00) also pulls `:latest`, which only ever points at a release.

### Hotfix: Direct Docker CP (no CI)

```bash
# Python files (requires restart)
scp server.py mba@hsb1.lan:/tmp/fk.py
ssh mba@hsb1.lan "docker cp /tmp/fk.py funkeykid:/app/server.py && docker restart funkeykid"

# Static files (no restart — aiohttp serves from disk)
scp static/index.html mba@hsb1.lan:/tmp/fk.html
ssh mba@hsb1.lan "docker cp /tmp/fk.html funkeykid:/app/static/index.html"
```

**Warning**: Hotfixed files are lost at the next recreate (a roll, a nixcfg switch that changes the spec, or the Saturday updater). Always commit, push, and release.

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

In `nixcfg/modules/funkeykid.nix`, enabled for hsb1 in nixcfg:

| Option | Default | Purpose |
|--------|---------|---------|
| `enable` | `false` | Systemd Python service. OFF when using Docker (hsb1 uses Docker) |
| `keyboardName` | `ACME BK03` | evdev name of the kids' keyboard. Hidden from the host by udev, and passed to the container as `FUNKEYKID_KEYBOARD` |
| `hardwareIsolation` | `true` | udev `72-funkeykid.rules` (every node named `<keyboardName>` or `<keyboardName> *` is hidden from X11/libinput/logind), plus logind ignoring the power, suspend, hibernate and reboot keys |
| `bluetoothReconnect` | `true` | Auto-connect the keyboard on boot, looked up by name (5 retries, no MAC in the config) |

**CRITICAL**: `hardwareIsolation` MUST stay `true` even with Docker. Without it, child keypresses type into host terminals and power keys shut down the system.

Inside the container, funkeykid also grabs every node of the keyboard exclusively (FUNK-59). The UI shows "Nicht exklusiv!" if that fails.

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

# Restart (same container, same image; to change the image use the locked roll above)
ssh mba@hsb1.lan "docker restart funkeykid"
```
