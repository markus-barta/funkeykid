"""Set export / import as ZIP (FUNK-43): round trip, conflicts, hostile files.

Run: python -m pytest tests/ (no hardware needed; uses temp dirs).
"""
import io
import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import setbundle as sb  # noqa: E402


def _set():
    return {
        "name": "Tiere", "description": "für Lena",
        "letters": {
            "A": {"entries": [
                {"word": "Affe", "image": "a_affe.png", "enabled": True, "_imageDesc": "a cheeky monkey",
                 "tracks": {"FX": {"file": "a_affe.mp3", "enabled": True, "prompt": "Affe kreischt", "volume": 140},
                            "DE": {"file": "a_affe_de.mp3", "enabled": False, "prompt": "Affe", "volume": 80}}},
                {"word": "Ameise", "image": "", "enabled": False, "tracks": {}},
            ]},
            "Ä": {"entries": [{"word": "Äpfel", "image": "ae.png", "enabled": True,
                               "tracks": {"EN": {"file": "apples_en.mp3", "enabled": True, "prompt": "apples", "volume": 100}}}]},
        },
        "numbers": {
            "3": {"word": "Drei Enten", "image_subject": "three ducks",
                  "tracks": {"DE": {"file": "drei.mp3", "enabled": True, "prompt": "Drei Enten", "volume": 120}},
                  "backgrounds": [{"image": "bg_teich.png", "enabled": True}, {"image": "bg_wiese.png", "enabled": False}]},
        },
        "track_order": [{"kind": "DE", "enabled": True}, {"kind": "FX", "enabled": True}, {"kind": "EN", "enabled": False}],
    }


FILES = {"sounds": {"a_affe.mp3": b"AFFE-FX", "a_affe_de.mp3": b"AFFE-DE", "apples_en.mp3": b"APPLES", "drei.mp3": b"DREI"},
         "images": {"a_affe.png": b"PNG-AFFE", "ae.png": b"PNG-AE", "bg_teich.png": b"PNG-TEICH", "bg_wiese.png": b"PNG-WIESE"}}


@pytest.fixture
def dirs(tmp_path):
    def make(name, files=FILES):
        base = tmp_path / name
        for folder in ("sounds", "images"):
            (base / folder).mkdir(parents=True)
            for fname, data in files.get(folder, {}).items():
                (base / folder / fname).write_bytes(data)
        return str(base / "sounds"), str(base / "images")
    return make


def _export(dirs, tmp_path, set_data=None):
    snd, img = dirs("src")
    body = sb.build_zip("tiere", set_data or _set(), snd, img, "test")
    path = tmp_path / "set.zip"
    path.write_bytes(body)
    return str(path)


def _import(path, snd, img, resolutions=None):
    set_data, members = sb.read_zip(path)
    return sb.apply(path, set_data, members, snd, img, resolutions)


# ── AC4: round trip ──────────────────────────────────────────────────────────

def test_round_trip_preserves_tracks_prompts_volume_backgrounds_and_files(dirs, tmp_path):
    path = _export(dirs, tmp_path)
    snd, img = dirs("dst", files={})
    set_data, members = sb.read_zip(path)
    plan = sb.plan(path, set_data, members, snd, img)
    assert {f["status"] for f in plan} == {"new"} and len(plan) == 8
    imported, summary = sb.apply(path, set_data, members, snd, img)
    assert imported == sb.clean_set(_set())
    a = imported["letters"]["A"]["entries"][0]
    assert a["tracks"]["FX"] == {"file": "a_affe.mp3", "enabled": True, "prompt": "Affe kreischt", "volume": 140}
    assert a["tracks"]["DE"]["enabled"] is False and a["_imageDesc"] == "a cheeky monkey"
    assert imported["numbers"]["3"]["backgrounds"][1] == {"image": "bg_wiese.png", "enabled": False}
    assert imported["track_order"][0] == {"kind": "DE", "enabled": True}
    for folder, base in (("sounds", snd), ("images", img)):
        for fname, data in FILES[folder].items():
            assert open(os.path.join(base, fname), "rb").read() == data
    assert len(summary["written"]) == 8 and not summary["renamed"] and not summary["missing"]


def test_export_lists_missing_files_and_never_fails_on_stored_data(dirs, tmp_path):
    snd, img = dirs("src", files={"sounds": {"a_affe.mp3": b"X"}})
    odd = _set()
    odd["letters"]["A"]["entries"][1]["image"] = "../../etc/passwd"     # a stored oddity
    body = sb.build_zip("tiere", odd, snd, img)
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        manifest = json.loads(z.read(sb.MANIFEST))
        assert sorted(z.namelist()) == [sb.MANIFEST, "sounds/a_affe.mp3"]
    assert "images/../../etc/passwd" in manifest["missing"] and "sounds/drei.mp3" in manifest["missing"]


# ── conflicts: rename (default) / keep / overwrite; identical files are reused ─

