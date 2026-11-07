# Issue #11 — Deterministic Musical-Unit Composition: Test Suite Specification

**Issue:** `realrandolph/mxm-mcp#11` — *Add deterministic musical-unit composition and editing operations*
**Branch:** `feat/musical-unit-composition-11`
**Status:** Test contract and current coverage. The pure layer
(`src/mxm_mcp/musical_units.py`) and the placement layer
(`src/mxm_mcp/composition.py`) are present in the working tree. Suites A–H are
materialized; Suites I–L and the gap tests in §18 remain to be written.
**Owner:** QA / audio software test engineering

## Contents

| § | Suite | Primary file |
|---|---|---|
| 0 | Status and baseline | — |
| 1 | Contract under test | — |
| 2 | Fixtures and helpers | `tests/conftest.py` |
| 3 | Test files and markers | — |
| 4 | A — Pitch parsing | `tests/test_musical_units.py` |
| 5 | B — Timebase | `tests/test_musical_units.py` |
| 6 | C — Chord dictionary | `tests/test_musical_units.py` |
| 7 | D — Phrase / motif parsing | `tests/test_musical_units.py` |
| 8 | E — Transformations | `tests/test_musical_units.py` |
| 9 | F — Rhythm patterns | `test_musical_units.py` (F1–F3), `test_composition_ops.py` (F4–F6) |
| 10 | G — Arrangement placement | `tests/test_composition_ops.py` |
| 11 | H — Automation | `tests/test_composition_ops.py` |
| 12 | I — Preview/apply parity | `tests/test_composition_mcp.py` |
| 13 | J — Round-trip persistence | `tests/test_composition_roundtrip.py` |
| 14 | K — Property invariants | `tests/test_musical_units.py` |
| 15 | L — End-to-end acceptance | `tests/test_lmms13_integration.py` |
| 16 | Edge-case matrix | — |
| 17 | Acceptance criteria map | — |
| 18 | Implementation gaps | — |
| 19 | Definition of done | — |
| 20 | Rollout order | — |

---

## 0. Purpose and testing philosophy

The feature makes the MCP a deterministic interpreter of **explicit musical structures**.
Therefore every test asserts one of four properties:

1. **Exactness** — a submitted structure produces exactly the predicted MIDI keys,
   ticks, lengths, velocities, and arrangement entries. No inference.
2. **Refusal** — underspecified, ambiguous, or out-of-range input raises a typed,
   stable error instead of being guessed or silently clamped.
3. **Purity of preview** — `apply=False` returns the same plan as `apply=True` and
   leaves the project byte-identical.
4. **Persistence** — the musical result survives `.mmp`/`.mmpz` save/load with
   identical note, clip, and automation data.

Tests never assert on MXM internals that the issue does not own (audio rendering,
plugin discovery). Rendering is covered by one optional `integration` test.

### 0.1 Baseline on the branch point

```
214 passed, 1 failed, 1 skipped
```

The single failure is pre-existing and unrelated:
`tests/test_lmms13_integration.py::test_mxm_native_vst3_renders_audibly`
(VST3 permission context unavailable). The new suite must not add failures and must
keep the fast suite independent of installed plugins.

### 0.2 Current status (working tree)

The pure layer, placement layer, MCP wrappers, and test files through Suite J are
present. Current suite:

```
1 failed, 579 passed, 1 skipped
```

The only failure is the pre-existing, unrelated VST3 permission test
(`tests/test_lmms13_integration.py::test_mxm_native_vst3_renders_audibly`).

Materialized: Suites A–F (`tests/test_musical_units.py`), G–H
(`tests/test_composition_ops.py`), I (`tests/test_composition_mcp.py`), and J
(`tests/test_composition_roundtrip.py`). Still to write: Suite K (invariants),
Suite L (integration), and the gap tests in §18.

Fixes confirmed at this snapshot:

* numeric chord qualities, negative-time rejection, `kind`/`type`, and the
  `tests.conftest` import;
* automation preview no longer mutates the project (Suite I passes);
* `xml_parser.add_automation_track` empty-points behavior is reconciled with its
  test (§18.18).

Remaining known defects:

* slash bass is not the lowest voice for non-chord-tone basses
  (`C4maj/B4`, `C4maj/F#4`);
* `place_chord` drops chord-level velocity/pan;
* `edit_automation` starts from an empty point list and ignores
  scale/offset/clamp.

---

## 1. Contract under test

The implementation is free to choose internal names, but the *observable* contract
below is what the tests pin. If the implementation chooses different module or
function names, update the imports in one place (`tests/conftest.py` helper section),
not the assertions.

### 1.1 Proposed module layout

| Module | Responsibility | Test file |
|---|---|---|
| `mxm_mcp/musical_units.py` | **Implemented.** Pure parsing, validation, transformation. No XML, no I/O. | `tests/test_musical_units.py` |
| `mxm_mcp/composition.py` | **Implemented (working tree).** Project placement, preview/apply, arrangement, automation editing. | `tests/test_composition_ops.py` |
| `mxm_mcp/server.py` | **Implemented.** MCP wrappers `add_phrase`, `add_chord`, `add_progression`, `add_rhythm`, `arrange_musical_units`, `edit_notes_batch`, `add_automation_batch`, `edit_automation`. | `tests/test_composition_mcp.py` |
| persistence | Existing `xml_parser.save_project` / `load_project`. | `tests/test_composition_roundtrip.py` |

Target imports used throughout the outline (adjust here only if the implementation
picks different names):

```python
from mxm_mcp.musical_units import (
    MusicalUnitError, parse_pitch, parse_time, parse_duration,
    parse_chord_symbol, parse_chord_spec, chord_pitches, chord_names,
    parse_phrase, parse_progression, parse_rhythm_pattern,
    transpose_notes, shift_notes, stretch_notes, reverse_notes,
    quantize_notes, scale_velocity, apply_transforms,
    reverse_automation_points,
)
from mxm_mcp.composition import (
    place_phrase, place_chord, place_chords, place_progression,
    place_rhythm, arrange_units, edit_notes,
    edit_automation, add_automation_batch,
)
from mxm_mcp import server
```

### 1.2 Canonical clip representation

MXM-saved projects use `<midiclip>` for instrument-track clips; BB tracks store
`<pattern>` inside their inner instrument container. The new API therefore writes:

| Track type | Element | Notes |
|---|---|---|
| instrument (0) | `<midiclip>` | one element per occurrence, `pos`/`len`/`name` |
| pattern / BB (1) | `<pattern>` inside `patterntrack/trackcontainer/track` | one shared pattern; `<bbtco>` clips reference it |

Legacy `<pattern>` elements on instrument tracks remain readable. Tests that read
existing projects must use a helper that collects both tags; tests that assert the
new API's output assert `<midiclip>`.

Additional naming rule: every occurrence of a unit uses the unit's `name` verbatim
(no `-1`/`-2` suffix). This makes arrangement assertions exact.

### 1.3 Canonical musical data model

```python
# ticks: int, 192 ticks per bar in 4/4, 48 per quarter, 12 per 16th
# pitch: MIDI int 0..127
# velocity: int 0..200 (100 = normal)
# panning: int -100..100

NoteEvent = {
    "key": int,        # required (or "pitch": int | str, normalized to "key")
    "pos": int,        # required for absolute notes; relative for phrase notes
    "length": int,     # required, >= 1
    "volume": int,     # optional, default 100
    "panning": int,    # optional, default 0
}

Phrase = {
    "name": str,
    "length_ticks": int,          # optional, inferred as max(note_end) if omitted
    "notes": [NoteEvent],         # relative positions
    "rests": [{"at": int, "length": int}],   # optional; absence is a rest anyway
}

ChordSpec = {
    "symbol": str,                # e.g. "C4min7", "F#3maj7/E3"
    # or explicit form:
    "pitches": [int | str],       # e.g. [60, "E4", "G4"]
    "at": int,                    # absolute tick
    "length": int,
    "volume": int,
    "panning": int,
    "inversion": int,             # optional, 0..len(intervals)-1
    "voicing": str,               # "close" | "drop2" | "drop3", default "close"
    "octave_shift": int,          # optional, default 0
}

Progression = {
    "name": str,
    "chords": [
        {"symbol": "C4maj", "bars": 1},
        {"rest": 1},              # explicit silence entry
        {"symbol": "G4maj", "bars": 2},
    ],
}

RhythmPattern = {
    "name": str,
    "steps": int,                 # required, >= 1
    "step_ticks": int,            # optional, default 12
    "hits": [
        {"step": int, "drum": str | None, "pitch": int | str | None,
         "velocity": int, "length_steps": int},
    ],
}
```

### 1.4 Response envelope (all new/modified MCP tools)

```json
{
  "ok": true,
  "applied": false,
  "plan": {
    "version": 1,
    "kind": "phrase",
    "target": {"track_index": 0, "pattern_index": 0},
    "occurrences": [
      {
        "index": 0,
        "at_ticks": 0,
        "transform": {},
        "clip": {"pos": 0, "len": 192},
        "notes": [
          {"key": 36, "pos": 0, "length": 48, "volume": 100, "panning": 0}
        ]
      }
    ],
    "warnings": [],
    "song_length_ticks": 192
  }
}
```

* `plan` is **identical** for preview and apply.
* `applied` is the only musical-content-neutral difference.
* Extra sibling keys (`created`, `message`) are allowed; tests compare `["plan"]` only.
* `apply` defaults to `True` for backwards-compatible mutation; preview is opt-in.
* Automation values serialize as float strings (`"120.0"`), matching MXM. Ticks and
  note attributes serialize as integer strings.
* `place_rhythm` takes `target="instrument"` (default; writes a `<midiclip>`) or
  `target="bb"` (shared `<pattern>` plus `<bbtco>` clips). A pattern/BB track
  requires `target="bb"`; the call is rejected otherwise.
* Automation envelopes differ by operation: `add_automation_batch` returns
  `plan["curves"] = [{target, points, progression, tension}, ...]`; a single-curve
  `edit_automation` returns `plan["points"]` for that curve.

### 1.5 Error contract

All invalid input returns the same envelope plus stable metadata:

```json
{
  "ok": false,
  "error": "Unknown chord quality 'maj9#11' for symbol 'C4maj9#11'",
  "code": "UNKNOWN_CHORD_QUALITY",
  "field": "chords[0].symbol",
  "value": "C4maj9#11",
  "accepted": ["maj", "min", "7", "maj7", "min7", "..."]
}
```

Pure functions raise `MusicalUnitError` with `.code`, `.field`, `.accepted`.

| Code | Trigger |
|---|---|
| `INVALID_PITCH` | `True`/`None`/float/numeric string as pitch |
| `INVALID_NOTE_NAME` | malformed name (`"C"`, `"H4"`, `"C 4"`, double accidental) |
| `PITCH_OUT_OF_RANGE` | result outside 0..127 |
| `AMBIGUOUS_PITCH` | bare letter with no octave |
| `INVALID_TIME` | non-numeric, NaN, infinity, non-integral float ticks |
| `INVALID_PHRASE` | phrase payload is not a dict |
| `INVALID_PROGRESSION` | progression payload is not a dict |
| `INVALID_RHYTHM` | rhythm payload is not a dict |
| `NEGATIVE_TIME` | negative position (unless explicitly allowed) |
| `UNSUPPORTED_TIME_SIGNATURE` | bar units used with non-4/4 project |
| `INVALID_CHORD_SYMBOL` | structural parse failure |
| `AMBIGUOUS_CHORD` | bare root (`"C"`) without explicit quality |
| `UNKNOWN_CHORD_QUALITY` | quality not in dictionary |
| `INVALID_INVERSION` | inversion outside `0..len(intervals)-1` |
| `INVALID_VOICING` | voicing not in `{"close","drop2","drop3"}` |
| `EMPTY_UNIT` | phrase/progression/pattern/chord with zero notes |
| `DUPLICATE_HIT` | same `(step, pitch)` twice without opt-in |
| `UNKNOWN_DRUM` | drum name not in the mapping table |
| `INVALID_STEP` | step outside `0..steps-1` |
| `INVALID_VELOCITY` | outside 0..200 |
| `INVALID_PANNING` | outside -100..100 |
| `INVALID_RATIO` | stretch ratio `<= 0` |
| `INVALID_GRID` | quantize grid `<= 0` |
| `TRACK_INDEX_OUT_OF_RANGE` | negative or `>= len(tracks)` |
| `PATTERN_INDEX_OUT_OF_RANGE` | negative/out-of-range pattern index or per-occurrence transform key |
| `WRONG_TRACK_TYPE` | notes on sample/automation track |
| `UNSUPPORTED_TRANSFORMATION` | per-occurrence transform on a shared BB pattern |
| `UNSUPPORTED_LINK` | linked instrument clips requested (MXM midiclip has no link) |
| `CLIP_TOO_SHORT` | `fit="error"` and notes exceed clip |
| `UNKNOWN_AUTOMATION_TARGET` | invalid type/param pair |
| `AUTOMATION_VALUE_OUT_OF_RANGE` | with `on_out_of_range="error"` |
| `DUPLICATE_AUTOMATION_POINT` | duplicate tick with `dedupe="error"` |

