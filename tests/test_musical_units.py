"""Tests for mxm_mcp.musical_units pure parsing, validation, and transformations."""

from __future__ import annotations

import random
import pytest

from mxm_mcp.musical_units import (
    CHORD_ALIASES,
    CHORD_QUALITIES,
    DRUM_MAP,
    MusicalUnitError,
    Phrase,
    apply_transforms,
    bars_to_ticks,
    chord_names,
    chord_pitches,
    midi_to_note_name,
    note_name_to_midi,
    parse_chord_spec,
    parse_chord_symbol,
    parse_duration,
    parse_phrase,
    parse_pitch,
    parse_progression,
    parse_rhythm_pattern,
    parse_time,
    quantize_notes,
    reverse_automation_points,
    reverse_notes,
    scale_velocity,
    shift_notes,
    stretch_notes,
    ticks_to_bars,
    transpose_notes,
)
from conftest import OFF_GRID, PHRASE_A, PROGRESSION_I_VI_IV_V, RHYTHM_KICK


# =====================================================================
# SUITE A: PITCH PARSING
# =====================================================================

class TestPitchParsing:
    @pytest.mark.parametrize("name,expected", [
        ("C-1", 0), ("C0", 12), ("C4", 60), ("C5", 72), ("G9", 127),
        ("A4", 69), ("B3", 59), ("B4", 71),
        ("C#4", 61), ("D#4", 63), ("F#4", 66), ("G#4", 68), ("A#4", 70),
        ("Db4", 61), ("Eb4", 63), ("Gb4", 66), ("Ab4", 68), ("Bb4", 70),
        ("Bb2", 46), ("Eb2", 39), ("Ab2", 44),
        ("C\u266f4", 61), ("B\u266d2", 46),
        ("c4", 60), ("f#3", 54), ("bb2", 46),
    ])
    def test_note_name_to_midi_exact(self, name, expected):
        assert parse_pitch(name) == expected
        assert note_name_to_midi(name) == expected

    @pytest.mark.parametrize("name,expected", [
        ("Cb4", 59),   # B3
        ("Cb5", 71),   # B4
        ("B#3", 60),   # C4
        ("B#4", 72),   # C5
        ("Fb4", 64),   # E4
        ("E#4", 65),   # F4
        ("E#3", 53),   # F3
        ("Cb-1", None),  # out of range (-1)
    ])
    def test_cross_octave_enharmonics(self, name, expected):
        if expected is None:
            with pytest.raises(MusicalUnitError) as exc:
                parse_pitch(name)
            assert exc.value.code == "PITCH_OUT_OF_RANGE"
        else:
            assert parse_pitch(name) == expected

    @pytest.mark.parametrize("value,code", [
        ("C-2", "PITCH_OUT_OF_RANGE"),
        ("G#9", "PITCH_OUT_OF_RANGE"),   # 128
        ("B9",  "PITCH_OUT_OF_RANGE"),   # 131
        (128,   "PITCH_OUT_OF_RANGE"),
        (-1,    "PITCH_OUT_OF_RANGE"),
    ])
    def test_pitch_out_of_range(self, value, code):
        with pytest.raises(MusicalUnitError) as exc:
            parse_pitch(value)
        assert exc.value.code == code

    @pytest.mark.parametrize("value,code", [
        ("C",   "AMBIGUOUS_PITCH"),      # no octave
        ("",    "INVALID_NOTE_NAME"),
        ("H4",  "INVALID_NOTE_NAME"),
        ("C#",  "AMBIGUOUS_PITCH"),
        ("C4#", "INVALID_NOTE_NAME"),
        ("C 4", "INVALID_NOTE_NAME"),
        ("C4.5","INVALID_NOTE_NAME"),
        ("C##4","INVALID_NOTE_NAME"),
        ("Cbb4","INVALID_NOTE_NAME"),
        ("C#b4","INVALID_NOTE_NAME"),
        ("60",  "INVALID_PITCH"),
        (60.0,  "INVALID_PITCH"),
        (True,  "INVALID_PITCH"),
        (None,  "INVALID_PITCH"),
    ])
    def test_invalid_pitch_input(self, value, code):
        with pytest.raises(MusicalUnitError) as exc:
            parse_pitch(value)
        assert exc.value.code == code

    @pytest.mark.parametrize("key", [-1, 128, 1000])
    def test_midi_to_note_name_rejects_out_of_range(self, key):
        with pytest.raises(MusicalUnitError) as exc:
            midi_to_note_name(key)
        assert exc.value.code == "PITCH_OUT_OF_RANGE"

    def test_roundtrip_all_128_keys(self):
        for key in range(128):
            assert parse_pitch(midi_to_note_name(key)) == key

    def test_spelling_controls_names_not_keys(self):
        spec = parse_chord_symbol("C4min")
        assert chord_pitches(spec) == [60, 63, 67]
        assert chord_names(spec, spelling="sharps") == ["C4", "D#4", "G4"]
        assert chord_names(spec, spelling="flats") == ["C4", "Eb4", "G4"]


