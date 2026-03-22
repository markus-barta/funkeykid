"""Display output — MQTT publish and optional direct Pixoo HTTP."""
import json
import os
import time
import random
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


class Display:
    """Manages MQTT publishing and optional direct Pixoo control."""

    def __init__(self, settings):
        self.settings = settings
        self._mqtt_client = None
        self._mqtt_connected = False

    def connect(self):
        """Connect MQTT if available."""
        if not MQTT_AVAILABLE:
            print("[display] paho-mqtt not installed, MQTT disabled", flush=True)
            return

        mqtt_cfg = self.settings.get("mqtt", {})
        host = mqtt_cfg.get("host") or os.environ.get("MOSQITTO_HOST_HSB1", "localhost")
        port = mqtt_cfg.get("port", 1883)
        user = mqtt_cfg.get("user") or os.environ.get("MOSQITTO_USER_HSB1", "smarthome")
        password = mqtt_cfg.get("password") or os.environ.get("MOSQITTO_PASS_HSB1")

        print(f"[display] MQTT config: host={host}, port={port}, user={user}, pass={'***' if password else 'EMPTY!'}", flush=True)
        try:
            self._mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            if password:
                self._mqtt_client.username_pw_set(user, password)
            else:
                print("[display] WARNING: No MQTT password!", flush=True)
            self._mqtt_client.on_connect = self._on_connect
            self._mqtt_client.on_disconnect = lambda c, u, f, rc, p=None: (
                setattr(self, '_mqtt_connected', False),
                print(f"[display] MQTT disconnected: rc={rc}", flush=True),
            )
            self._mqtt_client.connect_async(host, port, 60)
            self._mqtt_client.loop_start()
            print(f"[display] MQTT connecting...", flush=True)
        except Exception as e:
            print(f"[display] MQTT failed: {e}", flush=True)

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        self._mqtt_connected = True
        print("[display] MQTT connected", flush=True)

    def reload_settings(self, settings):
        self.settings = settings

    def _topic(self, suffix):
        prefix = self.settings.get("mqtt", {}).get("topic_prefix", "home/hsb1/funkeykid")
        return f"{prefix}/{suffix}"

    def publish_letter(self, letter, word, image=None):
        """Publish letter press to display(s)."""
        color = random.choice(COLORS)
        mode = self.settings.get("display_mode", "mqtt")

        if mode in ("mqtt", "both"):
            self._mqtt_publish_display(letter, word, image, color)

        if mode in ("direct", "both"):
            self._pixoo_direct(letter, word, image, color)

    def publish_volume(self, volume):
        """Show volume on display."""
        mode = self.settings.get("display_mode", "mqtt")
        if mode in ("mqtt", "both"):
            self._mqtt_publish(self._topic("display"), {
                "letter": f"{volume}%",
                "word": "lautstaerke",
                "color": "#FFCC00" if volume > 0 else "#FF0000",
                "timestamp": time.time(),
            })

    def log(self, message, level="info"):
        """Debug log via MQTT."""
        self._mqtt_publish(self._topic("debug"), {
            "timestamp": time.time(),
            "level": level,
            "message": message,
        })

    def _mqtt_publish_display(self, letter, word, image, color):
        self._mqtt_publish(self._topic("display"), {
            "letter": letter,
            "word": word,
            "image": image,
            "color": color,
            "timestamp": time.time(),
        })

    def _mqtt_publish(self, topic, payload):
        if self._mqtt_client and self._mqtt_connected:
            try:
                result = self._mqtt_client.publish(topic, json.dumps(payload), qos=0)
                if result.rc != 0:
                    print(f"[display] MQTT publish failed: rc={result.rc} topic={topic}", flush=True)
            except Exception as e:
                print(f"[display] MQTT error: {e}", flush=True)
        else:
            print(f"[display] MQTT not ready: client={self._mqtt_client is not None}, connected={self._mqtt_connected}", flush=True)

    def _pixoo_direct(self, letter, word, image, color):
        """Send frame directly to Pixoo HTTP API."""
        pixoo_ip = self.settings.get("pixoo_ip")
        if not pixoo_ip:
            return
        # TODO: Implement direct Pixoo rendering (64x64 pixel buffer)
        # For now, just log
        print(f"[display] Direct Pixoo: {letter} ({word}) → {pixoo_ip}", flush=True)

    def disconnect(self):
        if self._mqtt_client:
            self._mqtt_client.loop_stop()
            self._mqtt_client.disconnect()
