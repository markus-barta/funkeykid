"""Display output — MQTT publish and optional direct Pixoo HTTP.

MQTT contract for `<prefix>/keyboard-info` (retained, qos 1; prefix defaults
to `home/hsb1/funkeykid`). Consumers: pixdcon's funkeykid start screen and the
home scene's keyboard dot.

    {
      "online": true,            # false = funkeykid is gone (MQTT last-will)
      "connected": true,         # the configured keyboard has open evdev nodes
      "exclusive": true,         # every node is EVIOCGRAB-bed; false = keys can reach the host
      "battery_level": 80,       # percent, null when the keyboard reports none
      "battery_low": false,      # battery_level <= settings.battery_low_threshold (0 = off)
      "device_name": "ACME BK03",
      "heartbeat_s": 10,         # re-published this often; treat 3 missed beats as offline
      "timestamp": 1764000000.0
    }

The last-will publishes the same shape with online=false and everything else
false/null, so a crash or network loss flips the retained message by itself.
"""
import json
import os
import time
import random
import threading
import base64
import struct
import urllib.request

try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False


COLORS = [
    "#FF0000", "#00CC00", "#0066FF", "#FF6600", "#CC00CC",
    "#00CCCC", "#FFCC00", "#FF3399", "#6633FF", "#33CC33",
]

HEARTBEAT_SECONDS = 10


def offline_keyboard_payload():
    """keyboard-info payload published as MQTT last-will (funkeykid gone)."""
    return {
        "online": False,
        "connected": False,
        "exclusive": False,
        "battery_level": None,
        "battery_low": False,
        "device_name": None,
        "heartbeat_s": HEARTBEAT_SECONDS,
        "timestamp": None,
    }


def keyboard_info_payload(status):
    """keyboard-info payload for a live funkeykid (see module docstring)."""
    return {
        "online": True,
        "connected": bool(status.get("connected", False)),
        "exclusive": bool(status.get("exclusive", False)),
        "battery_level": status.get("battery_level"),
        "battery_low": bool(status.get("battery_low", False)),
        "device_name": status.get("device_name"),
        "heartbeat_s": HEARTBEAT_SECONDS,
        "timestamp": time.time(),
    }