# =====================================================================
# SUITE B: TIMEBASE
# =====================================================================

class TestTimeParsing:
    @pytest.mark.parametrize("bars,ticks", [
        (0, 0), (0.25, 48), (0.5, 96), (1, 192), (1.5, 288),
        (1.75, 336), (4, 768), (8.125, 1560), (16, 3072),
    ])
    def test_bars_to_ticks_exact(self, bars, ticks):
        assert parse_time(bars, unit="bars") == ticks
        assert bars_to_ticks(bars) == ticks

    @pytest.mark.parametrize("beats,ticks", [
        (0, 0), (0.25, 12), (0.5, 24), (1, 48), (2, 96), (4, 192), (7.5, 360),
    ])
    def test_beats_to_ticks_exact(self, beats, ticks):
        assert parse_time(beats, unit="beats") == ticks

    @pytest.mark.parametrize("steps,ticks", [(1, 12), (4, 48), (16, 192), (32, 384)])
    def test_steps_to_ticks_exact(self, steps, ticks):
        assert parse_time(steps, unit="steps") == ticks

    @pytest.mark.parametrize("bars,ticks", [
        (1/384, 1),    # 0.5 tick -> 1
        (5/384, 3),    # 2.5 ticks -> 3
        (7/384, 4),    # 3.5 ticks -> 4
        (3/384, 2),    # 1.5 ticks -> 2
    ])
    def test_round_half_away_from_zero(self, bars, ticks):
        assert parse_time(bars, unit="bars") == ticks
        assert bars_to_ticks(bars) == ticks

    @pytest.mark.parametrize("value,unit,code", [
        (-1, "ticks", "NEGATIVE_TIME"),
        (-0.001, "bars", "NEGATIVE_TIME"),
        (1.5, "ticks", "INVALID_TIME"),
        ("192", "ticks", "INVALID_TIME"),
        (float("nan"), "bars", "INVALID_TIME"),
        (float("inf"), "bars", "INVALID_TIME"),
        (True, "ticks", "INVALID_TIME"),
    ])
    def test_time_rejection(self, value, unit, code):
        with pytest.raises(MusicalUnitError) as exc:
            parse_time(value, unit=unit)
        assert exc.value.code == code

    @pytest.mark.parametrize("token,ticks", [
        ("1/1", 192), ("1/2", 96), ("1/4", 48), ("1/8", 24), ("1/16", 12),
        ("1/32", 6), ("1/8T", 16), ("1/16T", 8), ("1/4.", 72), ("1/8.", 36),
    ])
    def test_duration_tokens(self, token, ticks):
        assert parse_duration(token) == ticks

    @pytest.mark.parametrize("bad_token", ["1/3", "1/0", "0/4", "4/4", "1/16TT"])
    def test_invalid_duration_tokens(self, bad_token):
        with pytest.raises(MusicalUnitError) as exc:
            parse_duration(bad_token)
        assert exc.value.code == "INVALID_TIME"


# =====================================================================
# SUITE C: CHORDS
# =====================================================================