**Regression requirement:** negative track/pattern indices are rejected. Current
`add_note_to_track` accepts `track_index=-1` and writes to the last track; the new
API must not inherit this.

---

## 2. Shared fixtures and helpers

Create `tests/conftest.py` (none exists today):

```python
"""Shared fixtures for Issue #11 composition tests."""
from __future__ import annotations

import copy
import json
import xml.etree.ElementTree as ET

import pytest

from mxm_mcp.project import LMMSProject
from mxm_mcp.xml_parser import find_tracks


@pytest.fixture
def project() -> LMMSProject:
    p = LMMSProject()
    p.new(bpm=120)                    # 120 BPM, 4/4, master 100
    return p


@pytest.fixture
def song(project) -> LMMSProject:
    """Deterministic four-track song used by placement tests.

    0 instrument "Bass"  (tripleoscillator)
    1 instrument "Keys"  (tripleoscillator)
    2 pattern    "Drums" (inner kicker)
    3 sample     "FX"
    mixer 0 = Master, mixer 1 = "Bus"
    """
    project.add_track("instrument", "Bass", instrument="tripleoscillator")
    project.add_track("instrument", "Keys", instrument="tripleoscillator")
    project.add_track("pattern", "Drums")
    project.add_track("sample", "FX")
    project.add_mixer_channel("Bus")
    return project


@pytest.fixture
def automated_song(song):
    """song plus one master-volume curve at bars 0, 2, 4."""
    add_automation_batch(song, [AUTOMATION_SPEC], apply=True)
    return song


def instrument_clips(track):
    """All clips on an instrument track, in document order (legacy + new tag)."""
    return list(track.findall("midiclip")) + list(track.findall("pattern"))


def count_notes(root) -> int:
    return sum(len(c.findall("note"))
               for t in find_tracks(root)
               for c in instrument_clips(t))


def automation_tuples(root):
    """(name, prog, tension, [(pos, value)...], object_id) per automation curve."""
    out = []
    for track in find_tracks(root):
        if track.get("type") not in ("5", "6"):
            continue
        for pattern in track.findall("automationpattern"):
            points = [(int(t.get("pos", 0)), float(t.get("value", 0)))
                      for t in pattern.findall("time")]
            obj = pattern.find("object")
            out.append((pattern.get("name"), pattern.get("prog"),
                        pattern.get("tens"), points,
                        int(obj.get("id")) if obj is not None else None))
    return out


def assert_all_notes_inside_clips(root) -> None:
    for track in find_tracks(root):
        for clip in instrument_clips(track):
            clip_len = int(clip.get("len", 0))
            pos = int(clip.get("pos", 0))
            for n in clip.findall("note"):
                n_pos, n_len = int(n.get("pos", 0)), int(n.get("len", 0))
                assert 0 <= n_pos
                assert n_len >= 1
                assert n_pos + n_len <= clip_len


@pytest.fixture
def snapshot():
    """Byte-stable XML snapshot. NOTE: never use get_xml_string() here --
    it calls ET.indent() and mutates whitespace in the live tree."""
    def _snap(p: LMMSProject) -> str:
        return ET.tostring(p.root, encoding="unicode")
    return _snap


def note_tuples(root, track_index: int) -> list[tuple[int, int, int, int, int, int]]:
    """(clip_pos, note_pos, key, length, volume, panning), sorted."""
    out = []
    for pattern in (find_tracks(root)[track_index].findall("pattern")
                    + find_tracks(root)[track_index].findall("midiclip")):
        clip_pos = int(pattern.get("pos", 0))
        for n in pattern.findall("note"):
            out.append((clip_pos, int(n.get("pos", 0)), int(n.get("key", 0)),
                        int(n.get("len", 0)), int(n.get("vol", 100)),
                        int(n.get("pan", 0))))
    return sorted(out)


def pattern_notes(pattern):
    return sorted(
        (int(n.get("pos", 0)), int(n.get("key", 0)), int(n.get("len", 0)),
         int(n.get("vol", 100)), int(n.get("pan", 0)))
        for n in pattern.findall("note")
    )


def plan_of(tool_result: str) -> dict:
    payload = json.loads(tool_result)
    assert payload["ok"] is True, payload
    return payload["plan"]


def error_of(tool_result: str) -> dict:
    payload = json.loads(tool_result)
    assert payload["ok"] is False, payload
    return payload
```

Canonical fixture data:

```python
# tests/fixtures/issue11_units.py (or conftest constants)
PHRASE_A = {
    "name": "bass-riff-a",
    "length_ticks": 192,
    "notes": [
        {"key": 36, "pos": 0,   "length": 48, "volume": 100},
        {"key": 39, "pos": 48,  "length": 48, "volume": 80},
        {"key": 43, "pos": 96,  "length": 48, "volume": 120},
        {"key": 36, "pos": 144, "length": 48, "volume": 100},
    ],
}

OFF_GRID = {
    "name": "off-grid",
    "notes": [
        {"key": 60, "pos": 5,   "length": 10},
        {"key": 62, "pos": 19,  "length": 10},
        {"key": 64, "pos": 31,  "length": 10},
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
    "name": "kick-4otf",
    "steps": 16,
    "step_ticks": 12,
    "hits": [
        {"step": 0,  "drum": "kick", "length_steps": 1},
        {"step": 4,  "drum": "kick", "length_steps": 1},
        {"step": 8,  "drum": "kick", "length_steps": 1},
        {"step": 12, "drum": "kick", "length_steps": 1},
    ],
}

# master_volume is used because its legal range (0..200) makes clamping visible
AUTOMATION_SPEC = {
    "target_type": "song",
    "param": "master_volume",
    "points": [
        {"bar": 0, "value": 100},
        {"bar": 2, "value": 150},
        {"bar": 4, "value": 120},
    ],
}
```

---

## 3. Test files and markers

| File | Focus | Runtime |
|---|---|---|
| `tests/test_musical_units.py` | pitch/time/chord/phrase parsing + pure transforms | < 2 s |
| `tests/test_composition_ops.py` | placement, arrangement, rhythm, automation | < 10 s |
| `tests/test_composition_mcp.py` | MCP tool envelopes, preview/apply parity | < 10 s |
| `tests/test_composition_roundtrip.py` | `.mmp` / `.mmpz` persistence | < 10 s |
| `tests/test_lmms13_integration.py` | one end-to-end render (existing file) | skip if no MXM |

Add to `pyproject.toml`:

```toml
markers = [
    "integration: exercises an installed MXM application and plugins",
    "slow: exercises large arrangement or render-scale operations",
]
```

Commands:

```bash
uv run pytest tests/test_musical_units.py -q
uv run pytest tests/test_composition_ops.py tests/test_composition_mcp.py tests/test_composition_roundtrip.py -q
uv run pytest -q -m "not integration"
uv run pytest -q -m integration
```

---

## 4. Suite A — Pitch parsing (`test_musical_units.py::TestPitchParsing`)

### A1. Canonical table (parametrized, exact)

```python
@pytest.mark.parametrize("name,expected", [
    # naturals, octave boundaries
    ("C-1", 0), ("C0", 12), ("C4", 60), ("C5", 72), ("G9", 127),
    ("A4", 69), ("B3", 59), ("B4", 71),
    # sharps
    ("C#4", 61), ("D#4", 63), ("F#4", 66), ("G#4", 68), ("A#4", 70),
    # flats (enharmonic, same octave)
    ("Db4", 61), ("Eb4", 63), ("Gb4", 66), ("Ab4", 68), ("Bb4", 70),
    ("Bb2", 46), ("Eb2", 39), ("Ab2", 44),
    # Unicode accidentals
    ("C\u266f4", 61), ("B\u266d2", 46),
    # lowercase letters normalize
    ("c4", 60), ("f#3", 54), ("bb2", 46),
])
def test_note_name_to_midi_exact(name, expected):
    assert parse_pitch(name) == expected
    assert note_name_to_midi(name) == expected
```

### A2. Cross-octave accidentals (regression for `2842614`)

Current implementation maps `Cb`, `B#`, `Fb`, `E#` to a pitch class in the **same
octave**, which is wrong by an octave. These assertions must fail before the fix:

```python
@pytest.mark.parametrize("name,expected", [
    ("Cb4", 59),   # B3, not B4=71
    ("Cb5", 71),   # B4
    ("B#3", 60),   # C4, not C3=48
    ("B#4", 72),   # C5
    ("Fb4", 64),   # E4
    ("E#4", 65),   # F4
    ("E#3", 53),   # F3
    ("Cb-1", None),  # -> -1, must raise PITCH_OUT_OF_RANGE
])
def test_cross_octave_enharmonics(name, expected):
    if expected is None:
        with pytest.raises(MusicalUnitError) as exc:
            parse_pitch(name)
        assert exc.value.code == "PITCH_OUT_OF_RANGE"
    else:
        assert parse_pitch(name) == expected
```

### A3. Range and type validation

```python
@pytest.mark.parametrize("value,code", [
    ("C-2", "PITCH_OUT_OF_RANGE"),
    ("G#9", "PITCH_OUT_OF_RANGE"),   # 128
    ("B9",  "PITCH_OUT_OF_RANGE"),   # 131
    (128,   "PITCH_OUT_OF_RANGE"),
    (-1,    "PITCH_OUT_OF_RANGE"),
])
def test_pitch_out_of_range(value, code): ...

@pytest.mark.parametrize("value,code", [
    ("C",   "AMBIGUOUS_PITCH"),      # no octave
    ("",    "INVALID_NOTE_NAME"),
    ("H4",  "INVALID_NOTE_NAME"),
    ("C#",  "AMBIGUOUS_PITCH"),
    ("C4#", "INVALID_NOTE_NAME"),
    ("C 4", "INVALID_NOTE_NAME"),
    ("C4.5","INVALID_NOTE_NAME"),
    ("C##4","INVALID_NOTE_NAME"),    # doubles rejected in v1; message says use D4
    ("Cbb4","INVALID_NOTE_NAME"),
    ("C#b4","INVALID_NOTE_NAME"),
    ("60",  "INVALID_PITCH"),        # numeric strings are ambiguous
    (60.0,  "INVALID_PITCH"),        # reject float even if integral
    (True,  "INVALID_PITCH"),        # bool is an int subclass
    (None,  "INVALID_PITCH"),
])
def test_invalid_pitch_input(value, code): ...
```

`midi_to_note_name` must validate too:

```python
@pytest.mark.parametrize("key", [-1, 128, 1000])
def test_midi_to_note_name_rejects_out_of_range(key):
    with pytest.raises(MusicalUnitError) as exc:
        midi_to_note_name(key)
    assert exc.value.code == "PITCH_OUT_OF_RANGE"
```

### A4. Round-trip and spelling

```python
def test_roundtrip_all_128_keys():
    for key in range(128):
        assert parse_pitch(midi_to_note_name(key)) == key

def test_spelling_controls_names_not_keys():
    spec = parse_chord_symbol("C4min")
    assert chord_pitches(spec, spelling="sharps") == [60, 63, 67]
    assert chord_names(spec, spelling="sharps") == ["C4", "D#4", "G4"]
    assert chord_names(spec, spelling="flats")  == ["C4", "Eb4", "G4"]
```

---

## 5. Suite B — Timebase (`TestTimeParsing`)

Fixed conventions for v1: 4/4 only; `192 ticks/bar`, `48 ticks/beat`, `12 ticks/step`.
Float input is accepted only for `bars`, `beats`, `steps`; ticks must be `int`.

### B1. Exact conversions

```python
@pytest.mark.parametrize("bars,ticks", [
    (0, 0), (0.25, 48), (0.5, 96), (1, 192), (1.5, 288),
    (1.75, 336), (4, 768), (8.125, 1560), (16, 3072),
])
def test_bars_to_ticks_exact(bars, ticks):
    assert parse_time(bars, unit="bars") == ticks

@pytest.mark.parametrize("beats,ticks", [
    (0, 0), (0.25, 12), (0.5, 24), (1, 48), (2, 96), (4, 192), (7.5, 360),
])
def test_beats_to_ticks_exact(beats, ticks): ...

@pytest.mark.parametrize("steps,ticks", [(1, 12), (4, 48), (16, 192), (32, 384)])
def test_steps_to_ticks_exact(steps, ticks): ...

def test_integral_float_ticks_accepted():
    assert parse_time(1.0, unit="ticks") == 1
    assert parse_time(192.0, unit="ticks") == 192

def test_fractional_float_ticks_rejected():
    with pytest.raises(MusicalUnitError) as exc:
        parse_time(1.5, unit="ticks")
    assert exc.value.code == "INVALID_TIME"
```

