"""FUNK-88: the release history (inspr.release-history.v1), its frozen notes and the read-only API."""
import asyncio
import datetime
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import releasehistory as rh  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
NOW = datetime.datetime(2026, 11, 28, 17, 0, 0, tzinfo=datetime.timezone.utc)


def test_json_is_written_like_go():
    assert rh.gojson({"a": "<b> & ü"}) == '{"a":"\\u003cb\\u003e \\u0026 ü"}'
    assert rh.dump_file({"x": [1]}) == b'{\n  "x": [\n    1\n  ]\n}\n'


@pytest.mark.parametrize("v,ok", [("261128154900.0.0", True), ("260231120000.0.0", False), ("261128246000.0.0", False),
                                  ("v261128154900.0.0", False), ("261128154900.0.1", False), ("061128154900.0.0", False)])
def test_versions(v, ok):
    assert rh.valid_version(v) is ok


def test_tickets_classify_and_groups():
    assert rh.tickets("fix: FUNK-84 and FUNK-84, PIXD-126; UTF-8") == ["FUNK-84", "PIXD-126", "UTF-8"]
    assert rh.tickets("ＦＵＮＫ-1") == []                              # ASCII only, like Go
    assert rh.classify("feat(ui): x") == ("feat", "ui") and rh.classify("release: reserve") == ("release", "")
    assert rh.classify("release 3: x") == ("other", "") and rh.classify("hotfix!: y") == ("fix", "")
    meta = {"FUNK-1": {"bug": True, "benefit": False}, "FUNK-2": {"bug": False, "benefit": True}}
    assert rh.group_change("other", ["FUNK-2", "FUNK-1"], meta) == "fixes"
    assert rh.group_change("other", ["FUNK-9"], meta) == "other" and rh.group_change("release", [], meta) == ""


def export(key, bug=False, hide=None, **texts):
    d = {"issue_key": key, "type": "work", "tags": ["bug"] if bug else [], "pill_en": "P", "pill_de": "P de",
         "benefit_en": "B", "benefit_de": "B de"}
    d.update(texts)
    if hide is not None:
        d["hide_from_release_notes"] = hide
    return d


def test_freeze_takes_visible_tickets_and_names_gaps():
    s = rh.freeze([export("FUNK-1"), export("FUNK-2", bug=True), export("FUNK-3", hide=True),
                   export("FUNK-4", pill_de=""), export("FUNK-5", pill_en="", pill_de="", benefit_en="", benefit_de="")],
                  "261128170000.0.0", "inspr-calver-3", NOW)
    assert [(i["key"], i["group"]) for i in s["items"]] == [("FUNK-1", "features"), ("FUNK-2", "fixes"), ("FUNK-4", "features")]
    assert s["hidden"] == 1 and s["captured_at"] == "2026-11-28T17:00:00Z" and s["frozen"] is True
    assert s["gaps"] == ["FUNK-4: English or German release text is incomplete.", "FUNK-5: no public pill or benefit was captured."]
    assert rh.snapshot_gaps_error(s).startswith("release text missing")
    with pytest.raises(rh.HistoryError):
        rh.freeze({}, "261128170000.0.0", "inspr-calver-3", NOW)
    with pytest.raises(rh.HistoryError):
        rh.freeze([export("FUNK-1"), export("FUNK-1")], "261128170000.0.0", "inspr-calver-3", NOW)


def test_a_snapshot_is_read_strictly():
    raw = rh.dump_file(rh.freeze([export("FUNK-1")], "261128170000.0.0", "inspr-calver-3", NOW))
    n = rh.notes_from_snapshot(raw, "261128170000.0.0", "release-notes/261128170000.0.0.json")
    assert n["items"][0]["key"] == "FUNK-1" and len(n["snapshot_sha256"]) == 64 and n["release_revision"] == 0
    bad = json.loads(raw)
    bad["extra"] = 1
    for b in (rh.dump_file(bad), raw.replace(b'"frozen": true', b'"frozen": false')):
        with pytest.raises(rh.HistoryError):
            rh.notes_from_snapshot(b, "261128170000.0.0", "x")
    with pytest.raises(rh.HistoryError):
        rh.notes_from_snapshot(raw, "261128170001.0.0", "x")


