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
   docker inspect funkeykid --format '{{json .Config.Env}}' | grep PULSE
   # Must show: PULSE_SERVER=unix:/run/user/1001/pulse/native
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
   ssh mba@hsb1.lan "bluetoothctl info 20:73:00:04:21:4F"
   # Look for "Connected: yes"
   ```
2. **Manually reconnect**:
   ```bash
   ssh mba@hsb1.lan "bluetoothctl connect 20:73:00:04:21:4F"
   ```
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

Keyboard layout is set to `"de"` (QWERTZ) in settings.json. evdev reports US scancodes; `keyboard.py` maps Y↔Z. If you see wrong letters, check `settings.keyboard_layout`.

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
2. **Check pixdcon is running**: `docker ps | grep pixdcon`
3. **Check pixdcon scene**: pixoo-189 must run the `funkeykid` scene (check config.json)
4. **Check MQTT message arrives**:
   ```bash
   mosquitto_sub -h hsb1.lan -u smarthome -P '***' -t 'home/hsb1/funkeykid/display' -v
   ```
   Then press a key — should see the JSON payload.

### Keyboard status dots not showing in home scene

1. Check retained MQTT message exists (see keyboard section above)
2. Check pixdcon logs for subscription: `grep keyboard-info` in `docker logs pixdcon`
3. The home scene must be running on pixoo-159

### Images look wrong / old images showing

pixdcon's `loadPixooImage()` caches images at scene init. After updating images:
- Restart pixdcon: `docker compose restart pixdcon`
- Or trigger scene reload by touching the scene file

---

## Deployment

### Changes not taking effect after push

1. **CI built?**: `gh run list --limit 1` — check it's green
2. **Image pulled?**: `docker compose pull funkeykid` on hsb1
3. **Container recreated?**: `docker compose up -d funkeykid` (not just `restart`)
4. **Correct files in image?**: `docker exec funkeykid cat /app/server.py | head -5`

### Hotfix lost after update

`docker cp` changes live in the container layer. `docker compose up -d` recreates from the image, losing hotfixes. Always commit + push after hotfixing.

### Settings reset after update

Settings live in `/data/settings.json` (volume mount), NOT in the image. They survive container recreation. If settings reset: check the volume mount is correct in docker-compose.yml.

### Container exits with code 127 / PipeWire socket mount fails

**Symptom**: Container won't start, `docker inspect` shows error like:
```
error mounting "/run/user/1001/pipewire-0": not a directory:
Are you trying to mount a directory onto a file (or vice-versa)?
```

**Cause**: PipeWire creates `/run/user/1001/pipewire-0` as a Unix socket. If Docker tries to mount it and the path doesn't exist yet, Docker creates a **directory** at that path instead. This blocks PipeWire from creating its socket on next start, killing audio for everything on the kiosk session (funkeykid, babycam/VLC, etc.).

**Fix**: Do NOT mount `pipewire-0` directly in docker-compose.yml. Audio goes through PulseAudio's compatibility socket at `/run/user/1001/pulse/native` (already mounted). If this already happened:

```bash
# Remove the stale directory Docker created
sudo rmdir /run/user/1001/pipewire-0

# Restart PipeWire + PulseAudio for the kiosk user
sudo systemctl --user -M kiosk@ restart pipewire.socket pipewire.service pipewire-pulse.socket pipewire-pulse.service

# Recreate the container
cd ~/docker && docker compose up -d funkeykid
```

### No sound after reboot (paplay: Connection refused)

**Symptom**: Container starts fine but `paplay` reports `Connection refused`. `pactl info` inside the container also fails.

**Cause**: Docker's bind mount of `/run/user/1001/pulse` creates the `pulse/` directory as **root** before the kiosk user's PipeWire session starts. When `pipewire-pulse.socket` tries to create its listening socket at `/run/user/1001/pulse/native`, it gets **Permission denied** because the parent directory is owned by root.

Check with: `sudo journalctl _UID=1001 --grep 'pipewire-pulse'` — look for `Failed to create listening socket: Permission denied`.

**Quick fix** (manual, does not survive reboot):
```bash
sudo systemctl --user -M kiosk@ restart pipewire-pulse.socket pipewire-pulse.service
cd ~/docker && docker compose restart funkeykid
```

**Permanent fix (applied 2026-04-11)**: The bind mount in `~/docker/docker-compose.yml` on hsb1 now points at the parent runtime directory so Docker never creates the `pulse/` child as root:
```yaml
- /run/user/1001:/run/user/1001
```
This relies on the kiosk user session (uid 1001) being up before Docker starts the container, so `/run/user/1001` already exists and Docker simply binds the existing tmpfs. If the container ever boots before the kiosk session, the old symptom can return — in that case bounce the container after the kiosk session is up.
