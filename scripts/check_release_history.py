#!/usr/bin/env python3
"""Check version.json against the released history and the reservation ledger (FUNK-60).

    python3 scripts/check_release_history.py                    # PRs / pushes
    python3 scripts/check_release_history.py --exclude-tag v<version> --require-completed   # release job

Released history = the v<YYMMDDhhmmss.0.0> tags; reservations = the annotated
reservation/stable/<N> tags (fetch tags first). The record must be exactly
the coordinate reserved for its sequence. A version already released must be
identical to its release; the first calendar release must equal the anchor;
every later one must succeed the latest (strictly later coordinate, sequence +
1, same channel, unchanged anchor). With --require-completed, every earlier
tag must also be a completed release (published GitHub release + manifest).
Fails closed on any inconsistency.
"""
import argparse
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import release_ledger  # noqa: E402
import versioning  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--exclude-tag", action="append", default=[],
                    help="ignore this tag (the release being published)")
    ap.add_argument("--require-completed", action="store_true",
                    help="every earlier release tag must be a completed GitHub release")
    args = ap.parse_args()
    current = versioning.load_record()
    released = versioning.released_records(ROOT, exclude=set(args.exclude_tag))
    try:
        versioning.check_reservation(current, versioning.reservation_ledger(ROOT))
        versioning.check_history(current, released)
        if args.require_completed:
            release_ledger.require_completed(released)
    except (versioning.VersionError, release_ledger.LedgerError) as e:
        sys.exit(f"release history check failed: {e}")
    latest = (max(released, key=lambda r: versioning.parse_calendar(r["version"]))["version"]
              if released else "none")
    print(f"release history OK: {current['version']} ({current['release_channel']} #{current['release_sequence']}, "
          f"reserved); latest released: {latest}" + ("; predecessors completed" if args.require_completed else ""))


if __name__ == "__main__":
    main()
