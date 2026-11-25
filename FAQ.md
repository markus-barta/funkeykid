# funkeykid — FAQ / Troubleshooting

## Sound

### Sound plays but is very quiet

**Likely cause**: PipeWire sink volume drifted below 100%.

Since v2.5, `play_sound()` auto-pins the sink to 100% before every play. If still quiet:

```bash
# Check sink volume
ssh mba@hsb1.lan "sudo -u kiosk XDG_RUNTIME_DIR=/run/user/1001 pactl get-sink-volume @DEFAULT_SINK@"

# Manually fix
ssh mba@hsb1.lan "sudo -u kiosk XDG_RUNTIME_DIR=/run/user/1001 pactl set-sink-volume @DEFAULT_SINK@ 100%"
```

If the auto-pin isn't working, check container logs for `pactl` errors:
```bash
docker logs funkeykid 2>&1 | grep -i 'pactl\|sink\|sound'
```

### No sound at all

1. **Check volume in settings**: http://hsb1.lan:8081 → Einstellungen → Volume must be >0
2. **Check paplay target**: Container must use kiosk's PipeWire (uid 1001), not mba's (uid 1000)
   ```bash
   ssh mba@hsb1.lan "docker exec funkeykid printenv PULSE_SERVER"   # only this variable: the env also holds credentials
   # Must show: unix:/run/user/1001/pulse/native
   ```
3. **Check kiosk PipeWire is running**:
   ```bash
   ssh mba@hsb1.lan "sudo -u kiosk XDG_RUNTIME_DIR=/run/user/1001 pactl info"
   ```
4. **Check sink is real** (not null):
   ```bash
   ssh mba@hsb1.lan "sudo -u kiosk XDG_RUNTIME_DIR=/run/user/1001 pactl list sinks short"
   # Should show: alsa_output.pci-0000_00_1b.0.analog-stereo
   # NOT: auto_null
   ```
5. **Test manually**:
   ```bash
   ssh mba@hsb1.lan "sudo su - kiosk -s /bin/sh -c 'PULSE_SERVER=unix:/run/user/1001/pulse/native paplay --volume=65536 /tmp/test.mp3'"
   ```

### Sound plays but babycam is also affected

Babycam (VLC) volume is controlled independently via its telnet interface (Node-RED publishes to `home/hsb1/kiosk-vlc-volume`). funkeykid only touches the PipeWire sink and paplay per-stream volume.

If babycam volume changed: check if Node-RED sent a VLC volume command, not funkeykid.

### Volume +/- keys don't change audible volume much

The +/- keys change `paplay --volume` (per-stream, 0-65536). At 100% this is 0dB (unity gain). The source MP3 files may be mastered quiet (especially ElevenLabs TTS). To boost beyond 100%, the code would need to allow `pa_vol > 65536`. Currently capped at 100%.

---

## Keyboard

### Keyboard not detected

1. **Check Bluetooth connection**:
   ```bash
   ssh mba@hsb1.lan "bluetoothctl devices"          # find the keyboard's address
   ssh mba@hsb1.lan "bluetoothctl info <BK03_MAC>"
   # Look for "Connected: yes"
   ```
2. **Manually reconnect**:
   ```bash
   ssh mba@hsb1.lan "bluetoothctl connect <BK03_MAC>"
   ```
   Never commit the real address; MAC addresses belong only in encrypted `.age` files.
3. **Check evdev visibility inside container**:
   ```bash
   docker exec funkeykid python3 -c "import evdev; [print(evdev.InputDevice(p).name, p) for p in evdev.list_devices()]"
   ```
4. **Hot-plug issue**: If keyboard connected AFTER container started, the new `/dev/input/eventN` may not be visible. Use "Neu verbinden" in web UI or restart container.

### Keyboard types into host terminal

`hardwareIsolation` is disabled in NixOS config. Fix:
```nix
services.funkeykid.hardwareIsolation = true;  # MUST be true
```
Then `nixos-rebuild switch`.

### Wrong letters (Y/Z swapped)

The keyboard language is `"de"` (QWERTZ) or `"us"`. Set it in Einstellungen, or under Tastatur → Tastaturen verwalten. evdev reports US scancodes, and for `de` `keyboard.py` swaps Y↔Z. If you see wrong letters, check `settings.language` (older installs: `settings.keyboard_layout`, which is migrated).

### Keyboard shows disconnected in pixdcon home scene (gray dots)

1. Keyboard may be sleeping (BT power save). Press any key to wake it.
2. Check MQTT retained message:
   ```bash
   mosquitto_sub -h hsb1.lan -u smarthome -P '***' -t 'home/hsb1/funkeykid/keyboard-info' -W 2 -v
   ```
3. If `connected: false` but keyboard works, the connection callback may not have fired. Restart funkeykid.

---

## Display (Pixoo)

### Letter not showing on Pixoo

