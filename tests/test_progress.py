"""Per-letter progress counters (FUNK-40): pure, no server needed."""
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import keymap  # noqa: E402
import progress  # noqa: E402


class Clock:
    def __init__(self, day="2026-11-27"):
        self.day = day

    def __call__(self):
        return self.day


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def p(clock):
    return progress.Progress(keymap.LETTER_TARGETS, today=clock)


def test_presses_count_per_letter_and_day(p, clock):
    assert p.press("A") and p.press("A") and p.press("ß") and p.press("7")
    clock.day = "2026-11-28"
    p.press("A")
    snap = p.snapshot()
    a = snap["letters"]["A"]
    assert (a["presses"], a["today"]["presses"], a["week"]["presses"], a["last"]) == (3, 1, 3, "2026-11-28")
    assert snap["letters"]["ß"]["presses"] == 1 and snap["letters"]["7"]["presses"] == 1
    assert snap["max_presses"] == 3
    assert p.export()[1]["days"] == {"2026-11-27": {"A": {"p": 2}, "ß": {"p": 1}, "7": {"p": 1}},
                                     "2026-11-28": {"A": {"p": 1}}}


@pytest.mark.parametrize("key", ["ENTER", "SHIFT_3", "SS", "a", "", None, "KP1"])
def test_other_keys_are_not_counted(p, key):
    assert p.press(key) is False and p.quiz_result(key, True, True) is False
    assert p.rev == 0 and p.snapshot()["letters"] == {}


def test_quiz_results_and_the_learned_streak(p):
    assert p.snapshot()["mastery"] == progress.DEFAULT_MASTERY == 3
    for right, first in [(True, True), (True, True), (True, False), (True, True), (True, True)]:
        p.quiz_result("B", right, first)
    b = p.snapshot()["letters"]["B"]
    assert (b["asked"], b["right"], b["first"], b["streak"], b["learned"]) == (5, 5, 4, 2, False)
    p.quiz_result("B", True, True)
    assert p.snapshot()["letters"]["B"]["learned"] is True
    p.quiz_result("B", False, False)               # revealed: the badge goes until it is earned again
    b = p.snapshot()["letters"]["B"]
    assert (b["asked"], b["right"], b["streak"], b["learned"]) == (7, 6, 0, False)
    assert b["presses"] == 0 and b["today"] == {"presses": 0, "asked": 7, "right": 6, "first": 5}


def test_mastery_is_configurable(p):
    p.quiz_result("C", True, True)
    assert not p.snapshot()["letters"]["C"]["learned"]
    rev = p.rev
    p.set_mastery(1)
    assert p.rev == rev + 1 and p.snapshot()["letters"]["C"]["learned"]
    p.set_mastery(1)
    assert p.rev == rev + 1                         # no change, no new revision
    for bad in (0, 11, "3", 2.5, 5.0, True, None, [5]):
        with pytest.raises(progress.ProgressError):
            p.set_mastery(bad)
    assert p.mastery == 1


def test_reset_keeps_the_mastery_setting(p, clock):
    p.set_mastery(5)
    p.press("A")
    p.quiz_result("A", True, True)
    clock.day = "2026-12-01"
    rev = p.rev
    p.reset()
    snap = p.snapshot()
    assert snap["letters"] == {} and snap["mastery"] == 5 and snap["since"] == "2026-12-01"
    assert p.rev == rev + 1 and p.export()[1]["days"] == {}


def test_only_the_last_days_are_kept(p, clock):
    for day in range(1, 32):
        clock.day = f"2026-12-{day:02d}"
        p.press("A")
    days = p.export()[1]["days"]
    assert len(days) == progress.DAYS_KEPT and min(days) == "2026-12-02" and max(days) == "2026-12-31"
    a = p.snapshot()["letters"]["A"]
    assert a["presses"] == 31 and a["week"]["presses"] == 7    # totals keep every press


