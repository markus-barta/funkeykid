#!/usr/bin/env python3
"""Check version.json against the channel's released history (FUNK-60).

    python3 scripts/check_release_history.py                 # PRs / pushes
    python3 scripts/check_release_history.py --exclude-tag v<version>   # release job

Released history = the v<YYMMDDhhmmss.0.0> tags (fetch tags first). A version
already released must be identical to its release; the first calendar release
must equal the migration anchor; every later release must succeed the latest
one (strictly later coordinate, sequence + 1, same channel, unchanged anchor).
Fails closed on any inconsistency.
"""
import argparse
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

import versioning  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--exclude-tag", action="append", default=[],
                    help="ignore this tag (the release being published)")
    args = ap.parse_args()
    current = versioning.load_record()
    released = versioning.released_records(ROOT, exclude=set(args.exclude_tag))
    try:
        versioning.check_history(current, released)
    except versioning.VersionError as e:
        sys.exit(f"release history check failed: {e}")
    latest = (max(released, key=lambda r: versioning.parse_calendar(r["version"]))["version"]
              if released else "none")
    print(f"release history OK: {current['version']} ({current['release_channel']} #{current['release_sequence']}); "
          f"latest released: {latest}")


if __name__ == "__main__":
    main()
