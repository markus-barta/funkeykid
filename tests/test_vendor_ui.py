"""The parent UI's libraries are vendored and pinned (FUNK-69).

index.html loads Tailwind, DaisyUI and Alpine from static/vendor/ui only: no
CDN at runtime, so the page works offline and changes only with a release.
The files are byte-identical to the published packages (sha256 below); each
has a .gz sibling that aiohttp serves to browsers that accept gzip.

To update one: download the exact version from the package
(https://cdn.jsdelivr.net/npm/<name>@<version>/...), put it in a new
<name>-<version> folder with its LICENSE, regenerate the .gz
(gzip level 9, mtime 0), update index.html and the pins here.
"""
import gzip
import hashlib
import os
import re

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
UI = os.path.join(ROOT, "static", "vendor", "ui")

PINS = {
    "tailwindcss-2.2.19/tailwind.min.css": "b6ad97402eddb903e7a5d7a73ee47a679204efbdda4521a391cbad9df509b932",
    "daisyui-4.12.24/full.min.css": "85ab76db67c89f094af6170e05109bdcfc3642f2d7c4bd5a62e931d6aede4792",
    "alpinejs-3.17.5/cdn.min.js": "b4036e9e9ac22fa6f6ae1ba292e015e1f3e9c85d0d79e73e4c8eb9bb139b522e",
}


def _read(rel):
    with open(os.path.join(UI, rel), "rb") as f:
        return f.read()


def test_vendored_files_match_their_pins():
    for rel, digest in PINS.items():
        assert hashlib.sha256(_read(rel)).hexdigest() == digest, rel


def test_gz_siblings_hold_the_same_bytes():
    for rel in PINS:
        assert gzip.decompress(_read(rel + ".gz")) == _read(rel), rel


def test_every_vendored_library_has_its_license():
    for folder in {rel.split("/")[0] for rel in PINS}:
        text = _read(folder + "/LICENSE").decode()
        assert "MIT License" in text and "Permission is hereby granted" in text, folder


@pytest.mark.parametrize("asset,marker,holders", [
    # tailwind.min.css bundles modern-normalize, Alpine bundles @vue/shared and @vue/reactivity:
    # their MIT notices ship in the library's LICENSE
    ("tailwindcss-2.2.19/tailwind.min.css", "modern-normalize v1.1.0",
     ("Nicolas Gallagher", "Jonathan Neal", "Sindre Sorhus")),
    ("alpinejs-3.17.5/cdn.min.js", "@vue/reactivity v3.5.41", ("Yuxi (Evan) You",)),
])
def test_bundled_code_keeps_its_notices(asset, marker, holders):
    assert marker in _read(asset).decode()
    notice = _read(asset.split("/")[0] + "/LICENSE").decode()
    for holder in holders:
        assert holder in notice, holder


def test_no_unpinned_files_in_the_vendor_folder():
    found = set()
    for d, _, files in os.walk(UI):
        for f in files:
            found.add(os.path.relpath(os.path.join(d, f), UI).replace(os.sep, "/"))
    expected = set(PINS) | {r + ".gz" for r in PINS} | {r.split("/")[0] + "/LICENSE" for r in PINS}
    assert found == expected


def test_index_loads_only_local_scripts_and_styles():
    with open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8") as f:
        html = f.read()
    refs = re.findall(r'<(?:script|link)\b[^>]*?\b(?:src|href)="([^"]+)"', html)
    external = [r for r in refs if re.match(r"(?:https?:)?//", r)]
    assert external == []
    for rel in PINS:
        assert f"/static/vendor/ui/{rel}" in html, rel