def test_export_load_round_trip_through_json(p, clock):
    p.set_mastery(4)
    p.press("Ü")
    p.quiz_result("Ü", True, True)
    rev, data = p.export()
    q = progress.Progress(keymap.LETTER_TARGETS, today=clock)
    q.load(json.loads(json.dumps(data)))
    assert q.export()[1] == data and q.snapshot()["letters"] == p.snapshot()["letters"]


def test_export_is_a_copy(p):
    p.press("A")
    _, data = p.export()
    p.press("A")
    assert data["letters"]["A"]["presses"] == 1 and data["days"]["2026-11-27"]["A"] == {"p": 1}


@pytest.mark.parametrize("raw", [
    None, [], "x", {"letters": []}, {"days": "x"}, {"mastery": "3"},
    {"letters": {"A": {"presses": -1, "asked": True, "right": "2", "last": "yesterday"}}},
    {"letters": {"SS": {"presses": 3}, "A": "x"}},
    {"days": {"2026-13-40": {"A": {"p": 1}}, "2026-11-27": {"A": {"p": -2, "x": 1}, "??": {"p": 1}}}},
])
def test_load_drops_anything_malformed(clock, raw):
    q = progress.Progress(keymap.LETTER_TARGETS, today=clock)
    q.load(raw)
    _, data = q.export()
    assert data == {"version": 1, "since": "2026-11-27", "mastery": 3, "letters": {}, "days": {}, "sessions": [],
                    "key_days": {}}


def test_load_keeps_valid_parts(clock):
    q = progress.Progress(keymap.LETTER_TARGETS, today=clock)
    q.load({"since": "2026-10-01", "mastery": 7,
            "letters": {"A": {"presses": 4, "asked": 2, "right": 1, "first": 1, "streak": 1, "last": "2026-11-20"},
                        "Q": {"presses": "many"}},
            "days": {"2026-11-20": {"A": {"p": 4, "a": 2, "r": 1, "f": 1}},
                     "2026-01-01": {"A": {"p": 9}},           # older than DAYS_KEPT: dropped
                     "2027-01-01": {"A": {"p": 9}}}})         # in the future: dropped
    _, data = q.export()
    assert data["since"] == "2026-10-01" and data["mastery"] == 7
    assert data["letters"] == {"A": {"presses": 4, "asked": 2, "right": 1, "first": 1, "streak": 1,
                                     "last": "2026-11-20"}}
    assert data["days"] == {"2026-11-20": {"A": {"p": 4, "a": 2, "r": 1, "f": 1}}}


def test_snapshot_lists_letters_in_alphabet_order(p):
    for key in ("7", "ß", "B", "A"):
        p.press(key)
    assert list(p.snapshot()["letters"]) == ["A", "B", "ß", "7"]


