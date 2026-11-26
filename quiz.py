"""Quiz mode (FUNK-39): hear a word or a sound, press its letter.

A pure state machine, no I/O: the server injects playback, the display, the SSE
broadcast, persistence and a scheduler, so every path is unit-tested
(tests/test_quiz.py) without aiohttp, audio or a keyboard.

Flow: ``start`` → a question (a random letter of the active set, never the same
twice in a row; its word or sound is played) → the child presses a key →
``answer``:
- right: the "right" jingle, then the letter's own entry; next question;
- wrong: the "wrong" jingle and the question again; after MAX_TRIES wrong
  tries the letter is shown and played ("revealed"), then the next question.
The session ends after its number of questions, on ``stop``, or after
INACTIVE_STOP_SECONDS without a key press. Finished sessions are recorded.

Concurrency: one lock guards all state. Every phase change bumps ``token``;
callbacks (end of playback, timers) carry the token they were made for and do
nothing when it is stale, so a late callback never acts on a newer question.
Playback must call ``on_done`` once whenever its sequence ends, also when it
was cut short (Space, a newer sequence): the token decides what that means.
"""
import io
import math
import random
import struct
import threading
import time
import wave

MODES = {"word": "Wort", "sound": "Geräusch", "mixed": "Gemischt"}
QUESTION_COUNTS = (5, 10, 20, 0)      # 0 = endless
REMIND_CHOICES = (0, 10, 20, 30)      # seconds without an answer before the question is repeated
MAX_TRIES = 2                         # wrong tries before the letter is revealed
MAX_REMINDERS = 2                     # repeats per question, then the quiz just waits
INACTIVE_STOP_SECONDS = 300           # no key press for this long ends the session
WATCH_SECONDS = 30                    # how often inactivity is checked
NEXT_PAUSE_SECONDS = 0.8              # after the feedback, before the next question
HISTORY_MAX = 20

DEFAULT_CONFIG = {"mode": "word", "questions": 10, "letters": [], "hint_image": True, "remind_seconds": 0}


class QuizError(ValueError):
    pass


