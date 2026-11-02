"""High-level composition operations, placement, arrangement, and automation editing.

Implements Issue #11 contracts:
- Strict preview vs apply parity (plan is identical, apply=False leaves project untouched)
- Phrase, chord, progression, rhythm, and batch arrangement placements
- Instrument clips written as canonical <midiclip>
- BB tracks support shared pattern semantics with <bbtco> clips
- Comprehensive input validation and typed MusicalUnitError codes
"""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from .models import (
    TICKS_PER_BAR,
    bars_to_ticks,
    midi_to_note_name,
    ticks_to_bars,
)
from .musical_units import (
    CHORD_QUALITIES,
    DRUM_MAP,
    MusicalUnitError,
    Phrase,
    PhraseNote,
    apply_transforms,
    chord_pitches,
    parse_chord_spec,
    parse_chord_symbol,
    parse_duration,
    parse_phrase,
    parse_pitch,
    parse_progression,
    parse_rhythm_pattern,
    parse_time,
    reverse_automation_points,
    round_half_up,
    transform_notes,
)
from .xml_parser import (
    _model_id_counter,
    add_automation_track,
    find_track_element,
    find_tracks,
    get_arrangement,
    get_track_name,
    get_track_type,
    resolve_automation_target,
)


# =====================================================================
# TARGET VALIDATION HELPERS
# =====================================================================

def _validate_track(root: ET.Element, track_index: int, allowed_types: Optional[list[int]] = None) -> ET.Element:
    """Validate track_index exists and has allowed track type."""
    if track_index < 0:
        raise MusicalUnitError(
            f"Negative track index {track_index} is not allowed",
            code="TRACK_INDEX_OUT_OF_RANGE",
            field="track_index",
            value=track_index,
        )

    tracks = find_tracks(root)
    if track_index >= len(tracks):
        raise MusicalUnitError(
            f"Track index {track_index} out of range (project has {len(tracks)} tracks)",
            code="TRACK_INDEX_OUT_OF_RANGE",
            field="track_index",
            value=track_index,
        )

    track = tracks[track_index]
    t_type = get_track_type(track)
    if allowed_types is not None and t_type not in allowed_types:
        raise MusicalUnitError(
            f"Track {track_index} has type {t_type}; expected one of {allowed_types}",
            code="WRONG_TRACK_TYPE",
            field="track_index",
            value=t_type,
        )

    return track


def _check_time_signature_for_bars(project: Any, field: Optional[str] = None) -> None:
    """Ensure project time signature is 4/4 if bar units are used."""
    head = project.root.find("song/head")
    if head is None:
        head = project.root.find("head")
    if head is not None:
        num = int(head.get("timesig_numerator", "4"))
        den = int(head.get("timesig_denominator", "4"))
        if (num, den) != (4, 4):
            raise MusicalUnitError(
                f"Bar units only supported for 4/4 projects, found {num}/{den}",
                code="UNSUPPORTED_TIME_SIGNATURE",
                field=field,
                value=f"{num}/{den}",
            )


# =====================================================================
# XML CLIP WRITING
# =====================================================================

def _create_midiclip(
    track: ET.Element,
    pos_ticks: int,
    len_ticks: int,
    name: str = "Clip",
) -> ET.Element:
    """Create a canonical <midiclip> element on an instrument track."""
    return ET.SubElement(track, "midiclip", {
        "pos": str(pos_ticks),
        "len": str(len_ticks),
        "name": name,
        "type": "1",
        "muted": "0",
        "steps": "16",
        "autoresize": "1",
    })


def _add_notes_to_clip(clip: ET.Element, notes: list[dict[str, Any]]) -> None:
    """Add <note> children to an XML clip (notes have clip-relative pos)."""
    for n in notes:
        ET.SubElement(clip, "note", {
            "key": str(n["key"]),
            "pos": str(n["pos"]),
            "len": str(n["length"]),
            "vol": str(n.get("volume", 100)),
            "pan": str(n.get("panning", 0)),
            "type": "0",
        })


# =====================================================================
# PHRASE PLACEMENT
# =====================================================================