class Display:
    """Manages MQTT publishing and optional direct Pixoo control."""

    def __init__(self, settings):
        self.settings = settings
        self._mqtt_client = None
        self._mqtt_connected = False
        self._conn_identity = None  # (host, port, user, password, prefix) of the live connection
        self._prefix = None         # topic prefix frozen per connection (matches the last-will)
        self._closing = False       # set while retiring a connection: no publish may follow the offline message
        self._pub_lock = threading.Lock()        # orders every publish against retire/reconnect
        self._reconnect_lock = threading.Lock()  # one reconnect at a time
        self.on_connected = None  # called (from the MQTT thread) after every (re)connect

    def _mqtt_identity(self):
        mqtt_cfg = self.settings.get("mqtt", {}) or {}
        return (
            mqtt_cfg.get("host") or os.environ.get("MOSQITTO_HOST_HSB1", "localhost"),
            mqtt_cfg.get("port", 1883),
            mqtt_cfg.get("user") or os.environ.get("MOSQITTO_USER_HSB1", "smarthome"),
            mqtt_cfg.get("password") or os.environ.get("MOSQITTO_PASS_HSB1"),
            mqtt_cfg.get("topic_prefix", "home/hsb1/funkeykid"),
        )

    def connect(self):
        """Connect MQTT if available."""
        if not MQTT_AVAILABLE:
            print("[display] paho-mqtt not installed, MQTT disabled", flush=True)
            return

        identity = self._mqtt_identity()
        host, port, user, password, prefix = identity

        print(f"[display] MQTT config: host={host}, port={port}, user={user}, pass={'***' if password else 'EMPTY!'}, prefix={prefix}", flush=True)
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            if password:
                client.username_pw_set(user, password)
            else:
                print("[display] WARNING: No MQTT password!", flush=True)
            # Last-will: the broker flips the retained keyboard-info to offline
            # when funkeykid dies or loses the network (FUNK-59/FUNK-58).
            client.will_set(
                f"{prefix}/keyboard-info",
                json.dumps(offline_keyboard_payload()),
                qos=1,
                retain=True,
            )
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            # Everything published on this connection uses the prefix its
            # last-will was registered with; a settings change reconnects.
            with self._pub_lock:
                self._conn_identity = identity
                self._prefix = prefix
                self._mqtt_client = client
                self._mqtt_connected = False
                self._closing = False
            client.connect_async(host, port, 60)
            client.loop_start()
            print(f"[display] MQTT connecting...", flush=True)
        except Exception as e:
            print(f"[display] MQTT failed: {e}", flush=True)

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if client is not self._mqtt_client:  # late callback from a retired client
            return
        self._mqtt_connected = True
        print("[display] MQTT connected", flush=True)
        if self.on_connected:
            try:
                self.on_connected()
            except Exception as e:
                print(f"[display] on_connected hook failed: {e}", flush=True)

    def _on_disconnect(self, client, userdata, flags, rc, properties=None):
        if client is not self._mqtt_client:
            return
        self._mqtt_connected = False
        print(f"[display] MQTT disconnected: rc={rc}", flush=True)

    def reload_settings(self, settings):
        self.settings = settings
        # The last-will is fixed per connection. If broker or prefix changed,
        # retire the old retained status (offline on the old topic) and
        # reconnect with a last-will on the new topic. Runs off the caller's
        # thread: this is called from the asyncio loop, and teardown may wait.
        # Compare with the live connection's identity, not with whether a client
        # exists: a change arriving mid-reconnect must still be honoured.
        if self._conn_identity is not None and self._mqtt_identity() != self._conn_identity:
            threading.Thread(target=self._reconnect, daemon=True, name="mqtt-reconnect").start()

    def _reconnect(self):
        """Serialized: reconnect until the live connection matches the latest
        settings (a change can arrive while connect() is still running)."""
        with self._reconnect_lock:
            for _ in range(5):
                if self._conn_identity is None or self._mqtt_identity() == self._conn_identity:
                    return
                print("[display] MQTT settings changed — reconnecting with a new last-will", flush=True)
                self.disconnect()
                self.connect()
            print("[display] WARNING: MQTT settings kept changing; giving up after 5 reconnects", flush=True)

    def _topic(self, suffix):
        # `is not None`: an empty prefix is a real prefix and must stay the
        # connection's topic (and last-will) until it is retired.
        prefix = self._prefix if self._prefix is not None else self._mqtt_identity()[4]
        return f"{prefix}/{suffix}"

    def publish_letter(self, letter, word, image=None, color=None):
        """Publish letter press to display(s). ``color`` fixes the text colour (quiz, FUNK-39)."""
        color = color or random.choice(COLORS)
        mode = self.settings.get("display_mode", "mqtt")

        if mode in ("mqtt", "both"):
            self._mqtt_publish_display(letter, word, image, color)

        if mode in ("direct", "both"):
            self._pixoo_direct(letter, word, image, color)

    def publish_volume(self, volume):
        """Show volume on display.

        Emits both the legacy letter/word fields (text fallback) and
        structured `bar`/`percent` metadata so a scene can render a
        bar-graph visualization instead of plain text.
        """
        mode = self.settings.get("display_mode", "mqtt")
        if mode in ("mqtt", "both"):
            # Clamp 0-100 for the bar percentage (some volumes can transiently go higher).
            bar_pct = max(0, min(100, int(volume)))
            self._mqtt_publish("display", {
                "letter": f"{volume}%",
                "word": "lautstaerke",
                "color": "#FFCC00" if volume > 0 else "#FF0000",
                "bar": True,
                "percent": bar_pct,
                "bars_total": 10,
                "bars_filled": (bar_pct + 5) // 10,  # round to nearest 10%
                "timestamp": time.time(),
            })

    def publish_keyboard_status(self, status):
        """Publish keyboard status to MQTT (retained); also the heartbeat."""
        self._mqtt_publish("keyboard-info", keyboard_info_payload(status),
                           qos=1, retain=True, quiet=True)

    def log(self, message, level="info"):
        """Debug log via MQTT."""
        self._mqtt_publish("debug", {
            "timestamp": time.time(),
            "level": level,
            "message": message,
        })

    def _mqtt_publish_display(self, letter, word, image, color):
        self._mqtt_publish("display", {
            "letter": letter,
            "word": word,
            "image": image,
            "color": color,
            "timestamp": time.time(),
        })

    def _mqtt_publish(self, suffix, payload, qos=0, retain=False, quiet=False):
        """The only publish path. Topic and client are resolved under the lock,
        so nothing is published on a connection being retired (after its
        offline message) or on a stale prefix."""
        with self._pub_lock:
            client = self._mqtt_client
            if self._closing or client is None or not self._mqtt_connected:
                if not quiet and not self._closing:
                    print(f"[display] MQTT not ready: client={client is not None}, connected={self._mqtt_connected}", flush=True)
                return None
            topic = self._topic(suffix)
            try:
                result = client.publish(topic, json.dumps(payload), qos=qos, retain=retain)
                if result.rc != 0:
                    print(f"[display] MQTT publish failed: rc={result.rc} topic={topic}", flush=True)
                return result
            except Exception as e:
                print(f"[display] MQTT error on {topic}: {e}", flush=True)
                return None

    def _pixoo_direct(self, letter, word, image, color):
        """Send frame directly to Pixoo HTTP API."""
        pixoo_ip = self.settings.get("pixoo_ip")
        if not pixoo_ip:
            return
        # TODO: Implement direct Pixoo rendering (64x64 pixel buffer)
        # For now, just log
        print(f"[display] Direct Pixoo: {letter} ({word}) → {pixoo_ip}", flush=True)

    def disconnect(self):
        """Retire the connection: final offline status, then disconnect.

        A clean disconnect suppresses the last-will, so say goodbye ourselves.
        Under the publish lock the offline message is the LAST thing queued on
        this connection; messages are delivered in order, so the retained
        status ends offline. Then request the disconnect before joining Paho's
        network thread (loop_stop), so the join cannot hang.
        """
        info = None
        with self._pub_lock:
            client = self._mqtt_client
            if client is None:
                return
            self._closing = True
            if self._mqtt_connected:
                try:
                    info = client.publish(self._topic("keyboard-info"),
                                          json.dumps(offline_keyboard_payload()),
                                          qos=1, retain=True)
                except Exception as e:
                    print(f"[display] MQTT offline publish failed: {e}", flush=True)
        if info is not None:
            try:
                info.wait_for_publish(timeout=2)
            except Exception as e:
                print(f"[display] MQTT offline publish not confirmed: {e}", flush=True)
        try:
            client.disconnect()
        except Exception as e:
            print(f"[display] MQTT disconnect error: {e}", flush=True)
        try:
            client.loop_stop()
        except Exception as e:
            print(f"[display] MQTT loop_stop error: {e}", flush=True)
        with self._pub_lock:
            if self._mqtt_client is client:
                self._mqtt_client = None
                self._mqtt_connected = False
