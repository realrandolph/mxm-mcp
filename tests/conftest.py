"""Shared fixtures and helpers for Issue #11 composition tests."""

from __future__ import annotations

import copy
import json
import xml.etree.ElementTree as ET
from typing import Any

import pytest

from mxm_mcp.project import LMMSProject
from mxm_mcp.xml_parser import find_tracks


@pytest.fixture
def project() -> LMMSProject:
    p = LMMSProject()
    p.new(bpm=120)  # 120 BPM, 4/4, master 100
    return p


@pytest.fixture
def song(project: LMMSProject) -> LMMSProject:
    """Deterministic four-track song used by placement tests.

    0 instrument "Bass"  (tripleoscillator)
    1 instrument "Keys"  (tripleoscillator)
    2 pattern    "Drums" (inner kicker)
    3 sample     "FX"
    """
    project.add_track("instrument", "Bass", instrument="tripleoscillator")
    project.add_track("instrument", "Keys", instrument="tripleoscillator")
    project.add_track("pattern", "Drums")
    project.add_track("sample", "FX")
    return project


@pytest.fixture
def snapshot():
    """Byte-stable XML snapshot."""
    def _snap(p: LMMSProject) -> str:
        return ET.tostring(p.root, encoding="unicode")
    return _snap


def note_tuples(root: ET.Element, track_index: int) -> list[tuple[int, int, int, int, int, int]]:
    """(clip_pos, note_pos, key, length, volume, panning), sorted."""
    out = []
    tracks = find_tracks(root)
    if track_index >= len(tracks):
        return []
    track = tracks[track_index]
    for pattern in track.findall("pattern") + track.findall("midiclip"):
        clip_pos = int(pattern.get("pos", 0))
        for n in pattern.findall("note"):
            out.append((
                clip_pos,
                int(n.get("pos", 0)),
                int(n.get("key", 0)),
                int(n.get("len", 0)),
                int(n.get("vol", 100)),
                int(n.get("pan", 0)),
            ))
    return sorted(out)


def pattern_notes(pattern: ET.Element) -> list[tuple[int, int, int, int, int]]:
    return sorted(
        (
            int(n.get("pos", 0)),
            int(n.get("key", 0)),
            int(n.get("len", 0)),
            int(n.get("vol", 100)),
            int(n.get("pan", 0)),
        )
        for n in pattern.findall("note")
    )


def instrument_clips(track: ET.Element) -> list[ET.Element]:
    return [c for c in track if c.tag in ("midiclip", "pattern")]


def count_notes(root: ET.Element) -> int:
    return sum(len(c.findall("note")) for t in find_tracks(root) for c in (t.findall("midiclip") + t.findall("pattern")))


def automation_tuples(root: ET.Element) -> list[tuple[str, str, str, list[tuple[int, float]], int]]:
    """Return (name, prog, tens, [(pos, value)...], object_id) for each automation curve."""
    out = []
    for t in find_tracks(root):
        if t.get("type") == "5":
            pat = t.find("automationpattern")
            if pat is not None:
                name = pat.get("name", "")
                prog = pat.get("prog", "0")
                tens = pat.get("tens", "1.0")
                pts = [(int(time.get("pos", 0)), float(time.get("value", 0.0))) for time in pat.findall("time")]
                obj = pat.find("object")
                obj_id = int(obj.get("id")) if obj is not None else 0
                out.append((name, prog, tens, pts, obj_id))
    return out


def plan_of(tool_result: str) -> dict:
    payload = json.loads(tool_result)
    assert payload.get("ok") is True, payload
    return payload["plan"]


def error_of(tool_result: str) -> dict:
    payload = json.loads(tool_result)
    assert payload.get("ok") is False, payload
    return payload


# Canonical fixtures
PHRASE_A = {
    "name": "bass-riff-a",
    "length_ticks": 192,
    "notes": [
        {"key": 36, "pos": 0, "length": 48, "volume": 100},
        {"key": 39, "pos": 48, "length": 48, "volume": 80},
        {"key": 43, "pos": 96, "length": 48, "volume": 120},
        {"key": 36, "pos": 144, "length": 48, "volume": 100},
    ],
}

OFF_GRID = {
    "name": "off-grid",
    "notes": [
        {"key": 60, "pos": 5, "length": 10},
        {"key": 62, "pos": 19, "length": 10},
        {"key": 64, "pos": 31, "length": 10},
        {"key": 65, "pos": 100, "length": 10},
    ],
}

PROGRESSION_I_VI_IV_V = {
    "name": "i-vi-iv-v",
    "chords": [
        {"symbol": "C4maj", "bars": 1},
        {"symbol": "A3min", "bars": 1},
        {"symbol": "F4maj", "bars": 1},
        {"symbol": "G4maj", "bars": 1},
    ],
}

RHYTHM_KICK = {
    "name": "four-on-floor",
    "steps": 16,
    "hits": [
        {"step": 0, "drum": "kick"},
        {"step": 4, "drum": "kick"},
        {"step": 8, "drum": "kick"},
        {"step": 12, "drum": "kick"},
    ],
}

AUTOMATION_SPEC = {
    "target_type": "song",
    "param": "master_volume",
    "points": [
        {"bar": 0, "value": 100},
        {"bar": 2, "value": 150},
        {"bar": 4, "value": 120},
    ],
}
