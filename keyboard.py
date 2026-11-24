"""Keyboard listener — exclusive evdev grab of the configured keyboard (FUNK-59).

The configured keyboard is identified by its evdev name. One physical
Bluetooth keyboard usually registers several input nodes, e.g.
"ACME BK03", "ACME BK03 Consumer Control" and "ACME BK03 System Control";
the last one carries the sleep/power keys. The listener opens and EVIOCGRABs
every node of that device, so no key event reaches anything else on the host:
not X11/libinput, not logind (sleep/power), not the in-kernel console or SysRq.

Exclusive mode is always on, by design: the host must never act on the kids'
keyboard. Host-side udev isolation (nixcfg) stays as the second layer for the
time funkeykid is not running.
"""
import glob
import os
import re
import select
import subprocess
import threading
import time

try:
    import evdev
except ImportError:  # keeps the pure helpers importable on non-Linux hosts (tests)
    evdev = None


# Keyboard layouts: scancode → letter translation
LAYOUTS = {
    "us": {},  # Identity — evdev KEY_A = A
    "de": {
        # QWERTZ: physical Z (next to T) sends KEY_Y, physical Y sends KEY_Z
        "Y": "Z",
        "Z": "Y",
        # Special keys differ on German layout
        "RIGHTBRACE": "EQUAL",   # + key position
        "SLASH": "MINUS",        # - key position
    },
}

RESCAN_SECONDS = 1.0            # a (re)connected keyboard is grabbed within ~1 s
IDLE_POLL_SECONDS = 0.5         # scan interval while no node is open
SELECT_TIMEOUT_SECONDS = 0.25   # keeps stop/restart responsive
GRAB_RETRY_SECONDS = 5.0        # retry interval for a node another process holds
BATTERY_REFRESH_SECONDS = 60.0
RESTART_JOIN_SECONDS = 10.0     # the grab loop never blocks long; this is a safety cap

_MAC_RE = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")


# ── Pure helpers (unit-tested in tests/test_keyboard.py) ─────────────────────