def test_concurrent_presses_are_all_counted(p):
    def press():
        for _ in range(500):
            p.press("A")
    threads = [threading.Thread(target=press) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert p.snapshot()["letters"]["A"]["presses"] == 2000 and p.rev == 2000


# ── Play sessions and the dashboard (FUNK-42) ─────────────────────────────────

T0 = 1795770000          # 2026-11-27 09:00 UTC


class Now:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def now():
    return Now()


@pytest.fixture
def ps(clock, now):
    return progress.Progress(keymap.LETTER_TARGETS, today=clock, now=now)


def test_a_session_runs_until_five_minutes_without_a_key(ps, now):
    ps.key()
    ps.press("A")
    now.t += 120
    ps.key()
    ps.quiz_result("A", True, True)
    now.t += progress.SESSION_GAP           # exactly the gap: still the same session
    ps.key()
    now.t += progress.SESSION_GAP + 1       # one second more: a new one
    ps.key()
    s1, s2 = ps.export()[1]["sessions"]
    assert s1 == {"day": "2026-11-27", "start": T0, "end": T0 + 120 + progress.SESSION_GAP,
                  "keys": 3, "presses": 1, "asked": 1, "right": 1, "first": 1}
    assert (s2["start"], s2["end"], s2["keys"], s2["presses"]) == (s1["end"] + progress.SESSION_GAP + 1,) * 2 + (1, 0)


def test_other_keys_keep_the_session_going_but_count_no_letter(ps, now):
    ps.key()
    assert ps.press("ENTER") is False
    assert ps.rev == 1 and ps.snapshot()["letters"] == {}
    assert ps.export()[1]["sessions"][0]["keys"] == 1


def test_a_clock_set_back_starts_a_new_session(ps, now):
    ps.key()
    now.t -= 60
    ps.key()
    assert [x["start"] for x in ps.export()[1]["sessions"]] == [T0, T0 - 60]


def test_sessions_are_kept_90_days_and_capped(ps, clock, now, monkeypatch):
    ps.key()
    clock.day, now.t = "2027-02-24", T0 + 89 * 86400     # day 90: the first session is still kept
    ps.key()
    assert len(ps.export()[1]["sessions"]) == 2
    clock.day, now.t = "2027-02-25", T0 + 90 * 86400
    ps.key()
    assert [x["day"] for x in ps.export()[1]["sessions"]] == ["2027-02-24", "2027-02-25"]
    monkeypatch.setattr(progress, "SESSIONS_MAX", 3)
    for _ in range(5):
        now.t += progress.SESSION_GAP + 1
        ps.key()
    sessions = ps.export()[1]["sessions"]
    assert len(sessions) == 3 and sessions[-1]["start"] == now.t


def test_a_reload_after_the_clock_went_back_extends_the_running_session(ps, clock, now):
    """Codex review: sessions keep the order they happened in, so after a restart the running
    session is still the last one, even if the clock had gone back."""
    ps.key()                                 # 09:00
    now.t += 60
    ps.key()                                 # 09:01
    now.t = T0 - 60
    ps.key()                                 # the clock goes back to 08:59: a new session
    q = progress.Progress(keymap.LETTER_TARGETS, today=clock, now=now)
    q.load(json.loads(json.dumps(ps.export()[1])))
    now.t += 10
    q.key()                                  # 08:59:10 extends the 08:59 session
    assert [(x["start"], x["end"], x["keys"]) for x in q.export()[1]["sessions"]] == [
        (T0, T0 + 60, 2), (T0 - 60, T0 - 50, 2)]


def test_sessions_have_unique_ids_even_with_the_same_start(ps, now):
    ps.key()
    now.t += progress.SESSION_GAP + 1
    ps.key()
    now.t = T0
    ps.key()                                 # the clock went back: a third session that starts at T0 too
    recent = ps.dashboard()["recent"]
    assert [x["start"] for x in recent] == [T0, T0 + progress.SESSION_GAP + 1, T0]
    assert len({x["id"] for x in recent}) == 3
    ps.reset()
    ps.key()
    assert ps.dashboard()["recent"][0]["id"] not in {x["id"] for x in recent}


def test_a_new_day_prunes_and_bumps_rev_also_while_nobody_plays(ps, clock, now):
    ps.key()
    ps.press("A")
    rev = ps.rev
    assert ps.roll_day() is False and ps.rev == rev
    clock.day = "2027-02-25"                 # 90 days later, nobody played meanwhile
    assert ps.roll_day() is True and ps.rev == rev + 1
    _, data = ps.export()
    assert data["sessions"] == [] and data["days"] == {} and ps.dashboard()["recent"] == []
    assert data["letters"]["A"]["presses"] == 1          # totals stay
    clock.day = "2027-02-24"                 # the date corrected back: a new rev too, so pages take it
    assert ps.roll_day() is True and ps.rev == rev + 2 and ps.dashboard()["today"] == "2027-02-24"


def test_the_dashboard_says_how_long_ago_the_last_key_was(ps, now):
    assert ps.dashboard()["idle"] is None
    ps.key()
    now.t += 42
    assert ps.dashboard()["idle"] == 42
    now.t = T0 - 5                           # the clock went back: unknown, not "playing"
    assert ps.dashboard()["idle"] is None


def test_reset_clears_the_sessions(ps):
    ps.key()
    ps.reset()
    assert ps.export()[1]["sessions"] == [] and ps.export()[1]["key_days"] == {} and ps.dashboard()["recent"] == []


def test_keys_over_midnight_count_for_both_days(ps, clock, now):
    """Codex review: a session from 23:59 to 00:01 with keys that are not letters still makes
    today a day played; its play time stays on the day it started."""
    clock.day, now.t = "2026-11-26", T0 - 9 * 3600 - 60      # 23:59 the day before
    ps.key()
    clock.day, now.t = "2026-11-27", T0 - 9 * 3600 + 60      # 00:01
    ps.key()
    d = ps.dashboard()
    assert (d["ranges"]["1"]["keys"], d["ranges"]["1"]["active_days"], d["ranges"]["1"]["sessions"]) == (1, 1, 0)
    assert (d["ranges"]["7"]["active_days"], d["ranges"]["7"]["seconds"], d["ranges"]["7"]["keys"]) == (2, 120, 2)
    assert ps.export()[1]["key_days"] == {"2026-11-26": 1, "2026-11-27": 1}


@pytest.mark.parametrize("raw,want", [
    ({"2026-11-27": 4, "2026-11-20": 2}, {"2026-11-20": 2, "2026-11-27": 4}),
    ({"2026-11-27": -1, "x": 3, "2026-13-01": 2, "2026-11-26": True, "2026-11-28": 5, "2026-10-01": 5}, {}),
    ("x", {}),
])
def test_load_key_days(clock, raw, want):
    q = progress.Progress(keymap.LETTER_TARGETS, today=clock)
    q.load({"key_days": raw})
    assert q.export()[1]["key_days"] == want


@pytest.mark.parametrize("bad", [
    "x", {"start": T0}, {"day": "2026-11-27", "start": T0 + 10, "end": T0}, {"day": "nope", "start": T0, "end": T0},
    {"day": "2026-11-27", "start": True, "end": T0}, {"day": "2026-11-27", "start": 0, "end": T0},
    {"day": "2026-11-27", "start": 10**30, "end": 10**30 + 60},      # cannot be formatted as a time
    {"day": "2026-11-27", "start": T0, "end": float(T0)},
    {"day": "2026-11-28", "start": T0, "end": T0},                    # in the future
    {"day": "2026-08-01", "start": T0, "end": T0},                    # older than 90 days
])
def test_load_drops_malformed_sessions(clock, bad):
    q = progress.Progress(keymap.LETTER_TARGETS, today=clock)
    q.load({"sessions": [bad]})
    assert q.export()[1]["sessions"] == []


def test_load_keeps_valid_sessions_in_their_order_and_fixes_counts(clock):
    q = progress.Progress(keymap.LETTER_TARGETS, today=clock)
    q.load({"sessions": [{"day": "2026-11-27", "start": T0 + 300, "end": T0 + 400, "keys": 3, "presses": -1, "id": 9},
                         {"day": "2026-11-26", "start": T0, "end": T0, "asked": "2"}]})
    assert q.export()[1]["sessions"] == [          # the order they happened in, not sorted; no ids stored
        {"day": "2026-11-27", "start": T0 + 300, "end": T0 + 400, "keys": 3, "presses": 0, "asked": 0, "right": 0, "first": 0},
        {"day": "2026-11-26", "start": T0, "end": T0, "keys": 0, "presses": 0, "asked": 0, "right": 0, "first": 0}]


def play(p, clock, now, day, t, presses=(), quiz=(), keys=0):
    clock.day, now.t = day, t
    for k in presses:
        p.key()
        p.press(k)
    for letter, right, first in quiz:
        p.key()
        p.quiz_result(letter, right, first)
    for _ in range(keys):
        p.key()


def test_the_dashboard_sums_days_and_periods(ps, clock, now):
    play(ps, clock, now, "2026-11-01", T0 - 26 * 86400, presses="AB")        # older than 7 days, within 30
    play(ps, clock, now, "2026-11-24", T0 - 3 * 86400, presses="AAC", keys=2)
    now.t += 90
    ps.key()                                                                   # the same session, 90 s long
    play(ps, clock, now, "2026-11-27", T0, presses="A", quiz=[("A", True, True), ("B", False, False)])
    now.t += progress.SESSION_GAP + 60
    ps.key()                                                                   # a second session today
    d = ps.dashboard()
    assert d["today"] == "2026-11-27" and len(d["days"]) == progress.DAYS_KEPT
    assert d["days"][0]["day"] == "2026-10-29" and d["days"][-1]["day"] == "2026-11-27"
    by_day = {x["day"]: x for x in d["days"]}
    assert by_day["2026-11-24"] == {"day": "2026-11-24", "presses": 3, "asked": 0, "right": 0, "first": 0,
                                    "seconds": 90, "sessions": 1, "keys": 6}
    today = by_day["2026-11-27"]
    assert (today["presses"], today["asked"], today["right"], today["first"], today["sessions"], today["keys"]) \
        == (1, 2, 1, 1, 2, 4)
    r1, r7, r30 = d["ranges"]["1"], d["ranges"]["7"], d["ranges"]["30"]
    assert (r1["presses"], r1["active_days"], r1["sessions"]) == (1, 1, 2)
    assert (r7["presses"], r7["active_days"], r7["seconds"], r7["sessions"]) == (4, 2, 90, 3)
    assert (r30["presses"], r30["active_days"], r30["keys"]) == (6, 3, 12)
    assert r7["letters"] == {"A": {"presses": 3, "asked": 1, "right": 1, "first": 1},
                             "B": {"presses": 0, "asked": 1, "right": 0, "first": 0},
                             "C": {"presses": 1, "asked": 0, "right": 0, "first": 0}}
    assert list(r30["letters"]) == ["A", "B", "C"]
    assert [x["start"] for x in d["recent"]] == [now.t, T0, T0 - 3 * 86400, T0 - 26 * 86400]   # newest first
    assert d["recent"][1]["seconds"] == 0 and d["last"] == now.t
    assert d["keys"] == list(keymap.LETTER_TARGETS) and d["learned"] == []


def test_the_dashboard_lists_learned_letters_and_the_latest_sessions(ps, now):
    for _ in range(3):
        ps.quiz_result("M", True, True)
    for _ in range(progress.RECENT_SESSIONS + 2):
        now.t += progress.SESSION_GAP + 1
        ps.key()
    d = ps.dashboard()
    assert d["learned"] == ["M"] and len(d["recent"]) == progress.RECENT_SESSIONS
    assert d["recent"][0]["start"] == now.t


def test_an_empty_dashboard(ps):
    d = ps.dashboard()
    assert d["recent"] == [] and d["last"] is None and d["learned"] == []
    assert all(x["presses"] == 0 and x["seconds"] == 0 for x in d["days"])
    assert d["ranges"]["30"]["letters"] == {} and d["ranges"]["1"]["active_days"] == 0


def test_sessions_csv(ps, now):
    ps.key()
    ps.press("Ä")
    for step in (300, 300, 150):
        now.t += step
        ps.key()
    ps.quiz_result("Ä", True, False)
    now.t += 3600
    ps.key()
    text = ps.sessions_csv(localtime=time.gmtime)
    assert text.split("\r\n") == [
        "beginn;ende;minuten;tasten;buchstaben_ziffern;quiz_fragen;quiz_richtig;quiz_richtig_1_versuch",
        "2026-11-27 09:00:00;2026-11-27 09:12:30;12,5;4;1;1;1;0",
        "2026-11-27 10:12:30;2026-11-27 10:12:30;0,0;1;0;0;0;0", ""]


def test_letters_csv_lists_every_key(ps):
    ps.press("B")
    for _ in range(3):
        ps.quiz_result("ß", True, True)
    lines = ps.letters_csv().split("\r\n")
    assert lines[0] == "taste;gedrueckt;quiz_fragen;quiz_richtig;quiz_richtig_1_versuch;in_folge_richtig;gelernt;zuletzt"
    assert len(lines) == 1 + len(keymap.LETTER_TARGETS) + 1 and lines[-1] == ""
    assert lines[1] == "A;0;0;0;0;0;nein;" and lines[2] == "B;1;0;0;0;0;nein;2026-11-27"
    assert "ß;0;3;3;3;3;ja;2026-11-27" in lines
