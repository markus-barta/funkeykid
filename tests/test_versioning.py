"""Executable gates for INSPR Calendar Versioning in funkeykid (FUNK-60).

Covers the doctrine's required cases: valid/invalid calendar dates, rejection of
v1 and SemVer-legacy strings by the calendar parser, prerelease/build-metadata
rejection, same-second collisions, mixed-era ordering through the migration
anchor, absent/unknown schemes, the authoritative record and the release-tag pin.
"""
import datetime as dt
import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import versioning as v  # noqa: E402
import versioning_bundle as vb  # noqa: E402

UTC = dt.timezone.utc
ANCHOR = {"legacy_scheme": "legacy", "last_legacy_version": "3.2.0",
          "first_calendar_version": "261124195854.0.0", "first_release_sequence": 1}


# ── calendar grammar + Gregorian dates (doctrine "Examples") ────────────────

@pytest.mark.parametrize("value", [
    "260909113550.0.0", "261231235959.0.0", "280229120000.0.0", "991231235959.0.0", "100101000000.0.0"])
def test_valid_coordinates(value):
    assert v.parse_calendar(value) == int(value[:12])


@pytest.mark.parametrize("value", [
    "26.09.09", "26.09.09.11.35.50",            # v1
    "5.21.0", "3.2.0",                          # SemVer-legacy
    "20260909113550.0.0",                       # four-digit year
    "2609091135.0.0",                           # minute precision
    "260909113550",                             # missing .0.0
    "260909113550.0.1", "260909113550.1.0",     # MINOR/PATCH never move
    "260909113550.0.0-rc1",                     # prerelease
    "260909113550.0.0+g39d0b59",                # build metadata
    "260909240000.0.0", "260909116000.0.0",     # hour 24, minute 60
    "260229120000.0.0", "260431120000.0.0",     # non-existent dates
    "090909113550.0.0",                         # year < 10
    "v260909113550.0.0",                        # tag form is not a version
    " 260909113550.0.0", "260909113550.0.0\n",  # whitespace
    "", None, 260909113550,
])
def test_invalid_coordinates_rejected(value):
    with pytest.raises(v.VersionError):
        v.parse_calendar(value)


def test_calendar_datetime_is_utc():
    assert v.calendar_datetime("261124195854.0.0") == dt.datetime(2026, 11, 24, 19, 58, 54, tzinfo=UTC)


# ── discriminated schemes: never inferred, fail closed ──────────────────────

@pytest.mark.parametrize("scheme", [None, "", "semver", "calver", "INSPR-CalVer3", "inspr-calver-4"])
def test_absent_or_unknown_scheme_fails_closed(scheme):
    with pytest.raises(v.VersionError):
        v.parse(scheme, "261124195854.0.0")


def test_scheme_is_never_inferred_from_shape():
    # A calendar coordinate is also valid SemVer syntax; a legacy reader must not accept it as calendar,
    # and the calendar parser must not accept legacy/v1 strings.
    assert v.parse("legacy", "3.2.0").key == (3, 2, 0)
    with pytest.raises(v.VersionError):
        v.parse("inspr-calver-3", "3.2.0")
    with pytest.raises(v.VersionError):
        v.parse("inspr-calver-3", "26.09.09")
    with pytest.raises(v.VersionError):
        v.parse("legacy", "3.2.0-rc1")


def test_calver2_alias_shares_the_coordinate():
    a = v.parse("inspr-calendar-v2", "261001000000.0.0")
    b = v.parse("inspr-calver-3", "261124195854.0.0")
    assert v.compare(a, b) == -1 and v.compare(b, a) == 1


def test_v1_six_field_normalized():
    assert v.parse("inspr-calendar-v1", "26.09.09").key == (26, 9, 9, 0, 0, 0)
    with pytest.raises(v.VersionError):
        v.parse("inspr-calendar-v1", "26.02.30")


# ── ordering ────────────────────────────────────────────────────────────────

def test_calendar_ordering_is_integer_first_segment():
    xs = ["261124195854.0.0", "261001000000.0.0", "270101000000.0.0"]
    parsed = sorted((v.parse("inspr-calver-3", x) for x in xs), key=lambda p: p.key)
    assert [p.value for p in parsed] == ["261001000000.0.0", "261124195854.0.0", "270101000000.0.0"]


def test_mixed_era_ordering_only_through_anchor():
    legacy = v.parse("legacy", "3.2.0")
    older = v.parse("legacy", "3.1.1")
    cal = v.parse("inspr-calver-3", "261124195854.0.0")
    later = v.parse("inspr-calver-3", "270101000000.0.0")
    with pytest.raises(v.VersionError):
        v.compare(legacy, cal)                          # no anchor → fail closed
    assert v.compare(legacy, cal, ANCHOR) == -1
    assert v.compare(later, older, ANCHOR) == 1
    with pytest.raises(v.VersionError):                 # a legacy release after the anchor cannot exist
        v.compare(v.parse("legacy", "3.3.0"), cal, ANCHOR)
    with pytest.raises(v.VersionError):                 # calendar before the first calendar release
        v.compare(legacy, v.parse("inspr-calver-3", "261001000000.0.0"), ANCHOR)
    with pytest.raises(v.VersionError):                 # no anchor covers v1 here
        v.compare(v.parse("inspr-calendar-v1", "26.09.09"), cal, ANCHOR)


