"""Per-letter progress (FUNK-40) and play sessions (FUNK-42): key presses and quiz answers, per day.

Pure and thread-safe, no I/O: the server feeds it presses and quiz results
from the real keyboard (the parent page's test keys do not count), keeps
``export()`` in settings["progress"] and serves ``snapshot()`` to the parent
page. Unit-tested in tests/test_progress.py.

Stored as settings["progress"]::

    {"version": 1, "since": "2026-11-27", "mastery": 3,
     "letters": {"A": {"presses": 12, "asked": 4, "right": 3, "first": 2, "streak": 1,
                       "last": "2026-11-27"}},
     "days": {"2026-11-27": {"A": {"p": 5, "a": 1, "r": 1, "f": 1}}},
     "sessions": [{"day": "2026-11-27", "start": 1795788723, "end": 1795790421,
                   "keys": 140, "presses": 120, "asked": 6, "right": 5, "first": 4}],
     "key_days": {"2026-11-27": 140}}

- presses: the key pressed outside the quiz (letters and digits)
- asked: quiz questions about the letter that ended: answered right, or
  revealed after the last wrong try (a stopped question does not count)
- right: answered right, on any try; first: right on the first try
- streak: questions in a row answered right on the first try. The letter is
  "gelernt" while streak >= mastery, so a lucky guess does not make it, and a
  letter the child no longer knows loses the badge again.
- days: counts of the last DAYS_KEPT days (p/a/r/f as above); zeros are left out
- sessions (FUNK-42): play sessions of the last SESSIONS_DAYS_KEPT days, in the
  order they happened (not sorted by time: after a clock set back, the last one
  is still the running one). A session starts with a key from the real keyboard
  and ends after SESSION_GAP seconds without one; start/end are Unix seconds of
  the first and last key, day is the local date it started. keys counts every
  real key (also quiz answers and keys that are not letters);
  presses/asked/right/first as above. In memory each session also has an "id",
  unique for this run (the UI's key); it is not stored.
- key_days (FUNK-42): real keys per calendar day, for DAYS_KEPT days, counted on
  the day each key happens (a session over midnight plays on both days)
"""
import datetime
import random
import re
import threading
import time

VERSION = 1
DAYS_KEPT = 30
MASTERY_CHOICES = tuple(range(1, 11))
DEFAULT_MASTERY = 3
COUNTS = ("presses", "asked", "right", "first")
DAY_KEYS = {"presses": "p", "asked": "a", "right": "r", "first": "f"}
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
SESSION_GAP = 300             # seconds without a real key that end a play session
SESSIONS_DAYS_KEPT = 90
SESSIONS_MAX = 1000           # a cap for odd days (a key every 5 minutes, all day)
SESSION_COUNTS = ("keys", "presses", "asked", "right", "first")
RANGES = (1, 7, 30)           # the dashboard's periods: today, 7 and 30 days
RECENT_SESSIONS = 6
_SECONDS_FROM, _SECONDS_TO = 946684800, 4102444800     # 2000-01-01 .. 2100-01-01: a stored time outside is garbage


class ProgressError(ValueError):
    pass


def _today():
    return datetime.date.today().isoformat()


def _count(v):
    """A stored count: a non-negative int, anything else is 0."""
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def _second(v):
    """A stored Unix time: an int in a plausible range (so it always formats), else None."""
    return v if isinstance(v, int) and not isinstance(v, bool) and _SECONDS_FROM <= v < _SECONDS_TO else None


