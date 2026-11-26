"""Quiz mode engine (FUNK-39): pure state machine, no audio or aiohttp needed."""
import io
import os
import random
import sys
import wave

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import quiz as qz  # noqa: E402

UNIVERSE = tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ") + ("Ä", "Ö", "Ü", "ß")


def entry(word, de=True, fx=True):
    clips = {"word": (f"{word}_de.mp3", 100) if de else None, "sound": (f"{word}_fx.mp3", 90) if fx else None}
    return {"word": word, "image": f"{word}.png", "clips": clips,
            "files": [c for c in (clips["sound"], clips["word"]) if c]}


class Rig:
    """A quiz with fake playback, display, scheduler and clock."""

    def __init__(self, pool=None, seed=1):
        self.pool_data = pool if pool is not None else {"A": [entry("affe")], "B": [entry("ball")],
                                                        "C": [entry("clown")]}
        self.played, self.shown, self.snaps, self.records, self.timers = [], [], [], [], []
        self.t = 1000.0
        self.quiz = qz.Quiz(pool=self.pool, play=self.play, show=lambda *a: self.shown.append(a),
                            broadcast=self.snaps.append, record=self.records.append,
                            schedule=lambda d, fn, *a: self.timers.append((d, fn, a)),
                            now=lambda: self.t, wall=lambda: self.t, rng=random.Random(seed))

    def pool(self, letters):
        return {k: v for k, v in self.pool_data.items() if not letters or k in letters}

    def play(self, clips, on_done):
        self.played.append((list(clips), on_done))

    def done(self, i=-1):
        """The playback i ends (naturally or cut short)."""
        self.played[i][1]()

    def fire(self, kind):
        """Run (and remove) the first pending timer whose function name contains ``kind``."""
        for i, (_, fn, args) in enumerate(self.timers):
            if kind in fn.__name__:
                del self.timers[i]
                fn(*args)
                return True
        return False

    def start(self, **cfg):
        self.quiz.start(qz.validate_config(cfg, UNIVERSE))

    @property
    def target(self):
        return self.quiz.question["letter"]

    def wrong_letter(self):
        return next(l for l in UNIVERSE if l != self.target)


# ── config ───────────────────────────────────────────────────────────────────

def test_config_defaults_and_validation():
    assert qz.validate_config(None, UNIVERSE) == qz.DEFAULT_CONFIG
    cfg = qz.validate_config({"mode": "mixed", "questions": 0, "letters": ["C", "A", "A", "ß"],
                              "hint_image": False, "remind_seconds": 20, "junk": 1}, UNIVERSE)
    assert cfg == {"mode": "mixed", "questions": 0, "letters": ["A", "C", "ß"], "hint_image": False,
                   "remind_seconds": 20}


@pytest.mark.parametrize("raw", [
    {"mode": "loud"}, {"mode": []}, {"mode": {}}, {"questions": 7}, {"questions": []}, {"questions": True}, {"remind_seconds": 5}, {"hint_image": "yes"},
    {"letters": "AB"}, {"letters": ["1"]}, {"letters": ["a"]}, [], "x",
])
def test_config_rejects(raw):
    with pytest.raises(qz.QuizError):
        qz.validate_config(raw, UNIVERSE)


@pytest.mark.parametrize("mode,de,fx,want", [
    ("word", True, True, "word"), ("word", False, True, "sound"),
    ("sound", True, True, "sound"), ("sound", True, False, "word"),
])
def test_pick_clip(mode, de, fx, want):
    e = entry("x", de, fx)
    assert qz.pick_clip(e, mode, random.Random(0)) == e["clips"][want]


def test_mixed_uses_both_clips():
    e, rng = entry("x"), random.Random(3)
    assert {qz.pick_clip(e, "mixed", rng) for _ in range(40)} == {e["clips"]["word"], e["clips"]["sound"]}


# ── the game loop ────────────────────────────────────────────────────────────

def test_start_asks_a_question_with_its_clip_and_a_hint_picture():
    r = Rig()
    r.start()
    q = r.quiz.question
    assert r.quiz.phase == "asking" and q["n"] == 1
    assert r.played[-1][0] == [r.pool_data[q["letter"]][0]["clips"]["word"]]
    assert r.shown[-1] == ("question", "?", "", f"{q['word']}.png")
    snap = r.snaps[-1]
    assert snap["active"] and snap["question"]["letter"] == q["letter"] and snap["rev"] == r.quiz.rev


def test_no_hint_picture_when_off():
    r = Rig()
    r.start(hint_image=False)
    assert r.shown[-1] == ("question", "?", "", "")


def test_right_answer_plays_jingle_then_entry_then_next_question():
    r = Rig()
    r.start()
    letter, files = r.target, r.quiz.question["files"]
    assert r.quiz.answer(letter) == "right"
    assert r.quiz.phase == "feedback"
    assert r.played[-1][0] == ["right"] + files
    assert r.shown[-1][:2] == ("right", letter)
    assert r.quiz.answer(letter) is None                # presses during feedback do nothing
    r.done()
    assert r.fire("_next_if")
    assert r.quiz.question["n"] == 2 and r.quiz.phase == "asking"
    s = r.quiz.session
    assert (s["correct"], s["first_try"], s["wrong"]) == (1, 1, 0)


