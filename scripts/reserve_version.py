#!/usr/bin/env python3
"""Reserve the next funkeykid release coordinate in version.json (FUNK-60).

Run once at release preparation and commit the result in the release PR:

    python3 scripts/reserve_version.py                      # next release
    python3 scripts/reserve_version.py --first-from 3.2.0 \\
        --legacy-artifact ghcr.io/markus-barta/funkeykid@sha256:<digest>   # one-time migration

The coordinate is the current UTC second as YYMMDDhhmmss.0.0, strictly later
than every released one (a same-second clock waits for the next second); the
sequence is the latest released sequence + 1. A reservation is immutable: an
unreleased one is never replaced. The read/reserve/write runs under an
exclusive lock and the file is replaced atomically. The released history comes
from the v<version> tags (fetch tags first), so a stale checkout is rejected.
"""
import argparse
import fcntl
import json
import os
import subprocess
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

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
    args = ap.parse_args()

    subprocess.run(["git", "-C", ROOT, "fetch", "--tags", "--quiet"], check=False)
    with open(LOCK, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("another reservation is running")
        released = versioning.released_records(ROOT)

        if args.first_from:
            if os.path.exists(PATH) or released:
                sys.exit("already migrated: the migration anchor is immutable")
            versioning.parse(versioning.LEGACY, args.first_from)
            version = versioning.reserve()
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
            record["version"] = versioning.reserve(previous=latest["version"])
            record["release_sequence"] = latest["release_sequence"] + 1

        versioning.check_history(record, released)
        write_atomic(record)
    print(f"reserved {record['version']} ({record['version_scheme']}, "
          f"{record['release_channel']} #{record['release_sequence']}) → tag {versioning.release_tag(record['version'])}")


if __name__ == "__main__":
    main()