class TestChordSymbols:
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
    def test_chord_quality_table(self, quality, expected):
        assert chord_pitches(parse_chord_symbol(f"C4{quality}")) == expected

    @pytest.mark.parametrize("symbol,expected", [
        ("C4maj",   [60, 64, 67]),
        ("F#3min",  [54, 57, 61]),
        ("Bb2maj7", [46, 50, 53, 57]),
        ("Db5dim",  [73, 76, 79]),
        ("B3sus4",  [59, 64, 66]),
        ("Cb4maj",  [59, 63, 66]),  # B3 major
        ("B#3maj",  [60, 64, 67]),  # C4 major
    ])
    def test_chord_roots(self, symbol, expected):
        assert chord_pitches(parse_chord_symbol(symbol)) == expected

    @pytest.mark.parametrize("symbol,expected", [
        ("C4maj",     [60, 64, 67]),
        ("C4maj/E4",  [64, 67, 72]),
        ("C4maj/G4",  [67, 72, 76]),
        ("C4maj/D3",  [50, 60, 64, 67]),
        ("C4maj7/E4", [64, 67, 71, 72]),
        ("C4maj7/B4", [71, 72, 76, 79]),
    ])
    def test_slash_and_inversion(self, symbol, expected):
        assert chord_pitches(parse_chord_symbol(symbol)) == expected

    @pytest.mark.parametrize("inversion,expected", [
        (0, [60, 64, 67]),
        (1, [64, 67, 72]),
        (2, [67, 72, 76]),
    ])
    def test_explicit_inversion_parameter(self, inversion, expected):
        assert chord_pitches(parse_chord_symbol("C4maj", inversion=inversion)) == expected

    @pytest.mark.parametrize("voicing,expected", [
        ("close", [60, 64, 67, 71]),
        ("drop2", [55, 60, 64, 71]),   # 67 -> 55
        ("drop3", [52, 60, 67, 71]),   # 64 -> 52
    ])
    def test_voicing_definitions(self, voicing, expected):
        assert chord_pitches(parse_chord_symbol("C4maj7", voicing=voicing)) == expected

    def test_octave_shift(self):
        assert chord_pitches(parse_chord_symbol("C4maj", octave_shift=1)) == [72, 76, 79]
        assert chord_pitches(parse_chord_symbol("C4maj", octave_shift=-1)) == [48, 52, 55]

    def test_symbol_parsing_is_deterministic(self):
        a = parse_chord_symbol("F#3min7").to_dict()
        for _ in range(50):
            assert parse_chord_symbol("F#3min7").to_dict() == a

    @pytest.mark.parametrize("alias,canonical", [
        ("C4m", "C4min"), ("C4min", "C4min"), ("C4minor", "C4min"),
        ("C4maj", "C4maj"), ("C4major", "C4maj"),
        ("C4min7", "C4min7"), ("C4m7", "C4min7"),
    ])
    def test_documented_aliases_normalize(self, alias, canonical):
        assert parse_chord_symbol(alias).canonical() == canonical

    @pytest.mark.parametrize("symbol,code", [
        ("C",        "AMBIGUOUS_CHORD"),
        ("Cmaj",     "INVALID_CHORD_SYMBOL"),  # root octave required
        ("C4",       "AMBIGUOUS_CHORD"),
        ("C4M",      "UNKNOWN_CHORD_QUALITY"),
        ("C4M7",     "UNKNOWN_CHORD_QUALITY"),
        ("C4major7", "UNKNOWN_CHORD_QUALITY"),
        ("C4min7b5", "UNKNOWN_CHORD_QUALITY"),
        ("C4maj9#11","UNKNOWN_CHORD_QUALITY"),
        ("C4maj7/",  "INVALID_CHORD_SYMBOL"),
        ("C4maj7/E", "INVALID_CHORD_SYMBOL"),
        ("",         "INVALID_CHORD_SYMBOL"),
        ("H4maj",    "INVALID_CHORD_SYMBOL"),
        ("C4maj7/X9","INVALID_CHORD_SYMBOL"),
    ])
    def test_chord_rejection(self, symbol, code):
        with pytest.raises(MusicalUnitError) as exc:
            parse_chord_symbol(symbol)
        assert exc.value.code == code

    @pytest.mark.parametrize("inversion", [-1, 3, 99])
    def test_invalid_inversion(self, inversion):
        with pytest.raises(MusicalUnitError) as exc:
            parse_chord_symbol("C4maj", inversion=inversion)
        assert exc.value.code == "INVALID_INVERSION"

    def test_voicing_out_of_range_rejected(self):
        assert chord_pitches(parse_chord_symbol("C4maj", voicing="drop2")) == [52, 60, 67]
        with pytest.raises(MusicalUnitError) as exc:
            chord_pitches(parse_chord_symbol("C4maj7", voicing="drop3", octave_shift=-5))
        assert exc.value.code == "PITCH_OUT_OF_RANGE"

    def test_explicit_pitches_are_not_inferred(self):
        spec = parse_chord_spec({"pitches": ["C4", "E4", "G4"], "at": 0, "length": 48})
        assert [n["key"] for n in spec.notes] == [60, 64, 67]

    def test_explicit_pitches_preserve_order_and_duplicates(self):
        spec = parse_chord_spec({"pitches": [67, 60, 64], "at": 0, "length": 48})
        assert [n["key"] for n in spec.notes] == [60, 64, 67]

    def test_symbol_and_pitches_together_rejected(self):
        with pytest.raises(MusicalUnitError) as exc:
            parse_chord_spec({"symbol": "C4maj", "pitches": [60, 64, 67], "at": 0, "length": 48})
        assert exc.value.code == "INVALID_CHORD_SYMBOL"