### B2. Rounding policy — half away from zero

Python's `round()` is banker's rounding and must not be used. Required cases:

```python
@pytest.mark.parametrize("bars,ticks", [
    (1/384, 1),    # 0.5 tick -> 1 (banker's round() would give 0)
    (5/384, 3),    # 2.5 ticks -> 3
    (7/384, 4),    # 3.5 ticks -> 4
    (3/384, 2),    # 1.5 ticks -> 2 (agrees with banker's)
])
def test_round_half_away_from_zero(bars, ticks):
    assert parse_time(bars, unit="bars") == ticks
    assert bars_to_ticks(bars) == ticks
```

### B3. Rejection matrix

```python
@pytest.mark.parametrize("value,unit,code", [
    (-1, "ticks", "NEGATIVE_TIME"),
    (-0.001, "bars", "NEGATIVE_TIME"),
    (1.5, "ticks", "INVALID_TIME"),          # non-integer ticks
    ("192", "ticks", "INVALID_TIME"),
    (float("nan"), "bars", "INVALID_TIME"),
    (float("inf"), "bars", "INVALID_TIME"),
    (True, "ticks", "INVALID_TIME"),
])
def test_time_rejection(value, unit, code): ...
```

### B4. Time signature guard

```python
def test_bar_units_reject_non_4_4_project(project):
    project.new()
    project.set_time_signature(3, 4)
    with pytest.raises(MusicalUnitError) as exc:
        place_phrase(project, 0, PHRASE_A, at_bars=1, apply=False)
    assert exc.value.code == "UNSUPPORTED_TIME_SIGNATURE"
    # explicit ticks still work in 3/4
    plan = place_phrase(project, 0, PHRASE_A, at_ticks=192, apply=False)["plan"]
    assert plan["occurrences"][0]["at_ticks"] == 192
```

### B5. Duration strings (supporting parser)

```python
@pytest.mark.parametrize("token,ticks", [
    ("1/1", 192), ("1/2", 96), ("1/4", 48), ("1/8", 24), ("1/16", 12),
    ("1/32", 6), ("1/8T", 16), ("1/16T", 8), ("1/4.", 72), ("1/8.", 36),
])
def test_duration_tokens(token, ticks):
    assert parse_duration(token) == ticks
```

Reject `"1/3"`, `"1/0"`, `"0/4"`, `"4/4"` (not a note value here), `"1/16TT"`.

---

## 6. Suite C — Deterministic chord dictionary (`TestChordSymbols`)

### C1. Quality table (root C4, close voicing, inversion 0)

Parsing rules pinned by these tests:

* The root letter is case-insensitive; the **quality is case-sensitive**.
* A root **octave is required** in a symbol (`C4maj`, not `Cmaj`). The octave token
  is one optional `-` plus exactly one digit (`-1`..`9`); the rest of the string is
  the quality. This makes numeric qualities unambiguous: `C47` = C4 + `7`,
  `C413` = C4 + `13`, `C45` = C4 + `5`.
* A symbol with no quality (`C`, `C4`) is ambiguous and rejected; a symbol with a
  quality but no octave (`Cmaj`) is structurally invalid.
* Sharps are canonical in output; `spelling` changes names, never keys.
* Error precedence: malformed structure → `INVALID_CHORD_SYMBOL`; missing quality
  with valid root → `AMBIGUOUS_CHORD`; unknown quality → `UNKNOWN_CHORD_QUALITY`.

```python
CHORD_EXPECTATIONS = {
    "maj":   [60, 64, 67],
    "min":   [60, 63, 67],
    "dim":   [60, 63, 66],
    "aug":   [60, 64, 68],
    "5":     [60, 67],
    "sus2":  [60, 62, 67],
    "sus4":  [60, 65, 67],
    "6":     [60, 64, 67, 69],
    "min6":  [60, 63, 67, 69],
    "7":     [60, 64, 67, 70],
    "maj7":  [60, 64, 67, 71],
    "min7":  [60, 63, 67, 70],
    "m7b5":  [60, 63, 66, 70],
    "dim7":  [60, 63, 66, 69],
    "mMaj7": [60, 63, 67, 71],
    "add9":  [60, 64, 67, 74],
    "9":     [60, 64, 67, 70, 74],
    "maj9":  [60, 64, 67, 71, 74],
    "min9":  [60, 63, 67, 70, 74],
    "11":    [60, 64, 67, 70, 74, 77],
    "13":    [60, 64, 67, 70, 74, 81],
    "aug7":  [60, 64, 68, 70],
    "7sus4": [60, 65, 67, 70],
}

@pytest.mark.parametrize("quality,expected", CHORD_EXPECTATIONS.items())
def test_chord_quality_table(quality, expected):
    assert chord_pitches(parse_chord_symbol(f"C4{quality}")) == expected
```

Also assert the table is **closed**: every accepted quality is in the table, and
`accepted` in an unknown-quality error equals `sorted(CHORD_QUALITIES)`.

**Known failure:** qualities `5`, `6`, `7`, `9`, `11`, `13` are currently
unreachable because the root-octave regex is greedy. These cases must pass:

```python
@pytest.mark.parametrize("symbol,expected", [
    ("C45",   [60, 67]),
    ("C46",   [60, 64, 67, 69]),
    ("C47",   [60, 64, 67, 70]),
    ("C49",   [60, 64, 67, 70, 74]),
    ("C411",  [60, 64, 67, 70, 74, 77]),
    ("C413",  [60, 64, 67, 70, 74, 81]),
    ("C47sus4", [60, 65, 67, 70]),
    ("Bb27",  [46, 50, 53, 56]),
])
def test_numeric_quality_after_octave(symbol, expected):
    assert chord_pitches(parse_chord_symbol(symbol)) == expected
```

### C2. Root spelling and octave

```python
@pytest.mark.parametrize("symbol,expected", [
    ("C4maj",  [60, 64, 67]),
    ("C47",    [60, 64, 67, 70]),       # numeric quality after one-digit octave
    ("C413",   [60, 64, 67, 70, 74, 81]),
    ("C45",    [60, 67]),               # power chord, not octave 45
    ("F#3min", [54, 57, 61]),
    ("Bb2maj7", [46, 50, 53, 57]),
    ("Db5dim", [73, 76, 79]),
    ("B3sus4", [59, 64, 66]),
    ("Cb4maj", [59, 63, 66]),      # B3 major
    ("B#3maj", [60, 64, 67]),      # C4 major
])
def test_chord_roots(symbol, expected): ...
```

### C3. Inversions and slash bass

```python
@pytest.mark.parametrize("symbol,expected", [
    ("C4maj",        [60, 64, 67]),
    ("C4maj/E4",     [64, 67, 72]),
    ("C4maj/G4",     [67, 72, 76]),
    ("C4maj/D3",     [50, 60, 64, 67]),   # added bass, voices unchanged
    ("C4maj7/E4",    [64, 67, 71, 72]),   # inversion 1
    ("C4maj7/B4",    [71, 72, 76, 79]),   # inversion 2
])
def test_slash_and_inversion(symbol, expected): ...

@pytest.mark.parametrize("inversion,expected", [
    (0, [60, 64, 67]),
    (1, [64, 67, 72]),
    (2, [67, 72, 76]),
])
def test_explicit_inversion_parameter(inversion, expected):
    assert chord_pitches(parse_chord_symbol("C4maj", inversion=inversion)) == expected

@pytest.mark.parametrize("symbol,bass", [
    ("C4maj/E4", 64),    # chord tone, first inversion
    ("C4maj/G4", 67),    # chord tone, second inversion
    ("C4maj/D3", 50),    # added bass below chord
    ("C4maj/B4", 71),    # non-chord bass above chord (currently wrong)
    ("C4maj/F#4", 66),   # non-chord bass inside chord (currently wrong)
])
def test_slash_bass_is_lowest(symbol, bass):
    pitches = chord_pitches(parse_chord_symbol(symbol))
    assert bass in pitches
    assert min(pitches) == bass
```

### C4. Voicings (fixed definitions)

`close` = root position ascending. `drop2` = close voicing with the **second-highest**
voice lowered 12 (requires at least 2 voices). `drop3` = the **third-highest** voice
lowered 12 (requires at least 3 voices). Pipeline order is fixed: intervals →
inversion → `octave_shift` → voicing → ascending sort → slash bass.

```python
@pytest.mark.parametrize("symbol,voicing,expected", [
    ("C4maj",  "drop2", [52, 60, 67]),   # E4 -> E3
    ("C4maj",  "drop3", [48, 64, 67]),   # C4 -> C3
    ("C4maj7", "drop2", [55, 60, 64, 71]),
    ("C4maj7", "drop3", [52, 60, 67, 71]),
])
def test_voicing_table(symbol, voicing, expected):
    assert chord_pitches(parse_chord_symbol(symbol, voicing=voicing)) == expected
```

```python
@pytest.mark.parametrize("voicing,expected", [
    ("close", [60, 64, 67, 71]),
    ("drop2", [55, 60, 64, 71]),   # 67 -> 55
    ("drop3", [52, 60, 67, 71]),   # 64 -> 52
])
def test_voicing_definitions(voicing, expected):
    assert chord_pitches(parse_chord_symbol("C4maj7", voicing=voicing)) == expected

def test_octave_shift():
    assert chord_pitches(parse_chord_symbol("C4maj", octave_shift=1)) == [72, 76, 79]
    assert chord_pitches(parse_chord_symbol("C4maj", octave_shift=-1)) == [48, 52, 55]
```

### C5. Determinism and aliases

```python
def test_symbol_parsing_is_deterministic():
    a = parse_chord_symbol("F#3min7").to_dict()
    for _ in range(100):
        assert parse_chord_symbol("F#3min7").to_dict() == a

@pytest.mark.parametrize("alias,canonical", [
    ("C4m", "C4min"), ("C4min", "C4min"), ("C4minor", "C4min"),
    ("C4maj", "C4maj"), ("C4major", "C4maj"),
    ("C4min7", "C4min7"), ("C4m7", "C4min7"),
])
def test_documented_aliases_normalize(alias, canonical):
    assert parse_chord_symbol(alias).canonical() == canonical
```

### C6. Ambiguity and rejection

```python
@pytest.mark.parametrize("symbol,code", [
    ("C",        "AMBIGUOUS_CHORD"),       # bare root
    ("Cmaj",     "INVALID_CHORD_SYMBOL"),  # root octave required for symbols
    ("C4",       "AMBIGUOUS_CHORD"),
    ("C4M",      "UNKNOWN_CHORD_QUALITY"), # 'M' deliberately not an alias
    ("C4M7",     "UNKNOWN_CHORD_QUALITY"), # classic maj7/min-major ambiguity
    ("C4major7", "UNKNOWN_CHORD_QUALITY"),
    ("C4min7b5", "UNKNOWN_CHORD_QUALITY"), # canonical name is m7b5
    ("C4maj9#11","UNKNOWN_CHORD_QUALITY"), # not in v1 dictionary
    ("C4maj7/",  "INVALID_CHORD_SYMBOL"),
    ("C4maj7/E", "INVALID_CHORD_SYMBOL"),  # slash bass needs octave
    ("",         "INVALID_CHORD_SYMBOL"),
    ("H4maj",    "INVALID_CHORD_SYMBOL"),
    ("C4maj7/X9", "INVALID_CHORD_SYMBOL"),
])
def test_chord_rejection(symbol, code): ...

@pytest.mark.parametrize("inversion", [-1, 3, 99])
def test_invalid_inversion(inversion):
    with pytest.raises(MusicalUnitError) as exc:
        parse_chord_symbol("C4maj", inversion=inversion)
    assert exc.value.code == "INVALID_INVERSION"

def test_voicing_out_of_range_rejected():
    # C4maj has 3 voices: second-highest (E4=64) drops to 52
    assert chord_pitches(parse_chord_symbol("C4maj", voicing="drop2")) == [52, 60, 67]
    # octave_shift -5 makes the dropped voice negative
    with pytest.raises(MusicalUnitError) as exc:
        chord_pitches(parse_chord_symbol("C4maj7", voicing="drop3", octave_shift=-5))
    assert exc.value.code == "PITCH_OUT_OF_RANGE"
```

### C7. Explicit pitch form

```python
def test_explicit_pitches_are_not_inferred():
    spec = parse_chord_spec({"pitches": ["C4", "E4", "G4"], "at": 0, "length": 48})
    assert [n["key"] for n in spec.notes] == [60, 64, 67]

def test_explicit_pitches_preserve_order_and_duplicates():
    spec = parse_chord_spec({"pitches": [67, 60, 64], "at": 0, "length": 48})
    assert [n["key"] for n in spec.notes] == [60, 64, 67]  # sorted, no dedupe

def test_symbol_and_pitches_together_rejected():
    with pytest.raises(MusicalUnitError) as exc:
        parse_chord_spec({"symbol": "C4maj", "pitches": [60, 64, 67],
                          "at": 0, "length": 48})
    assert exc.value.code == "INVALID_CHORD_SYMBOL"

@pytest.mark.parametrize("field,value,code", [
    ("volume", 201, "INVALID_VELOCITY"),
    ("volume", -1, "INVALID_VELOCITY"),
    ("panning", 101, "INVALID_PANNING"),
    ("panning", -101, "INVALID_PANNING"),
])
def test_chord_spec_velocity_panning(field, value, code):
    payload = {"symbol": "C4maj", "at": 0, "length": 48, field: value}
    with pytest.raises(MusicalUnitError) as exc:
        parse_chord_spec(payload)
    assert exc.value.code == code
```

