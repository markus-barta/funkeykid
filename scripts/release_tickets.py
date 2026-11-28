#!/usr/bin/env python3
"""Write the reviewed ticket scope of a release for reserve_version.py --tickets (FUNK-88).

    python3 scripts/release_tickets.py FUNK-87 FUNK-88 > /tmp/release-tickets.json

Each key is exported with `aeon --instance ppm --json issue get <KEY>` (its EN/DE pill and benefit,
type and tags). List only the tickets this release really delivers (git log --first-parent since the
previous tag); no keys gives [] (a release without tickets).
"""
import json
import subprocess
import sys


def main():
    out = []
    for key in sys.argv[1:]:
        r = subprocess.run(["aeon", "--instance", "ppm", "--json", "issue", "get", key], capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"{key}: {r.stderr.strip() or 'aeon failed'}")
        out.append(json.loads(r.stdout))
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