# =====================================================================
# SUITE D: PHRASE PARSING
# =====================================================================

class TestPhraseParsing:
    def test_phrase_notes_keep_relative_offsets(self):
        phrase = parse_phrase(PHRASE_A)
        assert phrase.length_ticks == 192
        assert [(n.key, n.pos, n.length, n.volume) for n in phrase.notes] == [
            (36, 0, 48, 100), (39, 48, 48, 80), (43, 96, 48, 120), (36, 144, 48, 100),
        ]

    def test_phrase_length_inferred_from_notes(self):
        phrase = parse_phrase({"notes": [{"key": 60, "pos": 24, "length": 48}]})
        assert phrase.length_ticks == 72

    def test_phrase_rejects_note_past_declared_length(self):
        with pytest.raises(MusicalUnitError) as exc:
            parse_phrase({"length_ticks": 96, "notes": [{"key": 60, "pos": 72, "length": 48}]})
        assert exc.value.code == "CLIP_TOO_SHORT"

    def test_empty_phrase_rejected(self):
        with pytest.raises(MusicalUnitError) as exc:
            parse_phrase({"notes": []})
        assert exc.value.code == "EMPTY_UNIT"

    def test_phrase_missing_required_fields(self):
        for bad in ({"pos": 0, "length": 48}, {"key": 60, "length": 48}, {"key": 60, "pos": 0}):
            with pytest.raises(MusicalUnitError):
                parse_phrase({"notes": [bad]})

    def test_explicit_rests_are_timing_only(self):
        phrase = parse_phrase({
            "notes": [{"key": 60, "pos": 0, "length": 48}, {"key": 62, "pos": 96, "length": 48}],
            "rests": [{"at": 48, "length": 48}],
        })
        assert len(phrase.notes) == 2
        assert phrase.rests == [(48, 48)]

    def test_rest_overlapping_note_rejected(self):
        with pytest.raises(MusicalUnitError) as exc:
            parse_phrase({
                "notes": [{"key": 60, "pos": 0, "length": 96}],
                "rests": [{"at": 48, "length": 48}],
            })
        assert exc.value.code == "OVERLAP_CONFLICT"

    def test_same_pitch_overlap_is_flagged_not_rejected(self):
        phrase = parse_phrase({"notes": [
            {"key": 60, "pos": 0,  "length": 96},
            {"key": 60, "pos": 48, "length": 96},
        ]})
        assert len(phrase.notes) == 2
        assert any("same-pitch overlap" in w for w in phrase.warnings)

    def test_phrase_notes_are_sorted_by_pos_key_length(self):
        phrase = parse_phrase({"notes": [
            {"key": 67, "pos": 48, "length": 24},
            {"key": 60, "pos": 0,  "length": 48},
            {"key": 64, "pos": 48, "length": 24},
            {"key": 62, "pos": 48, "length": 48},
        ]})
        assert [(n.pos, n.key, n.length) for n in phrase.notes] == [
            (0, 60, 48), (48, 62, 48), (48, 64, 24), (48, 67, 24),
        ]

    @pytest.mark.parametrize("volume", [-1, 201, "100", True])
    def test_invalid_velocity(self, volume):
        with pytest.raises(MusicalUnitError) as exc:
            parse_phrase({"notes": [{"key": 60, "pos": 0, "length": 48, "volume": volume}]})
        assert exc.value.code == "INVALID_VELOCITY"

    @pytest.mark.parametrize("panning", [-101, 101])
    def test_invalid_panning(self, panning):
        with pytest.raises(MusicalUnitError) as exc:
            parse_phrase({"notes": [{"key": 60, "pos": 0, "length": 48, "panning": panning}]})
        assert exc.value.code == "INVALID_PANNING"


# =====================================================================
# SUITE E: TRANSFORMS
# =====================================================================

