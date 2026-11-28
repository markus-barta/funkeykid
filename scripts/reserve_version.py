#!/usr/bin/env python3
"""Reserve the next funkeykid release coordinate in version.json (FUNK-60).

Run once at release preparation and commit the result in the release PR:

    python3 scripts/reserve_version.py --tickets FILE       # next release (FUNK-88: FILE = scripts/release_tickets.py output)
    python3 scripts/reserve_version.py --first-from 3.2.0 \\
        --legacy-artifact ghcr.io/markus-barta/funkeykid@sha256:<digest>   # one-time migration

The coordinate is the current UTC second as YYMMDDhhmmss.0.0, strictly later
than every released one (a same-second clock waits for the next second); the
sequence is the latest released sequence + 1. A reservation is immutable:

- every earlier v<version> tag must be a completed release (published GitHub
  release with its manifest), otherwise finish that release first;
- the sequence is claimed atomically on the forge as the annotated tag
  reservation/stable/<N> (a competing preparer's push is rejected), and only
  then is version.json written (exclusive local lock, atomic replace).
"""
import argparse
import datetime
import fcntl
import json
import os
import subprocess
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import release_ledger  # noqa: E402
import versioning  # noqa: E402

PATH = os.path.join(ROOT, "version.json")
# FUNK-88: a checkout with release-history/ keeps a release history; its reservations freeze the ticket texts
HISTORY_ADOPTED = os.path.isdir(os.path.join(ROOT, "release-history"))
LOCK = PATH + ".lock"