def place_phrase(
    project: Any,
    track_index: int,
    phrase: Union[dict[str, Any], Phrase],
    at_ticks: Optional[int] = None,
    at_bars: Optional[float] = None,
    repeat: int = 1,
    spacing_bars: Optional[float] = None,
    spacing_ticks: Optional[int] = None,
    transforms: Optional[dict[int, dict[str, Any]]] = None,
    clip_name: Optional[str] = None,
    clip_len_ticks: Optional[int] = None,
    fit: Literal["extend", "truncate", "error"] = "extend",
    link: bool = False,
    pattern_index: Optional[int] = None,
    apply: bool = True,
) -> dict[str, Any]:
    """Place a phrase / motif with repetitions and per-occurrence transformations."""
    if link:
        raise MusicalUnitError(
            "Linked patterns are not supported for instrument clips",
            code="UNSUPPORTED_LINK",
        )

    if repeat < 1:
        raise MusicalUnitError("repeat must be at least 1", code="EMPTY_UNIT", value=repeat)

    parsed_phrase = parse_phrase(phrase) if isinstance(phrase, dict) else phrase
    name = clip_name or parsed_phrase.name

    # Determine start position
    if at_bars is not None:
        _check_time_signature_for_bars(project, field="at_bars")
        start_tick = parse_time(at_bars, unit="bars", field="at_bars")
    elif at_ticks is not None:
        start_tick = parse_time(at_ticks, unit="ticks", field="at_ticks")
    else:
        start_tick = 0

    # Determine spacing interval
    if spacing_bars is not None:
        _check_time_signature_for_bars(project, field="spacing_bars")
        interval = parse_time(spacing_bars, unit="bars", field="spacing_bars")
    elif spacing_ticks is not None:
        interval = parse_time(spacing_ticks, unit="ticks", field="spacing_ticks")
    else:
        interval = parsed_phrase.length_ticks

    if repeat > 1 and interval <= 0:
        raise MusicalUnitError("Spacing must be > 0 when repeat > 1", code="INVALID_TIME", value=interval)

    # Validate transform keys
    if transforms:
        for occ_idx in transforms.keys():
            if occ_idx < 0 or occ_idx >= repeat:
                raise MusicalUnitError(
                    f"Transform occurrence index {occ_idx} out of range [0, {repeat - 1}]",
                    code="PATTERN_INDEX_OUT_OF_RANGE",
                    value=occ_idx,
                )

    # Validate target track
    track = _validate_track(project.root, track_index, allowed_types=[0])  # Instrument tracks only

    if pattern_index is not None:
        patterns = track.findall("pattern") + track.findall("midiclip")
        if pattern_index < 0 or pattern_index >= len(patterns):
            raise MusicalUnitError(
                f"Pattern index {pattern_index} out of range (track has {len(patterns)} patterns)",
                code="PATTERN_INDEX_OUT_OF_RANGE",
                field="pattern_index",
                value=pattern_index,
            )

    # Build plan occurrences
    base_notes = parsed_phrase.notes_as_dicts()
    occurrences = []
    max_song_tick = 0

    for i in range(repeat):
        occ_tick = start_tick + i * interval
        occ_transform = transforms.get(i, {}) if transforms else {}

        # Apply transforms to note copies
        occ_notes = apply_transforms(base_notes, occ_transform, span_ticks=parsed_phrase.length_ticks)

        # Handle clip fit
        needed_len = max(n["pos"] + n["length"] for n in occ_notes) if occ_notes else parsed_phrase.length_ticks
        if clip_len_ticks is not None:
            if needed_len > clip_len_ticks:
                if fit == "error":
                    raise MusicalUnitError(
                        f"Notes exceed clip length {clip_len_ticks}",
                        code="CLIP_TOO_SHORT",
                        field="clip_len_ticks",
                    )
                elif fit == "truncate":
                    # Truncate notes
                    truncated = []
                    for n in occ_notes:
                        if n["pos"] < clip_len_ticks:
                            n_copy = copy.deepcopy(n)
                            n_copy["length"] = min(n_copy["length"], clip_len_ticks - n_copy["pos"])
                            truncated.append(n_copy)
                    occ_notes = truncated
                    actual_clip_len = clip_len_ticks
                else:  # extend
                    actual_clip_len = max(clip_len_ticks, needed_len)
            else:
                actual_clip_len = clip_len_ticks
        else:
            actual_clip_len = max(parsed_phrase.length_ticks, needed_len)

        # Ensure note positions are relative to clip start (0)
        clip_entry = {"pos": occ_tick, "len": actual_clip_len}
        occurrences.append({
            "index": i,
            "at_ticks": occ_tick,
            "transform": occ_transform,
            "clip": clip_entry,
            "notes": occ_notes,
        })
        max_song_tick = max(max_song_tick, occ_tick + actual_clip_len)

    plan = {
        "version": 1,
        "kind": "phrase",
        "target": {"track_index": track_index, "pattern_index": pattern_index or 0},
        "occurrences": occurrences,
        "warnings": parsed_phrase.warnings,
        "song_length_ticks": max_song_tick,
    }

    if apply:
        # Commit to live project XML
        if pattern_index is not None:
            patterns = track.findall("pattern") + track.findall("midiclip")
            target_clip = patterns[pattern_index]
            target_clip.set("name", name)
            # Add notes from first occurrence
            for occ in occurrences:
                _add_notes_to_clip(target_clip, occ["notes"])
                cur_len = int(target_clip.get("len", "192"))
                target_clip.set("len", str(max(cur_len, occ["clip"]["len"])))
        else:
            for occ in occurrences:
                clip = _create_midiclip(track, occ["at_ticks"], occ["clip"]["len"], name=name)
                _add_notes_to_clip(clip, occ["notes"])

        project._modified = True

    return {
        "ok": True,
        "applied": apply,
        "plan": plan,
    }