---

## 7. Suite D — Phrase / motif composition (`TestPhraseParsing`)

### D1. Relative offsets and normalization

```python
def test_phrase_notes_keep_relative_offsets():
    phrase = parse_phrase(PHRASE_A)
    assert phrase.length_ticks == 192
    assert [(n.key, n.pos, n.length, n.volume) for n in phrase.notes] == [
        (36, 0, 48, 100), (39, 48, 48, 80), (43, 96, 48, 120), (36, 144, 48, 100),
    ]

def test_phrase_length_inferred_from_notes():
    phrase = parse_phrase({"notes": [{"key": 60, "pos": 24, "length": 48}]})
    assert phrase.length_ticks == 72

def test_phrase_rejects_note_past_declared_length():
    with pytest.raises(MusicalUnitError) as exc:
        parse_phrase({"length_ticks": 96,
                      "notes": [{"key": 60, "pos": 72, "length": 48}]})
    assert exc.value.code == "CLIP_TOO_SHORT"

def test_empty_phrase_rejected():
    with pytest.raises(MusicalUnitError) as exc:
        parse_phrase({"notes": []})
    assert exc.value.code == "EMPTY_UNIT"

def test_phrase_missing_required_fields():
    for bad in ({"pos": 0, "length": 48},          # no key
                {"key": 60, "length": 48},         # no pos
                {"key": 60, "pos": 0}):            # no length
        with pytest.raises(MusicalUnitError):
            parse_phrase({"notes": [bad]})

def test_phrase_key_and_pitch_rejected():
    """Both fields present is ambiguous; current code silently prefers 'key'."""
    with pytest.raises(MusicalUnitError) as exc:
        parse_phrase({"notes": [{"key": 60, "pitch": "D4",
                                 "pos": 0, "length": 48}]})
    assert exc.value.code == "AMBIGUOUS_PITCH"
```

### D2. Rests and overlaps

```python
def test_explicit_rests_are_timing_only():
    phrase = parse_phrase({
        "notes": [{"key": 60, "pos": 0, "length": 48},
                  {"key": 62, "pos": 96, "length": 48}],
        "rests": [{"at": 48, "length": 48}],
    })
    assert len(phrase.notes) == 2
    assert phrase.rests == [(48, 48)]

def test_rest_overlapping_note_rejected():
    with pytest.raises(MusicalUnitError) as exc:
        parse_phrase({"notes": [{"key": 60, "pos": 0, "length": 96}],
                      "rests": [{"at": 48, "length": 48}]})
    assert exc.value.code == "OVERLAP_CONFLICT"

def test_same_pitch_overlap_is_flagged_not_rejected():
    phrase = parse_phrase({"notes": [
        {"key": 60, "pos": 0,  "length": 96},
        {"key": 60, "pos": 48, "length": 96},
    ]})
    assert len(phrase.notes) == 2
    assert "same-pitch overlap" in phrase.warnings[0]
```

### D3. Deterministic ordering

```python
def test_phrase_notes_are_sorted_by_pos_key_length():
    phrase = parse_phrase({"notes": [
        {"key": 67, "pos": 48, "length": 24},
        {"key": 60, "pos": 0,  "length": 48},
        {"key": 64, "pos": 48, "length": 24},
        {"key": 62, "pos": 48, "length": 48},
    ]})
    assert [(n.pos, n.key, n.length) for n in phrase.notes] == [
        (0, 60, 48), (48, 62, 48), (48, 64, 24), (48, 67, 24),
    ]
```

### D4. Velocity / pan validation

```python
@pytest.mark.parametrize("volume", [-1, 201, 1.5, "100", True])
def test_invalid_velocity(volume): ...   # INVALID_VELOCITY

@pytest.mark.parametrize("panning", [-101, 101])
def test_invalid_panning(panning): ...   # INVALID_PANNING
```

---

## 8. Suite E — Transformations (`TestTransforms`)

All transforms are pure: `transform(notes, ...) -> new_notes`, input untouched.

Transform object keys (per occurrence or standalone):

| Key | Type | Default | Meaning |
|---|---|---|---|
| `transpose` | int | 0 | semitones |
| `time_shift` | int | 0 | ticks |
| `stretch` | float | 1.0 | ratio around tick 0 |
| `reverse` | bool | false | mirror within the unit/clip span |
| `reverse_span_ticks` | int | unit length | explicit span for `reverse` |
| `quantize` | int | unset | grid in ticks |
| `quantize_mode` | str | `"start"` | `start`/`start+length`/`end` |
| `velocity_scale` | float | 1.0 | multiplier |
| `velocity_offset` | int | 0 | added after scaling |
| `velocity_clamp` | [int,int] | `[0,200]` | final bounds |

### E1. Transpose

```python
def test_transpose_exact():
    out = transpose_notes(PHRASE_A["notes"], 2)
    assert [n["key"] for n in out] == [38, 41, 45, 38]

def test_transpose_octaves():
    assert [n["key"] for n in transpose_notes(PHRASE_A["notes"], 12)] == [48, 51, 55, 48]
    assert [n["key"] for n in transpose_notes(PHRASE_A["notes"], -12)] == [24, 27, 31, 24]

def test_transpose_out_of_range_rejected():
    with pytest.raises(MusicalUnitError) as exc:
        transpose_notes([{"key": 120, "pos": 0, "length": 48}], 12)
    assert exc.value.code == "PITCH_OUT_OF_RANGE"

def test_transpose_does_not_touch_time_or_velocity():
    out = transpose_notes(PHRASE_A["notes"], 5)
    assert [(n["pos"], n["length"], n["volume"]) for n in out] == [
        (n["pos"], n["length"], n["volume"]) for n in PHRASE_A["notes"]
    ]
```

### E2. Time shift

```python
def test_time_shift_exact():
    out = shift_notes(PHRASE_A["notes"], 96)
    assert [n["pos"] for n in out] == [96, 144, 192, 240]
    assert all(n["length"] == 48 for n in out)

def test_time_shift_negative_rejected():
    with pytest.raises(MusicalUnitError) as exc:
        shift_notes(PHRASE_A["notes"], -1)
    assert exc.value.code == "NEGATIVE_TIME"
```

### E3. Time stretch (ratio, boundary preserving)

Definition: `new_pos = round_half_up(pos * ratio)`,
`new_end = round_half_up((pos + length) * ratio)`, `new_length = max(1, new_end - new_pos)`.
This prevents the cumulative drift produced by scaling lengths independently.

```python
@pytest.mark.parametrize("ratio,expected", [
    (2.0, [(0, 96), (96, 96), (192, 96), (288, 96)]),
    (0.5, [(0, 24), (24, 24), (48, 24), (72, 24)]),
    (1.5, [(0, 72), (72, 72), (144, 72), (216, 72)]),
    (1.0, [(0, 48), (48, 48), (96, 48), (144, 48)]),
])
def test_stretch_exact(ratio, expected):
    out = stretch_notes(PHRASE_A["notes"], ratio)
    assert [(n["pos"], n["length"]) for n in out] == expected

@pytest.mark.parametrize("ratio", [0, -1, float("nan"), float("inf")])
def test_invalid_stretch_ratio(ratio): ...   # INVALID_RATIO

def test_stretch_minimum_length_is_one_tick():
    out = stretch_notes([{"key": 60, "pos": 0, "length": 1}], 0.1)
    assert out[0]["length"] == 1
```

### E4. Reverse

Definition: mirror each note inside `span_ticks`: `new_pos = span - (pos + length)`.
Pitch, length, velocity, panning are unchanged. Reverse is an involution.

```python
def test_reverse_exact():
    out = reverse_notes(PHRASE_A["notes"], span_ticks=192)
    assert [(n["key"], n["pos"], n["length"], n["volume"]) for n in out] == [
        (36, 0, 48, 100),
        (43, 48, 48, 120),
        (39, 96, 48, 80),
        (36, 144, 48, 100),
    ]

def test_reverse_twice_is_identity():
    def norm(notes):
        return [(n["key"], n["pos"], n["length"], n["volume"],
                 n.get("panning", 0)) for n in notes]

    once = reverse_notes(PHRASE_A["notes"], span_ticks=192)
    twice = reverse_notes(once, span_ticks=192)
    # parse_phrase adds note_name/panning; compare musical fields only.
    assert norm(twice) == norm(parse_phrase(PHRASE_A).notes_as_dicts())

def test_reverse_rejects_note_outside_span():
    with pytest.raises(MusicalUnitError) as exc:
        reverse_notes([{"key": 60, "pos": 180, "length": 48}], span_ticks=192)
    assert exc.value.code == "NEGATIVE_TIME"

def test_reverse_span_default_is_note_end():
    """apply_transforms without span_ticks mirrors inside the note end.

    With a leading rest the mirror collapses; callers that need the musical
    phrase length must pass span_ticks explicitly.
    """
    notes = [{"key": 60, "pos": 96, "length": 48, "volume": 100}]
    default = apply_transforms(notes, {"reverse": True})
    explicit = apply_transforms(notes, {"reverse": True}, span_ticks=192)
    assert default[0]["pos"] == 96     # span = 144
    assert explicit[0]["pos"] == 48    # span = 192
```

### E5. Quantize

`quantize_notes(notes, grid_ticks, mode="start"|"start+length"|"end")`.
`start` snaps only the onset; `start+length` snaps both onset and end;
`end` snaps only the end and keeps the onset. Ties round **up** (away from zero).
Grid must be a positive integer. Idempotent for `start` and `start+length`.

```python
@pytest.mark.parametrize("grid,expected", [
    (12, [0, 24, 36, 96]),
    (24, [0, 24, 24, 96]),
    (48, [0, 0, 48, 96]),
])
def test_quantize_start(grid, expected):
    out = quantize_notes(OFF_GRID["notes"], grid_ticks=grid)
    assert [n["pos"] for n in out] == expected

@pytest.mark.parametrize("pos,expected", [
    (5, 0), (6, 12), (7, 12), (17, 12), (18, 24), (19, 24), (30, 36), (100, 96),
])
def test_quantize_ties_round_up(pos, expected):
    out = quantize_notes([{"key": 60, "pos": pos, "length": 10}], grid_ticks=12)
    assert out[0]["pos"] == expected

def test_quantize_start_and_length():
    out = quantize_notes([{"key": 60, "pos": 5, "length": 10}],
                         grid_ticks=12, mode="start+length")
    assert (out[0]["pos"], out[0]["length"]) == (0, 12)

def test_quantize_end_mode():
    out = quantize_notes([{"key": 60, "pos": 5, "length": 10}],
                         grid_ticks=12, mode="end")
    assert (out[0]["pos"], out[0]["length"]) == (5, 7)  # end 15 -> 12

def test_quantize_is_idempotent():
    once = quantize_notes(OFF_GRID["notes"], grid_ticks=12)
    assert quantize_notes(once, grid_ticks=12) == once

@pytest.mark.parametrize("grid", [0, -12, 1.5, True])
def test_invalid_quantize_grid(grid): ...   # INVALID_GRID
```

### E6. Velocity scaling / offset

Fixed order: `round_half_up(volume * multiplier) + offset`, then clamp 0..200.
`velocity_offset` is applied after scaling.

```python
@pytest.mark.parametrize("mult,offset,expected", [
    (1.0, 0,    [100, 80, 120, 100]),
    (0.5, 0,    [50, 40, 60, 50]),
    (1.5, 0,    [150, 120, 180, 150]),
    (1.0, -20,  [80, 60, 100, 80]),
    (1.5, 10,   [160, 130, 190, 160]),
    (2.0, 0,    [200, 160, 200, 200]),   # clamped
    (1.0, -500, [0, 0, 0, 0]),           # clamped
])
def test_velocity_transform(mult, offset, expected):
    out = scale_velocity(PHRASE_A["notes"], multiplier=mult, offset=offset)
    assert [n["volume"] for n in out] == expected

def test_velocity_half_rounds_up():
    out = scale_velocity([{"key": 60, "pos": 0, "length": 1, "volume": 101}],
                         multiplier=0.5)
    assert out[0]["volume"] == 51    # 50.5 -> 51
```

### E7. Fixed transform ordering on one occurrence

The apply order is: `transpose → time_shift → stretch → reverse → quantize →
velocity_scale → velocity_offset → pitch/time range validation`.

