#!/usr/bin/env python3
"""Generate or check funkeykid's release history (FUNK-88).

    python3 scripts/release_history.py           # regenerate release-history/history.json from Git (full clone with tags)
    python3 scripts/release_history.py --check   # offline: the committed history matches version.json and the captures

reserve_version.py regenerates it at every reservation; run this after a release tag is pushed (the
tagged release then shows as published) or when --check fails.
"""
import argparse
import json
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import releasehistory  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="validate the committed files without Git")
    args = ap.parse_args()
    try:
        if args.check:
            h = json.load(open(os.path.join(ROOT, releasehistory.HISTORY_PATH), encoding="utf-8"))
            releasehistory.validate_sources(ROOT, h)
            print(f"release history: {len(h['releases'])} releases, consistent with version.json and the captures")
        else:
            h = releasehistory.generate(ROOT)
            print(f"release history: {len(h['releases'])} releases from git -> {releasehistory.HISTORY_PATH}")
    except (OSError, ValueError) as e:
        sys.exit(f"release history: {e}")


if __name__ == "__main__":
    main()