def test_wrong_answer_retries_then_reveals():
    r = Rig()
    r.start()
    letter = r.target
    assert r.quiz.answer(r.wrong_letter()) == "wrong"
    assert r.played[-1][0] == ["wrong", r.quiz.question["clip"]]
    r.done()                                            # the question was played again
    assert r.quiz.phase == "asking" and r.quiz.question["tries"] == 1
    assert r.quiz.answer(r.wrong_letter()) == "revealed"
    assert r.shown[-1][:2] == ("revealed", letter)
    assert r.played[-1][0] == ["wrong"] + r.quiz.question["files"]
    r.done()
    assert r.fire("_next_if") and r.quiz.question["n"] == 2
    s = r.quiz.session
    assert (s["correct"], s["revealed"], s["wrong"]) == (0, 1, 2)
    assert s["letters"][letter] == {"right": 0, "wrong": 2}


def test_right_on_second_try_is_correct_but_not_first_try():
    r = Rig()
    r.start()
    r.quiz.answer(r.wrong_letter())
    r.done()
    assert r.quiz.answer(r.target) == "right"
    assert (r.quiz.session["correct"], r.quiz.session["first_try"]) == (1, 0)


def test_never_the_same_letter_twice_in_a_row():
    r = Rig(seed=7)
    r.start(questions=0)
    seen = []
    for _ in range(60):
        seen.append(r.target)
        r.quiz.answer(r.target)
        r.done()
        r.fire("_next_if")
    assert all(a != b for a, b in zip(seen, seen[1:]))
    assert set(seen) == {"A", "B", "C"}


def test_a_single_letter_may_repeat():
    r = Rig(pool={"A": [entry("affe"), entry("apfel")]})
    r.start(questions=5)
    for _ in range(4):
        assert r.target == "A"
        r.quiz.answer("A")
        r.done()
        r.fire("_next_if")


def test_letter_subset():
    r = Rig()
    r.start(letters=["B"], questions=0)
    for _ in range(5):
        assert r.target == "B"
        r.quiz.answer("B")
        r.done()
        r.fire("_next_if")


def test_session_ends_after_its_questions_and_is_recorded():
    r = Rig()
    r.start(questions=5)
    for i in range(5):
        if i == 2:
            r.quiz.answer(r.wrong_letter())
            r.done()
        r.quiz.answer(r.target)
        r.done()
        r.fire("_next_if")
    assert not r.quiz.active and r.quiz.phase == "idle"
    (rec,) = r.records
    assert (rec["answered"], rec["correct"], rec["first_try"], rec["wrong"], rec["reason"]) == (5, 5, 4, 1, "fertig")
    assert r.quiz.last == rec and r.snaps[-1]["last"] == rec and not r.snaps[-1]["active"]
    assert r.shown[-1] == ("done", "5/5", "super", "") and r.played[-1][0] == ["done"]


def test_stop_records_only_answered_sessions_and_silences():
    r = Rig()
    r.start()
    r.quiz.stop()
    assert r.records == [] and r.played[-1][0] == [] and not r.quiz.active
    r.start()
    r.quiz.answer(r.target)
    r.quiz.stop()
    assert len(r.records) == 1 and r.records[0]["reason"] == "gestoppt" and r.records[0]["answered"] == 1


def test_start_twice_or_without_letters_fails():
    r = Rig()
    r.start()
    with pytest.raises(qz.QuizError):
        r.start()
    r2 = Rig(pool={})
    with pytest.raises(qz.QuizError):
        r2.start()
    assert not r2.quiz.active


def test_pool_emptied_mid_session_ends_it():
    r = Rig()
    r.start()
    r.quiz.answer(r.target)
    r.pool_data = {}
    r.done()
    r.fire("_next_if")
    assert not r.quiz.active and r.records[0]["reason"] == "keine Buchstaben"


# ── late callbacks and timers never act on a newer state ────────────────────

def test_late_playback_callbacks_are_ignored():
    r = Rig()
    r.start()
    question_play = len(r.played) - 1
    r.quiz.answer(r.target)                             # cuts the question short …
    r.done(question_play)                               # … whose on_done arrives late
    assert r.quiz.phase == "feedback" and not r.timers[1:]   # only the watchdog is pending
    feedback = len(r.played) - 1
    r.done(feedback)
    r.done(feedback)                                    # twice: one next question only
    assert r.fire("_next_if") and not r.fire("_next_if")
    assert r.quiz.question["n"] == 2


def test_stop_during_feedback_ignores_its_end():
    r = Rig()
    r.start()
    r.quiz.answer(r.target)
    fb = len(r.played) - 1
    r.quiz.stop()
    r.done(fb)
    assert not r.fire("_next_if") or not r.quiz.active
    assert not r.quiz.active