def validate_config(raw, universe):
    """A complete, normalized config from ``raw``; letters must be in ``universe`` (ordered)."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise QuizError("config must be an object")
    cfg = dict(DEFAULT_CONFIG)
    if "mode" in raw:
        if not isinstance(raw["mode"], str) or raw["mode"] not in MODES:
            raise QuizError(f"mode must be one of {', '.join(MODES)}")
        cfg["mode"] = raw["mode"]
    if "questions" in raw:
        q = raw["questions"]
        if isinstance(q, bool) or q not in QUESTION_COUNTS:
            raise QuizError("questions must be 5, 10, 20 or 0 (endless)")
        cfg["questions"] = q
    if "remind_seconds" in raw:
        r = raw["remind_seconds"]
        if isinstance(r, bool) or r not in REMIND_CHOICES:
            raise QuizError("remind_seconds must be 0, 10, 20 or 30")
        cfg["remind_seconds"] = r
    if "hint_image" in raw:
        if not isinstance(raw["hint_image"], bool):
            raise QuizError("hint_image must be true or false")
        cfg["hint_image"] = raw["hint_image"]
    if "letters" in raw:
        letters = raw["letters"]
        if not isinstance(letters, list) or len(letters) > len(universe):
            raise QuizError("letters must be a list of letters")
        chosen = set()
        for letter in letters:
            if letter not in universe:
                raise QuizError(f"unknown letter {letter!r}")
            chosen.add(letter)
        cfg["letters"] = [letter for letter in universe if letter in chosen]
    return cfg


def pick_clip(entry, mode, rng):
    """The clip a question plays: the word (DE track), the sound (FX) or either."""
    word, sound = entry["clips"].get("word"), entry["clips"].get("sound")
    if mode == "word":
        return word or sound
    if mode == "sound":
        return sound or word
    return rng.choice([c for c in (word, sound) if c])


class Quiz:
    """The quiz engine. All public methods are thread-safe."""

    def __init__(self, *, pool, play, show, broadcast, record, schedule,
                 now=time.monotonic, wall=time.time, rng=None):
        # pool(letters) -> {letter: [entry]}: entries with a clip, of the given letters (all if empty).
        #   entry = {"word", "image", "clips": {"word": clip|None, "sound": clip|None}, "files": [clip]}
        # play(clips, on_done): clip = (path, volume) or a jingle name ("right", "wrong", "done");
        #   [] stops all sound. on_done() once the sequence has ended, however it ended.
        # show(kind, letter, word, image): kind = question | right | revealed | done
        # broadcast(snapshot), record(summary), schedule(seconds, fn, *args)
        self._pool, self._play, self._show = pool, play, show
        self._broadcast, self._record, self._schedule = broadcast, record, schedule
        self._now, self._wall = now, wall
        self._rng = rng or random.Random()
        self._lock = threading.RLock()
        self.rev = 0
        self.token = 0
        self.phase = "idle"            # idle | asking | feedback
        self.session = None
        self.question = None
        self.last = None               # summary of the last finished session
        self._sid = 0
        self._next_planned = None
        self._last_letter = None
        self._last_activity = now()
        self.boot = "%08x" % self._rng.getrandbits(32)   # a restart starts rev again: clients compare boot
        self._snap = self._build_snapshot()

    # ── state ────────────────────────────────────────────────────────────────

    @property
    def active(self):
        return self.session is not None

    def snapshot(self):
        """The state as of the last change. Built under the lock at each change, read without it,
        so a reader (the web server) never waits for a key press or playback."""
        return self._snap

    def _build_snapshot(self):
        s, q = self.session, self.question
        return {
            "boot": self.boot,
            "rev": self.rev,
            "active": s is not None,
            "phase": self.phase,
            "session": None if s is None else {
                k: s[k] for k in ("asked", "questions", "correct", "first_try", "revealed", "wrong", "mode")},
            "question": None if q is None else {k: q[k] for k in ("n", "letter", "word", "tries")},
            "last": self.last,
        }

    def _set_phase(self, phase):
        self.phase = phase
        self.token += 1
        self._changed()

    def _changed(self):
        self.rev += 1
        self._snap = self._build_snapshot()
        self._broadcast(self._snap)

    # ── commands ─────────────────────────────────────────────────────────────

    def start(self, config):
        with self._lock:
            if self.session is not None:
                raise QuizError("Das Quiz läuft schon")
            if not self._pool(config["letters"]):
                raise QuizError("Keine Buchstaben mit Wort oder Geräusch im aktiven Set")
            self._sid += 1
            self.session = {
                "id": self._sid, "config": dict(config), "mode": config["mode"],
                "questions": config["questions"], "asked": 0, "correct": 0, "first_try": 0,
                "revealed": 0, "wrong": 0, "letters": {}, "started": self._wall(),
            }
            self._last_letter = None
            self._last_activity = self._now()
            self._schedule(WATCH_SECONDS, self._on_watch, self._sid)
            self._next()

    def stop(self):
        with self._lock:
            if self.session is not None:
                self._finish("gestoppt")

    def touch(self):
        """A key was pressed (any key): the child is still there."""
        with self._lock:
            self._last_activity = self._now()

    def repeat(self):
        """Play the question again (Enter, or the parent's button)."""
        with self._lock:
            self._last_activity = self._now()
            if self.phase == "asking":
                # the cut-short playback and its pending reminder are void: the replay re-arms
                self.token += 1
                self._ask()

    def answer(self, letter):
        """The child pressed a letter. Returns "right", "wrong", "revealed" or None (not asking)."""
        with self._lock:
            self._last_activity = self._now()
            if self.phase != "asking":
                return None
            s, q = self.session, self.question
            stats = s["letters"].setdefault(q["letter"], {"right": 0, "wrong": 0})
            if letter == q["letter"]:
                s["correct"] += 1
                stats["right"] += 1
                if q["tries"] == 0:
                    s["first_try"] += 1
                self._set_phase("feedback")
                self._show("right", q["letter"], q["word"], q["image"])
                self._play(["right"] + q["files"], self._done_cb(self._after_feedback))
                return "right"
            q["tries"] += 1
            s["wrong"] += 1
            stats["wrong"] += 1
            if q["tries"] < MAX_TRIES:
                self._set_phase("feedback")
                self._play(["wrong", q["clip"]], self._done_cb(self._after_retry))
                return "wrong"
            s["revealed"] += 1
            self._set_phase("feedback")
            self._show("revealed", q["letter"], q["word"], q["image"])
            self._play(["wrong"] + q["files"], self._done_cb(self._after_feedback))
            return "revealed"

    # ── internals (lock held) ────────────────────────────────────────────────

    def _done_cb(self, fn):
        """A playback callback bound to the current token."""
        token = self.token
        return lambda: fn(token)

    def _next(self):
        s = self.session
        if s["questions"] and s["asked"] >= s["questions"]:
            return self._finish("fertig")
        pool = self._pool(s["config"]["letters"])
        if not pool:
            return self._finish("keine Buchstaben")
        letters = sorted(pool)
        letter = self._rng.choice([l for l in letters if l != self._last_letter] or letters)
        entry = self._rng.choice(pool[letter])
        s["asked"] += 1
        self.question = {"n": s["asked"], "letter": letter, "word": entry["word"], "image": entry["image"],
                         "clip": pick_clip(entry, s["mode"], self._rng), "files": list(entry["files"]),
                         "tries": 0, "reminders": 0}
        self._last_letter = letter
        self._set_phase("asking")
        self._show("question", "?", "", entry["image"] if s["config"]["hint_image"] else "")
        self._ask()

    def _ask(self):
        self._play([self.question["clip"]], self._done_cb(self._after_question))

    def _after_question(self, token):
        with self._lock:
            if token != self.token or self.phase != "asking":
                return
            remind = self.session["config"]["remind_seconds"]
            if remind and self.question["reminders"] < MAX_REMINDERS:
                self._schedule(remind, self._on_remind, token, self.question["reminders"])

    def _on_remind(self, token, reminders):
        with self._lock:
            if token != self.token or self.phase != "asking" or self.question["reminders"] != reminders:
                return
            if self._now() - self._last_activity < self.session["config"]["remind_seconds"]:
                # a key was pressed meanwhile (not an answer): wait a full period again
                self._schedule(self.session["config"]["remind_seconds"], self._on_remind, token, reminders)
                return
            self.question["reminders"] += 1
            self._ask()

    def _after_retry(self, token):
        with self._lock:
            if token != self.token:
                return
            self._set_phase("asking")
            self._after_question(self.token)

    def _after_feedback(self, token):
        with self._lock:
            if token != self.token or self._next_planned == token:
                return
            self._next_planned = token          # one next question per feedback, even if on_done comes twice
            self._schedule(NEXT_PAUSE_SECONDS, self._next_if, token)

    def _next_if(self, token):
        with self._lock:
            if token == self.token and self.session is not None:
                self._next()

    def _on_watch(self, sid):
        with self._lock:
            if self.session is None or self.session["id"] != sid:
                return
            if self._now() - self._last_activity >= INACTIVE_STOP_SECONDS:
                self._finish("inaktiv")
            else:
                self._schedule(WATCH_SECONDS, self._on_watch, sid)

    def _finish(self, reason):
        s = self.session
        answered = s["correct"] + s["revealed"]
        ended = self._wall()
        summary = {
            "started": s["started"], "ended": ended, "seconds": max(0, round(ended - s["started"])),
            "mode": s["mode"], "questions": s["questions"], "answered": answered,
            "correct": s["correct"], "first_try": s["first_try"], "revealed": s["revealed"],
            "wrong": s["wrong"], "reason": reason,
            "letters": {k: dict(v) for k, v in sorted(s["letters"].items())},
            "hard": [k for k, v in sorted(s["letters"].items(), key=lambda kv: (-kv[1]["wrong"], kv[0]))
                     if v["wrong"]],
        }
        self.session = None
        self.question = None
        self.last = summary
        self._set_phase("idle")
        if answered:
            self._record(summary)
            self._show("done", f"{s['correct']}/{answered}", "super" if reason == "fertig" else "quiz", "")
        if reason == "fertig":
            self._play(["done"], lambda: None)
        else:
            self._play([], lambda: None)          # stopped: silence now


# ── jingles: synthesized, so the quiz needs no sound files ───────────────────

_RATE = 22050
_NOTES = {"C4": 261.63, "E4": 329.63, "G4": 392.00, "C5": 523.25, "E5": 659.25, "G5": 783.99,
          "C6": 1046.50, "E6": 1318.51, "G6": 1567.98}
JINGLES = {
    # (note, seconds) …, amplitude
    "right": ([("C5", 0.10), ("E5", 0.10), ("G5", 0.10), ("C6", 0.30)], 0.32),
    "wrong": ([("E4", 0.20), ("C4", 0.32)], 0.18),
    "done": ([("C5", 0.14), ("E5", 0.14), ("G5", 0.14), ("C6", 0.14), ("E6", 0.14), ("G6", 0.50)], 0.30),
}


def jingle_wav(name):
    """A short tone sequence as 16-bit mono WAV bytes (deterministic)."""
    notes, amp = JINGLES[name]
    frames = bytearray()
    for note, seconds in notes:
        freq, n = _NOTES[note], int(_RATE * seconds)
        attack, release = int(_RATE * 0.01), int(_RATE * min(0.08, seconds / 2))
        for i in range(n):
            env = min(1.0, i / attack if attack else 1.0, (n - i) / release if release else 1.0)
            t = i / _RATE
            # a soft bell: the fundamental and a quiet octave
            v = math.sin(2 * math.pi * freq * t) + 0.25 * math.sin(4 * math.pi * freq * t)
            frames += struct.pack("<h", int(max(-1.0, min(1.0, amp * env * v / 1.25)) * 32767))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_RATE)
        w.writeframes(bytes(frames))
    return buf.getvalue()
