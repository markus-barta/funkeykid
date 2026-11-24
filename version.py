"""funkeykid version info.

The authoritative release coordinate is version.json (INSPR Calendar
Versioning, `inspr-calver-3`, FUNK-60), validated on import: an invalid record
fails closed. Reserve a new coordinate with scripts/reserve_version.py.
"""
import os

import versioning

_RECORD = versioning.load_record()

VERSION = _RECORD["version"]                    # canonical YYMMDDhhmmss.0.0
VERSION_SCHEME = _RECORD["version_scheme"]      # inspr-calver-3
RELEASE_CHANNEL = _RECORD["release_channel"]
RELEASE_SEQUENCE = _RECORD["release_sequence"]
MIGRATION = dict(_RECORD["migration"])          # legacy → calendar anchor (last legacy 3.2.0)
BUILD = os.environ.get("FUNKEYKID_BUILD", "dev")
REPO = "https://github.com/markus-barta/funkeykid"


def build_time():
    """Return container build time from /app/.build_time, or None."""
    try:
        with open(os.path.join(os.path.dirname(__file__), ".build_time")) as f:
            return f.read().strip()
    except Exception:
        return None
