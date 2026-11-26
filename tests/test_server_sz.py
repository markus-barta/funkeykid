"""ß as a letter (FUNK-46): server-side migration, set ids and generated file names.

server.py needs aiohttp; CI installs only the pure-helper dependencies, so these
tests run where aiohttp is present (the lab venv) and are skipped elsewhere.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

pytest.importorskip("aiohttp")
import server  # noqa: E402


def test_legacy_sz_content_moves_to_sz(monkeypatch):
    monkeypatch.setattr(server, "settings", {
        "sets": {"a": {"letters": {"ẞ": {"entries": [{"word": "X"}]}, "ß": {"entries": [{"word": "Y"}]},
                                   "SS": {"entries": [{"word": "Z"}]}, "B": {"entries": []}}},
                 "b": {"letters": {"SS": {"entries": [{"word": "W"}]}}}},
        "blacklist": {"ẞ": ["q"], "SS": ["r", "q"], "A": ["s"]},
    })
    server.settings["active_set"] = "a"
    server.settings["favorites"] = [{"letter": "SS", "entry_index": 0}, {"letter": "ẞ", "entry_index": 0},
                                    {"letter": "ß", "entry_index": 0}, {"letter": "A", "entry_index": 2}]
    server._migrate_sz_letter()
    # each favorite still plays the entry it played before: Y, then Z (was SS[0]), then X (was ẞ[0])
    assert server.settings["favorites"] == [{"letter": "ß", "entry_index": 1}, {"letter": "ß", "entry_index": 2},
                                            {"letter": "ß", "entry_index": 0}, {"letter": "A", "entry_index": 2}]
    sets = server.settings["sets"]
    assert [e["word"] for e in sets["a"]["letters"]["ß"]["entries"]] == ["Y", "Z", "X"]   # ß first
    assert set(sets["a"]["letters"]) == {"ß", "B"} and list(sets["b"]["letters"]) == ["ß"]
    assert server.settings["blacklist"] == {"ß": ["r", "q"], "A": ["s"]}


def test_new_set_ids_never_collide():
    sets = {"masse": {}, "neues-set": {}}
    assert server._new_set_id("Maße", sets) == "masse-2"
    assert server._new_set_id("Neues Set", sets) == "neues-set-2"
    assert server._new_set_id("A/B?", sets) == "a-b"
    assert server._new_set_id("???", sets) == "set"


@pytest.fixture
def media(monkeypatch):
    """No names issued yet and no settings referring to any."""
    monkeypatch.setattr(server, "_issued_media_names", set())
    monkeypatch.setattr(server, "settings", {"sets": {}, "keymaps": {}})


@pytest.mark.parametrize("requested,want", [
    ("../x.png", None), ("a/b.png", None), ("a\\b.png", None), (".hidden.png", None), ("x" * 201, None), (5, None),
    ("ok.png", "ok.png"), ("noext", "noext.png"), (None, "base.png"), ("", "base.png"),
])
def test_requested_media_names(tmp_path, media, requested, want):
    assert server._claim_media_name(requested, "base", "png", (str(tmp_path),)) == want


def test_a_name_is_never_given_twice(tmp_path, media):
    """Not while a job runs, not after it failed (an entry may already refer to it)."""
    (tmp_path / "b_busse.png").write_bytes(b"x")
    got = [server._claim_media_name("b_busse.png", "b_busse", "png", (str(tmp_path),)) for _ in range(3)]
    assert got == ["b_busse-2.png", "b_busse-3.png", "b_busse-4.png"]


def test_a_name_the_settings_refer_to_is_taken(tmp_path, media):
    server.settings["sets"] = {"a": {"letters": {"B": {"entries": [{"image": "b_busse.png",
                                                                    "tracks": {"DE": {"file": "b_busse_de.mp3"}}}]}}}}
    server.settings["keymaps"] = {"acme-bk03": {"CUT": {"action": "sound", "file": "schere.mp3"}}}
    d = (str(tmp_path),)
    assert server._claim_media_name(None, "b_busse", "png", d) == "b_busse-2.png"
    assert server._claim_media_name(None, "b_busse_de", "mp3", d) == "b_busse_de-2.mp3"
    assert server._claim_media_name("schere.mp3", "x", "mp3", d) == "schere-2.mp3"


def test_a_variant_stays_within_the_name_limit(tmp_path, media):
    long = "x" * 196 + ".mp3"                               # 200 characters
    d = (str(tmp_path),)
    first, second = (server._claim_media_name(long, "b", "mp3", d) for _ in range(2))
    assert first == long and len(second) == 200 and second.endswith("-2.mp3")
    assert server._claim_media_name("x." + "y" * 11, "b", "png", d) is None   # no 11-character extension


def test_a_word_never_makes_a_path(tmp_path, media):
    d = (str(tmp_path),)
    assert server._claim_media_name(None, server._slugify("../../etc/x"), "mp3", d) == "-..-etc-x.mp3"
    assert server._claim_media_name(None, ".versteckt", "png", d) == "versteckt.png"


def test_generated_media_never_replace_a_file(tmp_path, media):
    a, b = tmp_path / "sounds", tmp_path / "ai"
    a.mkdir(), b.mkdir()
    (a / "b_busse_de.mp3").write_bytes(b"x")
    (b / "b_busse_de-2.mp3").write_bytes(b"x")
    base = "b_" + server._slugify("Buße") + "_de"
    assert base == "b_busse_de"
    assert server._free_media_name(base, "mp3", (str(a), str(b))) == "b_busse_de-3.mp3"
    assert server._free_media_name("neu", "png", (str(a),)) == "neu.png"