def write_atomic(record):
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, PATH)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--first-from", metavar="LEGACY_VERSION",
                    help="create version.json for the migration from this last legacy version")
    ap.add_argument("--legacy-artifact", metavar="IMAGE@sha256:DIGEST",
                    help="immutable reference of the last legacy release artifact (with --first-from)")
    ap.add_argument("--tickets", metavar="FILE",
                    help="JSON array of the release's reviewed ticket exports (aeon --json issue get; "
                         "scripts/release_tickets.py writes it). Their EN/DE pills and benefits are frozen into "
                         "release-notes/<version>.json and the release history is regenerated (FUNK-88)")
    ap.add_argument("--recover", action="store_true",
                    help="restore your own interrupted reservation (already claimed in the ledger) into "
                         "version.json: no new coordinate, no ledger change")
    args = ap.parse_args()
    scope = None
    if args.tickets and not HISTORY_ADOPTED:
        sys.exit("--tickets needs a checkout with release-history/ (FUNK-88)")
    if args.tickets:
        import releasehistory
        try:
            scope = json.load(open(args.tickets, encoding="utf-8"))
            dry = releasehistory.freeze(scope, "000101000000.0.0", versioning.CALVER3, releasehistory.utc_now())
        except (OSError, ValueError) as e:
            sys.exit(f"--tickets {args.tickets}: {e}")
        gap = releasehistory.snapshot_gaps_error(dry)
        if gap:
            sys.exit(gap)                   # before anything is claimed

    fetched = subprocess.run(["git", "-C", ROOT, "fetch", "--tags", "--quiet"], capture_output=True, text=True)
    if fetched.returncode != 0:
        # e.g. a local claim conflicting with the forge ("would clobber existing tag"):
        # carry on; the forge check below / the atomic claim push decides.
        print(f"warning: git fetch --tags: {fetched.stderr.strip()}", file=sys.stderr)
    with open(LOCK, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("another reservation is running in this checkout")
        released = versioning.released_records(ROOT)
        ledger = versioning.reservation_ledger(ROOT)
        try:
            release_ledger.require_completed(released)
        except release_ledger.LedgerError as e:
            sys.exit(str(e))

        if args.first_from:
            if os.path.exists(PATH) or released or (ledger and not args.recover):
                sys.exit("already migrated: the migration anchor is immutable")
            if args.recover and 1 not in ledger:
                sys.exit("nothing to recover: sequence 1 is not reserved in the ledger")
            versioning.parse(versioning.LEGACY, args.first_from)
            version = ledger[1] if args.recover else versioning.reserve()
            record = {
                "version": version,
                "version_scheme": versioning.CALVER3,
                "release_channel": "stable",
                "release_sequence": 1,
                "migration": {
                    "legacy_scheme": versioning.LEGACY,
                    "last_legacy_version": args.first_from,
                    "first_calendar_version": version,
                    "first_release_sequence": 1,
                    "last_legacy_artifact": args.legacy_artifact,
                },
            }
        else:
            current = versioning.load_record(PATH)
            if current["version"] not in {r["version"] for r in released}:
                if (HISTORY_ADOPTED and not _capture_exists(current["version"])
                        and ledger.get(current["release_sequence"]) == current["version"]):
                    # FUNK-88: this reservation is written but its notes are not (interrupted): finish exactly it
                    if scope is None:
                        sys.exit(f"{current['version']} is reserved but its release notes are missing: rerun with "
                                 "--tickets FILE to freeze them (no new reservation is made)")
                    _notes_and_history(current, scope)
                    print(f"finished the release notes of the reservation {current['version']}")
                    return
                sys.exit(f"{current['version']} is reserved but not released yet — release it first "
                         "(reservations are immutable)")
            if HISTORY_ADOPTED and scope is None and not args.recover:     # before anything is claimed
                sys.exit("--tickets FILE is required: the release's ticket texts are frozen at reservation (FUNK-88); "
                         "write it with scripts/release_tickets.py <KEY>…  (an explicit [] means a release without tickets)")
            latest = max(released, key=lambda r: versioning.parse_calendar(r["version"]))
            record = dict(latest)
            record["release_sequence"] = latest["release_sequence"] + 1
            if args.recover:
                if record["release_sequence"] not in ledger:
                    sys.exit(f"nothing to recover: sequence {record['release_sequence']} is not reserved")
                record["version"] = ledger[record["release_sequence"]]
            else:
                # fail fast before taking a timestamp if the sequence is already claimed
                if record["release_sequence"] in ledger:
                    sys.exit(f"sequence {record['release_sequence']} is already reserved for "
                             f"{ledger[record['release_sequence']]}. If that was your own interrupted "
                             "reservation (push accepted, response lost, or stopped before writing "
                             "version.json), rerun with --recover; otherwise another preparer holds it.")
                floor = max([latest["version"], *ledger.values()], key=versioning.parse_calendar)
                try:
                    record["version"] = versioning.reserve(previous=floor)
                except versioning.VersionError as e:
                    sys.exit(str(e))

        versioning.check_history(record, released)
        if args.recover:
            # the local ledger holds this claim; the forge must hold exactly it too
            versioning.check_reservation(record, ledger)
            if HISTORY_ADOPTED and scope is None and not _capture_exists(record["version"]):
                sys.exit("--tickets FILE is required to recover: the release notes of "
                         f"{record['version']} are frozen with it (FUNK-88); version.json unchanged")
            try:
                how = release_ledger.ensure_remote_claim(record["release_sequence"])
            except release_ledger.LedgerError as e:
                sys.exit(f"{e} — version.json unchanged")
            write_atomic(record)
            print(f"recovered reservation {record['version']} (#{record['release_sequence']}, forge claim {how}) "
                  f"into version.json → release tag {versioning.release_tag(record['version'])}")
            _notes_and_history(record, scope)
            return
        if record["release_sequence"] in ledger:
            sys.exit(f"sequence {record['release_sequence']} is already reserved for "
                     f"{ledger[record['release_sequence']]}. If that was your own interrupted reservation "
                     "(push accepted, response lost, or stopped before writing version.json), rerun with "
                     "--recover; otherwise another preparer holds it.")
        try:
            tag = release_ledger.claim_reservation(record["release_sequence"], record["version"])
        except release_ledger.LedgerError as e:
            sys.exit(f"{e}. If that was your own interrupted reservation, rerun with --recover.")
        write_atomic(record)
        print(f"reserved {record['version']} ({record['version_scheme']}, {record['release_channel']} "
              f"#{record['release_sequence']}; ledger {tag}) → release tag {versioning.release_tag(record['version'])}")
        _notes_and_history(record, scope, fresh=True)   # still under the lock: no other preparer can freeze into it


def _capture_exists(version):
    return os.path.exists(os.path.join(ROOT, "release-notes", f"{version}.json"))


def _notes_and_history(record, scope, fresh=False):
    """FUNK-88: freeze the release's ticket texts (once; never overwritten) and regenerate the history.
    Called while the reservation lock is held."""
    if not HISTORY_ADOPTED:
        return
    import releasehistory
    v = record["version"]
    path = os.path.join(ROOT, releasehistory.NOTES_DIR, f"{v}.json")
    if fresh and os.path.exists(path):
        sys.exit(f"{releasehistory.NOTES_DIR}/{v}.json already exists for a version reserved just now: "
                 "a capture is never reused; investigate before committing")
    try:
        if scope is not None and not os.path.exists(path):
            now = datetime.datetime.strptime("20" + v[:12], "%Y%m%d%H%M%S").replace(
                tzinfo=datetime.timezone.utc)                    # captured at the reservation second
            snap = releasehistory.freeze(scope, v, record["version_scheme"], now)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "xb") as f:                          # exclusive: a capture is never rewritten
                f.write(releasehistory.dump_file(snap))
        releasehistory.generate(ROOT)
    except (OSError, ValueError) as e:
        sys.exit(f"reservation {v} written, but the release notes or history failed: {e}; fix the inputs and run "
                 "scripts/release_history.py before committing (do not reserve again)")
    print(f"notes: commit version.json, {releasehistory.NOTES_DIR}/{v}.json and {releasehistory.HISTORY_PATH} "
          "in the release commit")


if __name__ == "__main__":
    main()
