"""INSPR Calendar Versioning for funkeykid (FUNK-60).

Normative source: the INSPR versioning doctrine (AGENTS-VERSIONING.md,
`inspr-calver-3`). funkeykid migrated from `legacy` (MAJOR.MINOR.PATCH, last
3.2.0) to `inspr-calver-3` = `YYMMDDhhmmss.0.0`, the UTC reservation second.

Rules implemented here (all covered by tests/test_versioning.py):
- the calendar parser is strict: exact grammar + a real Gregorian date;
  v1 coordinates, SemVer-legacy strings, prerelease/build suffixes are rejected;
- versions are discriminated by an explicit scheme; absent/unknown schemes fail
  closed, and the scheme is never inferred from the string's shape;
- within the calendar era (CalVer3 and its alias CalVer2) the first segment is
  compared as one integer; across eras only via the migration anchor;
- a reservation never reuses or precedes the previous coordinate: it waits for
  the next UTC second.
"""
import datetime as _dt
import json
import os
import re
import time
from collections import namedtuple

CALVER3 = "inspr-calver-3"
CALVER2 = "inspr-calendar-v2"       # deprecated alias, same coordinate
CALVER1 = "inspr-calendar-v1"
LEGACY = "legacy"
SCHEMES = (LEGACY, CALVER1, CALVER2, CALVER3)
CALENDAR_SCHEMES = (CALVER3, CALVER2)

_CALENDAR = re.compile(
    r"(?:[1-9][0-9])(?:0[1-9]|1[0-2])(?:0[1-9]|[12][0-9]|3[01])"
    r"(?:[01][0-9]|2[0-3])(?:[0-5][0-9])(?:[0-5][0-9])\.0\.0")
_V1 = re.compile(r"([0-9]{2})\.([0-9]{2})\.([0-9]{2})(?:\.([0-9]{2})\.([0-9]{2})\.([0-9]{2}))?")
_LEGACY = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")

Version = namedtuple("Version", "scheme value key")


class VersionError(ValueError):
    """Invalid, ambiguous or incomparable version input (fail closed)."""


def _gregorian(yy, mm, dd, hh, mi, ss):
    try:
        return _dt.datetime(2000 + yy, mm, dd, hh, mi, ss, tzinfo=_dt.timezone.utc)
    except ValueError as e:
        raise VersionError(f"not a real calendar date/time: {e}") from None


def parse_calendar(value):
    """Validate a canonical `YYMMDDhhmmss.0.0` coordinate; return its first segment."""
    if not isinstance(value, str) or not _CALENDAR.fullmatch(value):
        raise VersionError(f"not a canonical calendar version: {value!r}")
    fields = [int(value[i:i + 2]) for i in range(0, 12, 2)]
    _gregorian(*fields)
    return int(value[:12])


def calendar_datetime(value):
    """UTC datetime of a canonical calendar coordinate."""
    parse_calendar(value)
    return _gregorian(*[int(value[i:i + 2]) for i in range(0, 12, 2)])


def format_calendar(moment):
    """Canonical coordinate for a UTC moment (whole seconds)."""
    moment = moment.astimezone(_dt.timezone.utc)
    if not 2010 <= moment.year <= 2099:
        raise VersionError("year outside 2010–2099 cannot be encoded")
    return moment.strftime("%y%m%d%H%M%S") + ".0.0"


def parse(scheme, value):
    """Discriminated parse. The scheme must be given explicitly; never inferred."""
    if scheme is None:
        raise VersionError("absent version scheme")
    if scheme not in SCHEMES:
        raise VersionError(f"unknown version scheme: {scheme!r}")
    if not isinstance(value, str) or not value:
        raise VersionError("absent version value")
    if scheme in CALENDAR_SCHEMES:
        return Version(scheme, value, parse_calendar(value))
    if scheme == CALVER1:
        m = _V1.fullmatch(value)
        if not m:
            raise VersionError(f"not an inspr-calendar-v1 coordinate: {value!r}")
        fields = [int(g) if g is not None else 0 for g in m.groups()]
        _gregorian(*fields)
        return Version(scheme, value, tuple(fields))     # six-field normalized
    m = _LEGACY.fullmatch(value)
    if not m:
        raise VersionError(f"not a legacy MAJOR.MINOR.PATCH version: {value!r}")
    return Version(scheme, value, tuple(int(g) for g in m.groups()))