# =====================================================================
# CHORD PLACEMENT
# =====================================================================

def place_chord(
    project: Any,
    track_index: int,
    chord: Union[dict[str, Any], ChordSpec],
    at_ticks: Optional[int] = None,
    at_bars: Optional[float] = None,
    repeat: int = 1,
    spacing_bars: Optional[float] = None,
    spacing_ticks: Optional[int] = None,
    transforms: Optional[dict[int, dict[str, Any]]] = None,
    clip_name: Optional[str] = None,
    apply: bool = True,
) -> dict[str, Any]:
    """Place a chord with all constituent notes sounding together."""
    if isinstance(chord, dict):
        spec = parse_chord_spec(chord)
        chord_len = parse_time(chord.get("length", 192), unit="ticks")
        c_name = clip_name or chord.get("name", spec.symbol)
        vol = chord.get("volume", 100)
        pan = chord.get("panning", 0)
    else:
        spec = chord
        chord_len = 192
        c_name = clip_name or spec.symbol
        vol = 100
        pan = 0

    pitches = chord_pitches(spec)
    notes = [{
        "key": p,
        "pos": 0,
        "length": chord_len,
        "volume": vol,
        "panning": pan,
    } for p in pitches]

    phrase_obj = Phrase(
        name=c_name,
        length_ticks=chord_len,
        notes=[PhraseNote(**n) for n in notes],
    )

    result = place_phrase(
        project=project,
        track_index=track_index,
        phrase=phrase_obj,
        at_ticks=at_ticks,
        at_bars=at_bars,
        repeat=repeat,
        spacing_bars=spacing_bars,
        spacing_ticks=spacing_ticks,
        transforms=transforms,
        clip_name=c_name,
        clip_len_ticks=chord_len,
        apply=apply,
    )
    result["plan"]["kind"] = "chord"
    return result


