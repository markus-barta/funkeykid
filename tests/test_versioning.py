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
LEGACY_ART = "ghcr.io/markus-barta/funkeykid@sha256:" + "b" * 64
ANCHOR = {"legacy_scheme": "legacy", "last_legacy_version": "3.2.0",
          "first_calendar_version": "261124195854.0.0", "first_release_sequence": 1,
          "last_legacy_artifact": LEGACY_ART}


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


# ── review round 1 (FUNK-60): anchor, release order, history, reservation, closure ──

import subprocess  # noqa: E402

REPO = os.path.join(os.path.dirname(__file__), "..")


@pytest.mark.parametrize("drop", ["first_release_sequence", "last_legacy_artifact", "last_legacy_version",
                                  "first_calendar_version", "legacy_scheme"])
def test_incomplete_anchor_fails_closed(drop):
    anchor = {k: val for k, val in ANCHOR.items() if k != drop}
    with pytest.raises(v.VersionError):
        v.validate_anchor(anchor)
    with pytest.raises(v.VersionError):
        v.compare(v.parse("legacy", "3.2.0"), v.parse("inspr-calver-3", "261124195854.0.0"), anchor)


@pytest.mark.parametrize("art", ["ghcr.io/x/y:latest", "ghcr.io/x/y@sha256:abc", "sha256:" + "b" * 64, None])
def test_legacy_artifact_must_be_immutable_reference(art):
    with pytest.raises(v.VersionError):
        v.validate_anchor(dict(ANCHOR, last_legacy_artifact=art))


def rel(version, seq, channel="stable", scheme="inspr-calver-3"):
    return {"version_scheme": scheme, "version": version, "release_channel": channel, "release_sequence": seq}


def test_compare_releases_by_channel_sequence():
    a, b = rel("261124195854.0.0", 1), rel("261201120000.0.0", 2)
    assert v.compare_releases(a, b, ANCHOR) == -1 and v.compare_releases(b, a, ANCHOR) == 1
    with pytest.raises(v.VersionError):          # sequence and coordinate disagree
        v.compare_releases(rel("261201120000.0.0", 1), rel("261124195854.0.0", 2), ANCHOR)
    with pytest.raises(v.VersionError):          # different channels have no order
        v.compare_releases(a, rel("261201120000.0.0", 2, channel="beta"), ANCHOR)
    with pytest.raises(v.VersionError):          # calendar release without sequence
        v.compare_releases(a, {"version_scheme": "inspr-calver-3", "version": "261201120000.0.0",
                               "release_channel": "stable"}, ANCHOR)
    legacy = {"version_scheme": "legacy", "version": "3.2.0"}
    assert v.compare_releases(legacy, a, ANCHOR) == -1


def released_record(version, seq):
    return _record(version=version, release_sequence=seq)


def test_check_successor_rules():
    first = _record()
    assert v.check_successor(first, released_record("261201120000.0.0", 2))
    for bad in (released_record("261124195854.0.0", 2),          # same coordinate
                released_record("261101000000.0.0", 2),          # earlier coordinate (would move :latest back)
                released_record("261201120000.0.0", 3),          # sequence gap
                released_record("261201120000.0.0", 1)):         # sequence reuse
        with pytest.raises(v.VersionError):
            v.check_successor(first, bad)
    moved = released_record("261201120000.0.0", 2)
    moved["migration"] = dict(ANCHOR, last_legacy_version="3.1.1")
    with pytest.raises(v.VersionError):
        v.check_successor(first, moved)


def test_check_history():
    first = _record()
    assert v.check_history(first, [])                                   # first release = anchor
    with pytest.raises(v.VersionError):
        v.check_history(released_record("261201120000.0.0", 2), [])    # skipping the anchor
    assert v.check_history(first, [first])                              # already released, identical
    second = released_record("261201120000.0.0", 2)
    assert v.check_history(second, [first])
    with pytest.raises(v.VersionError):                                 # stale reservation vs newer release
        v.check_history(released_record("261125000000.0.0", 2), [first, second])
    with pytest.raises(v.VersionError):                                 # released version with other metadata
        v.check_history(dict(second, release_sequence=2, migration=dict(ANCHOR, last_legacy_version="3.1.1")),
                        [first, second])


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}).stdout


