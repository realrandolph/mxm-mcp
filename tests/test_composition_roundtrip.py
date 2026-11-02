"""Persistence and round-trip tests for musical units, arrangement, and automation in .mmp/.mmpz."""

from __future__ import annotations

import pytest

from lmms_mcp.composition import (
    add_automation_batch,
    place_phrase,
    place_progression,
    place_rhythm,
)
from lmms_mcp.project import LMMSProject
from lmms_mcp.xml_parser import find_tracks, get_arrangement
from conftest import (
    PHRASE_A,
    PROGRESSION_I_VI_IV_V,
    RHYTHM_KICK,
    automation_tuples,
    note_tuples,
)


@pytest.mark.parametrize("suffix,compressed", [(".mmp", False), (".mmpz", True)])
def test_phrase_roundtrip(song, tmp_path, suffix, compressed):
    place_phrase(song, 0, PHRASE_A, at_bars=0, repeat=2, spacing_bars=1,
                 transforms={1: {"transpose": 12}}, apply=True)
    before = note_tuples(song.root, 0)
    path = tmp_path / f"phrase{suffix}"
    song.save(path, compressed=compressed)

    loaded = LMMSProject()
    loaded.load(path)
    assert note_tuples(loaded.root, 0) == before
    clips = [p for p in find_tracks(loaded.root)[0] if p.tag in ("pattern", "midiclip")]
    assert [(p.get("pos"), p.get("len"), p.get("name")) for p in clips] == [
        ("0", "192", "bass-riff-a"),
        ("192", "192", "bass-riff-a"),
    ]


def test_note_attributes_survive(song, tmp_path):
    place_phrase(song, 0, {
        "name": "attrs",
        "notes": [
            {"key": 36, "pos": 0, "length": 47, "volume": 0, "panning": -100},
            {"key": 127, "pos": 1, "length": 1, "volume": 200, "panning": 100},
        ],
    }, apply=True)
    path = tmp_path / "attrs.mmp"
    song.save(path, compressed=False)
    loaded = LMMSProject()
    loaded.load(path)
    assert note_tuples(loaded.root, 0) == [
        (0, 0, 36, 47, 0, -100),
        (0, 1, 127, 1, 200, 100),
    ]


def test_arrangement_roundtrip(song, tmp_path):
    place_phrase(song, 0, PHRASE_A, at_bars=0, repeat=2, spacing_bars=1, apply=True)
    place_progression(song, 1, PROGRESSION_I_VI_IV_V, at_bars=0, apply=True)
    place_rhythm(song, 2, RHYTHM_KICK, at_bars=4, repeat=2, spacing_bars=1, target="bb", apply=True)
    before = get_arrangement(song.root)
    path = tmp_path / "arr.mmpz"
    song.save(path)
    loaded = LMMSProject()
    loaded.load(path)
    assert get_arrangement(loaded.root) == before


def test_automation_roundtrip(song, tmp_path):
    add_automation_batch(song, [
        {"target_type": "song", "param": "tempo",
         "points": [{"bar": 0, "value": 120}, {"bar": 4, "value": 150}]},
        {"target_type": "track", "target_index": 0, "param": "volume",
         "points": [{"bar": 0, "value": 100}, {"bar": 8, "value": 40}]},
    ], apply=True)
    before = automation_tuples(song.root)
    path = tmp_path / "auto.mmpz"
    song.save(path)
    loaded = LMMSProject()
    loaded.load(path)
    assert automation_tuples(loaded.root) == before
    for _, _, _, _, model_id in before:
        assert loaded.root.find(f".//*[@id='{model_id}']") is not None


def test_pattern_container_tag_is_stable(song, tmp_path):
    place_phrase(song, 0, PHRASE_A, apply=True)
    path = tmp_path / "tags.mmp"
    song.save(path, compressed=False)
    loaded = LMMSProject()
    loaded.load(path)
    tags = [p.tag for p in find_tracks(loaded.root)[0] if p.tag in ("pattern", "midiclip")]
    assert tags == ["midiclip"]


def test_mmp_and_mmpz_are_musically_identical(song, tmp_path):
    place_phrase(song, 0, PHRASE_A, repeat=3, spacing_bars=1, apply=True)
    song.save(tmp_path / "a.mmp", compressed=False)
    song.save(tmp_path / "a.mmpz", compressed=True)
    p1, p2 = LMMSProject(), LMMSProject()
    p1.load(tmp_path / "a.mmp")
    p2.load(tmp_path / "a.mmpz")
    assert note_tuples(p1.root, 0) == note_tuples(p2.root, 0)