def place_chords(
    project: Any,
    track_index: int,
    chords: list[dict[str, Any]],
    apply: bool = True,
) -> dict[str, Any]:
    """Place multiple chords at their specified positions."""
    if not chords:
        raise MusicalUnitError("Chords list cannot be empty", code="EMPTY_UNIT")

    all_occurrences = []
    max_song_tick = 0
    track = _validate_track(project.root, track_index, allowed_types=[0])

    for idx, c in enumerate(chords):
        spec = parse_chord_spec(c)
        pitches = chord_pitches(spec)
        at_t = parse_time(c.get("at", 0), unit="ticks")
        dur = parse_time(c.get("length", 192), unit="ticks")
        notes = [{
            "key": p,
            "pos": 0,
            "length": dur,
            "volume": c.get("volume", 100),
            "panning": c.get("panning", 0),
        } for p in pitches]

        clip_entry = {"pos": at_t, "len": dur}
        all_occurrences.append({
            "index": idx,
            "at_ticks": at_t,
            "transform": {},
            "clip": clip_entry,
            "notes": notes,
        })
        max_song_tick = max(max_song_tick, at_t + dur)

        if apply:
            clip = _create_midiclip(track, at_t, dur, name=spec.symbol)
            _add_notes_to_clip(clip, notes)

    if apply:
        project._modified = True

    plan = {
        "version": 1,
        "kind": "chords",
        "target": {"track_index": track_index, "pattern_index": 0},
        "occurrences": all_occurrences,
        "warnings": [],
        "song_length_ticks": max_song_tick,
    }
    return {"ok": True, "applied": apply, "plan": plan}


# =====================================================================
# PROGRESSION PLACEMENT
# =====================================================================

def place_progression(
    project: Any,
    track_index: int,
    progression: Union[dict[str, Any], Progression],
    at_ticks: Optional[int] = None,
    at_bars: Optional[float] = None,
    repeat: int = 1,
    spacing_bars: Optional[float] = None,
    spacing_ticks: Optional[int] = None,
    transforms: Optional[dict[int, dict[str, Any]]] = None,
    layout: Literal["sequential", "absolute"] = "sequential",
    apply: bool = True,
) -> dict[str, Any]:
    """Place a chord progression with each chord getting its own clip sequentially."""
    if repeat < 1:
        raise MusicalUnitError("repeat must be at least 1", code="EMPTY_UNIT")

    parsed_prog = parse_progression(progression) if isinstance(progression, dict) else progression

    if at_bars is not None:
        _check_time_signature_for_bars(project, field="at_bars")
        start_tick = parse_time(at_bars, unit="bars", field="at_bars")
    elif at_ticks is not None:
        start_tick = parse_time(at_ticks, unit="ticks", field="at_ticks")
    else:
        start_tick = 0

    if spacing_bars is not None:
        _check_time_signature_for_bars(project, field="spacing_bars")
        interval = parse_time(spacing_bars, unit="bars", field="spacing_bars")
    elif spacing_ticks is not None:
        interval = parse_time(spacing_ticks, unit="ticks", field="spacing_ticks")
    else:
        interval = parsed_prog.length_ticks

    track = _validate_track(project.root, track_index, allowed_types=[0])

    occurrences = []
    max_song_tick = 0
    occ_idx = 0

    for rep in range(repeat):
        rep_start = start_tick + rep * interval
        for chord_item in parsed_prog.chords:
            chord_at = rep_start + chord_item.at_ticks
            occ_notes = copy.deepcopy(chord_item.notes)

            # Apply per-occurrence transform if supplied
            occ_transform = transforms.get(occ_idx, {}) if transforms else {}
            if occ_transform:
                occ_notes = apply_transforms(occ_notes, occ_transform)

            clip_entry = {"pos": chord_at, "len": chord_item.duration_ticks}
            occurrences.append({
                "index": occ_idx,
                "at_ticks": chord_at,
                "transform": occ_transform,
                "clip": clip_entry,
                "notes": occ_notes,
            })
            max_song_tick = max(max_song_tick, chord_at + chord_item.duration_ticks)

            if apply:
                clip = _create_midiclip(
                    track,
                    chord_at,
                    chord_item.duration_ticks,
                    name=chord_item.spec.symbol if chord_item.spec else "Chord",
                )
                _add_notes_to_clip(clip, occ_notes)

            occ_idx += 1

    if apply:
        project._modified = True

    plan = {
        "version": 1,
        "kind": "progression",
        "target": {"track_index": track_index, "pattern_index": 0},
        "occurrences": occurrences,
        "warnings": [],
        "song_length_ticks": max_song_tick,
    }
    return {"ok": True, "applied": apply, "plan": plan}


# =====================================================================
# RHYTHM PLACEMENT
# =====================================================================

