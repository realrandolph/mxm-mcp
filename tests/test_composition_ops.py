"""Tests for lmms_mcp.composition placement, arrangement, and automation operations."""

from __future__ import annotations

import pytest

from lmms_mcp.composition import (
    add_automation_batch,
    arrange_units,
    edit_automation,
    edit_notes,
    place_chord,
    place_chords,
    place_phrase,
    place_progression,
    place_rhythm,
)
from lmms_mcp.musical_units import (
    MusicalUnitError,
    parse_progression,
    reverse_automation_points,
    stretch_notes,
)
from lmms_mcp.xml_parser import (
    find_tracks,
    get_arrangement,
    resolve_automation_target,
)
from conftest import (
    AUTOMATION_SPEC,
    OFF_GRID,
    PHRASE_A,
    PROGRESSION_I_VI_IV_V,
    RHYTHM_KICK,
    instrument_clips,
    note_tuples,
)


# =====================================================================
# SUITE G: ARRANGEMENT AND PLACEMENT
# =====================================================================

class TestArrangementOps:
    def test_place_phrase_exact(self, song):
        plan = place_phrase(song, 0, PHRASE_A, at_bars=0, apply=True)["plan"]
        assert plan["occurrences"][0]["clip"] == {"pos": 0, "len": 192}
        assert note_tuples(song.root, 0) == [
            (0, 0, 36, 48, 100, 0),
            (0, 48, 39, 48, 80, 0),
            (0, 96, 43, 48, 120, 0),
            (0, 144, 36, 48, 100, 0),
        ]

    def test_place_phrase_at_bar(self, song):
        place_phrase(song, 0, PHRASE_A, at_bars=4, apply=True)
        assert note_tuples(song.root, 0)[0] == (768, 0, 36, 48, 100, 0)
        assert get_arrangement(song.root)[0]["pos"] == 768

    def test_repeat_with_per_occurrence_transforms(self, song):
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

    def test_transforms_keyed_by_out_of_range_occurrence(self, song):
        with pytest.raises(MusicalUnitError) as exc:
            place_phrase(song, 0, PHRASE_A, repeat=2, transforms={5: {"transpose": 1}})
        assert exc.value.code == "PATTERN_INDEX_OUT_OF_RANGE"

    def test_repeat_zero_rejected(self, song):
        with pytest.raises(MusicalUnitError) as exc:
            place_phrase(song, 0, PHRASE_A, repeat=0)
        assert exc.value.code in ("EMPTY_UNIT", "INVALID_TIME")

    def test_negative_at_rejected(self, song):
        with pytest.raises(MusicalUnitError) as exc:
            place_phrase(song, 0, PHRASE_A, at_ticks=-1)
        assert exc.value.code == "NEGATIVE_TIME"

    def test_zero_spacing_with_repeat_rejected(self, song):
        with pytest.raises(MusicalUnitError) as exc:
            place_phrase(song, 0, PHRASE_A, repeat=2, spacing_ticks=0)
        assert exc.value.code == "INVALID_TIME"

    def test_place_chord_together(self, song):
        plan = place_chord(song, 1, {"symbol": "C4maj7", "at": 0, "length": 192}, apply=True)["plan"]
        assert len(plan["occurrences"]) == 1
        assert note_tuples(song.root, 1) == [
            (0, 0, 60, 192, 100, 0),
            (0, 0, 64, 192, 100, 0),
            (0, 0, 67, 192, 100, 0),
            (0, 0, 71, 192, 100, 0),
        ]

    def test_place_progression_sequential(self, song):
        plan = place_progression(song, 1, PROGRESSION_I_VI_IV_V, at_bars=0, apply=True)["plan"]
        assert [o["at_ticks"] for o in plan["occurrences"]] == [0, 192, 384, 576]
        keys = [sorted(n["key"] for n in o["notes"]) for o in plan["occurrences"]]
        assert keys == [[60, 64, 67], [57, 60, 64], [65, 69, 72], [67, 71, 74]]

    def test_progression_with_rest_entry(self, song):
        plan = place_progression(song, 1, {"chords": [
            {"symbol": "C4maj", "bars": 1}, {"rest": 1}, {"symbol": "G4maj", "bars": 2},
        ]}, at_bars=0, apply=False)["plan"]
        assert [o["at_ticks"] for o in plan["occurrences"]] == [0, 384]
        assert plan["occurrences"][1]["clip"]["len"] == 384

    def test_progression_absolute_positions(self, song):
        plan = place_progression(song, 1, {"chords": [
            {"symbol": "C4maj", "at": 0},
            {"symbol": "G4maj", "at": 384},
        ]}, layout="absolute", apply=False)["plan"]
        assert [o["at_ticks"] for o in plan["occurrences"]] == [0, 384]

    def test_mixing_duration_and_at_rejected(self):
        with pytest.raises(MusicalUnitError) as exc:
            parse_progression({"chords": [
                {"symbol": "C4maj", "bars": 1},
                {"symbol": "G4maj", "at": 192},
            ]})
        assert exc.value.code == "INVALID_TIME"

    def test_each_occurrence_gets_its_own_clip_when_transformed(self, song):
        place_phrase(song, 0, PHRASE_A, repeat=2, spacing_bars=1,
                     transforms={1: {"transpose": 12}}, apply=True)
        clips = instrument_clips(find_tracks(song.root)[0])
        assert len(clips) == 2
        assert sorted(int(c.get("pos")) for c in clips) == [0, 192]
        assert all(len(c.findall("note")) == 4 for c in clips)
        assert all(c.tag == "midiclip" for c in clips)

    def test_linked_patterns_rejected_for_midiclip(self, song):
        with pytest.raises(MusicalUnitError) as exc:
            place_phrase(song, 0, PHRASE_A, repeat=2, link=True)
        assert exc.value.code == "UNSUPPORTED_LINK"

    def test_clip_extends_to_contain_notes(self, song):
        plan = place_phrase(song, 0,
                            {"name": "long", "notes": [
                                {"key": 60, "pos": 0, "length": 48},
                                {"key": 62, "pos": 180, "length": 48},
                            ]},
                            at_ticks=0, apply=True)["plan"]
        assert plan["occurrences"][0]["clip"]["len"] == 228
        clip = instrument_clips(find_tracks(song.root)[0])[0]
        assert clip.get("len") == "228"

    def test_fit_modes(self, song):
        phrase = {"notes": [{"key": 60, "pos": 0, "length": 96}]}
        with pytest.raises(MusicalUnitError) as exc:
            place_phrase(song, 0, phrase, clip_len_ticks=48, fit="error", apply=True)
        assert exc.value.code == "CLIP_TOO_SHORT"

        plan = place_phrase(song, 0, phrase, clip_len_ticks=48, fit="truncate", apply=True)["plan"]
        assert plan["occurrences"][0]["notes"][0]["length"] == 48

    @pytest.mark.parametrize("track_index,code", [
        (-1, "TRACK_INDEX_OUT_OF_RANGE"),
        (99, "TRACK_INDEX_OUT_OF_RANGE"),
    ])
    def test_bad_track_index(self, song, track_index, code):
        with pytest.raises(MusicalUnitError) as exc:
            place_phrase(song, track_index, PHRASE_A)
        assert exc.value.code == code

    def test_notes_on_sample_track_rejected(self, song):
        with pytest.raises(MusicalUnitError) as exc:
            place_phrase(song, 3, PHRASE_A)
        assert exc.value.code == "WRONG_TRACK_TYPE"

    def test_bad_pattern_index(self, song):
        place_phrase(song, 0, PHRASE_A, at_bars=0, apply=True)
        with pytest.raises(MusicalUnitError) as exc:
            place_phrase(song, 0, PHRASE_A, at_bars=1, pattern_index=5)
        assert exc.value.code == "PATTERN_INDEX_OUT_OF_RANGE"

    def test_arrangement_reflects_all_units(self, song):
        place_phrase(song, 0, PHRASE_A, at_bars=0, repeat=2, spacing_bars=1, apply=True)
        place_progression(song, 1, PROGRESSION_I_VI_IV_V, at_bars=0, apply=True)
        place_rhythm(song, 2, RHYTHM_KICK, at_bars=4, repeat=2, spacing_bars=1, target="bb", apply=True)
        clips = get_arrangement(song.root)
        assert clips == sorted(clips, key=lambda c: (c["pos"], c["track_index"]))
        assert {c["kind"] for c in clips}.issubset({"midiclip", "pattern", "bbtco"})
        assert all(c["len"] > 0 for c in clips)

    def test_no_note_outside_its_clip(self, song):
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

    def test_plan_song_length(self, song):
        plan = place_phrase(song, 0, PHRASE_A, at_bars=8, repeat=2, spacing_bars=2)["plan"]
        assert plan["song_length_ticks"] == 11 * 192

    def test_pattern_repeat_places_copies(self, song):
        plan = place_rhythm(song, 0, RHYTHM_KICK, at_bars=0, repeat=4, spacing_bars=1, apply=False)["plan"]
        assert [o["at_ticks"] for o in plan["occurrences"]] == [0, 192, 384, 576]
        assert len(plan["occurrences"][0]["notes"]) == 4

    def test_bb_clip_repeats_reference_same_pattern(self, song):
        plan = place_rhythm(song, 2, RHYTHM_KICK, at_bars=0, repeat=3, spacing_bars=1, target="bb", apply=True)["plan"]
        drums = find_tracks(song.root)[2]
        clips = drums.findall("bbtco")
        assert [c.get("pos") for c in clips] == ["0", "192", "384"]
        inner = drums.find("patterntrack/trackcontainer/track")
        if inner is None:
            inner = drums.find("bbtrack/trackcontainer/track")
        assert len(inner.findall("pattern")) == 1
        assert len(plan["occurrences"][0]["notes"]) == 4

    def test_per_occurrence_transform_on_bb_is_rejected(self, song):
        with pytest.raises(MusicalUnitError) as exc:
            place_rhythm(song, 2, RHYTHM_KICK, at_bars=0, repeat=2, spacing_bars=1, target="bb",
                         transforms={1: {"transpose": 12}})
        assert exc.value.code == "UNSUPPORTED_TRANSFORMATION"

    def test_bar_units_reject_non_4_4_project(self, project):
        project.new()
        project.set_time_signature(3, 4)
        with pytest.raises(MusicalUnitError) as exc:
            place_phrase(project, 0, PHRASE_A, at_bars=1, apply=False)
        assert exc.value.code == "UNSUPPORTED_TIME_SIGNATURE"
        project.add_track("instrument", "Bass")
        plan = place_phrase(project, 0, PHRASE_A, at_ticks=192, apply=False)["plan"]
        assert plan["occurrences"][0]["at_ticks"] == 192