class TestTransforms:
    def test_transpose_exact(self):
        out = transpose_notes(PHRASE_A["notes"], 2)
        assert [n["key"] for n in out] == [38, 41, 45, 38]

    def test_transpose_octaves(self):
        assert [n["key"] for n in transpose_notes(PHRASE_A["notes"], 12)] == [48, 51, 55, 48]
        assert [n["key"] for n in transpose_notes(PHRASE_A["notes"], -12)] == [24, 27, 31, 24]

    def test_transpose_out_of_range_rejected(self):
        with pytest.raises(MusicalUnitError) as exc:
            transpose_notes([{"key": 120, "pos": 0, "length": 48}], 12)
        assert exc.value.code == "PITCH_OUT_OF_RANGE"

    def test_transpose_does_not_touch_time_or_velocity(self):
        out = transpose_notes(PHRASE_A["notes"], 5)
        assert [(n["pos"], n["length"], n["volume"]) for n in out] == [
            (n["pos"], n["length"], n["volume"]) for n in PHRASE_A["notes"]
        ]

    def test_time_shift_exact(self):
        out = shift_notes(PHRASE_A["notes"], 96)
        assert [n["pos"] for n in out] == [96, 144, 192, 240]
        assert all(n["length"] == 48 for n in out)

    def test_time_shift_negative_rejected(self):
        with pytest.raises(MusicalUnitError) as exc:
            shift_notes(PHRASE_A["notes"], -1)
        assert exc.value.code == "NEGATIVE_TIME"

    @pytest.mark.parametrize("ratio,expected", [
        (2.0, [(0, 96), (96, 96), (192, 96), (288, 96)]),
        (0.5, [(0, 24), (24, 24), (48, 24), (72, 24)]),
        (1.5, [(0, 72), (72, 72), (144, 72), (216, 72)]),
        (1.0, [(0, 48), (48, 48), (96, 48), (144, 48)]),
    ])
    def test_stretch_exact(self, ratio, expected):
        out = stretch_notes(PHRASE_A["notes"], ratio)
        assert [(n["pos"], n["length"]) for n in out] == expected

    @pytest.mark.parametrize("ratio", [0, -1, float("nan"), float("inf")])
    def test_invalid_stretch_ratio(self, ratio):
        with pytest.raises(MusicalUnitError) as exc:
            stretch_notes(PHRASE_A["notes"], ratio)
        assert exc.value.code == "INVALID_RATIO"

    def test_stretch_minimum_length_is_one_tick(self):
        out = stretch_notes([{"key": 60, "pos": 0, "length": 1}], 0.1)
        assert out[0]["length"] == 1

    def test_reverse_exact(self):
        out = reverse_notes(PHRASE_A["notes"], span_ticks=192)
        assert [(n["key"], n["pos"], n["length"], n["volume"]) for n in out] == [
            (36, 0, 48, 100),
            (43, 48, 48, 120),
            (39, 96, 48, 80),
            (36, 144, 48, 100),
        ]

    def test_reverse_twice_is_identity(self):
        phrase_notes = parse_phrase(PHRASE_A).notes_as_dicts()
        once = reverse_notes(phrase_notes, span_ticks=192)
        twice = reverse_notes(once, span_ticks=192)
        assert twice == phrase_notes

    def test_reverse_rejects_note_outside_span(self):
        with pytest.raises(MusicalUnitError) as exc:
            reverse_notes([{"key": 60, "pos": 180, "length": 48}], span_ticks=192)
        assert exc.value.code == "NEGATIVE_TIME"

    @pytest.mark.parametrize("grid,expected", [
        (12, [0, 24, 36, 96]),
        (24, [0, 24, 24, 96]),
        (48, [0, 0, 48, 96]),
    ])
    def test_quantize_start(self, grid, expected):
        out = quantize_notes(OFF_GRID["notes"], grid_ticks=grid)
        assert [n["pos"] for n in out] == expected

    @pytest.mark.parametrize("pos,expected", [
        (5, 0), (6, 12), (7, 12), (17, 12), (18, 24), (19, 24), (30, 36), (100, 96),
    ])
    def test_quantize_ties_round_up(self, pos, expected):
        out = quantize_notes([{"key": 60, "pos": pos, "length": 10}], grid_ticks=12)
        assert out[0]["pos"] == expected

    def test_quantize_start_and_length(self):
        out = quantize_notes([{"key": 60, "pos": 5, "length": 10}], grid_ticks=12, mode="start+length")
        assert (out[0]["pos"], out[0]["length"]) == (0, 12)

    def test_quantize_end_mode(self):
        out = quantize_notes([{"key": 60, "pos": 5, "length": 10}], grid_ticks=12, mode="end")
        assert (out[0]["pos"], out[0]["length"]) == (5, 7)

    def test_quantize_is_idempotent(self):
        once = quantize_notes(OFF_GRID["notes"], grid_ticks=12)
        assert quantize_notes(once, grid_ticks=12) == once

    @pytest.mark.parametrize("grid", [0, -12, 1.5, True])
    def test_invalid_quantize_grid(self, grid):
        with pytest.raises(MusicalUnitError) as exc:
            quantize_notes(OFF_GRID["notes"], grid_ticks=grid)
        assert exc.value.code == "INVALID_GRID"

    @pytest.mark.parametrize("mult,offset,expected", [
        (1.0, 0,    [100, 80, 120, 100]),
        (0.5, 0,    [50, 40, 60, 50]),
        (1.5, 0,    [150, 120, 180, 150]),
        (1.0, -20,  [80, 60, 100, 80]),
        (1.5, 10,   [160, 130, 190, 160]),
        (2.0, 0,    [200, 160, 200, 200]),
        (1.0, -500, [0, 0, 0, 0]),
    ])
    def test_velocity_transform(self, mult, offset, expected):
        out = scale_velocity(PHRASE_A["notes"], multiplier=mult, offset=offset)
        assert [n["volume"] for n in out] == expected

    def test_velocity_half_rounds_up(self):
        out = scale_velocity([{"key": 60, "pos": 0, "length": 1, "volume": 101}], multiplier=0.5)
        assert out[0]["volume"] == 51

    def test_transform_order_is_fixed(self):
        out = apply_transforms(
            [{"key": 60, "pos": 0, "length": 48, "volume": 100}],
            {"transpose": 12, "time_shift": 48, "stretch": 0.5,
             "quantize": 12, "velocity_scale": 0.5, "velocity_offset": 10},
        )
        assert out == [{"key": 72, "pos": 24, "length": 24, "volume": 60, "panning": 0}]