def _era(version):
    return "calendar" if version.scheme in CALENDAR_SCHEMES else version.scheme


def _cmp(a, b):
    return (a > b) - (a < b)


_ARTIFACT = re.compile(r"[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}")


def _positive_int(value, name):
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise VersionError(f"{name} must be a positive integer")
    return value


def validate_anchor(anchor):
    """A complete legacy→calendar migration anchor, or VersionError."""
    if not isinstance(anchor, dict):
        raise VersionError("migration anchor missing")
    if anchor.get("legacy_scheme") not in SCHEMES or anchor.get("legacy_scheme") in CALENDAR_SCHEMES:
        raise VersionError("migration anchor needs a non-calendar legacy_scheme")
    parse(anchor["legacy_scheme"], anchor.get("last_legacy_version"))
    parse_calendar(anchor.get("first_calendar_version"))
    _positive_int(anchor.get("first_release_sequence"), "first_release_sequence")
    artifact = anchor.get("last_legacy_artifact")
    if not isinstance(artifact, str) or not _ARTIFACT.fullmatch(artifact):
        raise VersionError("last_legacy_artifact must be an immutable image reference (…@sha256:<64 hex>)")
    return anchor


def compare(a, b, anchor=None):
    """-1/0/1 for two parsed versions. Same era: that era's rules (CalVer2 and
    CalVer3 share one coordinate). Across eras: only through a complete
    migration `anchor`; anything outside it fails closed."""
    if _era(a) == _era(b):
        return _cmp(a.key, b.key)
    if anchor is None:
        raise VersionError("cross-era comparison needs the migration anchor")
    validate_anchor(anchor)
    legacy_scheme = anchor["legacy_scheme"]
    last_legacy = parse(legacy_scheme, anchor["last_legacy_version"])
    first_calendar = parse(CALVER3, anchor["first_calendar_version"])
    eras = {_era(a), _era(b)}
    if eras != {"calendar", _era(last_legacy)}:
        raise VersionError("no migration anchor covers these eras")
    old, new, sign = (a, b, -1) if _era(a) != "calendar" else (b, a, 1)
    if compare(old, last_legacy) > 0:
        raise VersionError(f"{old.value} is later than the last legacy release {last_legacy.value}")
    if compare(new, first_calendar) < 0:
        raise VersionError(f"{new.value} predates the first calendar release {first_calendar.value}")
    return sign


def compare_releases(a, b, anchor):
    """Order two release records {version_scheme, version, release_channel,
    release_sequence}. Calendar releases are ordered by their channel's
    immutable sequence, which must agree with the coordinates; legacy releases
    carry no sequence and are ordered against calendar ones only through the
    anchor. Missing or inconsistent metadata fails closed."""
    validate_anchor(anchor)
    pa, pb = parse(a.get("version_scheme"), a.get("version")), parse(b.get("version_scheme"), b.get("version"))
    first_seq = anchor["first_release_sequence"]
    for rec, p in ((a, pa), (b, pb)):
        if p.scheme in CALENDAR_SCHEMES:
            if _positive_int(rec.get("release_sequence"), "release_sequence") < first_seq:
                raise VersionError("calendar release_sequence precedes the migration anchor")
            if not isinstance(rec.get("release_channel"), str):
                raise VersionError("calendar release needs its release_channel")
    if pa.scheme in CALENDAR_SCHEMES and pb.scheme in CALENDAR_SCHEMES:
        if a["release_channel"] != b["release_channel"]:
            raise VersionError("releases from different channels have no order")
        by_seq = _cmp(a["release_sequence"], b["release_sequence"])
        by_coord = _cmp(pa.key, pb.key)
        if by_seq != by_coord:
            raise VersionError("release_sequence and coordinate order disagree")
        return by_seq
    return compare(pa, pb, anchor)


