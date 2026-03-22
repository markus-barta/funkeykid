"""Keyboard listener — evdev event loop with layout mapping."""
import evdev
import time
import threading
import os


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
            dev = evdev.InputDevice(path)
            if dev.name == self.device_name:
                return dev
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

    def _run(self):
        """Main event loop — retry on disconnect."""
        while self._running:
            device = self._find_device()
            if not device:
                print(f"[keyboard] Waiting for '{self.device_name}'...", flush=True)
                time.sleep(5)
                continue

            print(f"[keyboard] Opened: {device.name} at {device.path}", flush=True)
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

                    if self._should_process(key) and self._callback:
                        self._callback(key)

            except (OSError, Exception) as e:
                print(f"[keyboard] Device error: {e}", flush=True)
                time.sleep(5)

    def simulate_key(self, key_name):
        """Simulate a keypress (for web UI test mode)."""
        if self._callback:
            self._callback(key_name)
