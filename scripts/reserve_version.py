#!/usr/bin/env python3
"""Reserve the next funkeykid release coordinate in version.json (FUNK-60).

Run once at release preparation and commit the result in the release PR:

    python3 scripts/reserve_version.py                      # next release
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
    ap.add_argument("--recover", action="store_true",
                    help="restore your own interrupted reservation (already claimed in the ledger) into "
                         "version.json: no new coordinate, no ledger change")
    args = ap.parse_args()

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
                sys.exit(f"{current['version']} is reserved but not released yet — release it first "
                         "(reservations are immutable)")
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
            try:
                how = release_ledger.ensure_remote_claim(record["release_sequence"])
            except release_ledger.LedgerError as e:
                sys.exit(f"{e} — version.json unchanged")
            write_atomic(record)
            print(f"recovered reservation {record['version']} (#{record['release_sequence']}, forge claim {how}) "
                  f"into version.json → release tag {versioning.release_tag(record['version'])}")
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


if __name__ == "__main__":
    main()
