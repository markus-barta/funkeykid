"""Release ledger helpers for funkeykid (FUNK-60), shared by the reservation
and history scripts.

- A *reservation* is an annotated tag `reservation/stable/<N>` on the forge.
  Pushing it is atomic: a second preparer claiming the same sequence is
  rejected, so two clones can never both reserve sequence N.
- A release is *completed* only when its `v<version>` tag has a published (not
  draft) GitHub release carrying a `release-manifest.json` for that version.
  A successor may only be reserved or published after every earlier tag is
  completed.
"""
import json
import os
import subprocess
import tempfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


class LedgerError(RuntimeError):
    pass


def _git(*args, check=True):
    return subprocess.run(["git", "-C", ROOT, *args], check=check, capture_output=True, text=True)


def release_completed(tag, version):
    """True if tag has a published GitHub release with a manifest for version."""
    try:
        view = subprocess.run(["gh", "release", "view", tag, "--json", "isDraft,assets"],
                              cwd=ROOT, capture_output=True, text=True)
    except FileNotFoundError:
        raise LedgerError("the GitHub CLI (gh) is required to check release completion") from None
    if view.returncode != 0:
        return False
    info = json.loads(view.stdout)
    if info.get("isDraft") or "release-manifest.json" not in {a.get("name") for a in info.get("assets", [])}:
        return False
    with tempfile.TemporaryDirectory() as d:
        got = subprocess.run(["gh", "release", "download", tag, "-p", "release-manifest.json", "-D", d],
                             cwd=ROOT, capture_output=True, text=True)
        if got.returncode != 0:
            return False
        with open(os.path.join(d, "release-manifest.json")) as f:
            manifest = json.load(f)
    return manifest.get("version") == version and bool(manifest.get("artifacts"))


def require_completed(records):
    """Every given released record must be a completed release."""
    for r in records:
        tag = "v" + r["version"]
        if not release_completed(tag, r["version"]):
            raise LedgerError(f"{tag} is tagged but not a completed release (draft, missing manifest, or "
                              "failed publication): rerun its release job before releasing a successor")


def ensure_remote_claim(sequence, channel="stable"):
    """Make sure the forge holds exactly our local claim for sequence N.

    'confirmed' if the forge already has the identical tag object; 'pushed' if
    it had none and our unchanged local claim was published now. Raises if the
    forge holds a different claim (another preparer) or cannot be reached."""
    name = f"reservation/{channel}/{sequence}"
    local = _git("rev-parse", "--verify", "-q", f"refs/tags/{name}", check=False).stdout.strip()
    if not local:
        raise LedgerError(f"no local claim {name} to recover")
    remote = _git("ls-remote", "--tags", "origin", f"refs/tags/{name}", check=False)
    if remote.returncode != 0:
        raise LedgerError(f"cannot read the forge to verify {name}: {remote.stderr.strip()}")
    held = [line.split()[0] for line in remote.stdout.splitlines() if line.split()[-1] == f"refs/tags/{name}"]
    if held:
        if held[0] != local:
            raise LedgerError(f"the forge holds a different claim for {name} (another preparer). "
                              f"Drop your local one (git tag -d {name}), fetch tags and reserve again")
        return "confirmed"
    pushed = _git("push", "origin", f"refs/tags/{name}", check=False)
    if pushed.returncode != 0:
        raise LedgerError(f"could not publish your local claim {name}: "
                          f"{pushed.stderr.strip().splitlines()[-1] if pushed.stderr.strip() else 'push rejected'}")
    return "pushed"


def claim_reservation(sequence, version, channel="stable"):
    """Atomically claim sequence N for version on the forge (first wins)."""
    name = f"reservation/{channel}/{sequence}"
    body = json.dumps({"version": version, "release_sequence": sequence})
    made = _git("tag", "-a", name, "-m", body, check=False)
    if made.returncode != 0:
        # the tag is already known locally (e.g. fetched from another preparer)
        raise LedgerError(f"sequence {sequence} is already reserved ({made.stderr.strip() or 'tag exists'})")
    pushed = _git("push", "origin", f"refs/tags/{name}", check=False)
    if pushed.returncode != 0:
        _git("tag", "-d", name, check=False)
        raise LedgerError(f"sequence {sequence} is already reserved by another preparer "
                          f"({pushed.stderr.strip().splitlines()[-1] if pushed.stderr.strip() else 'push rejected'})")
    return name