def check_successor(previous, current):
    """`current` must be the next release of `previous`'s channel: same channel,
    unchanged migration anchor, strictly later coordinate, sequence + 1."""
    validate_record(previous)
    validate_record(current)
    if current["release_channel"] != previous["release_channel"]:
        raise VersionError("a release must stay on its channel")
    if current["migration"] != previous["migration"]:
        raise VersionError("the migration anchor is immutable")
    if parse_calendar(current["version"]) <= parse_calendar(previous["version"]):
        raise VersionError(f"{current['version']} is not later than the released {previous['version']}")
    if current["release_sequence"] != previous["release_sequence"] + 1:
        raise VersionError(f"release_sequence must be {previous['release_sequence'] + 1}")
    return current


def check_history(current, released):
    """Validate `current` against the channel's released records (immutable
    history). Already released → must be identical; otherwise the first
    release must equal the anchor, and every later one must succeed the latest."""
    validate_record(current)
    same_channel = [r for r in released if r["release_channel"] == current["release_channel"]]
    by_version = {r["version"]: r for r in same_channel}
    if current["version"] in by_version:
        if by_version[current["version"]] != current:
            raise VersionError(f"{current['version']} was released with different metadata")
        return current
    if not same_channel:
        m = current["migration"]
        if current["version"] != m["first_calendar_version"] or current["release_sequence"] != m["first_release_sequence"]:
            raise VersionError("the first calendar release must equal the migration anchor")
        return current
    latest = max(same_channel, key=lambda r: parse_calendar(r["version"]))
    return check_successor(latest, current)


def released_records(git_dir=None, exclude=()):
    """version.json of every calendar release tag (v<YYMMDDhhmmss.0.0>) in a
    git checkout; a tag whose record disagrees with its name fails closed."""
    import subprocess
    git_dir = git_dir or os.path.dirname(os.path.abspath(__file__))
    tags = subprocess.run(["git", "-C", git_dir, "tag", "--list", "v*"], check=True,
                          capture_output=True, text=True).stdout.split()
    records = []
    for tag in tags:
        version = tag[1:]
        if tag in exclude or not _CALENDAR.fullmatch(version):
            continue                      # legacy tags (v2.x) are not calendar releases
        raw = subprocess.run(["git", "-C", git_dir, "show", f"{tag}:version.json"], check=True,
                             capture_output=True, text=True).stdout
        record = validate_record(json.loads(raw))
        if record["version"] != version:
            raise VersionError(f"tag {tag} carries version.json {record['version']}")
        records.append(record)
    return records


def reserve(previous=None, clock=None, sleep=time.sleep):
    """Reserve a new coordinate: the current UTC second, strictly later than
    `previous`. A same-second (or earlier) clock waits for the next second;
    no hidden tie-breaker, no reuse."""
    clock = clock or (lambda: _dt.datetime.now(_dt.timezone.utc))
    floor = parse_calendar(previous) if previous else None
    while True:
        candidate = format_calendar(clock().replace(microsecond=0))
        if floor is None or parse_calendar(candidate) > floor:
            return candidate
        sleep(0.2)


def release_tag(version):
    """Git tag for a release: one literal `v` + the canonical version."""
    parse_calendar(version)
    return "v" + version


def tag_matches(tag, version):
    """A release tag must be exactly `v` + version.json's version."""
    return isinstance(tag, str) and tag == release_tag(version)


def validate_record(record):
    """Validate the authoritative version.json record; return it unchanged."""
    if not isinstance(record, dict):
        raise VersionError("version record must be an object")
    if record.get("version_scheme") != CALVER3:
        raise VersionError(f"version_scheme must be {CALVER3} for new reservations")
    parse_calendar(record.get("version"))
    channel = record.get("release_channel")
    if not isinstance(channel, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", channel):
        raise VersionError("release_channel must be a short lowercase identifier")
    sequence = _positive_int(record.get("release_sequence"), "release_sequence")
    anchor = validate_anchor(record.get("migration"))
    first = anchor["first_calendar_version"]
    first_seq = anchor["first_release_sequence"]
    if parse_calendar(record["version"]) < parse_calendar(first) or sequence < first_seq:
        raise VersionError("current release precedes the migration anchor")
    if (record["version"] == first) != (sequence == first_seq):
        raise VersionError("first calendar version and first release_sequence must coincide")
    return record


def load_record(path=None):
    """Read and validate version.json (fails closed on any problem)."""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "version.json")
    try:
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError) as e:
        raise VersionError(f"cannot read the authoritative version record: {e}") from None
    return validate_record(record)
