"""Unit tests for the MQTT last-will contract (FUNK-59 review finding 4).

Run: python -m pytest tests/ (no broker needed; paho is faked).
"""
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import display  # noqa: E402


class FakeClient:
    def __init__(self):
        self.will = None
        self.published = []
        self.connected_to = None

    def username_pw_set(self, *args):
        pass

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, json.loads(payload), retain)

    def connect_async(self, host, port, keepalive):
        self.connected_to = (host, port)

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, json.loads(payload), retain))
        return SimpleNamespace(rc=0, wait_for_publish=lambda timeout=None: None)


def make_display(settings):
    clients = []

    def factory(*args):
        clients.append(FakeClient())
        return clients[-1]

    fake_mqtt = SimpleNamespace(Client=factory, CallbackAPIVersion=SimpleNamespace(VERSION2=2))
    return clients, fake_mqtt


def test_last_will_follows_topic_prefix_change():
    settings = {"mqtt": {"host": "broker.invalid", "user": "u", "password": "test-stub",
                         "topic_prefix": "old/funkeykid"}}
    clients, fake_mqtt = make_display(settings)
    with patch.object(display, "MQTT_AVAILABLE", True), patch.object(display, "mqtt", fake_mqtt, create=True):
        d = display.Display(settings)
        d.connect()
        d._mqtt_connected = True
        assert clients[0].will[0] == "old/funkeykid/keyboard-info"
        assert clients[0].will[1]["online"] is False and clients[0].will[2] is True

        # In-place mutation, exactly like api_put_settings does
        settings["mqtt"]["topic_prefix"] = "new/funkeykid"
        d.reload_settings(settings)

        # old retained status retired as offline on the OLD topic, then reconnect
        assert ("old/funkeykid/keyboard-info", display.offline_keyboard_payload(), True) in clients[0].published
        assert len(clients) == 2
        assert clients[1].will[0] == "new/funkeykid/keyboard-info"

        d._mqtt_connected = True
        d.publish_keyboard_status({"connected": True, "exclusive": True})
        topic, payload, retain = clients[1].published[-1]
        assert topic == clients[1].will[0] and payload["online"] is True and retain is True


def test_unchanged_settings_do_not_reconnect():
    settings = {"mqtt": {"host": "broker.invalid", "topic_prefix": "home/hsb1/funkeykid"}}
    clients, fake_mqtt = make_display(settings)
    with patch.object(display, "MQTT_AVAILABLE", True), patch.object(display, "mqtt", fake_mqtt, create=True):
        d = display.Display(settings)
        d.connect()
        d.reload_settings(dict(settings))  # UI autosave sends the same values
        assert len(clients) == 1
        assert d._topic("keyboard-info") == "home/hsb1/funkeykid/keyboard-info"
