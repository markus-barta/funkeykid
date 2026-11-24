"""Verify the vendored INSPR versioning presentation bundle (FUNK-60).

The footer's Pretty/SemVer version display uses the shared renderer from
inspr-at/inspr, vendored offline into static/vendor/inspr-versioning/ with the
source repository's `scripts/versioning-bundle.mjs`, plus that commit's
LICENSE (the bundler omits it). Runs in pytest and in every Docker build:
missing, extra, altered, symlinked or non-regular payloads fail the build.

Refresh (release preparation, only when the approved config/renderer changed):
  node scripts/versioning-bundle.mjs <commit> <config-sha256> <new-dir>   # in inspr-at/inspr
  add LICENSE from the same commit to the manifest, keep upstreamManifestSha256,
  then update all three pins below together.
"""
import hashlib
import json
import os
import stat
import sys

# Pinned literals, independent of the candidate bundle being verified.
PIN = {
    "repository": "inspr-at/inspr",
    "revision": "bd0a13816f728ad91c36d849f88168bcac4fdc6e",
    "config_sha256": "bf6aa144cd73dc97dc941f0f0914138b04ae0a28fbead5123465b15b1f2a2fd9",
    "manifest_sha256": "49500c0c3373da4ce13eee46d81fc2a242531145e8f94dab41b029e51449fca6",
}
BUNDLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "vendor", "inspr-versioning")


class BundleError(ValueError):
    pass


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _regular_file(path):
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise BundleError(f"bundle payload must be a regular file: {os.path.basename(path)}")
    with open(path, "rb") as f:
        return f.read()


def verify(path=BUNDLE_DIR, pin=PIN):
    """Return the manifest if the bundle matches the pins exactly, else raise."""
    try:
        st = os.lstat(path)
    except OSError as e:
        raise BundleError(f"bundle missing: {e}") from None
    if not stat.S_ISDIR(st.st_mode):
        raise BundleError("bundle must be a regular directory")
    raw = _regular_file(os.path.join(path, "manifest.json"))
    if _sha256(raw) != pin["manifest_sha256"]:
        raise BundleError("versioning manifest digest mismatch")
    manifest = json.loads(raw)
    if (manifest.get("repository") != pin["repository"] or manifest.get("revision") != pin["revision"]
            or manifest.get("expectedConfigSha256") != pin["config_sha256"]):
        raise BundleError("versioning source pin mismatch")
    expected = {"manifest.json", *(f["outputPath"] for f in manifest["files"])}
    actual = set(os.listdir(path))
    if actual != expected:
        raise BundleError(f"bundle has missing or extra files: "
                          f"missing={sorted(expected - actual)} extra={sorted(actual - expected)}")
    for entry in manifest["files"]:
        payload = _regular_file(os.path.join(path, entry["outputPath"]))
        if len(payload) != entry["size"] or _sha256(payload) != entry["sha256"]:
            raise BundleError(f"versioning payload mismatch: {entry['outputPath']}")
        if entry["outputPath"] == "display.json" and _sha256(payload) != pin["config_sha256"]:
            raise BundleError("versioning configuration digest mismatch")
    return manifest


def verify_tracked(path=BUNDLE_DIR, repo=None):
    """Inside a git work tree: every bundle file must be tracked and equal to the
    committed bytes (an untracked or modified payload never reaches a build).
    Returns None outside git (the Docker build context has no .git; CI builds
    from a clean checkout of tracked files)."""
    import subprocess
    repo = repo or os.path.dirname(os.path.abspath(__file__))
    try:
        inside = subprocess.run(["git", "-C", repo, "rev-parse", "--is-inside-work-tree"],
                                capture_output=True, text=True)
    except FileNotFoundError:
        return None
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return None
    rel = os.path.relpath(path, repo)
    tracked = set(subprocess.run(["git", "-C", repo, "ls-files", "--", rel],
                                 capture_output=True, text=True, check=True).stdout.split())
    present = {os.path.join(rel, name) for name in os.listdir(path)}
    if present != tracked:
        raise BundleError(f"bundle files not tracked by git: untracked={sorted(present - tracked)} "
                          f"missing={sorted(tracked - present)}")
    dirty = subprocess.run(["git", "-C", repo, "status", "--porcelain", "--", rel],
                           capture_output=True, text=True, check=True).stdout.strip()
    if dirty:
        raise BundleError(f"bundle differs from the committed bytes: {dirty.splitlines()[:3]}")
    return True


if __name__ == "__main__":
    try:
        verify()
        tracked = verify_tracked()
    except (BundleError, OSError, ValueError, KeyError) as e:
        print(f"Invalid INSPR versioning bundle: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Verified INSPR versioning bundle at {PIN['revision']}"
          + (" (git-tracked, committed bytes)" if tracked else " (no git work tree: pins only)"))
