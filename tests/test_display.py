"""Unit tests for the MQTT last-will contract (FUNK-59 review findings).

Run: python -m pytest tests/ (no broker needed; paho is faked).
"""
import json
import os
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import display  # noqa: E402


class FakeClient:
    def __init__(self, calls):
        self.will = None
        self.published = []
        self.calls = calls          # shared, ordered log of lifecycle calls
        self.loop_stop_delay = 0.0
        self.on_publish_hook = None

    def username_pw_set(self, *args):
        pass

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, json.loads(payload), retain)

    def connect_async(self, host, port, keepalive):
        self.calls.append("connect")

    def loop_start(self):
        pass

    def loop_stop(self):
        self.calls.append("loop_stop")
        time.sleep(self.loop_stop_delay)

    def disconnect(self):
        self.calls.append("disconnect")

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, json.loads(payload), retain))
        if self.on_publish_hook:
            self.on_publish_hook(topic, json.loads(payload))
        return SimpleNamespace(rc=0, wait_for_publish=lambda timeout=None: None)


def fake_paho():
    clients, calls = [], []

    def factory(*args):
        clients.append(FakeClient(calls))
        return clients[-1]

    return clients, calls, SimpleNamespace(Client=factory, CallbackAPIVersion=SimpleNamespace(VERSION2=2))


def wait_for(pred, timeout=3.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.01)
    return False


def connected_display(settings, fake_mqtt):
    d = display.Display(settings)
    d.connect()
    d._mqtt_connected = True
    return d


def test_last_will_follows_topic_prefix_change():
    settings = {"mqtt": {"host": "broker.invalid", "user": "u", "password": "test-stub",
                         "topic_prefix": "old/funkeykid"}}
    clients, calls, fake_mqtt = fake_paho()
    with patch.object(display, "MQTT_AVAILABLE", True), patch.object(display, "mqtt", fake_mqtt, create=True):
        d = connected_display(settings, fake_mqtt)
        assert clients[0].will[0] == "old/funkeykid/keyboard-info"
        assert clients[0].will[1]["online"] is False and clients[0].will[2] is True

        settings["mqtt"]["topic_prefix"] = "new/funkeykid"   # in place, like api_put_settings
        d.reload_settings(settings)
        assert wait_for(lambda: len(clients) == 2)

        assert clients[0].published[-1] == ("old/funkeykid/keyboard-info", display.offline_keyboard_payload(), True)
        assert clients[1].will[0] == "new/funkeykid/keyboard-info"
        d._mqtt_connected = True
        d.publish_keyboard_status({"connected": True, "exclusive": True})
        topic, payload, retain = clients[1].published[-1]
        assert topic == clients[1].will[0] and payload["online"] is True and retain is True


def test_unchanged_settings_do_not_reconnect():
    settings = {"mqtt": {"host": "broker.invalid", "topic_prefix": "home/hsb1/funkeykid"}}
    clients, calls, fake_mqtt = fake_paho()
    with patch.object(display, "MQTT_AVAILABLE", True), patch.object(display, "mqtt", fake_mqtt, create=True):
        d = connected_display(settings, fake_mqtt)
        d.reload_settings(dict(settings))  # UI autosave sends the same values
        time.sleep(0.2)
        assert len(clients) == 1
        assert d._topic("keyboard-info") == "home/hsb1/funkeykid/keyboard-info"


def test_no_publish_lands_on_retired_connection_after_offline():
    """Round-2 finding 2: a heartbeat racing the retirement must not leave
    online=true retained on the old topic."""
    settings = {"mqtt": {"host": "broker.invalid", "topic_prefix": "old/funkeykid"}}
    clients, calls, fake_mqtt = fake_paho()
    with patch.object(display, "MQTT_AVAILABLE", True), patch.object(display, "mqtt", fake_mqtt, create=True):
        d = connected_display(settings, fake_mqtt)
        racers = []

        def race(topic, payload):
            if payload.get("online") is False:   # the offline (retirement) message
                t = threading.Thread(target=d.publish_keyboard_status,
                                     args=({"connected": True, "exclusive": True},))
                racers.append(t)
                t.start()
                time.sleep(0.05)                 # give the racer every chance to interleave

        clients[0].on_publish_hook = race
        settings["mqtt"]["topic_prefix"] = "new/funkeykid"
        d.reload_settings(settings)
        assert wait_for(lambda: len(clients) == 2)
        for t in racers:
            t.join(2)
        old_retained = [p for (tp, p, r) in clients[0].published if r]
        assert old_retained[-1]["online"] is False, old_retained


def test_settings_change_during_reconnect_is_not_lost():
    """Round-3 finding 1: a change (C) arriving while the reconnect to B is
    still building its client must end connected with C."""
    settings = {"mqtt": {"host": "broker.invalid", "topic_prefix": "a/funkeykid"}}
    clients, calls, fake_mqtt = fake_paho()
    real_factory = fake_mqtt.Client
    d_holder = {}

    def factory(*args):
        client = real_factory(*args)
        if len(clients) == 2:  # building B: C arrives right now
            settings["mqtt"]["topic_prefix"] = "c/funkeykid"
            d_holder["d"].reload_settings(settings)
        return client

    fake_mqtt.Client = factory
    with patch.object(display, "MQTT_AVAILABLE", True), patch.object(display, "mqtt", fake_mqtt, create=True):
        d = display.Display(settings)
        d_holder["d"] = d
        d.connect()
        d._mqtt_connected = True
        settings["mqtt"]["topic_prefix"] = "b/funkeykid"
        d.reload_settings(settings)
        assert wait_for(lambda: clients and clients[-1].will[0] == "c/funkeykid/keyboard-info", timeout=4)
        time.sleep(0.2)
        assert d._conn_identity[4] == "c/funkeykid"


def test_empty_prefix_is_retired_on_its_own_topic():
    """Round-3 finding 2: an empty prefix is a real prefix; retirement must
    publish offline on '/keyboard-info', not on the new prefix."""
    settings = {"mqtt": {"host": "broker.invalid", "topic_prefix": ""}}
    clients, calls, fake_mqtt = fake_paho()
    with patch.object(display, "MQTT_AVAILABLE", True), patch.object(display, "mqtt", fake_mqtt, create=True):
        d = connected_display(settings, fake_mqtt)
        assert clients[0].will[0] == "/keyboard-info"
        settings["mqtt"]["topic_prefix"] = "new"
        d.reload_settings(settings)
        assert wait_for(lambda: len(clients) == 2)
        assert clients[0].published[-1] == ("/keyboard-info", display.offline_keyboard_payload(), True)
        assert clients[1].will[0] == "new/keyboard-info"


def test_reconfigure_is_non_blocking_and_disconnects_before_loop_stop():
    """Round-2 finding 3: reload_settings runs on the asyncio loop and must not
    wait for Paho's network thread; disconnect() is requested before loop_stop()."""
    settings = {"mqtt": {"host": "broker.invalid", "topic_prefix": "old/funkeykid"}}
    clients, calls, fake_mqtt = fake_paho()
    with patch.object(display, "MQTT_AVAILABLE", True), patch.object(display, "mqtt", fake_mqtt, create=True):
        d = connected_display(settings, fake_mqtt)
        clients[0].loop_stop_delay = 1.5
        settings["mqtt"]["topic_prefix"] = "new/funkeykid"
        t0 = time.monotonic()
        d.reload_settings(settings)
        assert time.monotonic() - t0 < 0.2
        assert wait_for(lambda: len(clients) == 2, timeout=4)
        assert calls.index("disconnect") < calls.index("loop_stop")