# =====================================================================
# SUITE F: RHYTHM
# =====================================================================

class TestRhythm:
    @pytest.mark.parametrize("drum,key", DRUM_MAP.items())
    def test_drum_map(self, drum, key):
        pattern = parse_rhythm_pattern({
            "steps": 16,
            "hits": [{"step": 0, "drum": drum}],
        })
        assert pattern.hits[0].key == key

    def test_step_grid_positions(self):
        pattern = parse_rhythm_pattern({
            "steps": 16,
            "hits": [
                {"step": 0,  "drum": "kick", "length_steps": 1},
                {"step": 4,  "drum": "kick"},
                {"step": 8,  "drum": "kick"},
                {"step": 12, "drum": "snare"},
                {"step": 14, "drum": "closed_hat", "velocity": 60, "length_steps": 2},
            ],
        })
        assert [(h.pos, h.key, h.length, h.volume) for h in pattern.hits] == [
            (0, 36, 12, 100), (48, 36, 12, 100), (96, 36, 12, 100),
            (144, 38, 12, 100), (168, 42, 24, 60),
        ]
        assert pattern.length_ticks == 192

    def test_custom_step_ticks(self):
        pattern = parse_rhythm_pattern({"steps": 8, "step_ticks": 24, "hits": [{"step": 3, "drum": "kick"}]})
        assert pattern.hits[0].pos == 72
        assert pattern.length_ticks == 192

    def test_explicit_pitch_overrides_drum(self):
        pattern = parse_rhythm_pattern({"steps": 4, "hits": [{"step": 0, "pitch": "C2"}]})
        assert pattern.hits[0].key == 36

    @pytest.mark.parametrize("payload,code", [
        ({"steps": 16, "hits": []}, "EMPTY_UNIT"),
        ({"steps": 0, "hits": [{"step": 0, "drum": "kick"}]}, "INVALID_STEP"),
        ({"steps": 16, "hits": [{"step": 16, "drum": "kick"}]}, "INVALID_STEP"),
        ({"steps": 16, "hits": [{"step": -1, "drum": "kick"}]}, "INVALID_STEP"),
        ({"steps": 16, "hits": [{"step": 0, "drum": "cowbell_9000"}]}, "UNKNOWN_DRUM"),
        ({"steps": 16, "hits": [{"step": 0, "drum": "kick"}, {"step": 0, "drum": "kick"}]}, "DUPLICATE_HIT"),
        ({"steps": 16, "hits": [{"step": 0}]}, "INVALID_PITCH"),
    ])
    def test_rhythm_rejection(self, payload, code):
        with pytest.raises(MusicalUnitError) as exc:
            parse_rhythm_pattern(payload)
        assert exc.value.code == code