def test_conflicts_and_identical_files(dirs, tmp_path):
    path = _export(dirs, tmp_path)
    snd, img = dirs("dst", files={"sounds": {"a_affe.mp3": b"OTHER", "drei.mp3": b"DREI", "a_affe_de.mp3": b"OLD-DE"},
                                  "images": {"a_affe.png": b"OTHER-PNG", "a_affe-2.png": b"TAKEN"}})
    set_data, members = sb.read_zip(path)
    status = {f["path"]: f["status"] for f in sb.plan(path, set_data, members, snd, img)}
    assert status["sounds/a_affe.mp3"] == "conflict" and status["sounds/drei.mp3"] == "same"
    assert status["images/a_affe.png"] == "conflict" and status["images/ae.png"] == "new"
    imported, summary = sb.apply(path, set_data, members, snd, img,
                                 {"sounds/a_affe_de.mp3": "overwrite", "images/a_affe.png": "keep"})
    a = imported["letters"]["A"]["entries"][0]
    # default rename: stored next to the existing file, reference rewritten
    assert a["tracks"]["FX"]["file"] == "a_affe-2.mp3"
    assert open(os.path.join(snd, "a_affe-2.mp3"), "rb").read() == b"AFFE-FX"
    assert open(os.path.join(snd, "a_affe.mp3"), "rb").read() == b"OTHER"          # untouched
    # overwrite: same name, new content
    assert a["tracks"]["DE"]["file"] == "a_affe_de.mp3" and open(os.path.join(snd, "a_affe_de.mp3"), "rb").read() == b"AFFE-DE"
    # keep: reference stays, the existing file stays
    assert a["image"] == "a_affe.png" and open(os.path.join(img, "a_affe.png"), "rb").read() == b"OTHER-PNG"
    assert summary["renamed"] == {"sounds/a_affe.mp3": "a_affe-2.mp3"}
    assert "sounds/drei.mp3" in summary["reused"] and summary["kept"] == ["images/a_affe.png"]
    assert summary["overwritten"] == ["sounds/a_affe_de.mp3"]


def test_rename_skips_names_already_taken(dirs, tmp_path):
    path = _export(dirs, tmp_path)
    snd, img = dirs("dst", files={"sounds": {"drei.mp3": b"X", "drei-2.mp3": b"Y"}})
    imported, summary = _import(path, snd, img)
    assert imported["numbers"]["3"]["tracks"]["DE"]["file"] == "drei-3.mp3"


def test_missing_and_local_files(dirs, tmp_path):
    snd, img = dirs("src", files={"sounds": {"a_affe.mp3": b"X"}})
    path = tmp_path / "set.zip"
    path.write_bytes(sb.build_zip("tiere", _set(), snd, img))
    dsnd, dimg = dirs("dst", files={"images": {"ae.png": b"HERE"}})
    set_data, members = sb.read_zip(str(path))
    status = {f["path"]: f["status"] for f in sb.plan(str(path), set_data, members, dsnd, dimg)}
    assert status["sounds/a_affe.mp3"] == "new" and status["images/ae.png"] == "local" and status["sounds/drei.mp3"] == "missing"


def test_bad_resolution_is_rejected(dirs, tmp_path):
    path = _export(dirs, tmp_path)
    snd, img = dirs("dst", files={})
    with pytest.raises(sb.BundleError):
        _import(path, snd, img, {"sounds/a_affe.mp3": "delete-everything"})


# ── hostile or broken files ──────────────────────────────────────────────────

def _zip(tmp_path, members, manifest=None, fmt=sb.FORMAT):
    path = tmp_path / "evil.zip"
    with zipfile.ZipFile(path, "w") as z:
        if manifest is not False:
            z.writestr(sb.MANIFEST, json.dumps(manifest or {"format": fmt, "set": _set()}))
        for name, data in members.items():
            z.writestr(name, data)
    return str(path)


@pytest.mark.parametrize("member", ["../evil.mp3", "sounds/../../evil.mp3", "/etc/evil.mp3", "other/x.mp3",
                                    "sounds/.hidden.mp3", "sounds/x.exe", "images/x.mp3", "sounds/sub/x.mp3",
                                    "sounds\\..\\x.mp3"])
def test_unexpected_or_traversing_members_are_rejected(tmp_path, member):
    with pytest.raises(sb.BundleError):
        sb.read_zip(_zip(tmp_path, {member: b"x"}))