```python
def test_transform_order_is_fixed():
    # (pos 0, len 48) -> shift +48 -> (48,48) -> stretch 0.5 -> (24,24)
    out = apply_transforms(
        [{"key": 60, "pos": 0, "length": 48, "volume": 100}],
        {"transpose": 12, "time_shift": 48, "stretch": 0.5,
         "quantize": 12, "velocity_scale": 0.5, "velocity_offset": 10},
    )
    assert out == [{"key": 72, "pos": 24, "length": 24, "volume": 60,
                    "panning": 0}]

@pytest.mark.parametrize("bad", [
    {"transpose": 1.5}, {"stretch": "2"}, {"quantize": 0},
    {"velocity_scale": -1}, {"velocity_offset": 1.5},
])
def test_invalid_transform_arguments(bad): ...
```

---

## 9. Suite F — Rhythm patterns (`TestRhythm`)

### F1. Drum mapping (closed table)

```python
DRUM_MAP = {
    "kick": 36, "side_stick": 37, "snare": 38, "clap": 39,
    "closed_hat": 42, "pedal_hat": 44, "open_hat": 46,
    "low_tom": 45, "mid_tom": 47, "high_tom": 50,
    "crash": 49, "ride": 51, "rim": 37, "cowbell": 56,
}

@pytest.mark.parametrize("drum,key", DRUM_MAP.items())
def test_drum_map(drum, key):
    pattern = parse_rhythm_pattern({
        "steps": 16,
        "hits": [{"step": 0, "drum": drum}],
    })
    assert pattern.hits[0].key == key
```

Duplicate entries in the map (`side_stick`/`rim`) are documented aliases and must
resolve to the same key; the accepted set is exactly `sorted(DRUM_MAP)`.

### F2. Step grid and lengths

```python
def test_step_grid_positions():
    pattern = parse_rhythm_pattern({
        "steps": 16,
        "hits": [
            {"step": 0,  "drum": "kick", "length_steps": 1},
            {"step": 4,  "drum": "kick"},
            {"step": 8,  "drum": "kick"},
            {"step": 12, "drum": "snare"},
            {"step": 14, "drum": "closed_hat", "velocity": 60,
             "length_steps": 2},
        ],
    })
    assert [(h.pos, h.key, h.length, h.volume) for h in pattern.hits] == [
        (0, 36, 12, 100), (48, 36, 12, 100), (96, 36, 12, 100),
        (144, 38, 12, 100), (168, 42, 24, 60),
    ]
    assert pattern.length_ticks == 192

def test_custom_step_ticks():
    pattern = parse_rhythm_pattern({"steps": 8, "step_ticks": 24,
                                    "hits": [{"step": 3, "drum": "kick"}]})
    assert pattern.hits[0].pos == 72
    assert pattern.length_ticks == 192

def test_explicit_pitch_overrides_drum():
    pattern = parse_rhythm_pattern({"steps": 4,
                                    "hits": [{"step": 0, "pitch": "C2"}]})
    assert pattern.hits[0].key == 36
```

### F3. Rejection

```python
@pytest.mark.parametrize("payload,code", [
    ({"steps": 16, "hits": []}, "EMPTY_UNIT"),
    ({"steps": 0, "hits": [{"step": 0, "drum": "kick"}]}, "INVALID_STEP"),
    ({"steps": 16, "hits": [{"step": 16, "drum": "kick"}]}, "INVALID_STEP"),
    ({"steps": 16, "hits": [{"step": -1, "drum": "kick"}]}, "INVALID_STEP"),
    ({"steps": 16, "hits": [{"step": 0, "drum": "cowbell_9000"}]}, "UNKNOWN_DRUM"),
    ({"steps": 16, "hits": [{"step": 0, "drum": "kick"},
                            {"step": 0, "drum": "kick"}]}, "DUPLICATE_HIT"),
    ({"steps": 16, "hits": [{"step": 0}]}, "INVALID_PITCH"),
])
def test_rhythm_rejection(payload, code): ...

@pytest.mark.parametrize("payload,code", [
    ({"steps": 16, "hits": [{"step": 0, "drum": "kick", "velocity": 201}]},
     "INVALID_VELOCITY"),
    ({"steps": 16, "hits": [{"step": 0, "drum": "kick", "velocity": -1}]},
     "INVALID_VELOCITY"),
    ({"steps": 16, "hits": [{"step": 0, "drum": "kick", "panning": 101}]},
     "INVALID_PANNING"),
    ({"steps": 16, "hits": [{"step": 0, "drum": "kick", "length_steps": 0}]},
     "INVALID_TIME"),
    ({"steps": 16, "hits": [{"step": 0, "drum": "kick", "length_steps": -1}]},
     "INVALID_TIME"),
])
def test_rhythm_velocity_panning_length(payload, code): ...
```

### F4. Repeat / shift / stretch

```python
def test_pattern_repeat_places_copies():
    plan = place_rhythm(song, 2, RHYTHM_KICK, at_bars=0, repeat=4,
                        spacing_bars=1, target="bb", apply=False)["plan"]
    assert [o["at_ticks"] for o in plan["occurrences"]] == [0, 192, 384, 576]
    assert len(plan["occurrences"][0]["notes"]) == 4

def test_pattern_shift_and_stretch():
    out = stretch_notes(parse_rhythm_pattern(RHYTHM_KICK).as_dicts(), 2.0)
    assert [n["pos"] for n in out] == [0, 96, 192, 288]
```

### F5. BB pattern placement (shared pattern semantics)

```python
def test_bb_clip_repeats_reference_same_pattern(song):
    plan = place_rhythm(song, 2, RHYTHM_KICK, at_bars=0, repeat=3,
                        spacing_bars=1, target="bb", apply=True)["plan"]
    root = song.root
    drums = find_tracks(root)[2]
    clips = drums.findall("bbtco")
    assert [c.get("pos") for c in clips] == ["0", "192", "384"]
    # all clips trigger the one BB pattern: notes must not be duplicated
    inner = drums.find("patterntrack/trackcontainer/track")
    assert len(inner.findall("pattern")) == 1
    assert len(plan["occurrences"][0]["notes"]) == 4

def test_per_occurrence_transform_on_bb_is_rejected(song):
    with pytest.raises(MusicalUnitError) as exc:
        place_rhythm(song, 2, RHYTHM_KICK, at_bars=0, repeat=2,
                     spacing_bars=1, target="bb",
                     transforms={1: {"transpose": 12}})
    assert exc.value.code == "UNSUPPORTED_TRANSFORMATION"
```

### F6. AFP and kicker routing

The drum map is a MIDI map: it always writes the GM key. On an
`audiofileprocessor` track the loaded sample is fixed by
`set_audiofileprocessor_sample`, so the key is stored but does not choose a sample.
On a `kicker` track key 36 is the default kick. Tests assert the key attribute in
both cases and that the AFP sample path survives placement.

```python
def test_rhythm_on_afp_track_preserves_sample(song, tmp_path):
    from mxm_mcp.xml_parser import set_audiofileprocessor_sample

    song.add_track("instrument", "Kick", instrument="audiofileprocessor")
    idx = len(find_tracks(song.root)) - 1
    sample = tmp_path / "kick.wav"
    sample.write_bytes(b"")           # path only; no audio needed
    set_audiofileprocessor_sample(song.root, idx, str(sample))

    place_rhythm(song, idx, RHYTHM_KICK, at_bars=0, repeat=2,
                 spacing_bars=1, apply=True)

    track = find_tracks(song.root)[idx]
    afp = track.find("instrumenttrack/instrument/audiofileprocessor")
    assert afp.get("src") == str(sample)
    clips = instrument_clips(track)
    assert len(clips) == 2
    assert {n.get("key") for c in clips for n in c.findall("note")} == {"36"}

def test_rhythm_on_kicker_uses_key_36(song):
    place_rhythm(song, 2, RHYTHM_KICK, at_bars=0, repeat=1,
                 target="bb", apply=True)
    inner = find_tracks(song.root)[2].find("patterntrack/trackcontainer/track")
    assert inner.find("instrumenttrack/instrument").get("name") == "kicker"
    assert {n.get("key") for n in inner.findall("pattern/note")} == {"36"}
```

---

## 10. Suite G — Arrangement placement (`TestArrangementOps`)

### G1. Phrase placement

```python
def test_place_phrase_exact(song):
    plan = place_phrase(song, 0, PHRASE_A, at_bars=0, apply=True)["plan"]
    assert plan["occurrences"][0]["clip"] == {"pos": 0, "len": 192}
    assert note_tuples(song.root, 0) == [
        (0, 0, 36, 48, 100, 0), (0, 48, 39, 48, 80, 0),
        (0, 96, 43, 48, 120, 0), (0, 144, 36, 48, 100, 0),
    ]

def test_place_phrase_at_bar(song):
    place_phrase(song, 0, PHRASE_A, at_bars=4, apply=True)
    assert note_tuples(song.root, 0)[0] == (768, 0, 36, 48, 100, 0)
    assert get_arrangement(song.root)[0]["pos"] == 768
```

### G2. Repetition with spacing and per-occurrence transforms

```python
def test_repeat_with_per_occurrence_transforms(song):
    plan = place_phrase(
        song, 0, PHRASE_A, at_bars=0, repeat=4, spacing_bars=1,
        transforms={
            2: {"transpose": 12},
            3: {"transpose": -12, "velocity_scale": 0.5},
        },
        apply=True,
    )["plan"]
    assert [o["at_ticks"] for o in plan["occurrences"]] == [0, 192, 384, 576]
    occ = plan["occurrences"]
    assert [n["key"] for n in occ[0]["notes"]] == [36, 39, 43, 36]
    assert [n["key"] for n in occ[1]["notes"]] == [36, 39, 43, 36]
    assert [n["key"] for n in occ[2]["notes"]] == [48, 51, 55, 48]
    assert [n["key"] for n in occ[3]["notes"]] == [24, 27, 31, 24]
    assert [n["volume"] for n in occ[3]["notes"]] == [50, 40, 60, 50]

def test_transforms_keyed_by_out_of_range_occurrence(song):
    with pytest.raises(MusicalUnitError) as exc:
        place_phrase(song, 0, PHRASE_A, repeat=2, transforms={5: {"transpose": 1}})
    assert exc.value.code == "PATTERN_INDEX_OUT_OF_RANGE"

def test_repeat_zero_rejected(song): ...            # INVALID_TIME / EMPTY_UNIT
def test_negative_at_rejected(song): ...            # NEGATIVE_TIME
def test_zero_spacing_with_repeat_rejected(song): ...
```

### G3. Chord and progression placement

```python
def test_place_chord_together(song):
    plan = place_chord(song, 1, {"symbol": "C4maj7", "at": 0, "length": 192},
                       apply=True)["plan"]
    assert len(plan["occurrences"]) == 1
    assert note_tuples(song.root, 1) == [
        (0, 0, 60, 192, 100, 0), (0, 0, 64, 192, 100, 0),
        (0, 0, 67, 192, 100, 0), (0, 0, 71, 192, 100, 0),
    ]

def test_place_progression_sequential(song):
    plan = place_progression(song, 1, PROGRESSION_I_VI_IV_V,
                             at_bars=0, apply=True)["plan"]
    assert [o["at_ticks"] for o in plan["occurrences"]] == [0, 192, 384, 576]
    keys = [sorted(n["key"] for n in o["notes"]) for o in plan["occurrences"]]
    assert keys == [[60, 64, 67], [57, 60, 64], [65, 69, 72], [67, 71, 74]]

def test_place_chord_velocity_panning(song):
    """Chord-level volume/panning must reach every constituent note."""
    place_chord(song, 1, {"symbol": "C4maj", "at": 0, "length": 192,
                          "volume": 80, "panning": -20}, apply=True)
    notes = instrument_clips(find_tracks(song.root)[1])[0].findall("note")
    assert [n.get("vol") for n in notes] == ["80", "80", "80"]
    assert [n.get("pan") for n in notes] == ["-20", "-20", "-20"]

def test_progression_with_rest_entry(song):
    plan = place_progression(song, 1, {"chords": [
        {"symbol": "C4maj", "bars": 1}, {"rest": 1}, {"symbol": "G4maj", "bars": 2},
    ]}, at_bars=0, apply=False)["plan"]
    assert [o["at_ticks"] for o in plan["occurrences"]] == [0, 384]
    assert plan["occurrences"][1]["clip"]["len"] == 384

def test_progression_absolute_positions(song):
    plan = place_progression(song, 1, {"chords": [
        {"symbol": "C4maj", "at": 0},
        {"symbol": "G4maj", "at": 384},
    ]}, layout="absolute", apply=False)["plan"]
    assert [o["at_ticks"] for o in plan["occurrences"]] == [0, 384]

def test_mixing_duration_and_at_rejected():
    with pytest.raises(MusicalUnitError) as exc:
        parse_progression({"chords": [
            {"symbol": "C4maj", "bars": 1},
            {"symbol": "G4maj", "at": 192},
        ]})
    assert exc.value.code == "INVALID_TIME"
```