# =====================================================================
# SUITE K: INVARIANTS AND PROPERTY TESTS
# =====================================================================

class TestInvariants:
    @pytest.mark.parametrize("seed", range(10))
    def test_transpose_inverse(self, seed):
        rng = random.Random(20261102 + seed)
        notes = [{"key": rng.randint(12, 115), "pos": rng.randint(0, 768),
                  "length": rng.randint(1, 96), "volume": rng.randint(0, 200)}
                 for _ in range(20)]
        n = rng.randint(1, 12)
        out = transpose_notes(transpose_notes(notes, n), -n)
        assert out == notes

    @pytest.mark.parametrize("seed", range(10))
    def test_reverse_involution(self, seed):
        rng = random.Random(20261102 + seed)
        span = 768
        notes = [{"key": rng.randint(0, 127), "pos": rng.randint(0, span - 1),
                  "length": 1, "volume": 100, "panning": 0}
                 for _ in range(20)]
        notes = [n for n in notes if n["pos"] + n["length"] <= span]
        notes.sort(key=lambda x: (x["pos"], x["key"], x["length"]))
        assert reverse_notes(reverse_notes(notes, span), span) == notes

    @pytest.mark.parametrize("seed", range(10))
    def test_stretch_one_is_identity(self, seed):
        rng = random.Random(20261102 + seed)
        notes = [{"key": rng.randint(0, 127), "pos": rng.randint(0, 400),
                  "length": rng.randint(1, 48), "volume": 100} for _ in range(20)]
        assert stretch_notes(notes, 1.0) == notes

    @pytest.mark.parametrize("seed", range(10))
    def test_quantize_idempotent(self, seed):
        rng = random.Random(20261102 + seed)
        notes = [{"key": 60, "pos": rng.randint(0, 768),
                  "length": rng.randint(1, 48)} for _ in range(20)]
        once = quantize_notes(notes, grid_ticks=12)
        assert quantize_notes(once, grid_ticks=12) == once
        assert all(n["pos"] % 12 == 0 for n in once)


class TestGapRegressions:
    def test_slash_bass_is_lowest(self):
        spec = parse_chord_symbol("C4maj/B4")
        pitches = chord_pitches(spec)
        assert pitches[0] == 71  # B4 is lowest
        assert pitches == [71, 72, 76, 79]

    def test_chord_spec_velocity_panning(self):
        with pytest.raises(MusicalUnitError) as exc:
            parse_chord_spec({"symbol": "C4maj", "volume": 250, "at": 0, "length": 48})
        assert exc.value.code == "INVALID_VELOCITY"

        with pytest.raises(MusicalUnitError) as exc:
            parse_chord_spec({"symbol": "C4maj", "panning": -150, "at": 0, "length": 48})
        assert exc.value.code == "INVALID_PANNING"

    def test_rhythm_velocity_panning_length(self):
        with pytest.raises(MusicalUnitError) as exc:
            parse_rhythm_pattern({"steps": 16, "hits": [{"step": 0, "drum": "kick", "length_steps": 0}]})
        assert exc.value.code == "INVALID_TIME"

        with pytest.raises(MusicalUnitError) as exc:
            parse_rhythm_pattern({"steps": 16, "hits": [{"step": 0, "drum": "kick", "velocity": 999}]})
        assert exc.value.code == "INVALID_VELOCITY"

        with pytest.raises(MusicalUnitError) as exc:
            parse_rhythm_pattern({"steps": 16, "hits": [{"step": 0, "drum": "kick", "panning": 105}]})
        assert exc.value.code == "INVALID_PANNING"

    def test_phrase_key_and_pitch_rejected(self):
        with pytest.raises(MusicalUnitError) as exc:
            parse_phrase({"notes": [{"key": 60, "pitch": "D4", "pos": 0, "length": 48}]})
        assert exc.value.code == "INVALID_PITCH"

