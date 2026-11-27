"""Per-letter progress counters (FUNK-40): pure, no server needed."""
import json
import os
import sys
import threading

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
    assert data == {"version": 1, "since": "2026-11-27", "mastery": 3, "letters": {}, "days": {}}


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
