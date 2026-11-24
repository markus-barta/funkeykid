#!/usr/bin/env python3
"""Write .source-manifest.json, the build gate's source evidence (FUNK-60).

Every Docker build (CI or local) needs it: the Dockerfile copies it (missing →
the build fails) and verifies the vendored versioning bundle in the build
context against it, so an untracked or modified bundle payload can never reach
an image, even though the build context has no .git.

    python3 scripts/source_manifest.py && docker build .

It refuses to write unless the bundle is git-tracked and equal to the committed
bytes (versioning_bundle.verify_tracked).
"""
import hashlib
import json
import os
import subprocess
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

import versioning_bundle  # noqa: E402

OUT = os.path.join(ROOT, ".source-manifest.json")


def main():
    try:
        versioning_bundle.verify()
        if versioning_bundle.verify_tracked() is not True:
            sys.exit("source manifest needs a git work tree (the bundle must be git-tracked)")
    except versioning_bundle.BundleError as e:
        sys.exit(f"refusing to write the source manifest: {e}")
    git = lambda *a: subprocess.run(["git", "-C", ROOT, *a], check=True, capture_output=True, text=True).stdout
    rel = os.path.relpath(versioning_bundle.BUNDLE_DIR, ROOT)
    files = {}
    for path in git("ls-files", "--", rel).split():
        with open(os.path.join(ROOT, path), "rb") as f:
            files[os.path.basename(path)] = hashlib.sha256(f.read()).hexdigest()
    manifest = {
        "schema": "funkeykid.source-manifest.v1",
        "commit": git("rev-parse", "HEAD").strip(),
        "dirty": bool(git("status", "--porcelain", "--untracked-files=no").strip()),
        "bundle_dir": rel,
        "bundle_files": files,
    }
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    os.replace(tmp, OUT)
    print(f"source manifest: {manifest['commit'][:12]}{' (dirty)' if manifest['dirty'] else ''}, "
          f"{len(files)} tracked bundle files")


if __name__ == "__main__":
    main()