def _read_text(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def scan_input_nodes():
    """List evdev nodes as dicts {path, name, phys, uniq}, read from sysfs."""
    nodes = []
    for path in sorted(glob.glob("/dev/input/event*")):
        base = f"/sys/class/input/{os.path.basename(path)}/device"
        node = {
            "path": path,
            "name": _read_text(base + "/name"),
            "phys": _read_text(base + "/phys") or "",
            "uniq": _read_text(base + "/uniq") or "",
        }
        if node["name"] is None and evdev is not None:
            try:
                dev = evdev.InputDevice(path)
                node.update(name=dev.name, phys=dev.phys or "", uniq=dev.uniq or "")
                dev.close()
            except OSError:
                continue
        if node["name"] is not None:
            nodes.append(node)
    return nodes


def _device_key(node):
    """Identity of the physical device a node belongs to, or None."""
    uniq = (node.get("uniq") or "").strip()
    if uniq:
        return "uniq:" + uniq.lower()
    phys = node.get("phys") or ""
    # USB phys looks like "usb-0000:00:14.0-1/input0"; the part before the last
    # "/" names the device. A Bluetooth phys is the host adapter's address and
    # is shared by every Bluetooth device, so it must never group nodes.
    if "/" in phys:
        return "phys:" + phys.rsplit("/", 1)[0]
    return None


def select_keyboard_nodes(nodes, device_name):
    """Return every node that belongs to the configured keyboard.

    A node matches by name: exactly ``device_name``, or ``device_name`` followed
    by a space and a suffix ("ACME BK03 System Control"). Nodes of the same
    physical device as a matching node (same Bluetooth address, or same USB
    parent) are included whatever their name.
    """
    if not device_name:
        return []

    def name_matches(node):
        name = node.get("name") or ""
        return name == device_name or name.startswith(device_name + " ")

    matched = [n for n in nodes if name_matches(n)]
    keys = {k for k in (_device_key(n) for n in matched) if k}
    for node in nodes:
        if node not in matched and _device_key(node) in keys:
            matched.append(node)
    return sorted(matched, key=lambda n: n["path"])


def primary_node(nodes, device_name):
    """The node named exactly ``device_name``, else the first one."""
    for node in nodes:
        if node.get("name") == device_name:
            return node
    return nodes[0] if nodes else None


def parse_bt_battery(text):
    """Battery percentage from `bluetoothctl info` output, or None."""
    m = re.search(r"Battery Percentage:\s*0x[0-9A-Fa-f]+\s*\((\d+)\)", text or "")
    return int(m.group(1)) if m else None


def is_battery_low(level, threshold):
    """True when a known battery level is at or below a positive threshold."""
    try:
        threshold = int(threshold)
    except (TypeError, ValueError):
        threshold = 20
    return threshold > 0 and level is not None and level <= threshold


# ── Listener ─────────────────────────────────────────────────────────────────

class KeyboardListener:
    """Exclusively owns the configured keyboard and applies layout mapping."""

    def __init__(self, device_name, layout="de", debounce_seconds=0.8):
        self.device_name = device_name
        self._desired_device = device_name  # latest requested device; set_device() converges to it
        self.layout = LAYOUTS.get(layout, {})
        self.debounce_seconds = debounce_seconds
        self._last_key_time = {}
        self._last_any_key_time = 0
        self._callback = None
        self._connection_callback = None
        self._status_callback = None
        self._running = False
        self._thread = None
        self._gen = 0
        self._lock = threading.Lock()         # guards live status + generation
        self._lifecycle = threading.RLock()   # serializes start/stop/restart
        self._grab_failed_at = {}
        self._shift_held = {}    # path -> {shift keycodes currently down}, from the event stream
        self._bt_address = None  # never exposed via status or diagnostics
        self._primary = None     # primary open node, read by the battery worker
        # Live status
        self.connected = False
        self.exclusive = False
        self.nodes = []          # [{path, name, open, grabbed}] for every matched node
        self.device_path = None
        self.last_key = None
        self.last_raw_key = None
        self.last_key_at = 0
        self.battery_level = None

    # ── public API ──────────────────────────────────────────────────────────

    def on_key(self, callback):
        """Register callback: callback(key_name: str, raw_key: str)"""
        self._callback = callback

    def on_connection_change(self, callback):
        """Register callback: callback(connected: bool, status: dict)"""
        self._connection_callback = callback

    def on_status_change(self, callback):
        """Register callback for non-connection changes: callback(status: dict)
        (exclusive flag, grabbed nodes, battery level)."""
        self._status_callback = callback

    def start(self):
        """Start listening in a background thread (no-op while one is running)."""
        with self._lifecycle:
            if self._running and self._thread is not None and self._thread.is_alive():
                return
            with self._lock:
                self._gen += 1
                gen = self._gen
                self._running = True
            listener = threading.Thread(target=self._run, args=(gen,), daemon=True,
                                        name=f"keyboard-{gen}")
            battery = threading.Thread(target=self._battery_loop, args=(gen,), daemon=True,
                                       name=f"keyboard-battery-{gen}")
            self._thread = listener
            listener.start()
            battery.start()

    def stop(self):
        """Stop the listener; its thread releases every grab on the way out.

        Status is cleared right away: until a new thread has re-grabbed, the
        listener must never claim to be connected or exclusive.
        """
        with self._lifecycle:
            with self._lock:
                self._gen += 1
                self._running = False
                was_connected = self.connected
                self.connected = False
                self.exclusive = False
                self.nodes = []
                self.device_path = None
                self.battery_level = None
                self._primary = None
            if was_connected and self._connection_callback:
                self._connection_callback(False, self.get_status())

    def set_device(self, device_name):
        """Request the listener to own ``device_name``; the latest request wins.

        Non-blocking: a background applier converges to the latest desired
        device under the lifecycle lock, so a superseded request (e.g. B then A
        again while a restart is running) never ends on the wrong device.
        """
        with self._lock:
            if device_name == self._desired_device and device_name == self.device_name:
                return
            self._desired_device = device_name
        threading.Thread(target=self._apply_desired_device, daemon=True,
                         name="keyboard-set-device").start()

    def _apply_desired_device(self):
        with self._lifecycle:
            desired = self._desired_device
            if desired and desired != self.device_name:
                self.restart(desired)

    def restart(self, device_name=None):
        """Stop, wait until the old thread has released its grabs, start again.

        The whole sequence is serialized, so concurrent restarts (reconnect
        button + settings change) cannot interleave. Without a name it restarts
        on the latest desired device.
        """
        with self._lifecycle:
            device_name = device_name or self._desired_device
            if device_name:
                self.device_name = device_name
            old = self._thread
            self.stop()
            if old is not None:
                deadline = time.monotonic() + RESTART_JOIN_SECONDS
                while old.is_alive() and time.monotonic() < deadline:
                    old.join(timeout=0.5)
                if old.is_alive():
                    print("[keyboard] WARNING: previous listener still running after "
                          f"{RESTART_JOIN_SECONDS:.0f} s; starting anyway (grabs retry)", flush=True)
            self._bt_address = None
            self._shift_held = {}
            self._grab_failed_at = {}
            self.start()

    def get_status(self):
        """Return current keyboard status for the web UI and MQTT."""
        grabbed = sum(1 for n in self.nodes if n["grabbed"])
        return {
            "connected": self.connected,
            "exclusive": self.exclusive,
            "grabbed": grabbed,
            "nodes": [dict(n) for n in self.nodes],
            "device_name": self.device_name,
            "device_path": self.device_path,
            "last_key": self.last_key,
            "last_raw_key": self.last_raw_key,
            "last_key_at": self.last_key_at,
            "battery_level": self.battery_level,
            "searching": self._running and not self.connected,
        }

    def get_diagnostics(self):
        """Return detailed diagnostics for troubleshooting (no MAC addresses)."""
        diag = {"evdev_devices": [], "bluetooth": {}, "logs": []}

        try:
            nodes = scan_input_nodes()
            mine = {n["path"] for n in select_keyboard_nodes(nodes, self.device_name)}
            grabbed = {n["path"] for n in self.nodes if n["grabbed"]}
            for node in nodes:
                diag["evdev_devices"].append({
                    "path": node["path"],
                    "name": node["name"],
                    "matched": node["path"] in mine,
                    "grabbed": node["path"] in grabbed,
                })
        except Exception as e:
            diag["logs"].append(f"evdev scan error: {e}")

        address = self._bt_address or self._find_bt_address_by_name()
        if address:
            text = self._bluetoothctl("info", address, logs=diag["logs"])
            for line in (text or "").splitlines():
                line = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
                if not line or line.startswith(("[CHG]", "[NEW]", "[DEL]", "Device ")):
                    continue
                if _MAC_RE.search(line) or ":" not in line:
                    continue
                k, _, v = line.replace("\t", "").partition(":")
                k, v = k.strip(), v.strip()
                if k == "UUID" and "UUID" in diag["bluetooth"]:
                    continue
                diag["bluetooth"][k] = v
        else:
            diag["logs"].append(f"bluetoothctl: kein Gerät namens '{self.device_name}' bekannt")

        try:
            diag["dev_input_files"] = sorted(os.listdir("/dev/input"))
        except Exception as e:
            diag["logs"].append(f"/dev/input error: {e}")

        diag["logs"].append(f"Thread alive: {self._thread.is_alive() if self._thread else False}")
        diag["logs"].append(f"Running flag: {self._running}")
        diag["logs"].append(f"Exklusiv: {self.exclusive} "
                            f"({sum(1 for n in self.nodes if n['grabbed'])}/{len(self.nodes)} Knoten)")
        diag["logs"].append(f"Layout: {self.layout}")
        return diag

    def simulate_key(self, key_name):
        """Simulate a keypress (for web UI test mode)."""
        if self._callback:
            self._callback(key_name, key_name)

    # ── internals ───────────────────────────────────────────────────────────

    def _bluetoothctl(self, *args, logs=None):
        try:
            result = subprocess.run(["bluetoothctl", *args], capture_output=True,
                                    text=True, timeout=5)
            if result.returncode == 0:
                return result.stdout
            if logs is not None:
                logs.append("bluetoothctl: nicht verfuegbar")
        except FileNotFoundError:
            if logs is not None:
                logs.append("bluetoothctl: nicht installiert")
        except Exception as e:
            if logs is not None:
                logs.append(f"bluetoothctl: {e}")
        return None

    def _find_bt_address_by_name(self):
        text = self._bluetoothctl("devices") or ""
        for line in text.splitlines():
            parts = line.strip().split(" ", 2)
            if len(parts) == 3 and parts[0] == "Device" and parts[2] == self.device_name:
                return parts[1]
        return None

    def _read_battery(self, node):
        """Battery level of the keyboard: kernel HID battery, else BlueZ."""
        uniq = (node or {}).get("uniq", "")
        candidates = []
        for u in dict.fromkeys(v for v in (uniq, uniq.lower()) if v):
            candidates.append(f"/sys/class/power_supply/hid-{u}-battery/capacity")
        if node:
            ev = os.path.basename(node["path"])
            candidates += glob.glob(f"/sys/class/input/{ev}/device/device/power_supply/*/capacity")
        for path in candidates:
            value = _read_text(path)
            if value and value.isdigit():
                return int(value)
        if self._bt_address:
            return parse_bt_battery(self._bluetoothctl("info", self._bt_address))
        return None

    def _set_status(self, gen, open_devs, grabbed, wanted):
        """Update live status; fire callbacks only from the current thread.

        Exclusive means EVERY matched node is open and grabbed: a node we could
        not open (or grab) still delivers its keys to the host.
        """
        with self._lock:
            if gen != self._gen:
                return
            prev = (self.connected, self.exclusive,
                    tuple((n["path"], n["open"], n["grabbed"]) for n in self.nodes))
            self.connected = bool(open_devs)
            self.exclusive = (self.connected and bool(wanted)
                              and all(p in open_devs and p in grabbed for p in wanted))
            self.nodes = [{"path": p, "name": (wanted[p] or {}).get("name", ""),
                           "open": p in open_devs, "grabbed": p in grabbed}
                          for p in sorted(wanted)]
            prim = primary_node([wanted[p] for p in sorted(wanted) if p in open_devs], self.device_name)
            self._primary = prim
            self.device_path = prim["path"] if prim else None
            if not self.connected:
                self.battery_level = None
            now = (self.connected, self.exclusive,
                   tuple((n["path"], n["open"], n["grabbed"]) for n in self.nodes))
        if now == prev:
            return
        if now[0] != prev[0]:
            if self._connection_callback:
                self._connection_callback(self.connected, self.get_status())
        elif self._status_callback:
            self._status_callback(self.get_status())

    def _battery_loop(self, gen):
        """Battery reads can block (bluetoothctl, up to 5 s), so they run apart
        from the grab loop and never delay grabbing or a restart."""
        last = None
        while gen == self._gen:
            prim = self._primary
            if self.connected and prim is not None:
                if last is None or time.monotonic() - last >= BATTERY_REFRESH_SECONDS:
                    last = time.monotonic()
                    try:
                        level = self._read_battery(prim)
                    except Exception as e:
                        print(f"[keyboard] Battery read failed: {e}", flush=True)
                        level = None
                    self._set_battery(gen, level)
            else:
                last = None
            time.sleep(0.5)

    def _set_battery(self, gen, level):
        with self._lock:
            if gen != self._gen or not self.connected or level == self.battery_level:
                return
            self.battery_level = level
        if self._status_callback:
            self._status_callback(self.get_status())

    def _sync_nodes(self, open_devs, grabbed):
        """Open and grab every node of the keyboard; drop vanished ones."""
        try:
            wanted = {n["path"]: n for n in select_keyboard_nodes(scan_input_nodes(), self.device_name)}
        except Exception as e:
            print(f"[keyboard] Error scanning devices: {e}", flush=True)
            wanted = {}
        for path in list(open_devs):
            if path not in wanted:
                self._close(open_devs.pop(path), path in grabbed)
                grabbed.discard(path)
                self._shift_held.pop(path, None)
                print(f"[keyboard] Node gone: {path}", flush=True)
        now = time.monotonic()
        for path, node in wanted.items():
            if path not in open_devs:
                try:
                    open_devs[path] = evdev.InputDevice(path)
                except OSError as e:
                    print(f"[keyboard] Cannot open {path} ({node['name']}): {e}", flush=True)
                    continue
            if path in grabbed:
                continue
            failed_at = self._grab_failed_at.get(path)
            if failed_at is not None and now - failed_at < GRAB_RETRY_SECONDS:
                continue
            try:
                open_devs[path].grab()
                grabbed.add(path)
                self._grab_failed_at.pop(path, None)
                print(f"[keyboard] Grabbed (exclusive): {node['name']} at {path}", flush=True)
            except OSError as e:
                self._grab_failed_at[path] = now
                print(f"[keyboard] WARNING: grab failed for {node['name']} at {path}: {e} "
                      f"— key presses can reach the host", flush=True)
        prim = primary_node(list(wanted.values()), self.device_name)
        uniq = (prim or {}).get("uniq", "")
        if uniq and _MAC_RE.fullmatch(uniq):
            self._bt_address = uniq
        return wanted

    @staticmethod
    def _close(dev, was_grabbed):
        try:
            if was_grabbed:
                dev.ungrab()
        except OSError:
            pass
        try:
            dev.close()
        except OSError:
            pass

    def _handle_event(self, dev, event):
        if event.type != evdev.ecodes.EV_KEY:
            return
        # Track Shift from the event stream: reading the live key state later
        # (active_keys) misses a Shift that is already released by then.
        if event.code in (evdev.ecodes.KEY_LEFTSHIFT, evdev.ecodes.KEY_RIGHTSHIFT):
            held = self._shift_held.setdefault(dev.path, set())
            if event.value:
                held.add(event.code)
            else:
                held.discard(event.code)
        key_event = evdev.categorize(event)
        if key_event.keystate != evdev.KeyEvent.key_down:
            return
        raw = key_event.keycode
        if isinstance(raw, (list, tuple)):
            raw = raw[0]
        raw = str(raw)
        if raw.startswith("KEY_"):
            raw = raw[4:]
        key = self.layout.get(raw, raw)

        # Shift+digit → SHIFT_<digit> (used for favorites)
        if key in ("1", "2", "3", "4", "5", "6", "7", "8", "9", "0") and self._shift_held.get(dev.path):
            key = "SHIFT_" + key

        self.last_key = key
        self.last_raw_key = raw
        self.last_key_at = time.time()
        if self._should_process(key) and self._callback:
            self._callback(key, raw)

    def _should_process(self, key_name):
        """Debounce check."""
        now = time.time()
        # Volume/space/replay keys bypass debounce
        if key_name in ('EQUAL', 'MINUS', 'KPPLUS', 'KPMINUS', 'SPACE', 'ENTER',
                        'RIGHT', 'LEFT', 'UP', 'DOWN'):
            return True
        if now - self._last_any_key_time < self.debounce_seconds:
            return False
        if key_name in self._last_key_time:
            if now - self._last_key_time[key_name] < self.debounce_seconds:
                return False
        self._last_key_time[key_name] = now
        self._last_any_key_time = now
        return True

    def _run(self, gen):
        """Main loop: keep every node of the keyboard open and grabbed."""
        open_devs = {}   # path -> evdev.InputDevice
        grabbed = set()
        wanted = {}
        last_scan = 0.0
        waiting_logged = False
        try:
            while gen == self._gen:
                now = time.monotonic()
                if not open_devs or now - last_scan >= RESCAN_SECONDS:
                    last_scan = now
                    had = bool(open_devs)
                    wanted = self._sync_nodes(open_devs, grabbed)
                    self._set_status(gen, open_devs, grabbed, wanted)
                    if not open_devs:
                        if not waiting_logged:
                            print(f"[keyboard] Waiting for '{self.device_name}'...", flush=True)
                            waiting_logged = True
                        time.sleep(IDLE_POLL_SECONDS)
                        continue
                    if not had:
                        waiting_logged = False
                        print(f"[keyboard] Opened '{self.device_name}': {len(open_devs)}/{len(wanted)} "
                              f"node(s), exclusive={self.exclusive}", flush=True)

                try:
                    ready = select.select(list(open_devs.values()), [], [], SELECT_TIMEOUT_SECONDS)[0]
                except (OSError, ValueError):
                    ready = []
                    last_scan = 0.0
                for dev in ready:
                    try:
                        for event in dev.read():
                            if gen != self._gen:
                                break
                            try:
                                self._handle_event(dev, event)
                            except Exception as e:  # never let one odd event kill the grab
                                print(f"[keyboard] Event error on {dev.path}: {e}", flush=True)
                    except BlockingIOError:
                        continue
                    except OSError as e:
                        print(f"[keyboard] Device error on {dev.path}: {e}", flush=True)
                        path = dev.path
                        self._close(open_devs.pop(path, dev), path in grabbed)
                        grabbed.discard(path)
                        self._shift_held.pop(path, None)
                        last_scan = 0.0
        finally:
            for path, dev in list(open_devs.items()):
                self._close(dev, path in grabbed)
            open_devs.clear()
            grabbed.clear()
            self._set_status(gen, open_devs, grabbed, {})