@pytest.mark.parametrize("msg,want", [
    ("funkeykid v261128170000.0.0 · inspr-calver-3 · stable · release_sequence 29 · Version history", ("Version history", "stable", 29)),
    ("261128154900.0.0 (stable #28): redo old voices (FUNK-87)\n\nmore", ("redo old voices (FUNK-87)", "stable", 28)),
    ("something else", ("something else", "", 0)),
])
def test_tag_messages(msg, want):
    assert rh.parse_tag_message(msg) == want


# ── a whole history from a small Git repository ─────────────────────────────

def git(repo, *args, env=None):
    e = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
             GIT_COMMITTER_EMAIL="t@example.invalid", **(env or {}))
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=e).stdout


def write_version(repo, v, seq):
    (repo / "version.json").write_text(json.dumps({"version": v, "version_scheme": "inspr-calver-3", "release_channel": "stable",
                                                   "release_sequence": seq}, indent=2) + "\n")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(rh, "LAST_UNCAPTURED", "261128100000.0.0")
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("a")
    git(r, "add", ".")
    git(r, "commit", "-q", "-m", "feat: first thing (FUNK-1)")
    write_version(r, "261128100000.0.0", 1)
    git(r, "add", ".")
    git(r, "commit", "-q", "-m", "release: reserve 261128100000.0.0 (stable #1)")
    git(r, "tag", "-a", "v261128100000.0.0", "-m", "261128100000.0.0 (stable #1): first thing (FUNK-1)")
    (r / "b.txt").write_text("b")
    git(r, "add", ".")
    git(r, "commit", "-q", "-m", "qa: FUNK-2 smoke")
    git(r, "commit", "-q", "--allow-empty", "-m", "fix: the bug (FUNK-3)")
    write_version(r, "261128110000.0.0", 2)
    (r / "release-notes").mkdir()
    snap = rh.freeze([export("FUNK-2", pill_en="Better voices", pill_de="Bessere Stimmen"), export("FUNK-3", bug=True)],
                     "261128110000.0.0", "inspr-calver-3", datetime.datetime(2026, 11, 28, 11, 0, tzinfo=datetime.timezone.utc))
    (r / "release-notes" / "261128110000.0.0.json").write_bytes(rh.dump_file(snap))
    git(r, "add", ".")
    git(r, "commit", "-q", "-m", "release: reserve 261128110000.0.0 (stable #2)")
    git(r, "tag", "-a", "v261128110000.0.0", "-m", "funkeykid v261128110000.0.0 · inspr-calver-3 · stable · release_sequence 2 · Better voices")
    return r


def test_build_walks_tags_versions_and_commits(repo):
    h = rh.build(str(repo), NOW)
    assert [r["version"] for r in h["releases"]] == ["261128110000.0.0", "261128100000.0.0"]
    new, old = h["releases"]
    assert new["state"] == old["state"] == "published" and new["tag"] == "v261128110000.0.0"
    assert old["headline"] == "first thing (FUNK-1)" and old["notes"]["source"] == "unavailable" and old["release_sequence"] == 1
    assert new["notes"]["source"] == "v261128110000.0.0:release-notes/261128110000.0.0.json"
    assert new["presentation"]["headline"] == "Better voices · P" and new["headline"] == "Better voices"
    subjects = {c["subject"]: c for c in new["changes"]}
    assert subjects["qa: FUNK-2 smoke"]["group"] == "features" and subjects["qa: FUNK-2 smoke"]["linked_tickets"][0]["key"] == "FUNK-2"
    assert subjects["fix: the bug (FUNK-3)"]["group"] == "fixes" and "group" not in subjects["release: reserve 261128110000.0.0 (stable #2)"]
    assert new["tickets"] == ["FUNK-2", "FUNK-3"]
    assert new["evidence"]["source_url"].endswith(new["evidence"]["source_commit"]) and new["evidence"]["unavailable"] == [rh.EVIDENCE_ALWAYS]
    assert h["pending"]["base_version"] == "261128110000.0.0" and h["pending"]["unavailable"] == []
    rh.validate(h)
    assert list(new) == ["version_scheme", "notes", "presentation", "version", "tag", "release_channel", "release_sequence", "state",
                         "reserved_at", "tagged_at", "published_at", "headline", "tickets", "changes", "changes_omitted", "evidence"]