def test_semver_style_comparison_is_not_used_across_eras():
    # Lexically/SemVer-wise "3.2.0" < "261124195854.0.0" happens to hold, but must not be used:
    # with no anchor the comparison is refused.
    with pytest.raises(v.VersionError):
        v.compare(v.parse("legacy", "999.0.0"), v.parse("inspr-calver-3", "261124195854.0.0"))


# ── reservation: UTC, strictly later, same-second waits ─────────────────────

def test_reservation_is_utc_second():
    clock = lambda: dt.datetime(2026, 11, 24, 21, 58, 54, 999999, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    assert v.reserve(clock=clock) == "261124195854.0.0"


def test_same_second_reservation_waits_for_next_second():
    ticks = iter([dt.datetime(2026, 11, 24, 19, 58, 54, 100, tzinfo=UTC),
                  dt.datetime(2026, 11, 24, 19, 58, 54, 700000, tzinfo=UTC),
                  dt.datetime(2026, 11, 24, 19, 58, 55, 10, tzinfo=UTC)])
    slept = []
    got = v.reserve(previous="261124195854.0.0", clock=lambda: next(ticks), sleep=slept.append)
    assert got == "261124195855.0.0" and len(slept) == 2


def test_reservation_never_precedes_previous():
    ticks = iter([dt.datetime(2026, 11, 24, 19, 0, 0, tzinfo=UTC),       # clock behind the last release
                  dt.datetime(2026, 11, 24, 19, 58, 55, tzinfo=UTC)])
    assert v.reserve(previous="261124195854.0.0", clock=lambda: next(ticks), sleep=lambda s: None) == "261124195855.0.0"


# ── authoritative record + release tag pin ──────────────────────────────────

def test_repository_version_record_is_valid():
    r = v.load_record()
    assert r["version_scheme"] == "inspr-calver-3"
    assert r["migration"]["last_legacy_version"] == "3.2.0"
    assert v.parse_calendar(r["version"]) >= v.parse_calendar(r["migration"]["first_calendar_version"])


def _record(**over):
    r = {"version": "261124195854.0.0", "version_scheme": "inspr-calver-3", "release_channel": "stable",
         "release_sequence": 1, "migration": dict(ANCHOR)}
    r.update(over)
    return r


@pytest.mark.parametrize("bad", [
    {"version_scheme": "inspr-calendar-v2"},          # new reservations must declare CalVer3
    {"version_scheme": None},
    {"version": "3.2.0"},
    {"release_sequence": 0}, {"release_sequence": True}, {"release_sequence": "1"},
    {"release_channel": "Stable!"},
    {"migration": None},
    {"version": "261001000000.0.0"},                  # before the anchor
    {"release_sequence": 2},                          # first version must carry the first sequence
])
def test_invalid_records_fail_closed(bad):
    with pytest.raises(v.VersionError):
        v.validate_record(_record(**bad))


def test_next_release_record_is_valid():
    assert v.validate_record(_record(version="261201120000.0.0", release_sequence=2))


def test_load_record_fails_closed_on_unreadable(tmp_path):
    p = tmp_path / "version.json"
    p.write_text("{not json")
    with pytest.raises(v.VersionError):
        v.load_record(str(p))
    with pytest.raises(v.VersionError):
        v.load_record(str(tmp_path / "missing.json"))


def test_release_tag_must_equal_version():
    assert v.tag_matches("v261124195854.0.0", "261124195854.0.0")
    assert not v.tag_matches("261124195854.0.0", "261124195854.0.0")
    assert not v.tag_matches("v261124195855.0.0", "261124195854.0.0")
    assert not v.tag_matches("v3.2.0", "261124195854.0.0")


# ── vendored presentation bundle (pins) ─────────────────────────────────────

def test_vendored_bundle_matches_pins():
    m = vb.verify()
    assert m["revision"] == vb.PIN["revision"]


def _copy_bundle(tmp_path):
    dst = tmp_path / "bundle"
    shutil.copytree(vb.BUNDLE_DIR, dst)
    return dst


def test_bundle_rejects_altered_payload(tmp_path):
    d = _copy_bundle(tmp_path)
    (d / "version.js").write_bytes((d / "version.js").read_bytes() + b"\n// tampered")
    with pytest.raises(vb.BundleError):
        vb.verify(str(d))


def test_bundle_rejects_extra_and_missing_files(tmp_path):
    d = _copy_bundle(tmp_path)
    (d / "extra.js").write_text("x")
    with pytest.raises(vb.BundleError):
        vb.verify(str(d))
    (d / "extra.js").unlink()
    (d / "schemes.json").unlink()
    with pytest.raises(vb.BundleError):
        vb.verify(str(d))


def test_bundle_rejects_symlinked_payload(tmp_path):
    d = _copy_bundle(tmp_path)
    real = tmp_path / "real-display.json"
    shutil.copy(d / "display.json", real)
    (d / "display.json").unlink()
    os.symlink(real, d / "display.json")
    with pytest.raises(vb.BundleError):
        vb.verify(str(d))


def test_bundle_rejects_wrong_pins(tmp_path):
    d = _copy_bundle(tmp_path)
    bad = dict(vb.PIN, revision="0" * 40)
    with pytest.raises(vb.BundleError):
        vb.verify(str(d), pin=bad)
    manifest = json.loads((d / "manifest.json").read_text())
    manifest["revision"] = "0" * 40
    (d / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(vb.BundleError):
        vb.verify(str(d))
