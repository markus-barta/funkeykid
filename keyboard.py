"""Keyboard listener — evdev event loop with layout mapping."""
import evdev
import glob
import os
import random
import time
import threading


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


class KeyboardListener:
    """Listens to a dedicated keyboard via evdev, applies layout mapping."""

    def __init__(self, device_name, layout="de", debounce_seconds=0.8):
        self.device_name = device_name
        self.layout = LAYOUTS.get(layout, {})
        self.debounce_seconds = debounce_seconds
        self._last_key_time = {}
        self._last_any_key_time = 0
        self._callback = None
        self._running = False
        self._thread = None
        # Live status
        self.connected = False
        self.device_path = None
        self.last_key = None
        self.last_key_at = 0
        self.battery_level = None
        self._device = None

    def on_key(self, callback):
        """Register callback: callback(key_name: str)"""
        self._callback = callback

    def start(self):
        """Start listening in a background thread."""
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _find_device(self):
        """Find input device by name."""
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
                if dev.name == self.device_name:
                    return dev
            except (OSError, FileNotFoundError):
                continue
        return None

    def _translate_key(self, raw_key):
        """Apply keyboard layout mapping."""
        return self.layout.get(raw_key, raw_key)

    def _should_process(self, key_name):
        """Debounce check."""
        now = time.time()
        # Volume/space keys bypass debounce
        if key_name in ('EQUAL', 'MINUS', 'KPPLUS', 'KPMINUS', 'SPACE'):
            return True
        if now - self._last_any_key_time < self.debounce_seconds:
            return False
        if key_name in self._last_key_time:
            if now - self._last_key_time[key_name] < self.debounce_seconds:
                return False
        self._last_key_time[key_name] = now
        self._last_any_key_time = now
        return True

    def _read_battery(self, device):
        """Try to read battery level from sysfs for BT HID devices."""
        try:
            # Check common sysfs paths for BT device battery
            import glob
            patterns = [
                "/sys/class/power_supply/hid-*",
                "/sys/class/power_supply/*BK03*",
            ]
            for pattern in patterns:
                for path in glob.glob(pattern):
                    cap_file = os.path.join(path, "capacity")
                    if os.path.exists(cap_file):
                        with open(cap_file) as f:
                            return int(f.read().strip())
            # Also try device-specific path
            if hasattr(device, 'info'):
                bt_path = f"/sys/class/power_supply/hid-{device.info.bustype:02x}:{device.info.vendor:04x}:{device.info.product:04x}.*/capacity"
                for cap_file in glob.glob(bt_path):
                    with open(cap_file) as f:
                        return int(f.read().strip())
        except Exception:
            pass
        return None

    def get_status(self):
        """Return current keyboard status for the web UI."""
        return {
            "connected": self.connected,
            "device_name": self.device_name,
            "device_path": self.device_path,
            "last_key": self.last_key,
            "last_key_at": self.last_key_at,
            "battery_level": self.battery_level,
        }

    def _run(self):
        """Main event loop — retry on disconnect. Never exits unless stopped."""
        while self._running:
            try:
                device = self._find_device()
            except Exception as e:
                print(f"[keyboard] Error scanning devices: {e}", flush=True)
                device = None

            if not device:
                self.connected = False
                self.device_path = None
                self._device = None
                print(f"[keyboard] Waiting for '{self.device_name}'...", flush=True)
                time.sleep(5)
                continue

            self._device = device
            self.connected = True
            self.device_path = device.path
            self.battery_level = self._read_battery(device)
            print(f"[keyboard] Opened: {device.name} at {device.path} (battery={self.battery_level})", flush=True)

            try:
                for event in device.read_loop():
                    if not self._running:
                        break
                    if event.type != evdev.ecodes.EV_KEY:
                        continue
                    key_event = evdev.categorize(event)
                    if key_event.keystate != evdev.KeyEvent.key_down:
                        continue

                    raw = key_event.keycode
                    if isinstance(raw, list):
                        raw = raw[0]
                    if raw.startswith("KEY_"):
                        raw = raw[4:]

                    # Apply layout
                    key = self._translate_key(raw)

                    # Track for status
                    self.last_key = key
                    self.last_key_at = time.time()

                    # Refresh battery periodically (every ~60 key presses)
                    if random.random() < 0.02:
                        self.battery_level = self._read_battery(device)

                    if self._should_process(key) and self._callback:
                        self._callback(key)

            except (OSError, Exception) as e:
                print(f"[keyboard] Device error: {e}", flush=True)
                self.connected = False
                self.device_path = None
                self._device = None
                time.sleep(5)

    def simulate_key(self, key_name):
        """Simulate a keypress (for web UI test mode)."""
        if self._callback:
            self._callback(key_name)