### G4. Pattern references and clip length

```python
def test_each_occurrence_gets_its_own_clip_when_transformed(song):
    place_phrase(song, 0, PHRASE_A, repeat=2, spacing_bars=1,
                 transforms={1: {"transpose": 12}}, apply=True)
    clips = instrument_clips(find_tracks(song.root)[0])
    assert len(clips) == 2
    assert sorted(int(c.get("pos")) for c in clips) == [0, 192]
    assert all(len(c.findall("note")) == 4 for c in clips)
    assert all(c.tag == "midiclip" for c in clips)

def test_linked_patterns_rejected_for_midiclip(song):
    with pytest.raises(MusicalUnitError) as exc:
        place_phrase(song, 0, PHRASE_A, repeat=2, link=True)
    assert exc.value.code == "UNSUPPORTED_LINK"
    # BB clips DO support references and are tested in F5

def test_clip_extends_to_contain_notes(song):
    plan = place_phrase(song, 0,
                        {"name": "long", "notes": [
                            {"key": 60, "pos": 0, "length": 48},
                            {"key": 62, "pos": 180, "length": 48},
                        ]},
                        at_tick=0, apply=True)["plan"]
    assert plan["occurrences"][0]["clip"]["len"] == 228
    clip = instrument_clips(find_tracks(song.root)[0])[0]
    assert clip.get("len") == "228"

def test_fit_modes(song):
    phrase = {"notes": [{"key": 60, "pos": 0, "length": 96}]}
    with pytest.raises(MusicalUnitError) as exc:
        place_phrase(song, 0, phrase, clip_len_ticks=48, fit="error", apply=True)
    assert exc.value.code == "CLIP_TOO_SHORT"
    plan = place_phrase(song, 0, phrase, clip_len_ticks=48, fit="truncate",
                        apply=True)["plan"]
    assert plan["occurrences"][0]["notes"][0]["length"] == 48
```

### G5. Target validation

```python
@pytest.mark.parametrize("track_index,code", [
    (-1, "TRACK_INDEX_OUT_OF_RANGE"),
    (99, "TRACK_INDEX_OUT_OF_RANGE"),
])
def test_bad_track_index(song, track_index, code): ...

def test_notes_on_sample_track_rejected(song):
    with pytest.raises(MusicalUnitError) as exc:
        place_phrase(song, 3, PHRASE_A)
    assert exc.value.code == "WRONG_TRACK_TYPE"

def test_bad_pattern_index(song):
    place_phrase(song, 0, PHRASE_A, at_bars=0, apply=True)
    with pytest.raises(MusicalUnitError) as exc:
        place_phrase(song, 0, PHRASE_A, at_bars=1, pattern_index=5)
    assert exc.value.code == "PATTERN_INDEX_OUT_OF_RANGE"
```

### G6. Arrangement read-back and boundaries

```python
def test_arrangement_reflects_all_units(song):
    place_phrase(song, 0, PHRASE_A, at_bars=0, repeat=2, spacing_bars=1)
    place_progression(song, 1, PROGRESSION_I_VI_IV_V, at_bars=0)
    place_rhythm(song, 2, RHYTHM_KICK, at_bars=4, repeat=2,
                 spacing_bars=1, target="bb")
    clips = get_arrangement(song.root)
    assert clips == sorted(clips, key=lambda c: (c["pos"], c["track_index"]))
    assert {c["kind"] for c in clips} == {"midiclip", "bbtco"}
    assert all(c["len"] > 0 for c in clips)

def test_no_note_outside_its_clip(song):
    place_phrase(song, 0, PHRASE_A, at_bars=0, repeat=4, spacing_bars=1,
                 transforms={3: {"stretch": 2.0}}, apply=True)
    for track in find_tracks(song.root):
        for clip in track.findall("pattern") + track.findall("midiclip"):
            clip_len = int(clip.get("len", 0))
            for n in clip.findall("note"):
                pos, length = int(n.get("pos", 0)), int(n.get("len", 0))
                assert 0 <= pos
                assert length >= 1
                assert pos + length <= clip_len

def test_plan_song_length(song):
    plan = place_phrase(song, 0, PHRASE_A, at_bars=8, repeat=2,
                        spacing_bars=2)["plan"]
    # occurrences at bars 8 and 10; the 1-bar phrase ends at bar 11
    assert plan["song_length_ticks"] == 11 * 192
```

### G7. Batch placement (`arrange_units`, `place_chords`)

```python
def test_place_chords_at_absolute_positions(song):
    plan = place_chords(song, 1, [
        {"symbol": "C4maj", "at": 0,   "length": 192},
        {"symbol": "G4maj", "at": 384, "length": 192},
    ], apply=True)["plan"]
    assert len(plan["occurrences"]) == 2
    assert note_tuples(song.root, 1) == [
        (0, 0, 60, 192, 100, 0), (0, 0, 64, 192, 100, 0),
        (0, 0, 67, 192, 100, 0), (384, 0, 67, 192, 100, 0),
        (384, 0, 71, 192, 100, 0), (384, 0, 74, 192, 100, 0),
    ]

def test_place_chords_empty_rejected(song):
    with pytest.raises(MusicalUnitError) as exc:
        place_chords(song, 1, [])
    assert exc.value.code == "EMPTY_UNIT"

def test_arrange_units_across_tracks(song):
    placements = [
        {"type": "phrase", "track_index": 0, "phrase": PHRASE_A,
         "at_bars": 0, "repeat": 2, "spacing_bars": 1},
        {"type": "progression", "track_index": 1,
         "progression": PROGRESSION_I_VI_IV_V, "at_bars": 0},
        {"type": "rhythm", "track_index": 2, "rhythm": RHYTHM_KICK,
         "at_bars": 4, "repeat": 2, "spacing_bars": 1, "target": "bb"},
    ]
    preview = arrange_units(song, placements, apply=False)
    applied = arrange_units(song, placements, apply=True)
    assert applied["plan"] == preview["plan"]
    assert applied["plan"]["kind"] == "arrangement"
    assert len(applied["plan"]["units"]) == 3
    assert len(note_tuples(song.root, 0)) == 8
    assert len(get_arrangement(song.root)) >= 5
    assert applied["plan"]["song_length_ticks"] == 6 * 192

def test_arrange_units_unknown_kind_rejected(song):
    with pytest.raises(MusicalUnitError) as exc:
        arrange_units(song, [{"type": "sonata", "track_index": 0}])
    assert exc.value.code == "INVALID_PHRASE"
```

### G8. Selected note edits (`edit_notes`)

```python
def test_edit_notes_filter_keys(song):
    place_phrase(song, 0, PHRASE_A, apply=True)
    plan = edit_notes(song, 0, transforms={"transpose": 12},
                      filter_keys=[36], apply=True)["plan"]
    assert plan["notes_matched"] == 2          # two C2 notes in PHRASE_A
    keys = [k for _, _, k, _, _, _ in note_tuples(song.root, 0)]
    assert keys.count(48) == 2                 # transposed
    assert 39 in keys and 43 in keys           # untouched

def test_edit_notes_filter_time_range_is_clip_relative(song):
    place_phrase(song, 0, PHRASE_A, at_bars=4, apply=True)
    plan = edit_notes(song, 0, transforms={"transpose": 12},
                      filter_time_range=(0, 48), apply=True)["plan"]
    assert plan["notes_matched"] == 1          # only the first note (pos 0)

def test_edit_notes_preview_does_not_mutate(song, snapshot):
    place_phrase(song, 0, PHRASE_A, apply=True)
    before = snapshot(song)
    plan = edit_notes(song, 0, transforms={"transpose": 12},
                      filter_keys=[36], apply=False)["plan"]
    assert plan["notes_matched"] == 2
    assert snapshot(song) == before
```

---

## 11. Suite H — Automation (`TestAutomationOps`)

### H1. Batch creation

```python
def test_batch_automation_curves(song):
    result = add_automation_batch(song, [
        {"target_type": "song", "param": "tempo",
         "points": [{"bar": 0, "value": 120}, {"bar": 2, "value": 150},
                    {"bar": 4, "value": 120}]},
        {"target_type": "track", "target_index": 0, "param": "volume",
         "points": [{"bar": 0, "value": 100}, {"bar": 8, "value": 60}]},
        {"target_type": "mixer", "target_index": 1, "param": "volume",
         "points": [{"bar": 0, "value": 1.0}, {"bar": 8, "value": 0.5}]},
    ], apply=True)
    assert result["created"] == 3
    auto = [t for t in find_tracks(song.root) if t.get("type") == "5"]
    assert len(auto) == 3
    times = auto[0].find("automationpattern").findall("time")
    assert [(t.get("pos"), t.get("value")) for t in times] == [
        ("0", "120.0"), ("384", "150.0"), ("768", "120.0"),
    ]

def test_automation_curve_length_and_object_link(song):
    model_id, _ = resolve_automation_target(song.root, "song", 0, "tempo")
    add_automation_batch(song, [{"target_type": "song", "param": "tempo",
                                 "points": [{"bar": 0, "value": 120},
                                            {"bar": 4, "value": 140}]}])
    pattern = [t for t in find_tracks(song.root) if t.get("type") == "5"][0] \
        .find("automationpattern")
    assert pattern.get("len") == "769"        # max tick + 1
    assert pattern.get("prog") == "1"         # smooth default
    assert int(pattern.find("object").get("id")) == model_id
```

### H2. Point transformations

```python
@pytest.mark.parametrize("ops,expected", [
    ([{"scale": 0.5}], [50, 75, 60]),
    ([{"offset": 10}], [110, 160, 130]),
    ([{"clamp": [0, 120]}], [100, 120, 120]),
    ([{"scale": 2.0, "offset": -50}], [150, 200, 190]),
])
def test_automation_value_transforms(automated_song, ops, expected): ...

def test_automation_time_shift(automated_song):
    plan = edit_automation(automated_song, curve_index=0,
                           ops=[{"time_shift_ticks": 192}], apply=False)["plan"]
    assert [p["pos"] for p in plan["points"]] == [192, 576, 960]

def test_edit_automation_preserves_unmentioned_points(automated_song):
    """A shift must start from the existing points, not an empty list."""
    plan = edit_automation(automated_song, curve_index=0,
                           ops=[{"time_shift_ticks": 0}], apply=False)["plan"]
    assert len(plan["points"]) == 3

def test_edit_automation_no_ops_is_noop(automated_song):
    plan = edit_automation(automated_song, curve_index=0, ops=None,
                           apply=False)["plan"]
    assert [p["value"] for p in plan["points"]] == [100.0, 150.0, 120.0]

def test_automation_reverse(song):
    points = [{"pos": 0, "value": 100}, {"pos": 192, "value": 150},
              {"pos": 384, "value": 120}]
    out = reverse_automation_points(points, span_ticks=384)
    assert [(p["pos"], p["value"]) for p in out] == [
        (0, 120), (192, 150), (384, 100),
    ]

def test_automation_dedupe_last_wins(automated_song):
    plan = edit_automation(automated_song, curve_index=0, ops=[
        {"set": [{"pos": 0, "value": 100}, {"pos": 0, "value": 140}]},
    ])["plan"]
    assert plan["points"] == [{"pos": 0, "value": 140.0}]

def test_automation_duplicate_error_policy(automated_song):
    with pytest.raises(MusicalUnitError) as exc:
        edit_automation(automated_song, curve_index=0, dedupe="error", ops=[
            {"set": [{"pos": 0, "value": 100}, {"pos": 0, "value": 140}]},
        ])
    assert exc.value.code == "DUPLICATE_AUTOMATION_POINT"
```

### H3. Value ranges

```python
@pytest.mark.parametrize("target,index,param,value,code", [
    ("song", 0, "tempo", 9, "AUTOMATION_VALUE_OUT_OF_RANGE"),
    ("song", 0, "tempo", 1000, "AUTOMATION_VALUE_OUT_OF_RANGE"),
    ("song", 0, "master_volume", 201, "AUTOMATION_VALUE_OUT_OF_RANGE"),
    ("song", 0, "master_pitch", 13, "AUTOMATION_VALUE_OUT_OF_RANGE"),
    ("track", 0, "panning", -101, "AUTOMATION_VALUE_OUT_OF_RANGE"),
    ("mixer", 99, "volume", 1.0, "UNKNOWN_AUTOMATION_TARGET"),
])
def test_automation_ranges_and_targets(song, target, index, param, value, code):
    with pytest.raises(MusicalUnitError) as exc:
        add_automation_batch(song, [{"target_type": target, "target_index": index,
                                     "param": param,
                                     "points": [{"bar": 0, "value": value}]}],
                             on_out_of_range="error")
    assert exc.value.code == code

def test_automation_clamp_policy_default(song):
    plan = add_automation_batch(song, [{
        "target_type": "song", "param": "tempo",
        "points": [{"bar": 0, "value": 120}, {"bar": 4, "value": 5000}],
    }], apply=False)["plan"]
    assert plan["curves"][0]["points"][-1]["value"] == 999
```