@pytest.mark.parametrize("bad_set", [
    {"letters": {"A": {"entries": [{"image": "../x.png"}]}}},
    {"letters": {"A": {"entries": [{"tracks": {"FX": {"file": "/etc/passwd.mp3"}}}]}}},
    {"numbers": {"3": {"backgrounds": [{"image": "x.svg"}]}}},
    {"numbers": {"12": {}}},
    {"letters": {"TOOLONG": {"entries": []}}},
    {"letters": {"<": {"entries": []}}},
    {"letters": {"/": {"entries": []}}},
    {"letters": {"a": {"entries": []}, "A": {"entries": []}}},
    {"letters": {"A": {"entries": ["not an object"]}}},
    {"letters": {"A": {"entries": [{"word": "w" * (sb.WORD_MAX + 1)}]}}},
    {"name": ""},
    {"letters": {str(i): {"entries": []} for i in range(sb.MAX_LETTERS + 1)}},
])
def test_malformed_sets_are_rejected(tmp_path, bad_set):
    raw = dict(_set(), **bad_set)
    with pytest.raises(sb.BundleError):
        sb.read_zip(_zip(tmp_path, {}, {"format": sb.FORMAT, "set": raw}))


def test_not_a_set_file(tmp_path):
    for path in (_zip(tmp_path, {}, manifest=False), _zip(tmp_path, {}, fmt="something.else")):
        with pytest.raises(sb.BundleError):
            sb.read_zip(path)
    (tmp_path / "plain.zip").write_bytes(b"not a zip at all")
    with pytest.raises(sb.BundleError):
        sb.read_zip(str(tmp_path / "plain.zip"))
    path = tmp_path / "badjson.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(sb.MANIFEST, "{not json")
    with pytest.raises(sb.BundleError):
        sb.read_zip(str(path))


def test_size_and_count_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "MAX_FILE_BYTES", 10)
    with pytest.raises(sb.BundleError):
        sb.read_zip(_zip(tmp_path, {"sounds/a_affe.mp3": b"x" * 11}))
    monkeypatch.setattr(sb, "MAX_FILE_BYTES", 1000)
    monkeypatch.setattr(sb, "MAX_TOTAL_BYTES", 15)
    with pytest.raises(sb.BundleError):
        sb.read_zip(_zip(tmp_path, {"sounds/a_affe.mp3": b"x" * 10, "sounds/drei.mp3": b"x" * 10}))
    monkeypatch.setattr(sb, "MAX_MEMBERS", 2)
    with pytest.raises(sb.BundleError):
        sb.read_zip(_zip(tmp_path, {"sounds/a_affe.mp3": b"1", "sounds/drei.mp3": b"2"}))


def test_duplicate_member_names_are_rejected(tmp_path):
    path = tmp_path / "dup.zip"
    with zipfile.ZipFile(path, "w") as z, pytest.warns(UserWarning):
        z.writestr(sb.MANIFEST, json.dumps({"format": sb.FORMAT, "set": _set()}))
        z.writestr("sounds/drei.mp3", b"1")
        z.writestr("sounds/drei.mp3", b"2")
    with pytest.raises(sb.BundleError):
        sb.read_zip(str(path))


def test_a_member_lying_about_its_size_cannot_write_more(dirs, tmp_path, monkeypatch):
    """The declared size can lie: writing stops at the limit while streaming."""
    path = _export(dirs, tmp_path)
    snd, img = dirs("dst", files={})
    set_data, members = sb.read_zip(path)
    monkeypatch.setattr(sb, "MAX_FILE_BYTES", 5)        # smaller than the real member sizes
    with pytest.raises(sb.BundleError):
        sb.apply(path, set_data, members, snd, img)
    assert not [f for f in os.listdir(snd) if f.endswith(".tmp")]


def test_encrypted_members_are_rejected(tmp_path):
    path = _zip(tmp_path, {"sounds/drei.mp3": b"x"})
    data = bytearray(open(path, "rb").read())
    # set the "encrypted" flag bit in every central directory header (signature PK\\x01\\x02, flags at +8)
    i = data.find(b"PK\x01\x02")
    while i != -1:
        data[i + 8] |= 0x1
        i = data.find(b"PK\x01\x02", i + 4)
    open(path, "wb").write(bytes(data))
    with pytest.raises(sb.BundleError):
        sb.read_zip(path)


def test_clean_set_drops_unknown_fields_and_migrates_legacy_sound():
    raw = _set()
    raw["evil"] = "<script>"
    raw["letters"]["A"]["entries"][0]["_generating"] = True
    raw["letters"]["B"] = {"entries": [{"word": "Bär", "sound": "baer.mp3", "_soundDesc": "brummt"}]}
    raw["numbers"]["3"]["tracks"]["DE"]["volume"] = 999
    clean = sb.clean_set(raw)
    assert "evil" not in clean and "_generating" not in clean["letters"]["A"]["entries"][0]
    assert clean["letters"]["B"]["entries"][0]["tracks"]["FX"] == {"file": "baer.mp3", "enabled": True,
                                                                   "prompt": "brummt", "volume": 100}
    assert clean["numbers"]["3"]["tracks"]["DE"]["volume"] == 200


def test_letters_are_stored_upper_case_like_the_server_does():
    raw = dict(_set(), letters={"b": {"entries": []}, "ß": {"entries": []}, "ö": {"entries": []}})
    assert sorted(sb.clean_set(raw)["letters"]) == ["B", "Ö", "ß"]