def _tagged_repo(tmp_path, records):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README").write_text("x")
    _git(repo, "add", "README")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "tag", "v2.4.0")                                         # legacy tag: ignored
    for tag, record in records:
        (repo / "version.json").write_text(json.dumps(record))
        _git(repo, "add", "version.json")
        _git(repo, "commit", "-q", "-m", tag)
        _git(repo, "tag", tag)
    return repo


def test_released_records_from_tags(tmp_path):
    first, second = _record(), released_record("261201120000.0.0", 2)
    repo = _tagged_repo(tmp_path, [("v261124195854.0.0", first), ("v261201120000.0.0", second)])
    got = v.released_records(str(repo))
    assert sorted(r["version"] for r in got) == ["261124195854.0.0", "261201120000.0.0"]
    assert [r["version"] for r in v.released_records(str(repo), exclude={"v261201120000.0.0"})] == ["261124195854.0.0"]


def test_released_records_rejects_mislabelled_tag(tmp_path):
    repo = _tagged_repo(tmp_path, [("v261201120000.0.0", _record())])  # tag ≠ its version.json
    with pytest.raises(v.VersionError):
        v.released_records(str(repo))


def _reserve_sandbox(tmp_path, record, tag=None):
    repo = tmp_path / "rs"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(os.path.join(REPO, "versioning.py"), repo / "versioning.py")
    shutil.copy(os.path.join(REPO, "scripts", "reserve_version.py"), repo / "scripts" / "reserve_version.py")
    (repo / "version.json").write_text(json.dumps(record, indent=2) + "\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    if tag:
        _git(repo, "tag", tag)
    return repo


def _reserve(repo):
    return subprocess.run([sys.executable, str(repo / "scripts" / "reserve_version.py")],
                          capture_output=True, text=True)


def test_reserve_refuses_to_replace_an_unreleased_reservation(tmp_path):
    repo = _reserve_sandbox(tmp_path, _record())
    before = (repo / "version.json").read_text()
    res = _reserve(repo)
    assert res.returncode != 0 and "not released yet" in (res.stderr + res.stdout)
    assert (repo / "version.json").read_text() == before


def test_reserve_next_after_release(tmp_path):
    repo = _reserve_sandbox(tmp_path, _record(), tag="v261124195854.0.0")
    res = _reserve(repo)
    assert res.returncode == 0, res.stderr
    new = json.loads((repo / "version.json").read_text())
    assert new["release_sequence"] == 2 and v.parse_calendar(new["version"]) > 261124195854
    assert new["migration"] == ANCHOR
    assert not (repo / "version.json.tmp").exists()


def test_bundle_tracked_closure(tmp_path):
    repo = tmp_path / "bt"
    bundle = repo / "static" / "vendor" / "inspr-versioning"
    shutil.copytree(vb.BUNDLE_DIR, bundle)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "add", "static")
    _git(repo, "rm", "-q", "--cached", "static/vendor/inspr-versioning/version.js")   # untracked payload
    _git(repo, "commit", "-q", "-m", "partial")
    with pytest.raises(vb.BundleError):
        vb.verify_tracked(str(bundle), repo=str(repo))
    _git(repo, "add", "static")
    _git(repo, "commit", "-q", "-m", "full")
    assert vb.verify_tracked(str(bundle), repo=str(repo)) is True
    (bundle / "display.json").write_bytes((bundle / "display.json").read_bytes() + b" ")
    with pytest.raises(vb.BundleError):                                 # modified vs committed bytes
        vb.verify_tracked(str(bundle), repo=str(repo))
    outside = tmp_path / "no-git"
    shutil.copytree(vb.BUNDLE_DIR, outside)
    assert vb.verify_tracked(str(outside), repo=str(outside)) is None   # no work tree: pins only