# =====================================================================
# SUITE H: AUTOMATION OPERATIONS
# =====================================================================

class TestAutomationOps:
    def test_batch_automation_curves(self, song):
        result = add_automation_batch(song, [
            {"target_type": "song", "param": "tempo",
             "points": [{"bar": 0, "value": 120}, {"bar": 2, "value": 150}, {"bar": 4, "value": 120}]},
            {"target_type": "track", "target_index": 0, "param": "volume",
             "points": [{"bar": 0, "value": 100}, {"bar": 8, "value": 60}]},
            {"target_type": "mixer", "target_index": 0, "param": "volume",
             "points": [{"bar": 0, "value": 100}, {"bar": 8, "value": 50}]},
        ], apply=True)
        assert result["created"] == 3
        auto = [t for t in find_tracks(song.root) if t.get("type") == "5"]
        assert len(auto) == 3
        times = auto[0].find("automationpattern").findall("time")
        assert [(t.get("pos"), t.get("value")) for t in times] == [
            ("0", "120.0"), ("384", "150.0"), ("768", "120.0"),
        ]

    def test_automation_curve_length_and_object_link(self, song):
        model_id, _ = resolve_automation_target(song.root, "song", 0, "tempo")
        add_automation_batch(song, [{"target_type": "song", "param": "tempo",
                                     "points": [{"bar": 0, "value": 120}, {"bar": 4, "value": 140}]}], apply=True)
        pattern = [t for t in find_tracks(song.root) if t.get("type") == "5"][0].find("automationpattern")
        assert pattern.get("len") == "769"
        assert pattern.get("prog") == "1"
        assert int(pattern.find("object").get("id")) == model_id

    def test_automation_reverse(self, song):
        points = [{"pos": 0, "value": 100}, {"pos": 192, "value": 150}, {"pos": 384, "value": 120}]
        out = reverse_automation_points(points, span_ticks=384)
        assert [(p["pos"], p["value"]) for p in out] == [
            (0, 120), (192, 150), (384, 100),
        ]

    def test_automation_dedupe_last_wins(self, song):
        add_automation_batch(song, [AUTOMATION_SPEC], apply=True)
        plan = edit_automation(song, curve_index=0, ops=[
            {"set": [{"pos": 0, "value": 100}, {"pos": 0, "value": 140}]},
        ], apply=False)["plan"]
        assert plan["points"] == [{"pos": 0, "value": 140.0}]

    def test_automation_duplicate_error_policy(self, song):
        add_automation_batch(song, [AUTOMATION_SPEC], apply=True)
        with pytest.raises(MusicalUnitError) as exc:
            edit_automation(song, curve_index=0, dedupe="error", ops=[
                {"set": [{"pos": 0, "value": 100}, {"pos": 0, "value": 140}]},
            ], apply=False)
        assert exc.value.code == "DUPLICATE_AUTOMATION_POINT"

    @pytest.mark.parametrize("target,index,param,value,code", [
        ("song", 0, "tempo", 9, "AUTOMATION_VALUE_OUT_OF_RANGE"),
        ("song", 0, "tempo", 1000, "AUTOMATION_VALUE_OUT_OF_RANGE"),
        ("song", 0, "master_volume", 201, "AUTOMATION_VALUE_OUT_OF_RANGE"),
        ("song", 0, "master_pitch", 13, "AUTOMATION_VALUE_OUT_OF_RANGE"),
        ("track", 0, "panning", -101, "AUTOMATION_VALUE_OUT_OF_RANGE"),
        ("mixer", 99, "volume", 1.0, "UNKNOWN_AUTOMATION_TARGET"),
    ])
    def test_automation_ranges_and_targets(self, song, target, index, param, value, code):
        with pytest.raises(MusicalUnitError) as exc:
            add_automation_batch(song, [{"target_type": target, "target_index": index,
                                         "param": param, "points": [{"bar": 0, "value": value}]}],
                                 on_out_of_range="error")
        assert exc.value.code == code

    def test_automation_clamp_policy_default(self, song):
        plan = add_automation_batch(song, [{
            "target_type": "song", "param": "tempo",
            "points": [{"bar": 0, "value": 120}, {"bar": 4, "value": 5000}],
        }], apply=False)["plan"]
        assert plan["curves"][0]["points"][-1]["value"] == 999.0

    def test_smooth_false_writes_linear_progression(self, song):
        add_automation_batch(song, [{"target_type": "song", "param": "tempo", "smooth": False,
                                     "points": [{"bar": 0, "value": 120}, {"bar": 4, "value": 140}]}], apply=True)
        pattern = [t for t in find_tracks(song.root) if t.get("type") == "5"][0].find("automationpattern")
        assert pattern.get("prog") == "0"

    def test_tension_written(self, song):
        add_automation_batch(song, [{"target_type": "song", "param": "tempo", "tension": 0.5,
                                     "points": [{"bar": 0, "value": 120}, {"bar": 4, "value": 140}]}], apply=True)
        pattern = [t for t in find_tracks(song.root) if t.get("type") == "5"][0].find("automationpattern")
        assert pattern.get("tens") == "0.5"

    def test_issue11_acceptance_song(self, tmp_path):
        from lmms_mcp.project import LMMSProject

        project = LMMSProject()
        project.new(bpm=128)
        project.add_track("instrument", "Bass", instrument="lb302")
        project.add_track("instrument", "Keys", instrument="tripleoscillator")
        project.add_track("pattern", "Drums")

        # 4-bar bass phrase = 4 x PHRASE_A
        phrase = {
            "name": "bass-4bar",
            "notes": [
                {**n, "pos": n["pos"] + 192 * bar}
                for bar in range(4) for n in PHRASE_A["notes"]
            ],
        }
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
        loaded = LMMSProject()
        loaded.load(path)
        assert note_tuples(loaded.root, 0) == note_tuples(project.root, 0)
        assert get_arrangement(loaded.root) == get_arrangement(project.root)
        assert loaded.root.find(".//automationpattern/time") is not None