def place_rhythm(
    project: Any,
    track_index: int,
    rhythm: Union[dict[str, Any], RhythmPattern],
    at_ticks: Optional[int] = None,
    at_bars: Optional[float] = None,
    repeat: int = 1,
    spacing_bars: Optional[float] = None,
    spacing_ticks: Optional[int] = None,
    target: Literal["instrument", "bb"] = "instrument",
    transforms: Optional[dict[int, dict[str, Any]]] = None,
    clip_name: Optional[str] = None,
    apply: bool = True,
) -> dict[str, Any]:
    """Place a rhythm pattern on an instrument track or BB pattern track."""
    if repeat < 1:
        raise MusicalUnitError("repeat must be at least 1", code="EMPTY_UNIT")

    parsed_rhythm = parse_rhythm_pattern(rhythm) if isinstance(rhythm, dict) else rhythm
    name = clip_name or parsed_rhythm.name

    if at_bars is not None:
        _check_time_signature_for_bars(project, field="at_bars")
        start_tick = parse_time(at_bars, unit="bars", field="at_bars")
    elif at_ticks is not None:
        start_tick = parse_time(at_ticks, unit="ticks", field="at_ticks")
    else:
        start_tick = 0

    if spacing_bars is not None:
        _check_time_signature_for_bars(project, field="spacing_bars")
        interval = parse_time(spacing_bars, unit="bars", field="spacing_bars")
    elif spacing_ticks is not None:
        interval = parse_time(spacing_ticks, unit="ticks", field="spacing_ticks")
    else:
        interval = parsed_rhythm.length_ticks

    if target == "bb":
        # BB tracks cannot have per-occurrence transforms because all bbtco clips reference one pattern
        if transforms:
            raise MusicalUnitError(
                "Per-occurrence transforms are not supported on shared BB tracks",
                code="UNSUPPORTED_TRANSFORMATION",
            )
        track = _validate_track(project.root, track_index, allowed_types=[1])
    else:
        track = _validate_track(project.root, track_index, allowed_types=[0])

    base_hits = parsed_rhythm.as_dicts()
    occurrences = []
    max_song_tick = 0

    for i in range(repeat):
        occ_tick = start_tick + i * interval
        occ_transform = transforms.get(i, {}) if transforms else {}
        occ_notes = apply_transforms(base_hits, occ_transform, span_ticks=parsed_rhythm.length_ticks)

        clip_entry = {"pos": occ_tick, "len": parsed_rhythm.length_ticks}
        occurrences.append({
            "index": i,
            "at_ticks": occ_tick,
            "transform": occ_transform,
            "clip": clip_entry,
            "notes": occ_notes,
        })
        max_song_tick = max(max_song_tick, occ_tick + parsed_rhythm.length_ticks)

    plan = {
        "version": 1,
        "kind": "rhythm",
        "target": {"track_index": track_index, "pattern_index": 0},
        "occurrences": occurrences,
        "warnings": [],
        "song_length_ticks": max_song_tick,
    }

    if apply:
        if target == "bb":
            # Add notes to the inner instrument pattern once
            inner_track = track.find("patterntrack/trackcontainer/track")
            if inner_track is None:
                inner_track = track.find("bbtrack/trackcontainer/track")
            if inner_track is not None:
                pat = inner_track.find("pattern")
                if pat is None:
                    pat = ET.SubElement(inner_track, "pattern", {
                        "name": name,
                        "type": "1",
                        "steps": str(parsed_rhythm.steps),
                        "len": str(parsed_rhythm.length_ticks),
                        "muted": "0",
                    })
                _add_notes_to_clip(pat, base_hits)

            # Place bbtco clips at occurrences
            for occ in occurrences:
                ET.SubElement(track, "bbtco", {
                    "pos": str(occ["at_ticks"]),
                    "len": str(occ["clip"]["len"]),
                    "name": name,
                    "muted": "0",
                    "color": "0",
                    "usestyle": "1",
                })
        else:
            for occ in occurrences:
                clip = _create_midiclip(track, occ["at_ticks"], occ["clip"]["len"], name=name)
                _add_notes_to_clip(clip, occ["notes"])

        project._modified = True

    return {
        "ok": True,
        "applied": apply,
        "plan": plan,
    }