def _days_back(today, n):
    """The date n days before ``today`` (ISO)."""
    return (datetime.date.fromisoformat(today) - datetime.timedelta(days=n)).isoformat()


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

    def __init__(self, universe, *, today=_today, now=time.time, rng=None):
        self._universe = tuple(universe)        # the keys that are counted, in display order
        self._known = set(self._universe)
        self._today = today
        self._now = now
        self._lock = threading.Lock()
        self.boot = "%08x" % (rng or random.Random()).getrandbits(32)   # a restart starts rev again
        self.rev = 0
        self._last_id = 0                       # session ids: unique for this run, also across a reset
        self._reset_state(DEFAULT_MASTERY)

    def _reset_state(self, mastery):
        self.since = self._today()
        self.mastery = mastery
        self.letters = {}
        self.days = {}
        self.sessions = []
        self.key_days = {}
        self._day = self.since                  # the day the counters last saw (roll_day)

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
            for raw_s in (raw.get("sessions") if isinstance(raw.get("sessions"), list) else []):
                if not isinstance(raw_s, dict):
                    continue
                start, end, day = _second(raw_s.get("start")), _second(raw_s.get("end")), _date(raw_s.get("day"))
                if start is None or end is None or end < start or day is None:
                    continue
                self.sessions.append({"id": self._new_id(), "day": day, "start": start, "end": end,
                                      **{k: _count(raw_s.get(k)) for k in SESSION_COUNTS}})
            for day, n in (raw.get("key_days") if isinstance(raw.get("key_days"), dict) else {}).items():
                if _date(day) and _count(n):
                    self.key_days[day] = n
            self._prune(self._today())

    def export(self):
        """(rev, a deep copy for settings["progress"]): the copy never changes after it is handed out."""
        with self._lock:
            return self.rev, {
                "version": VERSION, "since": self.since, "mastery": self.mastery,
                "letters": {k: dict(v) for k, v in self.letters.items()},
                "days": {d: {k: dict(v) for k, v in rows.items()} for d, rows in sorted(self.days.items())},
                "sessions": [{k: v for k, v in x.items() if k != "id"} for x in self.sessions],
                "key_days": dict(sorted(self.key_days.items())),
            }

    def _new_id(self):
        self._last_id += 1
        return self._last_id

    # ── counting ─────────────────────────────────────────────────────────────

    def key(self):
        """Any key from the real keyboard, counted or not (also quiz answers): the session goes on."""
        with self._lock:
            today = self._today()
            if today not in self.key_days:
                self._prune(today)
            self.key_days[today] = self.key_days.get(today, 0) + 1
            self._session(keys=1)
            self.rev += 1

    def press(self, letter):
        """A key pressed outside the quiz. Returns False for keys that are not counted."""
        with self._lock:
            if letter not in self._known:
                return False
            self._add(letter, presses=1)
            self._session(presses=1)
            return True

    def quiz_result(self, letter, right, first_try):
        """A quiz question about ``letter`` ended: answered (right) or revealed."""
        with self._lock:
            if letter not in self._known:
                return False
            first = bool(right and first_try)
            row = self._add(letter, asked=1, right=int(bool(right)), first=int(first))
            row["streak"] = row["streak"] + 1 if first else 0
            self._session(asked=1, right=int(bool(right)), first=int(first))
            return True

    def _session(self, **counts):
        """Add to the running play session, or start one after SESSION_GAP seconds without a key
        (or when the clock went back)."""
        now = int(self._now())
        cur = self.sessions[-1] if self.sessions else None
        if cur is None or not 0 <= now - cur["end"] <= SESSION_GAP:
            today = self._today()
            cur = {"id": self._new_id(), "day": today, "start": now, "end": now, **{k: 0 for k in SESSION_COUNTS}}
            self.sessions.append(cur)
            self._prune_sessions(today)
        cur["end"] = now
        for k, n in counts.items():
            cur[k] += n

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
        first = _days_back(today, DAYS_KEPT - 1)
        for d in list(self.days):
            if not first <= d <= today:
                del self.days[d]
        for d in list(self.key_days):
            if not first <= d <= today:
                del self.key_days[d]
        self._prune_sessions(today)

    def _prune_sessions(self, today):
        """Keep the sessions of the SESSIONS_DAYS_KEPT calendar days up to ``today``, at most SESSIONS_MAX."""
        first = _days_back(today, SESSIONS_DAYS_KEPT - 1)
        self.sessions = [x for x in self.sessions if first <= x["day"] <= today][-SESSIONS_MAX:]

    def roll_day(self):
        """Called every second by the server: on a new day (or a date set back) drop what is past
        the kept days, also while nobody plays, and bump rev, so the parent page installs the new
        day. Returns True on a day change."""
        with self._lock:
            today = self._today()
            if today == self._day:
                return False
            self._day = today
            self._prune(today)
            self.rev += 1
            return True

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

    def dashboard(self):
        """The parent dashboard (FUNK-42): totals per day for DAYS_KEPT days (oldest first), each
        period in RANGES summed (with its per-letter counts), the latest sessions and the learned
        letters. Play time is first to last key of each session, booked on the day it started."""
        with self._lock:
            today = self._today()
            dates = [_days_back(today, n) for n in range(DAYS_KEPT - 1, -1, -1)]
            played = {}                                     # sessions: booked on the day they started
            for x in self.sessions:
                d = played.setdefault(x["day"], {"seconds": 0, "sessions": 0})
                d["seconds"] += x["end"] - x["start"]
                d["sessions"] += 1
            days = []
            for date in dates:
                rows = self.days.get(date, {})
                totals = {k: sum(c.get(short, 0) for c in rows.values()) for k, short in DAY_KEYS.items()}
                days.append({"day": date, **totals, **played.get(date, {"seconds": 0, "sessions": 0}),
                             "keys": self.key_days.get(date, 0)})     # keys: on the day they happened
            ranges = {}
            for n in RANGES:
                r = {k: 0 for k in COUNTS + ("seconds", "sessions", "keys", "active_days")}
                per = {}
                for date, day in zip(dates[-n:], days[-n:]):
                    for k in COUNTS + ("seconds", "sessions", "keys"):
                        r[k] += day[k]
                    if day["keys"] or day["presses"] or day["asked"]:
                        r["active_days"] += 1
                    for letter, c in self.days.get(date, {}).items():
                        row = per.setdefault(letter, {k: 0 for k in COUNTS})
                        for k, short in DAY_KEYS.items():
                            row[k] += c.get(short, 0)
                r["letters"] = {k: per[k] for k in self._universe if k in per}
                ranges[str(n)] = r
            recent = [dict(x, seconds=x["end"] - x["start"]) for x in reversed(self.sessions[-RECENT_SESSIONS:])]
            idle = int(self._now()) - self.sessions[-1]["end"] if self.sessions else None
            return {
                "boot": self.boot, "rev": self.rev, "today": today, "since": self.since,
                "mastery": self.mastery, "keys": list(self._universe),
                "learned": [k for k in self._universe
                            if k in self.letters and self.letters[k]["streak"] >= self.mastery],
                "days": days, "ranges": ranges, "recent": recent,
                "last": self.sessions[-1]["end"] if self.sessions else None,
                "idle": idle if idle is not None and idle >= 0 else None,    # seconds since the last key, by the server's clock
                "session_gap": SESSION_GAP, "sessions_days_kept": SESSIONS_DAYS_KEPT,
            }

    # ── CSV exports (FUNK-42) ────────────────────────────────────────────────
    # Semicolons and decimal commas, as German spreadsheet apps expect; times in local time.

    def sessions_csv(self, localtime=time.localtime):
        """Every kept session, oldest first."""
        def at(t):
            return time.strftime("%Y-%m-%d %H:%M:%S", localtime(t))
        with self._lock:
            sessions = [dict(x) for x in self.sessions]
        sessions.sort(key=lambda x: x["start"])
        lines = ["beginn;ende;minuten;tasten;buchstaben_ziffern;quiz_fragen;quiz_richtig;quiz_richtig_1_versuch"]
        for x in sessions:
            minutes = f"{(x['end'] - x['start']) / 60:.1f}".replace(".", ",")
            lines.append(";".join([at(x["start"]), at(x["end"]), minutes]
                                  + [str(x[k]) for k in SESSION_COUNTS]))
        return "\r\n".join(lines) + "\r\n"

    def letters_csv(self):
        """Every counted key in display order, totals since ``since``."""
        with self._lock:
            mastery = self.mastery
            rows = [(k, dict(self.letters[k]) if k in self.letters else None) for k in self._universe]
        lines = ["taste;gedrueckt;quiz_fragen;quiz_richtig;quiz_richtig_1_versuch;in_folge_richtig;gelernt;zuletzt"]
        for k, r in rows:
            r = r or {**{c: 0 for c in COUNTS + ("streak",)}, "last": None}
            lines.append(";".join([k] + [str(r[c]) for c in COUNTS + ("streak",)]
                                  + ["ja" if r["streak"] >= mastery else "nein", r["last"] or ""]))
        return "\r\n".join(lines) + "\r\n"