1. **Check MQTT connection**: Container logs should show `[display] MQTT connected`
2. **Check pixdcon is running**: `docker ps | grep pixdcon`. `pixdcon2-edge` drives the displays; v1 `pixdcon` is the fallback.
3. **Check pixdcon scene**: pixoo-189 must run the `funkeykid` scene. Check it in the pixdcon2-edge UI at http://hsb1.lan:8082; its config is `~/docker/mounts/pixdcon2-edge/config.json` and belongs to the pixdcon project.
4. **Check MQTT message arrives**:
   ```bash
   mosquitto_sub -h hsb1.lan -u smarthome -P '***' -t 'home/hsb1/funkeykid/display' -v
   ```
   Then press a key — should see the JSON payload.

### Keyboard status dots not showing in home scene

1. Check retained MQTT message exists (see keyboard section above)
2. Check pixdcon logs for subscription: `docker logs pixdcon2-edge 2>&1 | grep keyboard-info`
3. The home scene must be running on pixoo-159

### Images look wrong / old images showing

pixdcon's `loadPixooImage()` caches images per process (`lib/pixoo-image.js`), and a scene reload does not clear that cache. After updating images, restart the instance that drives pixoo-189: `ssh mba@hsb1.lan "docker restart pixdcon2-edge"`.

---

## Deployment

### Changes not taking effect after push

A push to main does **not** reach hsb1: it only publishes `:<commit-sha>`. hsb1 runs `:latest`, which moves only with a release.

1. **Released?** Follow DEPLOY.md § Release procedure: reserve the version, merge, then tag `v<version>`. Check that the release job is green: `gh run list --limit 3`.
2. **Rolled?** Use the locked single-service roll in DEPLOY.md § Normal (pull, dry run, `up -d --no-deps funkeykid`). A plain `docker restart` keeps the old image.
3. **Right version running?** `curl -s http://hsb1.lan:8081/api/version` must show the release's version, and the image digest must equal its `release-manifest.json`.

### Hotfix lost after update

`docker cp` changes live in the container layer. Every recreate starts from the image again and drops the hotfix: a roll, a nixcfg switch that changes the spec, or the Saturday `compose-hsb1-update.timer`. Always commit, push and release after hotfixing.

### Settings reset after update

Settings live in `/data/settings.json` (volume mount), NOT in the image. They survive container recreation. If settings reset: check the `settings.json` mount of the `funkeykid` service in nixcfg `hosts/hsb1/docker/compose-spec.nix`.

### Container exits with code 127 / PipeWire socket mount fails

**Symptom**: Container won't start (`docker ps -a` shows it Exited); the compose output or `journalctl -u compose-hsb1.service` shows an error like:
```
error mounting "/run/user/1001/pipewire-0": not a directory:
Are you trying to mount a directory onto a file (or vice-versa)?
```

**Cause**: PipeWire creates `/run/user/1001/pipewire-0` as a Unix socket. If Docker tries to mount it and the path doesn't exist yet, Docker creates a **directory** at that path instead. This blocks PipeWire from creating its socket on next start, killing audio for everything on the kiosk session (funkeykid, babycam/VLC, etc.).

**Fix**: Do NOT mount `pipewire-0` directly (nixcfg `compose-spec.nix`). Audio goes through PulseAudio's compatibility socket at `/run/user/1001/pulse/native` (already mounted). If this already happened:

```bash
# Remove the stale directory Docker created
sudo rmdir /run/user/1001/pipewire-0

# Restart PipeWire + PulseAudio for the kiosk user
sudo systemctl --user -M kiosk@ restart pipewire.socket pipewire.service pipewire-pulse.socket pipewire-pulse.service

# Recreate the container: the locked `up -d --no-deps funkeykid` from DEPLOY.md § Normal (no pull needed)
```

### No sound after reboot (paplay: Connection refused)

**Symptom**: Container starts fine but `paplay` reports `Connection refused`. `pactl info` inside the container also fails.

**Cause**: Docker's bind mount of `/run/user/1001/pulse` creates the `pulse/` directory as **root** before the kiosk user's PipeWire session starts. When `pipewire-pulse.socket` tries to create its listening socket at `/run/user/1001/pulse/native`, it gets **Permission denied** because the parent directory is owned by root.

Check with: `sudo journalctl _UID=1001 --grep 'pipewire-pulse'` — look for `Failed to create listening socket: Permission denied`.

**Quick fix** (manual, does not survive reboot):
```bash
sudo systemctl --user -M kiosk@ restart pipewire-pulse.socket pipewire-pulse.service
docker restart funkeykid
```

**Permanent fix (applied 2026-04-11, now in nixcfg `compose-spec.nix`)**: The bind mount now points at the parent runtime directory so Docker never creates the `pulse/` child as root:
```yaml
- /run/user/1001:/run/user/1001
```
This relies on the kiosk user session (uid 1001) being up before Docker starts the container, so `/run/user/1001` already exists and Docker simply binds the existing tmpfs. If the container ever boots before the kiosk session, the old symptom can return — in that case bounce the container after the kiosk session is up.