# =====================================================================
# BATCH ARRANGEMENT
# =====================================================================

def arrange_units(
    project: Any,
    placements: list[dict[str, Any]],
    apply: bool = True,
) -> dict[str, Any]:
    """Place multiple musical units across tracks and sections in a batch."""
    if not placements:
        raise MusicalUnitError("Placements cannot be empty", code="EMPTY_UNIT")

    results = []
    max_song_tick = 0
    for p in placements:
        kind = p.get("type", p.get("kind", "phrase"))
        track_idx = p["track_index"]

        if kind == "phrase":
            res = place_phrase(
                project=project,
                track_index=track_idx,
                phrase=p["phrase"],
                at_ticks=p.get("at_ticks"),
                at_bars=p.get("at_bars"),
                repeat=p.get("repeat", 1),
                spacing_bars=p.get("spacing_bars"),
                spacing_ticks=p.get("spacing_ticks"),
                transforms=p.get("transforms"),
                clip_name=p.get("name"),
                apply=apply,
            )
        elif kind == "chord":
            res = place_chord(
                project=project,
                track_index=track_idx,
                chord=p["chord"],
                at_ticks=p.get("at_ticks"),
                at_bars=p.get("at_bars"),
                repeat=p.get("repeat", 1),
                spacing_bars=p.get("spacing_bars"),
                spacing_ticks=p.get("spacing_ticks"),
                transforms=p.get("transforms"),
                clip_name=p.get("name"),
                apply=apply,
            )
        elif kind == "progression":
            res = place_progression(
                project=project,
                track_index=track_idx,
                progression=p["progression"],
                at_ticks=p.get("at_ticks"),
                at_bars=p.get("at_bars"),
                repeat=p.get("repeat", 1),
                spacing_bars=p.get("spacing_bars"),
                spacing_ticks=p.get("spacing_ticks"),
                transforms=p.get("transforms"),
                apply=apply,
            )
        elif kind == "rhythm":
            res = place_rhythm(
                project=project,
                track_index=track_idx,
                rhythm=p["rhythm"],
                at_ticks=p.get("at_ticks"),
                at_bars=p.get("at_bars"),
                repeat=p.get("repeat", 1),
                spacing_bars=p.get("spacing_bars"),
                spacing_ticks=p.get("spacing_ticks"),
                target=p.get("target", "instrument"),
                transforms=p.get("transforms"),
                clip_name=p.get("name"),
                apply=apply,
            )
        else:
            raise MusicalUnitError(f"Unknown placement kind {kind!r}", code="INVALID_PHRASE", value=kind)

        results.append(res["plan"])
        max_song_tick = max(max_song_tick, res["plan"]["song_length_ticks"])

    plan = {
        "version": 1,
        "kind": "arrangement",
        "units": results,
        "song_length_ticks": max_song_tick,
    }
    return {"ok": True, "applied": apply, "plan": plan}


# =====================================================================
# NOTE EDITING
# =====================================================================