### H4. Curve shape

```python
def test_smooth_false_writes_linear_progression(song):
    add_automation_batch(song, [{"target_type": "song", "param": "tempo",
                                 "smooth": False,
                                 "points": [{"bar": 0, "value": 120},
                                            {"bar": 4, "value": 140}]}])
    pattern = [t for t in find_tracks(song.root) if t.get("type") == "5"][0] \
        .find("automationpattern")
    assert pattern.get("prog") == "0"

def test_tension_written(song):
    add_automation_batch(song, [{"target_type": "song", "param": "tempo",
                                 "tension": 0.5,
                                 "points": [{"bar": 0, "value": 120},
                                            {"bar": 4, "value": 140}]}])
    pattern = [t for t in find_tracks(song.root) if t.get("type") == "5"][0] \
        .find("automationpattern")
    assert pattern.get("tens") == "0.5"
```

---

## 12. Suite I — Preview / Apply parity (`test_composition_mcp.py::TestParity`)

The central acceptance criterion. Parameterize over every mutating operation.

### I1. No mutation on preview

```python
OPS = [
    ("place_phrase",   lambda song: place_phrase(song, 0, PHRASE_A, at_bars=0, apply=False)),
    ("place_chord",    lambda song: place_chord(song, 1, {"symbol": "C4maj7",
                                                          "at": 0, "length": 192},
                                                apply=False)),
    ("place_progression", lambda song: place_progression(song, 1,
                                                         PROGRESSION_I_VI_IV_V,
                                                         apply=False)),
    ("place_rhythm",   lambda song: place_rhythm(song, 2, RHYTHM_KICK, target="bb", apply=False)),
    ("automation",     lambda song: add_automation_batch(song, [AUTOMATION_SPEC],
                                                         apply=False)),
]

@pytest.mark.parametrize("name,run", OPS, ids=[o[0] for o in OPS])
def test_preview_does_not_mutate(song, snapshot, name, run):
    before_xml = snapshot(song)
    before_modified = song.modified
    run(song)
    assert snapshot(song) == before_xml
    assert song.modified == before_modified
```

### I2. Plan equality (engine level)

Parameterize on callables taking `apply` so preview and apply can never drift:

```python
PREVIEWABLE = [
    ("phrase", lambda p, apply: place_phrase(
        p, 0, PHRASE_A, at_bars=0, repeat=4, spacing_bars=1, apply=apply)),
    ("chord", lambda p, apply: place_chord(
        p, 1, {"symbol": "C4maj7", "at": 0, "length": 192}, apply=apply)),
    ("progression", lambda p, apply: place_progression(
        p, 1, PROGRESSION_I_VI_IV_V, at_bars=0, apply=apply)),
    ("rhythm", lambda p, apply: place_rhythm(
        p, 2, RHYTHM_KICK, at_bars=0, repeat=2, spacing_bars=1,
        target="bb", apply=apply)),
    ("automation", lambda p, apply: add_automation_batch(
        p, [AUTOMATION_SPEC], apply=apply)),
]

@pytest.mark.parametrize("name,run", PREVIEWABLE, ids=[p[0] for p in PREVIEWABLE])
def test_plan_equality(song, name, run):
    preview = run(song, False)
    assert preview["applied"] is False
    assert run(song, False)["plan"] == preview["plan"]   # deterministic preview
    applied = run(song, True)
    assert applied["applied"] is True
    assert applied["plan"] == preview["plan"]
```

Concrete server-level version:

```python
@pytest.mark.parametrize("preview_call,apply_call", [
    (
        lambda: server.add_phrase(0, PHRASE_A, at_bars=0, repeat=4,
                                  spacing_bars=1, apply=False),
        lambda: server.add_phrase(0, PHRASE_A, at_bars=0, repeat=4,
                                  spacing_bars=1, apply=True),
    ),
    (
        lambda: server.add_chord(1, symbol="C4maj7", at_bars=0,
                                 length_bars=1, apply=False),
        lambda: server.add_chord(1, symbol="C4maj7", at_bars=0,
                                 length_bars=1, apply=True),
    ),
    (
        lambda: server.add_progression(1, PROGRESSION_I_VI_IV_V,
                                       at_bars=0, apply=False),
        lambda: server.add_progression(1, PROGRESSION_I_VI_IV_V,
                                       at_bars=0, apply=True),
    ),
    (
        lambda: server.add_automation_batch([AUTOMATION_SPEC], apply=False),
        lambda: server.add_automation_batch([AUTOMATION_SPEC], apply=True),
    ),
])
def test_mcp_preview_apply_parity(song, preview_call, apply_call):
    before = ET.tostring(song.root, encoding="unicode")
    preview = json.loads(preview_call())
    assert json.loads(preview_call()) == preview      # deterministic preview
    assert ET.tostring(song.root, encoding="unicode") == before
    applied = json.loads(apply_call())
    assert applied["plan"] == preview["plan"]
    assert applied["applied"] is True
```

### I3. Error parity

```python
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
```

### I4. Preview after load

```python
def test_preview_plan_is_stable_across_save_load(song, tmp_path):
    path = tmp_path / "preview.mmp"
    song.save(path)
    loaded = LMMSProject()
    loaded.load(path)
    a = json.loads(server.add_phrase(0, PHRASE_A, at_bars=0, apply=False))["plan"]
    server.set_project(loaded)
    b = json.loads(server.add_phrase(0, PHRASE_A, at_bars=0, apply=False))["plan"]
    assert a == b
```

---

## 13. Suite J — Round-trip persistence (`test_composition_roundtrip.py`)

### J1. `.mmp` and `.mmpz`

```python
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
    assert [(p.get("pos"), p.get("len"), p.get("name"))
            for p in instrument_clips(find_tracks(loaded.root)[0])] == [
        ("0", "192", "bass-riff-a"), ("192", "192", "bass-riff-a"),
    ]
    assert all(p.tag == "midiclip"
               for p in instrument_clips(find_tracks(loaded.root)[0]))
```

### J2. Exact note attributes

```python
def test_note_attributes_survive(song, tmp_path):
    place_phrase(song, 0, {
        "name": "attrs",
        "notes": [
            {"key": 36, "pos": 0,  "length": 47, "volume": 0,   "panning": -100},
            {"key": 127, "pos": 1, "length": 1,  "volume": 200, "panning": 100},
        ],
    }, apply=True)
    path = tmp_path / "attrs.mmp"
    song.save(path, compressed=False)
    loaded = LMMSProject(); loaded.load(path)
    assert note_tuples(loaded.root, 0) == [
        (0, 0, 36, 47, 0, -100), (0, 1, 127, 1, 200, 100),
    ]
```

### J3. Arrangement and automation

```python
def test_arrangement_roundtrip(song, tmp_path):
    place_phrase(song, 0, PHRASE_A, at_bars=0, repeat=2, spacing_bars=1)
    place_progression(song, 1, PROGRESSION_I_VI_IV_V, at_bars=0)
    place_rhythm(song, 2, RHYTHM_KICK, at_bars=4, repeat=2,
                 spacing_bars=1, target="bb")
    before = get_arrangement(song.root)
    path = tmp_path / "arr.mmpz"
    song.save(path)
    loaded = LMMSProject(); loaded.load(path)
    assert get_arrangement(loaded.root) == before

def test_automation_roundtrip(song, tmp_path):
    add_automation_batch(song, [
        {"target_type": "song", "param": "tempo",
         "points": [{"bar": 0, "value": 120}, {"bar": 4, "value": 150}]},
        {"target_type": "track", "target_index": 0, "param": "volume",
         "points": [{"bar": 0, "value": 100}, {"bar": 8, "value": 40}]},
    ])
    before = automation_tuples(song.root)
    path = tmp_path / "auto.mmpz"
    song.save(path)
    loaded = LMMSProject(); loaded.load(path)
    assert automation_tuples(loaded.root) == before
    # object IDs still resolve to the same model elements
    for _, _, _, model_id in before:
        assert loaded.root.find(f".//*[@id='{model_id}']") is not None
```

`automation_tuples` returns `(curve_name, prog, tension, [(pos, value)...], object_id)`.

### J4. No tag drift

```python
def test_clip_tag_is_stable(song, tmp_path):
    place_phrase(song, 0, PHRASE_A, apply=True)
    path = tmp_path / "tags.mmp"
    song.save(path, compressed=False)
    loaded = LMMSProject(); loaded.load(path)
    tags = [p.tag for p in instrument_clips(find_tracks(loaded.root)[0])]
    # Instrument-track clips use MXM's saved representation. The integration
    # test below proves MXM actually loads and renders this tag.
    assert tags == ["midiclip"]
```

### J5. Doubles / compress

```python
def test_mmp_and_mmpz_are_musically_identical(song, tmp_path):
    place_phrase(song, 0, PHRASE_A, repeat=3, spacing_bars=1)
    song.save(tmp_path / "a.mmp", compressed=False)
    song.save(tmp_path / "a.mmpz", compressed=True)
    p1, p2 = LMMSProject(), LMMSProject()
    p1.load(tmp_path / "a.mmp"); p2.load(tmp_path / "a.mmpz")
    assert note_tuples(p1.root, 0) == note_tuples(p2.root, 0)
```

---

## 14. Suite K — Property and invariance tests (`TestInvariants`)

No new dependency. Use `random.Random(20261102)` (seeded, deterministic).

```python
@pytest.mark.parametrize("seed", range(20))
def test_transpose_inverse(seed):
    rng = random.Random(20261102 + seed)
    notes = [{"key": rng.randint(12, 115), "pos": rng.randint(0, 768),
              "length": rng.randint(1, 96), "volume": rng.randint(0, 200)}
             for _ in range(20)]
    n = rng.randint(1, 12)
    out = transpose_notes(transpose_notes(notes, n), -n)
    assert out == notes

@pytest.mark.parametrize("seed", range(20))
def test_reverse_involution(seed):
    rng = random.Random(20261102 + seed)
    span = 768
    notes = [{"key": rng.randint(0, 127), "pos": rng.randint(0, span - 1),
              "length": 1}
             for _ in range(20)]
    notes = [n for n in notes if n["pos"] + n["length"] <= span]
    assert reverse_notes(reverse_notes(notes, span), span) == notes

@pytest.mark.parametrize("seed", range(20))
def test_stretch_one_is_identity(seed):
    rng = random.Random(20261102 + seed)
    notes = [{"key": rng.randint(0, 127), "pos": rng.randint(0, 400),
              "length": rng.randint(1, 48), "volume": 100} for _ in range(20)]
    assert stretch_notes(notes, 1.0) == notes

@pytest.mark.parametrize("seed", range(20))
def test_quantize_idempotent(seed):
    rng = random.Random(20261102 + seed)
    notes = [{"key": 60, "pos": rng.randint(0, 768),
              "length": rng.randint(1, 48)} for _ in range(20)]
    once = quantize_notes(notes, grid_ticks=12)
    assert quantize_notes(once, grid_ticks=12) == once
    assert all(n["pos"] % 12 == 0 for n in once)

@pytest.mark.parametrize("seed", range(10))
def test_every_placed_note_is_inside_its_clip(song, seed):
    rng = random.Random(20261102 + seed)
    for _ in range(10):
        place_phrase(song, 0, PHRASE_A, at_bars=rng.randint(0, 8),
                     transforms={0: {"stretch": rng.choice([0.5, 1.0, 2.0])}},
                     apply=True)
    assert_all_notes_inside_clips(song.root)

def test_note_count_is_conserved(song):
    before = count_notes(song.root)
    plan = place_phrase(song, 0, PHRASE_A, repeat=4, spacing_bars=1,
                        apply=True)["plan"]
    assert count_notes(song.root) == before + 4 * len(PHRASE_A["notes"])

def test_determinism_across_projects():
    plans = []
    for _ in range(3):
        p = LMMSProject(); p.new(bpm=120)
        p.add_track("instrument", "Bass", instrument="tripleoscillator")
        plans.append(place_phrase(p, 0, PHRASE_A, repeat=3,
                                  spacing_bars=1, apply=False)["plan"])
    assert plans[0] == plans[1] == plans[2]
```

---

## 15. Suite L — End-to-end acceptance (`@pytest.mark.integration`)

One test that mirrors the issue's acceptance criterion: a four-bar bass phrase and a
chord progression, repeated across a multi-section song, selected occurrences
transposed, automation added, saved, reloaded, and rendered if MXM is installed.

