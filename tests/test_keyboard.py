"""Unit tests for the pure keyboard/payload helpers (FUNK-59, FUNK-58).

Run: python -m pytest tests/ (no evdev or hardware needed).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import time  # noqa: E402

from display import HEARTBEAT_SECONDS, keyboard_info_payload, offline_keyboard_payload  # noqa: E402
from keyboard import (  # noqa: E402
    KeyboardListener,
    is_battery_low,
    parse_bt_battery,
    primary_node,
    select_keyboard_nodes,
)

# Opaque stand-ins: real values are Bluetooth addresses, which never go into the repo.
BT_UNIQ = "test-bt-device-1"
OTHER_UNIQ = "test-bt-device-2"
ADAPTER = "test-bt-adapter"        # Bluetooth phys = the host adapter, shared by all BT devices


def node(path, name, phys="", uniq=""):
    return {"path": path, "name": name, "phys": phys, "uniq": uniq}


def paths(nodes):
    return [n["path"] for n in nodes]


def test_matches_all_suffix_nodes_of_bluetooth_keyboard():
    nodes = [
        node("/dev/input/event3", "Power Button"),
        node("/dev/input/event12", "ACME BK03", ADAPTER, BT_UNIQ),
        node("/dev/input/event13", "ACME BK03 Consumer Control", ADAPTER, BT_UNIQ),
        node("/dev/input/event14", "ACME BK03 System Control", ADAPTER, BT_UNIQ),
        node("/dev/input/event6", "Apple, Inc Apple Keyboard", "usb-0000:00:14.0-2/input0"),
    ]
    assert paths(select_keyboard_nodes(nodes, "ACME BK03")) == [
        "/dev/input/event12", "/dev/input/event13", "/dev/input/event14"]


def test_groups_differently_named_node_by_bluetooth_address():
    nodes = [
        node("/dev/input/event12", "ACME BK03", ADAPTER, BT_UNIQ),
        node("/dev/input/event15", "Vendor HID Extra", ADAPTER, BT_UNIQ),
    ]
    assert paths(select_keyboard_nodes(nodes, "ACME BK03")) == [
        "/dev/input/event12", "/dev/input/event15"]


def test_shared_bluetooth_adapter_phys_never_groups_other_devices():
    nodes = [
        node("/dev/input/event12", "ACME BK03", ADAPTER, BT_UNIQ),
        node("/dev/input/event20", "Some BT Mouse", ADAPTER, OTHER_UNIQ),
        node("/dev/input/event21", "Other BT Thing", ADAPTER, ""),
    ]
    assert paths(select_keyboard_nodes(nodes, "ACME BK03")) == ["/dev/input/event12"]


def test_groups_usb_interfaces_of_same_device_by_phys_parent():
    nodes = [
        node("/dev/input/event7", "Kids Board", "usb-0000:00:14.0-1/input0"),
        node("/dev/input/event8", "Kids Board Vendor Keys", "usb-0000:00:14.0-1/input1"),
        node("/dev/input/event9", "Unrelated", "usb-0000:00:14.0-1/input2"),
        node("/dev/input/event10", "Unrelated", "usb-0000:00:14.0-2/input0"),
    ]
    # event9 shares the USB parent, so it is the same physical device
    assert paths(select_keyboard_nodes(nodes, "Kids Board")) == [
        "/dev/input/event7", "/dev/input/event8", "/dev/input/event9"]


def test_prefix_requires_space_separator_and_empty_name_matches_nothing():
    nodes = [
        node("/dev/input/event12", "ACME BK03", ADAPTER, BT_UNIQ),
        node("/dev/input/event30", "ACME BK030", ADAPTER, OTHER_UNIQ),
    ]
    assert paths(select_keyboard_nodes(nodes, "ACME BK03")) == ["/dev/input/event12"]
    assert select_keyboard_nodes(nodes, "") == []
    assert select_keyboard_nodes(nodes, None) == []


def test_no_match_returns_empty():
    nodes = [node("/dev/input/event6", "Apple, Inc Apple Keyboard", "usb-0000:00:14.0-2/input0")]
    assert select_keyboard_nodes(nodes, "ACME BK03") == []


def test_primary_node_prefers_exact_name():
    nodes = [
        node("/dev/input/event13", "ACME BK03 Consumer Control"),
        node("/dev/input/event14", "ACME BK03"),
    ]
    assert primary_node(nodes, "ACME BK03")["path"] == "/dev/input/event14"
    assert primary_node(nodes[:1], "ACME BK03")["path"] == "/dev/input/event13"
    assert primary_node([], "ACME BK03") is None


def test_parse_bt_battery():
    text = "\tName: ACME BK03\n\tConnected: yes\n\tBattery Percentage: 0x55 (85)\n"
    assert parse_bt_battery(text) == 85
    assert parse_bt_battery("\tConnected: no\n") is None
    assert parse_bt_battery(None) is None


def test_is_battery_low():
    assert is_battery_low(14, 20) is True
    assert is_battery_low(20, 20) is True
    assert is_battery_low(21, 20) is False
    assert is_battery_low(5, 0) is False          # 0 disables the warning
    assert is_battery_low(None, 20) is False      # unknown level never warns
    assert is_battery_low(10, "15") is True       # tolerant of string settings
    assert is_battery_low(15, "junk") is True     # bad threshold → default 20


def test_keyboard_info_payload_shape():
    p = keyboard_info_payload({"connected": True, "exclusive": True, "battery_level": 14,
                               "battery_low": True, "device_name": "ACME BK03",
                               "nodes": [{"path": "/dev/input/event12"}]})
    assert p["online"] is True and p["connected"] is True and p["exclusive"] is True
    assert p["battery_level"] == 14 and p["battery_low"] is True
    assert p["device_name"] == "ACME BK03"
    assert p["heartbeat_s"] == HEARTBEAT_SECONDS and isinstance(p["timestamp"], float)
    assert "nodes" not in p  # payload stays small and stable


def _listener_with_recorded_restarts(name):
    kl = KeyboardListener(name)
    restarts = []

    def fake_restart(device_name=None):
        device_name = device_name or kl._desired_device
        restarts.append(device_name)
        kl.device_name = device_name

    kl.restart = fake_restart
    return kl, restarts


def _settle(seconds=0.3):
    time.sleep(seconds)


def test_set_device_latest_request_wins_while_restart_in_progress():
    """Round-2 finding 1: B then A again while a restart holds the lifecycle
    lock must end on A, not on the superseded B."""
    kl, restarts = _listener_with_recorded_restarts("ACME BK03")
    with kl._lifecycle:                 # a reconnect restart is in progress
        kl.set_device("Other Kid Board")
        kl.set_device("ACME BK03")
        _settle(0.1)
        assert restarts == []           # appliers are waiting for the lock
    _settle()
    assert kl.device_name == "ACME BK03"
    assert restarts == []               # the superseded switch never ran


def test_set_device_switches_and_is_idempotent():
    kl, restarts = _listener_with_recorded_restarts("ACME BK03")
    kl.set_device("Other Kid Board")
    _settle()
    assert restarts == ["Other Kid Board"] and kl.device_name == "Other Kid Board"
    kl.set_device("Other Kid Board")
    _settle()
    assert restarts == ["Other Kid Board"]


def test_offline_payload_shape_matches_live_payload():
    off = offline_keyboard_payload()
    live = keyboard_info_payload({})
    assert set(off) == set(live)
    assert off["online"] is False and off["connected"] is False and off["exclusive"] is False
    assert off["battery_level"] is None and off["battery_low"] is False