def edit_notes(
    project: Any,
    track_index: int,
    pattern_index: Optional[int] = None,
    transforms: Optional[dict[str, Any]] = None,
    filter_keys: Optional[list[int]] = None,
    filter_time_range: Optional[tuple[int, int]] = None,
    apply: bool = True,
) -> dict[str, Any]:
    """Batch edit notes in an existing track with transformations and preview/apply parity."""
    track = _validate_track(project.root, track_index, allowed_types=[0])
    patterns = track.findall("pattern") + track.findall("midiclip")
    if not patterns:
        return {"ok": True, "applied": apply, "plan": {"notes_matched": 0, "notes": []}}

    if pattern_index is not None:
        if pattern_index < 0 or pattern_index >= len(patterns):
            raise MusicalUnitError(
                f"Pattern index {pattern_index} out of range",
                code="PATTERN_INDEX_OUT_OF_RANGE",
            )
        target_patterns = [patterns[pattern_index]]
    else:
        target_patterns = patterns

    matched_elems = []
    matched_notes = []
    for pat in target_patterns:
        for ne in pat.findall("note"):
            k = int(ne.get("key", "60"))
            p = int(ne.get("pos", "0"))
            l = int(ne.get("len", "48"))
            v = int(ne.get("vol", "100"))
            pan = int(ne.get("pan", "0"))

            if filter_keys and k not in filter_keys:
                continue
            if filter_time_range:
                start, end = filter_time_range
                if not (start <= p < end):
                    continue

            matched_elems.append((pat, ne))
            matched_notes.append({
                "key": k,
                "pos": p,
                "length": l,
                "volume": v,
                "panning": pan,
            })

    if not matched_notes:
        return {"ok": True, "applied": apply, "plan": {"notes_matched": 0, "notes": []}}

    transformed = apply_transforms(matched_notes, transforms or {})

    if apply:
        for (pat, ne), t in zip(matched_elems, transformed):
            ne.set("key", str(t["key"]))
            ne.set("pos", str(t["pos"]))
            ne.set("len", str(t["length"]))
            ne.set("vol", str(t["volume"]))
            ne.set("pan", str(t.get("panning", 0)))
            # Extend pattern length if needed
            cur_len = int(pat.get("len", "192"))
            if t["pos"] + t["length"] > cur_len:
                pat.set("len", str(t["pos"] + t["length"]))
        project._modified = True

    plan = {
        "track_index": track_index,
        "notes_matched": len(transformed),
        "notes": transformed,
    }
    return {"ok": True, "applied": apply, "plan": plan}


# =====================================================================
# AUTOMATION CREATION AND EDITING
# =====================================================================

AUTOMATION_LIMITS: dict[str, tuple[float, float]] = {
    "tempo": (10.0, 999.0),
    "master_volume": (0.0, 200.0),
    "master_pitch": (-12.0, 12.0),
    "volume": (0.0, 200.0),
    "panning": (-100.0, 100.0),
}


def add_automation_batch(
    project: Any,
    curves: list[dict[str, Any]],
    on_out_of_range: Literal["clamp", "error"] = "clamp",
    apply: bool = True,
) -> dict[str, Any]:
    """Batch create automation tracks and curves with strict validation and preview/apply parity."""
    if not curves:
        raise MusicalUnitError("Curves list cannot be empty", code="EMPTY_UNIT")

    target_root = project.root if apply else copy.deepcopy(project.root)
    saved_model_counter = _model_id_counter[0]
    created_curves = []
    try:
        for idx, c in enumerate(curves):
            f = f"curves[{idx}]"
            target_type = c.get("target_type", "song")
            param = c.get("param", "tempo")
            target_index = c.get("target_index", 0)
            raw_points = c.get("points")
            name = c.get("name", param.replace("_", " ").title())
            smooth = c.get("smooth", True)
            tension = float(c.get("tension", 1.0))

            if not raw_points:
                raise MusicalUnitError(f"Points list empty for curve {idx}", code="EMPTY_UNIT", field=f)

            # Validate target and get model_id
            try:
                model_id, _ = resolve_automation_target(target_root, target_type, target_index, param)
            except (ValueError, IndexError) as exc:
                raise MusicalUnitError(
                    f"Unknown automation target: {exc}",
                    code="UNKNOWN_AUTOMATION_TARGET",
                    field=f,
                )

            # Validate & parse points
            parsed_points = []
            limits = AUTOMATION_LIMITS.get(param)

            for pt in raw_points:
                if "bar" in pt:
                    pt_pos = parse_time(pt["bar"], unit="bars")
                elif "pos" in pt:
                    pt_pos = parse_time(pt["pos"], unit="ticks")
                else:
                    raise MusicalUnitError("Point missing 'bar' or 'pos'", code="INVALID_TIME", field=f)

                val = float(pt["value"])
                if limits is not None:
                    min_v, max_v = limits
                    if not (min_v <= val <= max_v):
                        if on_out_of_range == "error":
                            raise MusicalUnitError(
                                f"Value {val} outside allowed range [{min_v}, {max_v}] for {param}",
                                code="AUTOMATION_VALUE_OUT_OF_RANGE",
                                field=f,
                                value=val,
                            )
                        else:
                            val = max(min_v, min(max_v, val))

                parsed_points.append({"pos": pt_pos, "value": val})

            parsed_points.sort(key=lambda p: p["pos"])
            max_tick = parsed_points[-1]["pos"]
            curve_len = max(192, max_tick + 1)

            created_curves.append({
                "target_type": target_type,
                "target_index": target_index,
                "param": param,
                "name": name,
                "smooth": smooth,
                "tension": tension,
                "model_id": model_id,
                "curve_len": curve_len,
                "points": parsed_points,
            })

            if apply:
                track = add_automation_track(
                    project.root,
                    name=name,
                    points=[(p["pos"], p["value"]) for p in parsed_points],
                    target_id=model_id,
                    progression=1 if smooth else 0,
                    tension=tension,
                    position=0,
                )
    finally:
        if not apply:
            _model_id_counter[0] = saved_model_counter

    if apply:
        project._modified = True

    plan = {
        "version": 1,
        "kind": "automation_batch",
        "curves": created_curves,
    }
    return {
        "ok": True,
        "applied": apply,
        "created": len(created_curves),
        "plan": plan,
    }


