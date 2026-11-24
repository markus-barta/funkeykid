#!/usr/bin/env python3
"""Reserve the next funkeykid release coordinate in version.json (FUNK-60).

Run once at release preparation and commit the result in the release PR:

    python3 scripts/reserve_version.py                      # next release
    python3 scripts/reserve_version.py --first-from 3.2.0   # one-time migration from legacy

The coordinate is the current UTC second as YYMMDDhhmmss.0.0, strictly later
than the previous one (a same-second clock waits for the next second). Every
build, image, tag and manifest of the release reuses this value; build nodes
never derive it from their own clocks.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import versioning  # noqa: E402

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "version.json")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--first-from", metavar="LEGACY_VERSION",
                    help="create version.json for the migration from this last legacy version")
    args = ap.parse_args()

    if args.first_from:
        if os.path.exists(PATH):
            sys.exit("version.json exists: the migration anchor is immutable")
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
            },
        }
    else:
        record = versioning.load_record(PATH)
        record["version"] = versioning.reserve(previous=record["version"])
        record["release_sequence"] += 1

    versioning.validate_record(record)
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
        f.write("\n")
    print(f"reserved {record['version']} ({record['version_scheme']}, "
          f"{record['release_channel']} #{record['release_sequence']}) → tag {versioning.release_tag(record['version'])}")


if __name__ == "__main__":
    main()