```python
@pytest.mark.integration
def test_issue11_acceptance_song(tmp_path):
    exe = lmms_app.find_mxm_binary()
    project = LMMSProject(); project.new(bpm=128)
    project.add_track("instrument", "Bass", instrument="lb302")
    project.add_track("instrument", "Keys", instrument="tripleoscillator")
    project.add_track("pattern", "Drums")

    # 4-bar bass phrase = 4 x PHRASE_A
    phrase = {"name": "bass-4bar", "notes": [
        {**n, "pos": n["pos"] + 192 * bar}
        for bar in range(4) for n in PHRASE_A["notes"]
    ]}
    place_phrase(project, 0, phrase, at_bars=0, repeat=3, spacing_bars=4,
                 transforms={1: {"transpose": 5},
                             2: {"transpose": -2, "velocity_scale": 0.8}},
                 apply=True)
    place_phrase(project, 0, phrase, at_bars=16, repeat=2, spacing_bars=4,
                 transforms={1: {"transpose": 7, "reverse": True}},
                 apply=True)
    place_progression(project, 1, PROGRESSION_I_VI_IV_V, at_bars=0,
                      repeat=6, spacing_bars=4, apply=True)
    place_rhythm(project, 2, RHYTHM_KICK, at_bars=0, repeat=8,
                 spacing_bars=4, target="bb", apply=True)
    add_automation_batch(project, [
        {"target_type": "song", "param": "tempo",
         "points": [{"bar": 0, "value": 128}, {"bar": 24, "value": 140}]},
        {"target_type": "track", "target_index": 0, "param": "volume",
         "points": [{"bar": 0, "value": 100}, {"bar": 16, "value": 80}]},
    ])

    path = tmp_path / "issue11.mmp"
    project.save(path, compressed=False)
    loaded = LMMSProject(); loaded.load(path)
    assert note_tuples(loaded.root, 0) == note_tuples(project.root, 0)
    assert get_arrangement(loaded.root) == get_arrangement(project.root)
    assert loaded.root.find(".//automationpattern/time") is not None

    if exe is None:
        pytest.skip("MXM binary is not installed")
    out = tmp_path / "issue11.wav"
    proc = subprocess.run([str(exe), "render", str(path), "-o", str(out)],
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert audio_peak(out) > 0
```

---

## 16. Edge-case matrix (must all have a test)

| Area | Case | Expected |
|---|---|---|
| Pitch | `Cb4`, `B#3`, `E#4`, `Fb4` | correct octave (A2) |
| Pitch | `C##4`, `Cbb4` | `INVALID_NOTE_NAME` |
| Pitch | `128`, `-1`, `True`, `60.0`, `"60"` | typed error |
| Pitch | `C` (no octave) | `AMBIGUOUS_PITCH` |
| Time | `1/384` bar | rounds to 1 tick |
| Time | non-4/4 + bars | `UNSUPPORTED_TIME_SIGNATURE` |
| Chord | bare `C` / `C4` | `AMBIGUOUS_CHORD` |
| Chord | `C4M`, `C4M7` | `UNKNOWN_CHORD_QUALITY` |
| Chord | inversion 3 on triad | `INVALID_INVERSION` |
| Chord | `drop3` pushes below key 0 | `PITCH_OUT_OF_RANGE` |
| Chord | slash bass below root | accepted, bass prepended |
| Phrase | note past declared length | `CLIP_TOO_SHORT` |
| Phrase | rest overlaps note | `OVERLAP_CONFLICT` |
| Phrase | same-pitch overlap | accepted + warning |
| Transform | transpose past 127 | `PITCH_OUT_OF_RANGE` |
| Transform | shift below 0 | `NEGATIVE_TIME` |
| Transform | stretch 0 / negative | `INVALID_RATIO` |
| Transform | quantize 0 | `INVALID_GRID` |
| Transform | velocity over 200 | clamped |
| Rhythm | step == steps | `INVALID_STEP` |
| Rhythm | duplicate hit | `DUPLICATE_HIT` |
| Rhythm | unknown drum | `UNKNOWN_DRUM` |
| Arrangement | track `-1` | `TRACK_INDEX_OUT_OF_RANGE` |
| Arrangement | notes on sample track | `WRONG_TRACK_TYPE` |
| Arrangement | repeat 0 | error |
| Arrangement | spacing 0 with repeat > 1 | error |
| Arrangement | per-occurrence transform on BB | `UNSUPPORTED_TRANSFORMATION` |
| Arrangement | clip shorter than notes, `fit="error"` | `CLIP_TOO_SHORT` |
| Arrangement | clip shorter, `fit="truncate"` | note trimmed |
| Automation | duplicate tick, `dedupe="last"` | last wins |
| Automation | duplicate tick, `dedupe="error"` | `DUPLICATE_AUTOMATION_POINT` |
| Automation | tempo 5000, clamp policy | 999 |
| Automation | tempo 5000, error policy | `AUTOMATION_VALUE_OUT_OF_RANGE` |
| Automation | mixer 99 | `UNKNOWN_AUTOMATION_TARGET` |
| Preview | `apply=False` | XML byte-identical, `modified` unchanged |
| Preview | same call twice | identical plan |
| Apply | preview plan vs applied plan | deep equal |
| Roundtrip | `.mmp` / `.mmpz` | identical note tuples |
| Roundtrip | automation object id | resolves after load |

---

## 17. Acceptance criteria → tests

| Issue acceptance criterion | Tests |
|---|---|
| Four-bar bass phrase + chord progression | `test_issue11_acceptance_song`, `test_place_phrase_exact`, `test_place_progression_sequential` |
| Repeat/place across a multi-section song | `test_repeat_with_per_occurrence_transforms`, `test_arrangement_reflects_all_units` |
| Explicitly transpose selected occurrences | `test_repeat_with_per_occurrence_transforms`, `test_transpose_exact` |
| Add automation | `TestAutomationOps` (H1–H4) |
| Batch placement across tracks/sections | §G7 (`arrange_units`, `place_chords`) |
| Selected-occurrence / filtered edits | §G8 (`edit_notes`) |
| Exact output | All `*_exact` tests; chord/pitch tables |
| Preview/apply parity | `TestParity` (I1–I4) |
| Transformations | `TestTransforms` (E1–E7) |
| Boundaries | §16 matrix, `test_no_note_outside_its_clip`, `test_clip_extends_to_contain_notes` |
| MXM project round-trips | `test_composition_roundtrip.py` (J1–J5) |
| Reject underspecified/ambiguous | §4 A3, §6 C6, §7 D1/D2, §9 F3, §10 G5 |

---

## 18. Implementation gaps this spec deliberately pins

These are real defects/ambiguities that tests must capture. Items marked **fixed**
are already handled in `musical_units.py`; the test still exists as a regression.

1. **FIXED — numeric chord qualities.** `CHORD_REGEX` now uses a one-digit root
   octave, so `C47`, `C413`, `C47sus4` parse. Kept as regression:
   `test_chord_quality_table`, `test_chord_roots`,
   `test_numeric_quality_after_octave`.
2. **Slash bass is not guaranteed to be the lowest voice** when the bass is not a
   chord tone and is above the chord (`C4maj/B4` → `[60,64,67,71]`,
   `C4maj/F#4` → `[60,64,66,67]`). Tests: `test_slash_bass_is_lowest`.
3. **`parse_chord_spec` does not validate `volume`/`panning`**; invalid values
   reach the note dicts. Tests: `test_chord_spec_velocity_panning`.
4. **`parse_rhythm_pattern` does not validate `velocity`, `panning`, or
   `length_steps`**; `length_steps: 0` creates zero-length notes and
   `velocity: 999` escapes. Tests: `test_rhythm_velocity_panning_length`.
5. **`parse_phrase` silently prefers `key` when both `key` and `pitch` are given**;
   ambiguous input should be rejected. Test: `test_phrase_key_and_pitch_rejected`.
6. **`apply_transforms` reverse span defaults to the current note end**, not the
   declared phrase length; leading rests shorten the mirror. Tests pass
   `span_ticks` explicitly and assert the default warning. Test: `test_reverse_span_default`.
7. **`parse_time` accepts integral floats for ticks** (`1.0` → 1). This is
   documented; tests pin it and reject `1.5`.
8. **Legacy negative indices** are still accepted by `add_note_to_track`
   (`track_index=-1` writes to the last track). The placement layer must reject
   them. Suite G5.
9. **Dual clip tags (partly fixed)**: `_create_midiclip` now writes `<midiclip>`
   for every new instrument clip. Legacy `place_instrument_pattern` still writes
   `<pattern>`, and `_select_pattern` prefers `pattern` regardless of timeline
   position. Legacy readers must handle both; new placements must stay
   `<midiclip>`. Suite J4, G4.
10. **No note-boundary validation** in `add_note`: negative `pos`, `length=0`,
    `key=999`, `volume=-5`. The new layer must validate. Suite G6.
11. **The legacy `add_automation` tool cannot edit** and does not clamp values or
    dedupe duplicate ticks. The new `edit_automation` exists but is incomplete
    (§18.16). Suite H2–H3.
12. **Preview mode is implemented** (`apply=False`) and passes for phrases,
    chords, progressions, rhythms, and note edits. Automation preview is impure
    (§18.19). Suite I.
13. **`get_xml_string()` mutates the tree** via `ET.indent()`, so preview-purity
    tests must snapshot with `ET.tostring()` (documented in `conftest.py`).
14. **FIXED — tiny negative bar values.** `parse_time` now checks the sign before
    rounding. Kept as regression: `test_time_rejection` (`-0.001 bars`).
15. **`place_chord` discards chord-level `volume`/`panning`**, hardcoding 100/0 in
    `composition.py`. Test: `test_place_chord_velocity_panning`.
16. **`edit_automation` builds `points = []` instead of loading the curve**, so
    `ops=None` or `[{"time_shift_ticks": 0}]` clears the automation. It also
    ignores `{"scale": ...}`, `{"offset": ...}`, and `{"clamp": ...}` even though
    the issue requires scaling, shifting, and value clamping. Tests:
    `test_edit_automation_preserves_unmentioned_points`,
    `test_edit_automation_no_ops_is_noop`, `test_automation_value_transforms`.
17. **`edit_notes` / `batch_transform` `filter_time_range` is clip-relative**, not
    song-absolute. Document the unit or convert at the boundary; test with a clip
    placed at bar 4 and a filter that should match only the placed notes.
18. **FIXED — `xml_parser.add_automation_track` empty points.** The old test and
    the new empty-track behavior are reconciled, so the suite is green apart from
    the pre-existing VST3 test.
19. **FIXED — `add_automation_batch(apply=False)` mutated the project.** The
    preview path now resolves the target on a copy, and Suite I passes. Kept as
    regression: `test_preview_does_not_mutate[automation]`.
20. **FIXED — import drift while the layer was edited**: `composition.py`
    imported `transform_notes` after the pure module renamed it to
    `apply_transforms` / `transpose_notes`. Keep the guard: a test run begins with
    `uv run python -c "import mxm_mcp.composition"` so collection failures are
    reported as build breaks, not as musical failures.

### 18.1 Concrete reproduction commands

```bash
uv run python -c "from mxm_mcp.musical_units import *; print(chord_pitches(parse_chord_symbol('C47')))"
# [60, 64, 67, 70]

uv run python -c "from mxm_mcp.musical_units import *; print(chord_pitches(parse_chord_symbol('C4maj/B4')))"
# [60, 64, 67, 71]  (bass B is not lowest; expected B below C)

uv run python -c "from mxm_mcp.musical_units import *; print(parse_phrase({'notes':[{'key':60,'pitch':'D4','pos':0,'length':48}]}).notes)"
# silently uses key 60 (expected rejection or documented precedence)
```

---

## 19. Definition of done

- All suites A–K pass in under 25 s on a machine without plugins.
- Suite L is skipped cleanly when MXM is absent and passes when present.
- The pre-existing VST3 failure remains the only failing test in the repository
  (or is fixed separately, not by Issue #11 tests).
- Coverage of `musical_units.py` ≥ 95 % line and branch; `composition.py` ≥ 90 %.
- No test reaches outside the workspace; all writes use `tmp_path`.
- No test asserts an implementation-private attribute (`_modified` is allowed;
  other `_` names are not).

---

## 20. Rollout order

1. **Land `tests/conftest.py`** with the fixtures and helpers from §2 (no feature
   dependency beyond `xml_parser` and `LMMSProject`).
2. **Land Suites A–F** in `tests/test_musical_units.py`. Eight chord/time cases fail
   until the two parser fixes in §18 land; all other cases should be green.
3. **Fix the chord regex** (one-digit root octave) and the negative-time sign check.
   Confirm all of A–F is green before writing the placement layer.
4. **Land `composition.py` + MCP tools**, then Suites G–I.
5. **Land Suites J–K** (round-trip, invariants).
6. **Land Suite L** behind `@pytest.mark.integration`; it must skip cleanly without
   MXM and must not be the only evidence that the project is editable.

Materializing tests from this document should not require changing assertions,
only imports. If an implementation detail legitimately differs (for example a
different module name), change it in the target-import block in §1 and in
`tests/conftest.py`.
