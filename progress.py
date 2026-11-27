"""Per-letter progress (FUNK-40): key presses and quiz answers, per day.

Pure and thread-safe, no I/O: the server feeds it presses and quiz results
from the real keyboard (the parent page's test keys do not count), keeps
``export()`` in settings["progress"] and serves ``snapshot()`` to the parent
page. Unit-tested in tests/test_progress.py.

Stored as settings["progress"]::

    {"version": 1, "since": "2026-11-27", "mastery": 3,
     "letters": {"A": {"presses": 12, "asked": 4, "right": 3, "first": 2, "streak": 1,
                       "last": "2026-11-27"}},
     "days": {"2026-11-27": {"A": {"p": 5, "a": 1, "r": 1, "f": 1}}}}

- presses: the key pressed outside the quiz (letters and digits)
- asked: quiz questions about the letter that ended: answered right, or
  revealed after the last wrong try (a stopped question does not count)
- right: answered right, on any try; first: right on the first try
- streak: questions in a row answered right on the first try. The letter is
  "gelernt" while streak >= mastery, so a lucky guess does not make it, and a
  letter the child no longer knows loses the badge again.
- days: counts of the last DAYS_KEPT days (p/a/r/f as above); zeros are left out
"""
import datetime
import random
import re
import threading

VERSION = 1
DAYS_KEPT = 30
MASTERY_CHOICES = tuple(range(1, 11))
DEFAULT_MASTERY = 3
COUNTS = ("presses", "asked", "right", "first")
DAY_KEYS = {"presses": "p", "asked": "a", "right": "r", "first": "f"}
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


class ProgressError(ValueError):
    pass


def _today():
    return datetime.date.today().isoformat()


def _count(v):
    """A stored count: a non-negative int, anything else is 0."""
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def _date(v):
    if not isinstance(v, str) or not _DATE.match(v):
        return None
    try:
        datetime.date.fromisoformat(v)
    except ValueError:
        return None
    return v


def validate_mastery(v):
    if not isinstance(v, int) or isinstance(v, bool) or v not in MASTERY_CHOICES:
        raise ProgressError(f"mastery must be a whole number from {MASTERY_CHOICES[0]} to {MASTERY_CHOICES[-1]}")
    return v


class Progress:
    """The counters. All public methods are thread-safe; none blocks on I/O."""

    def __init__(self, universe, *, today=_today, rng=None):
        self._universe = tuple(universe)        # the keys that are counted, in display order
        self._known = set(self._universe)
        self._today = today
        self._lock = threading.Lock()
        self.boot = "%08x" % (rng or random.Random()).getrandbits(32)   # a restart starts rev again
        self.rev = 0
        self._reset_state(DEFAULT_MASTERY)

    def _reset_state(self, mastery):
        self.since = self._today()
        self.mastery = mastery
        self.letters = {}
        self.days = {}

    # ── persistence ──────────────────────────────────────────────────────────

    def load(self, raw):
        """Take over saved progress; anything malformed is dropped, never raised."""
        with self._lock:
            self._reset_state(DEFAULT_MASTERY)
            if not isinstance(raw, dict):
                return
            try:
                self.mastery = validate_mastery(raw.get("mastery"))
            except ProgressError:
                pass
            self.since = _date(raw.get("since")) or self.since
            for letter, c in (raw.get("letters") if isinstance(raw.get("letters"), dict) else {}).items():
                if letter not in self._known or not isinstance(c, dict):
                    continue
                row = {k: _count(c.get(k)) for k in COUNTS + ("streak",)}
                row["last"] = _date(c.get("last"))
                if any(row[k] for k in COUNTS):
                    self.letters[letter] = row
            for day, letters in (raw.get("days") if isinstance(raw.get("days"), dict) else {}).items():
                if not _date(day) or not isinstance(letters, dict):
                    continue
                rows = {}
                for letter, c in letters.items():
                    if letter in self._known and isinstance(c, dict):
                        row = {k: _count(c.get(k)) for k in DAY_KEYS.values() if _count(c.get(k))}
                        if row:
                            rows[letter] = row
                if rows:
                    self.days[day] = rows
            self._prune(self._today())

    def export(self):
        """(rev, a deep copy for settings["progress"]): the copy never changes after it is handed out."""
        with self._lock:
            return self.rev, {
                "version": VERSION, "since": self.since, "mastery": self.mastery,
                "letters": {k: dict(v) for k, v in self.letters.items()},
                "days": {d: {k: dict(v) for k, v in rows.items()} for d, rows in sorted(self.days.items())},
            }

    # ── counting ─────────────────────────────────────────────────────────────

    def press(self, letter):
        """A key pressed outside the quiz. Returns False for keys that are not counted."""
        with self._lock:
            if letter not in self._known:
                return False
            self._add(letter, presses=1)
            return True

    def quiz_result(self, letter, right, first_try):
        """A quiz question about ``letter`` ended: answered (right) or revealed."""
        with self._lock:
            if letter not in self._known:
                return False
            first = bool(right and first_try)
            row = self._add(letter, asked=1, right=int(bool(right)), first=int(first))
            row["streak"] = row["streak"] + 1 if first else 0
            return True

    def _add(self, letter, **counts):
        today = self._today()
        row = self.letters.setdefault(letter, {k: 0 for k in COUNTS + ("streak",)} | {"last": None})
        if today not in self.days:
            self._prune(today)
        day = self.days.setdefault(today, {}).setdefault(letter, {})
        for k, n in counts.items():
            row[k] += n
            if n:
                day[DAY_KEYS[k]] = day.get(DAY_KEYS[k], 0) + n
        row["last"] = today
        self.rev += 1
        return row

    def _prune(self, today):
        """Keep the DAYS_KEPT calendar days up to ``today``; dates after it (a clock set back) go too."""
        first = (datetime.date.fromisoformat(today) - datetime.timedelta(days=DAYS_KEPT - 1)).isoformat()
        for d in list(self.days):
            if not first <= d <= today:
                del self.days[d]

    # ── parent commands ──────────────────────────────────────────────────────

    def set_mastery(self, n):
        n = validate_mastery(n)
        with self._lock:
            if n != self.mastery:
                self.mastery = n
                self.rev += 1

    def reset(self):
        """Start again from zero; the mastery setting stays."""
        with self._lock:
            self._reset_state(self.mastery)
            self.rev += 1

    # ── the parent page ──────────────────────────────────────────────────────

    def snapshot(self):
        """Totals, today and the last 7 days per letter, and which letters are learned."""
        with self._lock:
            today = self._today()
            week_from = (datetime.date.fromisoformat(today) - datetime.timedelta(days=6)).isoformat()
            letters = {}
            for letter in self._universe:
                row = self.letters.get(letter)
                if row is None:
                    continue
                periods = {"today": {k: 0 for k in COUNTS}, "week": {k: 0 for k in COUNTS}}
                for day, rows in self.days.items():
                    c = rows.get(letter)
                    if c is None or not week_from <= day <= today:
                        continue
                    for k, short in DAY_KEYS.items():
                        periods["week"][k] += c.get(short, 0)
                        if day == today:
                            periods["today"][k] += c.get(short, 0)
                letters[letter] = {**{k: row[k] for k in COUNTS + ("streak", "last")},
                                   "learned": row["streak"] >= self.mastery, **periods}
            return {
                "boot": self.boot, "rev": self.rev, "today": today, "since": self.since,
                "mastery": self.mastery, "mastery_choices": list(MASTERY_CHOICES),
                "days_kept": DAYS_KEPT, "letters": letters,
                "max_presses": max((r["presses"] for r in self.letters.values()), default=0),
            }
