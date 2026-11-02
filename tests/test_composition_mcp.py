"""Tests for MCP preview/apply parity and server-level musical composition tools."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
import pytest

from lmms_mcp import server
from lmms_mcp.project import LMMSProject
from lmms_mcp.composition import (
    add_automation_batch,
    place_chord,
    place_phrase,
    place_progression,
    place_rhythm,
)
from conftest import (
    AUTOMATION_SPEC,
    PHRASE_A,
    PROGRESSION_I_VI_IV_V,
    RHYTHM_KICK,
)


@pytest.fixture(autouse=True)
def bind_server_project(song):
    """Ensure MCP module-level project singleton points to the test song."""
    server.set_project(song)
    yield
    server.set_project(None)


PREVIEWABLE = [
    (
        "phrase",
        lambda p, apply: place_phrase(p, 0, PHRASE_A, at_bars=0, repeat=4, spacing_bars=1, apply=apply),
    ),
    (
        "chord",
        lambda p, apply: place_chord(p, 1, {"symbol": "C4maj7", "at": 0, "length": 192}, apply=apply),
    ),
    (
        "progression",
        lambda p, apply: place_progression(p, 1, PROGRESSION_I_VI_IV_V, at_bars=0, apply=apply),
    ),
    (
        "rhythm",
        lambda p, apply: place_rhythm(p, 0, RHYTHM_KICK, at_bars=0, repeat=2, spacing_bars=1, apply=apply),
    ),
    (
        "automation",
        lambda p, apply: add_automation_batch(p, [AUTOMATION_SPEC], apply=apply),
    ),
]


@pytest.mark.parametrize("name,run", PREVIEWABLE, ids=[p[0] for p in PREVIEWABLE])
def test_preview_does_not_mutate(song, snapshot, name, run):
    before_xml = snapshot(song)
    before_modified = song.modified
    res = run(song, False)
    assert res["applied"] is False
    assert snapshot(song) == before_xml
    assert song.modified == before_modified


@pytest.mark.parametrize("name,run", PREVIEWABLE, ids=[p[0] for p in PREVIEWABLE])
def test_plan_equality(song, name, run):
    preview = run(song, False)
    applied = run(song, True)
    assert applied["plan"] == preview["plan"]
    assert preview["applied"] is False
    assert applied["applied"] is True


@pytest.mark.parametrize("preview_call,apply_call", [
    (
        lambda: server.add_phrase(0, PHRASE_A, at_bars=0, repeat=4, spacing_bars=1, apply=False),
        lambda: server.add_phrase(0, PHRASE_A, at_bars=0, repeat=4, spacing_bars=1, apply=True),
    ),
    (
        lambda: server.add_chord(1, symbol="C4maj7", at_bars=0, length_bars=1, apply=False),
        lambda: server.add_chord(1, symbol="C4maj7", at_bars=0, length_bars=1, apply=True),
    ),
    (
        lambda: server.add_progression(1, PROGRESSION_I_VI_IV_V, at_bars=0, apply=False),
        lambda: server.add_progression(1, PROGRESSION_I_VI_IV_V, at_bars=0, apply=True),
    ),
    (
        lambda: server.add_automation_batch([AUTOMATION_SPEC], apply=False),
        lambda: server.add_automation_batch([AUTOMATION_SPEC], apply=True),
    ),
])
def test_mcp_preview_apply_parity(song, preview_call, apply_call):
    before = ET.tostring(song.root, encoding="unicode")
    preview = json.loads(preview_call())
    assert json.loads(preview_call()) == preview
    assert ET.tostring(song.root, encoding="unicode") == before
    applied = json.loads(apply_call())
    assert applied["plan"] == preview["plan"]
    assert applied["applied"] is True


@pytest.mark.parametrize("payload,code", [
    ({"symbol": "C4maj9#11", "at": 0, "length": 48}, "UNKNOWN_CHORD_QUALITY"),
    ({"symbol": "C4maj", "at": -1, "length": 48}, "NEGATIVE_TIME"),
    ({"symbol": "C4maj", "at": 0, "length": 0}, "INVALID_TIME"),
])
def test_preview_and_apply_fail_identically(song, snapshot, payload, code):
    before = snapshot(song)
    for apply in (False, True):
        result = json.loads(server.add_chord(1, apply=apply, **payload))
        assert result["ok"] is False
        assert result["code"] == code
    assert snapshot(song) == before


def test_preview_plan_is_stable_across_save_load(song, tmp_path):
    path = tmp_path / "preview.mmp"
    song.save(path)
    loaded = LMMSProject()
    loaded.load(path)
    a = json.loads(server.add_phrase(0, PHRASE_A, at_bars=0, apply=False))["plan"]
    server.set_project(loaded)
    b = json.loads(server.add_phrase(0, PHRASE_A, at_bars=0, apply=False))["plan"]
    assert a == b