def test_a_reserved_head_reads_its_working_tree_capture_and_needs_one(repo):
    write_version(repo, "261128120000.0.0", 3)
    with pytest.raises(rh.HistoryError, match="needs release-notes/261128120000.0.0.json"):
        rh.build(str(repo), NOW)
    snap = rh.freeze([export("FUNK-9")], "261128120000.0.0", "inspr-calver-3", NOW)
    (repo / "release-notes" / "261128120000.0.0.json").write_bytes(rh.dump_file(snap))
    h = rh.generate(str(repo), now=NOW)
    top = h["releases"][0]
    assert top["state"] == "reserved" and top["tag"] == "" and top["notes"]["source"] == "release-notes/261128120000.0.0.json"
    assert h["pending"]["unavailable"] == [rh.PENDING_OFFLINE]
    rh.validate_sources(str(repo), json.loads((repo / "release-history" / "history.json").read_text()))


def test_frozen_notes_can_not_change(repo):
    write_version(repo, "261128120000.0.0", 3)
    path = repo / "release-notes" / "261128120000.0.0.json"
    path.write_bytes(rh.dump_file(rh.freeze([export("FUNK-9")], "261128120000.0.0", "inspr-calver-3", NOW)))
    rh.generate(str(repo), now=NOW)
    path.write_bytes(rh.dump_file(rh.freeze([export("FUNK-9", benefit_en="changed")], "261128120000.0.0", "inspr-calver-3", NOW)))
    with pytest.raises(rh.HistoryError, match="frozen notes changed"):
        rh.generate(str(repo), now=NOW)


def test_validate_rejects_broken_histories(repo):
    h = rh.build(str(repo), NOW)
    for breaker in (lambda x: x["releases"][0].update(tickets=None), lambda x: x["releases"].reverse(),
                    lambda x: x["releases"][0].update(tag="v1"), lambda x: x.update(product="hausnetz"),
                    lambda x: x["releases"][0]["evidence"].update(source_url="https://example.invalid/")):
        bad = json.loads(json.dumps(h))
        breaker(bad)
        with pytest.raises(rh.HistoryError):
            rh.validate(bad)


def test_the_committed_history_matches_version_json_and_the_captures():
    """The offline gate (no Git): what the image will carry is current and consistent."""
    h = json.load(open(os.path.join(ROOT, rh.HISTORY_PATH), encoding="utf-8"))
    rh.validate_sources(ROOT, h)


# ── the API ─────────────────────────────────────────────────────────────────

pytest.importorskip("aiohttp")
import server  # noqa: E402


class Req:
    def __init__(self, **match):
        self.match_info, self.headers, self.query = match, {}, {}