def test_restart_ignores_the_old_sessions_timers():
    r = Rig()
    r.start()
    r.quiz.answer(r.target)
    r.done()                                            # schedules _next_if for session 1
    r.quiz.stop()
    r.start()
    n = r.quiz.question["n"]
    r.fire("_next_if")
    assert r.quiz.question["n"] == n == 1


def test_reminder_repeats_the_question_after_silence():
    r = Rig()
    r.start(remind_seconds=10)
    plays = len(r.played)
    r.done()                                            # question played: the reminder is armed
    delay = next(d for d, fn, _ in r.timers if fn.__name__ == "_on_remind")
    assert delay == 10
    r.t += 10
    assert r.fire("_on_remind")
    assert len(r.played) == plays + 1 and r.played[-1][0] == [r.quiz.question["clip"]]
    r.done()
    r.t += 10
    assert r.fire("_on_remind")
    r.done()
    assert not any(fn.__name__ == "_on_remind" for _, fn, _ in r.timers)   # MAX_REMINDERS reached


def test_reminder_waits_again_after_a_key_press():
    r = Rig()
    r.start(remind_seconds=10)
    r.done()
    r.t += 8
    r.quiz.touch()                                      # Space, volume … not an answer
    r.t += 2
    plays = len(r.played)
    assert r.fire("_on_remind")
    assert len(r.played) == plays                       # not yet: rescheduled
    r.t += 10
    assert r.fire("_on_remind") and len(r.played) == plays + 1


def test_no_reminder_by_default():
    r = Rig()
    r.start()
    r.done()
    assert not any(fn.__name__ == "_on_remind" for _, fn, _ in r.timers)


def test_inactivity_ends_the_session():
    r = Rig()
    r.start()
    r.quiz.answer(r.target)
    r.t += qz.INACTIVE_STOP_SECONDS - 1
    assert r.fire("_on_watch") and r.quiz.active        # not yet: watched again
    r.t += 1
    assert r.fire("_on_watch") and not r.quiz.active
    assert r.records[0]["reason"] == "inaktiv"


def test_repeat_replays_the_question_only_while_asking():
    r = Rig()
    r.start()
    r.quiz.repeat()
    assert r.played[-1][0] == [r.quiz.question["clip"]]
    r.quiz.answer(r.target)
    n = len(r.played)
    r.quiz.repeat()
    assert len(r.played) == n


def test_repeated_reminder_timers_fire_once():
    """A repeat while a reminder is pending leaves two timers: only one plays."""
    r = Rig()
    r.start(remind_seconds=10)
    r.done()                                            # reminder 1 armed
    r.quiz.repeat()
    r.done()                                            # reminder armed again (same count)
    r.t += 10
    plays = len(r.played)
    assert r.fire("_on_remind") and r.fire("_on_remind")
    assert len(r.played) == plays + 1


def test_rev_only_grows():
    r = Rig()
    r.start()
    r.quiz.answer(r.wrong_letter())
    r.done()
    r.quiz.stop()
    revs = [s["rev"] for s in r.snaps]
    assert revs == sorted(revs) and len(set(revs)) == len(revs)


# ── jingles ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", list(qz.JINGLES))
def test_jingles_are_short_valid_wavs(name):
    data = qz.jingle_wav(name)
    assert data == qz.jingle_wav(name)                  # deterministic
    with wave.open(io.BytesIO(data)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 22050)
        seconds = w.getnframes() / w.getframerate()
    assert 0.3 < seconds < 1.5


def test_snapshot_is_built_at_each_change_and_carries_the_boot():
    r = Rig()
    first = r.quiz.snapshot()
    assert first["boot"] == r.quiz.boot and first["rev"] == 0 and not first["active"]
    r.start()
    snap = r.quiz.snapshot()
    assert snap is r.snaps[-1] and snap["rev"] == r.quiz.rev and snap["question"]["n"] == 1
    r.quiz.touch()                                      # no visible change: same snapshot
    assert r.quiz.snapshot() is snap


def test_a_stopped_session_with_answers_shows_its_score():
    r = Rig()
    r.start()
    r.quiz.answer(r.target)
    r.quiz.stop()
    assert r.shown[-1] == ("done", "1/1", "quiz", "")


def test_repeat_voids_the_cut_short_playback_and_its_reminder():
    """Enter during a long question: the cut-short clip's end must not arm a reminder that
    would interrupt the replay; the replay arms its own."""
    r = Rig()
    r.start(remind_seconds=10)
    cut = len(r.played) - 1
    r.quiz.repeat()
    r.done(cut)                                         # the cut-short clip ends late
    assert not any(fn.__name__ == "_on_remind" for _, fn, _ in r.timers)
    r.done()                                            # the replay ends
    assert sum(fn.__name__ == "_on_remind" for _, fn, _ in r.timers) == 1