def edit_automation(
    project: Any,
    curve_index: int = 0,
    ops: Optional[list[dict[str, Any]]] = None,
    dedupe: Literal["last_wins", "error"] = "last_wins",
    apply: bool = True,
) -> dict[str, Any]:
    """Edit points of an existing automation track."""
    auto_tracks = [t for t in find_tracks(project.root) if get_track_type(t) == 5]
    if curve_index < 0 or curve_index >= len(auto_tracks):
        raise MusicalUnitError(
            f"Automation curve index {curve_index} out of range (have {len(auto_tracks)})",
            code="TRACK_INDEX_OUT_OF_RANGE",
        )

    track = auto_tracks[curve_index]
    pattern = track.find("automationpattern")
    if pattern is None:
        raise MusicalUnitError("Automation track has no pattern", code="EMPTY_UNIT")

    # Load existing curve points
    points = [
        {"pos": int(te.get("pos", "0")), "value": float(te.get("value", "0.0"))}
        for te in pattern.findall("time")
    ]

    if ops:
        for op in ops:
            if "set" in op:
                points = [{"pos": parse_time(p.get("pos", p.get("bar", 0)), unit="ticks" if "pos" in p else "bars"), "value": float(p["value"])} for p in op["set"]]
            if "time_shift_ticks" in op:
                shift = op["time_shift_ticks"]
                points = [{"pos": max(0, p["pos"] + shift), "value": p["value"]} for p in points]
            if "scale" in op:
                s = float(op["scale"])
                points = [{"pos": p["pos"], "value": p["value"] * s} for p in points]
            if "offset" in op:
                off = float(op["offset"])
                points = [{"pos": p["pos"], "value": p["value"] + off} for p in points]
            if "clamp" in op:
                min_v, max_v = op["clamp"]
                points = [{"pos": p["pos"], "value": max(min_v, min(max_v, p["value"]))} for p in points]
            if "reverse" in op:
                span = op.get("span_ticks", max((p["pos"] for p in points), default=192))
                points = reverse_automation_points(points, span_ticks=span)

    # Dedupe points
    deduped_map: dict[int, float] = {}
    for p in points:
        pos = p["pos"]
        if pos in deduped_map and dedupe == "error":
            raise MusicalUnitError(f"Duplicate point at tick {pos}", code="DUPLICATE_AUTOMATION_POINT")
        deduped_map[pos] = p["value"]

    final_points = [{"pos": pos, "value": val} for pos, val in sorted(deduped_map.items())]

    if apply:
        for te in pattern.findall("time"):
            pattern.remove(te)
        max_pos = 0
        for pt in final_points:
            ET.SubElement(pattern, "time", {
                "pos": str(pt["pos"]),
                "value": str(pt["value"]),
                "outValue": str(pt["value"]),
                "inTan": "0",
                "outTan": "0",
                "lockedTan": "0",
            })
            max_pos = max(max_pos, pt["pos"])
        pattern.set("len", str(max(192, max_pos + 1)))
        project._modified = True

    plan = {
        "curve_index": curve_index,
        "points": final_points,
    }
    return {"ok": True, "applied": apply, "plan": plan}