def test_the_api_serves_the_history_with_the_running_version(monkeypatch, repo):
    h = rh.build(str(repo), NOW)
    monkeypatch.setattr(server, "RELEASE_HISTORY", h)
    monkeypatch.setattr(server, "VERSION", "261128110000.0.0")
    r = asyncio.run(server.api_releases(Req()))
    d = json.loads(r.body)
    assert r.status == 200 and r.headers["Cache-Control"] == "no-store" and list(d)[-2:] == ["current", "live_since"]
    assert d["current"] == "261128110000.0.0" and len(d["releases"]) == 2
    assert json.loads(asyncio.run(server.api_release(Req(version="v261128100000.0.0"))).body)["version"] == "261128100000.0.0"
    assert asyncio.run(server.api_release(Req(version="nope"))).status == 400
    assert asyncio.run(server.api_release(Req(version="261128090000.0.0"))).status == 404
    p = json.loads(asyncio.run(server.api_releases_pending(Req())).body)
    assert p["base_version"] == "261128110000.0.0"
    monkeypatch.setattr(server, "VERSION", "261128130000.0.0")
    p = json.loads(asyncio.run(server.api_releases_pending(Req())).body)
    assert p["unavailable"] == [rh.PENDING_SYNTH]
    monkeypatch.setattr(server, "RELEASE_HISTORY", None)
    assert asyncio.run(server.api_releases(Req())).status == 503 and asyncio.run(server.api_release(Req(version="x"))).status == 503


# ── round 1 of the gate: strict types, schemes, the pending tip ─────────────

def test_validate_checks_types_and_times_like_go(repo):
    h = rh.build(str(repo), NOW)
    for breaker in (lambda x: x.update(generated_at="not-a-time"),
                    lambda x: x["releases"][0]["changes"][0].update(tickets=[123]),
                    lambda x: x["releases"][0]["notes"].update(hidden=True),
                    lambda x: x["releases"][0]["notes"]["items"][0].update(benefit_en=5),
                    lambda x: x["releases"][0].update(tagged_at="yesterday"),
                    lambda x: x["releases"][0]["evidence"].update(image="x"),
                    lambda x: x["pending"].update(source_ref=None)):
        bad = json.loads(json.dumps(h))
        breaker(bad)
        with pytest.raises(rh.HistoryError):
            rh.validate(bad)


def test_snapshots_are_typed_strictly():
    good = rh.freeze([export("FUNK-1")], "261128170000.0.0", "inspr-calver-3", NOW)
    for k, v in (("hidden", True), ("captured_at", "soon")):
        with pytest.raises(rh.HistoryError):
            rh.notes_from_snapshot(rh.dump_file(dict(good, **{k: v})), "261128170000.0.0", "x")
    bad = json.loads(json.dumps(good))
    bad["items"][0]["benefit_en"] = 7
    with pytest.raises(rh.HistoryError):
        rh.notes_from_snapshot(rh.dump_file(bad), "261128170000.0.0", "x")


def test_the_offline_gate_compares_schemes(repo):
    write_version(repo, "261128120000.0.0", 3)
    (repo / "release-notes" / "261128120000.0.0.json").write_bytes(
        rh.dump_file(rh.freeze([export("FUNK-9")], "261128120000.0.0", "inspr-calver-3", NOW)))
    h = rh.generate(str(repo), now=NOW)
    bad = json.loads(json.dumps(h))
    bad["releases"][0]["version_scheme"] = "inspr-calendar-v2"
    with pytest.raises(rh.HistoryError):
        rh.validate_sources(str(repo), bad)
    (repo / "release-notes" / "261128120000.0.0.json").write_bytes(
        rh.dump_file(rh.freeze([export("FUNK-9")], "261128120000.0.0", "inspr-calendar-v2", NOW)))
    with pytest.raises(rh.HistoryError):
        rh.validate_sources(str(repo), h)


def test_pending_names_the_main_tip(repo):
    git(repo, "commit", "-q", "--allow-empty", "-m", "docs: after the release")
    tip = git(repo, "rev-parse", "HEAD").strip()
    p = rh.build(str(repo), NOW)["pending"]
    assert p["source_commit"] == tip and p["source_ref"] == "refs/heads/main"
    assert [c["subject"] for c in p["changes"]] == ["docs: after the release"]
