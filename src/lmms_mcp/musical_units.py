"""Pure deterministic musical parsing, validation, and transformation engine.

No XML manipulation, no I/O, no project mutation.
Implements Issue #11 contracts:
- Pitch parsing with enharmonic & cross-octave support
- Strict timebase conversions with round-half-up
- Closed, deterministic chord dictionary with inversions, slash bass, voicings
- Phrase, progression, and rhythm pattern parsing
- Pure mathematical transformations (transpose, shift, stretch, reverse, quantize, velocity)
- Structured MusicalUnitError with stable error codes
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

# =====================================================================
# ERROR HANDLING
# =====================================================================

class MusicalUnitError(ValueError):
    """Structured musical validation and parsing error."""

    def __init__(
        self,
        message: str,
        code: str,
        field: Optional[str] = None,
        accepted: Optional[list[Any]] = None,
        value: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.field = field
        self.accepted = accepted
        self.value = value

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "ok": False,
            "error": self.message,
            "code": self.code,
        }
        if self.field is not None:
            d["field"] = self.field
        if self.value is not None:
            d["value"] = self.value
        if self.accepted is not None:
            d["accepted"] = self.accepted
        return d


# =====================================================================
# TIMEBASE CONSTANTS AND ROUNDING
# =====================================================================

TICKS_PER_BAR = 192
TICKS_PER_BEAT = 48
TICKS_PER_STEP = 12


def round_half_up(x: float) -> int:
    """Round half away from zero (standard commercial / mathematical rounding)."""
    if x >= 0:
        return int(math.floor(x + 0.5))
    else:
        return int(math.ceil(x - 0.5))


def parse_time(
    value: Union[int, float],
    unit: str = "ticks",
    field: Optional[str] = None,
) -> int:
    """Parse time value into exact integer ticks."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MusicalUnitError(
            f"Invalid time value {value!r}: must be numeric",
            code="INVALID_TIME",
            field=field,
            value=value,
        )

    if math.isnan(value) or math.isinf(value):
        raise MusicalUnitError(
            f"Invalid time value {value!r}: cannot be NaN or infinity",
            code="INVALID_TIME",
            field=field,
            value=value,
        )

    if value < 0:
        raise MusicalUnitError(
            f"Time value cannot be negative: {value}",
            code="NEGATIVE_TIME",
            field=field,
            value=value,
        )

    u = unit.lower().strip()
    if u == "ticks":
        if isinstance(value, float) and not value.is_integer():
            raise MusicalUnitError(
                f"Tick value {value} must be an integer",
                code="INVALID_TIME",
                field=field,
                value=value,
            )
        ticks = int(value)
    elif u == "bars":
        ticks = round_half_up(value * TICKS_PER_BAR)
    elif u == "beats":
        ticks = round_half_up(value * TICKS_PER_BEAT)
    elif u == "steps":
        ticks = round_half_up(value * TICKS_PER_STEP)
    else:
        raise MusicalUnitError(
            f"Unknown time unit {unit!r}",
            code="INVALID_TIME",
            field=field,
            accepted=["ticks", "bars", "beats", "steps"],
            value=unit,
        )

    if ticks < 0:
        raise MusicalUnitError(
            f"Time value cannot be negative: {ticks} ticks",
            code="NEGATIVE_TIME",
            field=field,
            value=value,
        )

    return ticks


def bars_to_ticks(bars: float) -> int:
    """Convert bars float to integer ticks using round-half-up."""
    return parse_time(bars, unit="bars")


def ticks_to_bars(ticks: int) -> float:
    """Convert integer ticks to floating-point bars."""
    return ticks / TICKS_PER_BAR


DURATION_TOKENS: dict[str, int] = {
    "1/1": 192,
    "1/2": 96,
    "1/4": 48,
    "1/8": 24,
    "1/16": 12,
    "1/32": 6,
    "1/8T": 16,
    "1/16T": 8,
    "1/4.": 72,
    "1/8.": 36,
}


def parse_duration(token: str, field: Optional[str] = None) -> int:
    """Parse rhythmic duration string (e.g. '1/4', '1/8T', '1/4.') to ticks."""
    t = token.strip()
    if t in DURATION_TOKENS:
        return DURATION_TOKENS[t]
    raise MusicalUnitError(
        f"Unknown duration token {token!r}",
        code="INVALID_TIME",
        field=field,
        accepted=sorted(DURATION_TOKENS.keys()),
        value=token,
    )


# =====================================================================
# PITCH PARSING AND ENHARMONICS
# =====================================================================

NOTE_NAMES_SHARP = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
NOTE_NAMES_FLAT = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]

NATURAL_PITCH_CLASS: dict[str, int] = {
    "C": 0,
    "D": 2,
    "E": 4,
    "F": 5,
    "G": 7,
    "A": 9,
    "B": 11,
}

PITCH_RE = re.compile(r"^([A-Ga-g])([#b♭♯]?)(-?\d+)$")


def parse_pitch(pitch_input: Union[int, str], field: Optional[str] = None) -> int:
    """Parse note name or integer into a valid MIDI key (0..127).

    Supports sharps (#, ♯) and flats (b, ♭), case-insensitive letter,
    and cross-octave enharmonics (e.g. Cb4 -> B3=59, B#3 -> C4=60, Fb4 -> E4=64, E#4 -> F4=65).
    """
    if isinstance(pitch_input, bool) or pitch_input is None:
        raise MusicalUnitError(
            f"Invalid pitch input: {pitch_input!r}",
            code="INVALID_PITCH",
            field=field,
            value=pitch_input,
        )

    if isinstance(pitch_input, float):
        raise MusicalUnitError(
            f"Float pitch {pitch_input!r} not accepted; use int or note name",
            code="INVALID_PITCH",
            field=field,
            value=pitch_input,
        )

    if isinstance(pitch_input, int):
        if not 0 <= pitch_input <= 127:
            raise MusicalUnitError(
                f"MIDI pitch {pitch_input} out of range [0, 127]",
                code="PITCH_OUT_OF_RANGE",
                field=field,
                value=pitch_input,
            )
        return pitch_input

    if not isinstance(pitch_input, str):
        raise MusicalUnitError(
            f"Invalid pitch type {type(pitch_input)}",
            code="INVALID_PITCH",
            field=field,
            value=pitch_input,
        )

    val = pitch_input.strip()
    if not val:
        raise MusicalUnitError("Empty note name", code="INVALID_NOTE_NAME", field=field, value=val)

    # Check for numeric string (e.g. "60") -> ambiguous/invalid
    if re.match(r"^-?\d+$", val):
        raise MusicalUnitError(
            f"Numeric string {pitch_input!r} is ambiguous; use integer for MIDI key",
            code="INVALID_PITCH",
            field=field,
            value=pitch_input,
        )

    # Check for bare note name without octave (e.g. "C", "C#", "bb") -> ambiguous pitch
    if re.match(r"^[A-Ga-g][#b♭♯]?$", val):
        raise MusicalUnitError(
            f"Bare note name {pitch_input!r} without octave is ambiguous",
            code="AMBIGUOUS_PITCH",
            field=field,
            value=pitch_input,
        )

    m = PITCH_RE.match(val)
    if not m:
        raise MusicalUnitError(
            f"Invalid note name {pitch_input!r}",
            code="INVALID_NOTE_NAME",
            field=field,
            value=pitch_input,
        )

    letter = m.group(1).upper()
    acc = m.group(2).replace("♯", "#").replace("♭", "b")
    octave = int(m.group(3))

    # Base natural pitch for the letter and octave
    base_natural = (octave + 1) * 12 + NATURAL_PITCH_CLASS[letter]

    # Delta for accidental
    delta = 0
    if acc == "#":
        delta = 1
    elif acc == "b":
        delta = -1

    midi_key = base_natural + delta
    if not 0 <= midi_key <= 127:
        raise MusicalUnitError(
            f"Pitch {pitch_input!r} (MIDI {midi_key}) out of range [0, 127]",
            code="PITCH_OUT_OF_RANGE",
            field=field,
            value=pitch_input,
        )

    return midi_key


def note_name_to_midi(name: str) -> int:
    """Wrapper matching models.py interface."""
    return parse_pitch(name)


def midi_to_note_name(midi_key: int, spelling: Literal["sharps", "flats"] = "sharps") -> str:
    """Convert MIDI key (0..127) to note name with octave."""
    if isinstance(midi_key, bool) or not isinstance(midi_key, int) or not (0 <= midi_key <= 127):
        raise MusicalUnitError(
            f"MIDI key {midi_key} out of range [0, 127]",
            code="PITCH_OUT_OF_RANGE",
            value=midi_key,
        )
    octave = (midi_key // 12) - 1
    pc = midi_key % 12
    table = NOTE_NAMES_FLAT if spelling == "flats" else NOTE_NAMES_SHARP
    return f"{table[pc]}{octave}"


# =====================================================================
# CHORD DICTIONARY AND SYMBOL PARSING
# =====================================================================

CHORD_QUALITIES: dict[str, list[int]] = {
    "maj": [0, 4, 7],
    "min": [0, 3, 7],
    "dim": [0, 3, 6],
    "aug": [0, 4, 8],
    "5": [0, 7],
    "sus2": [0, 2, 7],
    "sus4": [0, 5, 7],
    "6": [0, 4, 7, 9],
    "min6": [0, 3, 7, 9],
    "7": [0, 4, 7, 10],
    "maj7": [0, 4, 7, 11],
    "min7": [0, 3, 7, 10],
    "m7b5": [0, 3, 6, 10],
    "dim7": [0, 3, 6, 9],
    "mMaj7": [0, 3, 7, 11],
    "add9": [0, 4, 7, 14],
    "9": [0, 4, 7, 10, 14],
    "maj9": [0, 4, 7, 11, 14],
    "min9": [0, 3, 7, 10, 14],
    "11": [0, 4, 7, 10, 14, 17],
    "13": [0, 4, 7, 10, 14, 21],
    "aug7": [0, 4, 8, 10],
    "7sus4": [0, 5, 7, 10],
}

CHORD_ALIASES: dict[str, str] = {
    "m": "min",
    "minor": "min",
    "major": "maj",
    "m7": "min7",
}

# Strict chord symbol regex: Root(letter+acc+octave[-1..9]) + Quality + optional Slash(/letter+acc+octave)
CHORD_REGEX = re.compile(
    r"^([A-Ga-g][#b♭♯]?(?:-1|[0-9]))"
    r"([A-Za-z0-9#b♭♯]*?)"
    r"(?:/([A-Ga-g][#b♭♯]?(?:-1|[0-9])))?$"
)


@dataclass
class ChordSpec:
    symbol: str
    root: str
    octave: int
    quality: str
    slash_bass: Optional[str] = None
    inversion: int = 0
    voicing: str = "close"
    octave_shift: int = 0
    notes: list[dict] = field(default_factory=list)

    def canonical(self) -> str:
        s = f"{self.root}{self.octave}{self.quality}"
        if self.slash_bass:
            s += f"/{self.slash_bass}"
        return s

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "canonical": self.canonical(),
            "root": self.root,
            "octave": self.octave,
            "quality": self.quality,
            "slash_bass": self.slash_bass,
            "inversion": self.inversion,
            "voicing": self.voicing,
            "octave_shift": self.octave_shift,
        }


def parse_chord_symbol(
    symbol: str,
    inversion: int = 0,
    voicing: str = "close",
    octave_shift: int = 0,
    field: Optional[str] = None,
) -> ChordSpec:
    """Parse chord symbol deterministically with required root octave."""
    if not isinstance(symbol, str) or not symbol.strip():
        raise MusicalUnitError(
            f"Invalid chord symbol {symbol!r}",
            code="INVALID_CHORD_SYMBOL",
            field=field,
            value=symbol,
        )

    val = symbol.strip()

    # Reject bare root (e.g. "C")
    if re.match(r"^[A-Ga-g][#b♭♯]?$", val):
        raise MusicalUnitError(
            f"Bare root note {symbol!r} without quality or octave is ambiguous",
            code="AMBIGUOUS_CHORD",
            field=field,
            value=symbol,
        )

    m = CHORD_REGEX.match(val)
    if not m:
        raise MusicalUnitError(
            f"Invalid chord symbol {symbol!r}",
            code="INVALID_CHORD_SYMBOL",
            field=field,
            value=symbol,
        )

    root_token = m.group(1)
    quality_token = m.group(2) or ""
    slash_token = m.group(3)

    if not quality_token:
        raise MusicalUnitError(
            f"Chord {symbol!r} has no quality",
            code="AMBIGUOUS_CHORD",
            field=field,
            value=symbol,
        )

    # Validate root note
    root_pitch = parse_pitch(root_token, field=field)
    root_m = PITCH_RE.match(root_token)
    root_letter = root_m.group(1).upper() + root_m.group(2).replace("♯", "#").replace("♭", "b")
    root_oct = int(root_m.group(3))

    # Match quality
    if quality_token in CHORD_ALIASES:
        canonical_quality = CHORD_ALIASES[quality_token]
    elif quality_token in CHORD_QUALITIES:
        canonical_quality = quality_token
    else:
        raise MusicalUnitError(
            f"Unknown chord quality {quality_token!r} for symbol {symbol!r}",
            code="UNKNOWN_CHORD_QUALITY",
            field=field,
            accepted=sorted(CHORD_QUALITIES.keys()),
            value=symbol,
        )

    intervals = CHORD_QUALITIES[canonical_quality]

    # Validate inversion
    if inversion < 0 or inversion >= len(intervals):
        raise MusicalUnitError(
            f"Inversion {inversion} out of range [0, {len(intervals) - 1}]",
            code="INVALID_INVERSION",
            field=field,
            value=inversion,
        )

    # Validate voicing
    if voicing not in ("close", "drop2", "drop3"):
        raise MusicalUnitError(
            f"Invalid voicing {voicing!r}",
            code="INVALID_VOICING",
            field=field,
            accepted=["close", "drop2", "drop3"],
            value=voicing,
        )

    # Validate slash bass if given
    if slash_token:
        parse_pitch(slash_token, field=field)

    spec = ChordSpec(
        symbol=symbol,
        root=root_letter,
        octave=root_oct,
        quality=canonical_quality,
        slash_bass=slash_token,
        inversion=inversion,
        voicing=voicing,
        octave_shift=octave_shift,
    )
    return spec


def chord_pitches(
    spec: ChordSpec,
    spelling: Literal["as_written", "sharps", "flats"] = "as_written",
) -> list[int]:
    """Calculate exact ordered MIDI pitches for a ChordSpec."""
    root_pitch = parse_pitch(f"{spec.root}{spec.octave}")
    intervals = CHORD_QUALITIES[spec.quality]

    # 1. Base pitches
    pitches = [root_pitch + iv for iv in intervals]

    # 2. Inversion (rotate lowest voices up 12)
    if spec.inversion > 0:
        for i in range(spec.inversion):
            pitches[i] += 12
        pitches.sort()

    # 3. Octave shift
    if spec.octave_shift != 0:
        pitches = [p + 12 * spec.octave_shift for p in pitches]

    # 4. Voicing
    if spec.voicing == "drop2" and len(pitches) >= 2:
        # Lower 2nd highest note by 12
        idx = len(pitches) - 2
        pitches[idx] -= 12
        pitches.sort()
    elif spec.voicing == "drop3" and len(pitches) >= 3:
        # Lower 3rd highest note by 12
        idx = len(pitches) - 3
        pitches[idx] -= 12
        pitches.sort()

    # 5. Ascending sort
    pitches.sort()

    # 6. Slash bass
    if spec.slash_bass:
        bass_pitch = parse_pitch(spec.slash_bass)
        other_pitches = [p for p in pitches if p != bass_pitch]
        adjusted = []
        for p in other_pitches:
            val = p
            while val <= bass_pitch:
                val += 12
            adjusted.append(val)
        pitches = [bass_pitch] + sorted(adjusted)

    # Validate range
    for p in pitches:
        if not 0 <= p <= 127:
            raise MusicalUnitError(
                f"Resulting chord pitch {p} out of MIDI range [0, 127]",
                code="PITCH_OUT_OF_RANGE",
                value=p,
            )

    return sorted(pitches)


def chord_names(
    spec: ChordSpec,
    spelling: Literal["sharps", "flats"] = "sharps",
) -> list[str]:
    """Return note names for the chord pitches."""
    pitches = chord_pitches(spec)
    return [midi_to_note_name(p, spelling=spelling) for p in pitches]


def parse_chord_spec(chord_dict: dict[str, Any], field: Optional[str] = None) -> ChordSpec:
    """Parse chord specification dict with either 'symbol' or 'pitches'."""
    has_sym = "symbol" in chord_dict and chord_dict["symbol"] is not None
    has_pitches = "pitches" in chord_dict and chord_dict["pitches"] is not None

    if has_sym and has_pitches:
        raise MusicalUnitError(
            "Cannot specify both 'symbol' and 'pitches' in chord spec",
            code="INVALID_CHORD_SYMBOL",
            field=field,
        )

    if not has_sym and not has_pitches:
        raise MusicalUnitError(
            "Chord spec must contain either 'symbol' or 'pitches'",
            code="INVALID_CHORD_SYMBOL",
            field=field,
        )

    inv = chord_dict.get("inversion", 0)
    voicing = chord_dict.get("voicing", "close")
    oct_shift = chord_dict.get("octave_shift", 0)
    at_ticks = parse_time(chord_dict.get("at", 0), unit="ticks", field=field)
    length_ticks = parse_time(chord_dict.get("length", 48), unit="ticks", field=field)
    volume = chord_dict.get("volume", 100)
    if isinstance(volume, bool) or not isinstance(volume, int) or not (0 <= volume <= 200):
        raise MusicalUnitError(f"Chord volume {volume!r} out of range [0, 200]", code="INVALID_VELOCITY", field=field, value=volume)

    panning = chord_dict.get("panning", 0)
    if isinstance(panning, bool) or not isinstance(panning, int) or not (-100 <= panning <= 100):
        raise MusicalUnitError(f"Chord panning {panning!r} out of range [-100, 100]", code="INVALID_PANNING", field=field, value=panning)

    if has_sym:
        spec = parse_chord_symbol(
            chord_dict["symbol"],
            inversion=inv,
            voicing=voicing,
            octave_shift=oct_shift,
            field=field,
        )
        pitches = chord_pitches(spec)
    else:
        raw_pitches = chord_dict["pitches"]
        if not raw_pitches:
            raise MusicalUnitError("Empty pitches list", code="EMPTY_UNIT", field=field)
        pitches = [parse_pitch(p, field=field) for p in raw_pitches]
        pitches.sort()
        spec = ChordSpec(
            symbol="explicit",
            root=midi_to_note_name(pitches[0]),
            octave=pitches[0] // 12 - 1,
            quality="explicit",
            inversion=0,
            voicing="close",
        )

    notes = []
    for p in pitches:
        notes.append({
            "key": p,
            "pos": at_ticks,
            "length": length_ticks,
            "volume": volume,
            "panning": panning,
            "note_name": midi_to_note_name(p),
        })
    spec.notes = notes
    return spec


# =====================================================================
# PHRASE AND MOTIF PARSING
# =====================================================================

@dataclass
class PhraseNote:
    key: int
    pos: int
    length: int
    volume: int = 100
    panning: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "pos": self.pos,
            "length": self.length,
            "volume": self.volume,
            "panning": self.panning,
            "note_name": midi_to_note_name(self.key),
        }


@dataclass
class Phrase:
    name: str
    length_ticks: int
    notes: list[PhraseNote]
    rests: list[tuple[int, int]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def notes_as_dicts(self) -> list[dict[str, Any]]:
        return [n.to_dict() for n in self.notes]


def parse_phrase(phrase_dict: dict[str, Any], field: Optional[str] = None) -> Phrase:
    """Parse phrase specification dict with relative note positions and validation."""
    if not isinstance(phrase_dict, dict):
        raise MusicalUnitError("Phrase must be a dict", code="INVALID_PHRASE", field=field)

    name = phrase_dict.get("name", "phrase")
    raw_notes = phrase_dict.get("notes")
    if raw_notes is None or not isinstance(raw_notes, list) or len(raw_notes) == 0:
        raise MusicalUnitError(
            "Phrase notes cannot be empty",
            code="EMPTY_UNIT",
            field=f"{field}.notes" if field else "notes",
        )

    parsed_notes: list[PhraseNote] = []
    for idx, nd in enumerate(raw_notes):
        f = f"notes[{idx}]"
        # Check required fields: key (or pitch), pos, length
        has_key = "key" in nd and nd["key"] is not None
        has_pitch = "pitch" in nd and nd["pitch"] is not None
        if has_key and has_pitch:
            raise MusicalUnitError(f"Cannot specify both 'key' and 'pitch' in note {idx}", code="INVALID_PITCH", field=f)
        if not has_key and not has_pitch:
            raise MusicalUnitError(f"Note {idx} missing 'key'", code="INVALID_PITCH", field=f)
        if "pos" not in nd:
            raise MusicalUnitError(f"Note {idx} missing 'pos'", code="INVALID_TIME", field=f)
        if "length" not in nd:
            raise MusicalUnitError(f"Note {idx} missing 'length'", code="INVALID_TIME", field=f)

        k = parse_pitch(nd["key"] if has_key else nd["pitch"], field=f)
        p = parse_time(nd["pos"], unit="ticks", field=f)
        l = parse_time(nd["length"], unit="ticks", field=f)
        if l < 1:
            raise MusicalUnitError(f"Note {idx} length must be >= 1", code="INVALID_TIME", field=f)

        vol = nd.get("volume", 100)
        if isinstance(vol, bool) or not isinstance(vol, int) or not (0 <= vol <= 200):
            raise MusicalUnitError(
                f"Note {idx} volume {vol!r} out of range [0, 200]",
                code="INVALID_VELOCITY",
                field=f,
                value=vol,
            )

        pan = nd.get("panning", 0)
        if isinstance(pan, bool) or not isinstance(pan, int) or not (-100 <= pan <= 100):
            raise MusicalUnitError(
                f"Note {idx} panning {pan!r} out of range [-100, 100]",
                code="INVALID_PANNING",
                field=f,
                value=pan,
            )

        parsed_notes.append(PhraseNote(key=k, pos=p, length=l, volume=vol, panning=pan))

    # Sort notes by pos, key, length
    parsed_notes.sort(key=lambda n: (n.pos, n.key, n.length))

    calc_len = max(n.pos + n.length for n in parsed_notes)
    if "length_ticks" in phrase_dict:
        declared_len = parse_time(phrase_dict["length_ticks"], unit="ticks", field="length_ticks")
        if calc_len > declared_len:
            raise MusicalUnitError(
                f"Phrase notes end at {calc_len} ticks exceeding declared length {declared_len}",
                code="CLIP_TOO_SHORT",
                field="length_ticks",
                value=declared_len,
            )
        total_len = declared_len
    else:
        total_len = calc_len

    # Parse optional rests
    parsed_rests = []
    if "rests" in phrase_dict and phrase_dict["rests"]:
        for idx, r in enumerate(phrase_dict["rests"]):
            r_at = parse_time(r.get("at", 0), unit="ticks")
            r_len = parse_time(r.get("length", 48), unit="ticks")
            # Check overlap with any note
            for n in parsed_notes:
                # Overlap test: max(r_at, n.pos) < min(r_at + r_len, n.pos + n.length)
                if max(r_at, n.pos) < min(r_at + r_len, n.pos + n.length):
                    raise MusicalUnitError(
                        f"Rest at {r_at} (len {r_len}) overlaps note at {n.pos} (len {n.length})",
                        code="OVERLAP_CONFLICT",
                    )
            parsed_rests.append((r_at, r_len))

    # Check same-pitch overlaps for warnings
    warnings = []
    for i in range(len(parsed_notes)):
        for j in range(i + 1, len(parsed_notes)):
            n1 = parsed_notes[i]
            n2 = parsed_notes[j]
            if n1.key == n2.key and max(n1.pos, n2.pos) < min(n1.pos + n1.length, n2.pos + n2.length):
                warnings.append(f"same-pitch overlap on key {n1.key} at ticks {n1.pos} and {n2.pos}")

    return Phrase(
        name=name,
        length_ticks=total_len,
        notes=parsed_notes,
        rests=parsed_rests,
        warnings=warnings,
    )


# =====================================================================
# CHORD PROGRESSION PARSING
# =====================================================================

@dataclass
class ProgressionChord:
    spec: ChordSpec
    at_ticks: int
    duration_ticks: int
    notes: list[dict]


@dataclass
class Progression:
    name: str
    length_ticks: int
    chords: list[ProgressionChord]


def parse_progression(prog_dict: dict[str, Any], field: Optional[str] = None) -> Progression:
    """Parse chord progression with sequential or absolute layout."""
    if not isinstance(prog_dict, dict):
        raise MusicalUnitError("Progression must be a dict", code="INVALID_PROGRESSION", field=field)

    name = prog_dict.get("name", "progression")
    raw_chords = prog_dict.get("chords")
    if not raw_chords or not isinstance(raw_chords, list):
        raise MusicalUnitError("Chord progression cannot be empty", code="EMPTY_UNIT", field=field)

    layout = prog_dict.get("layout", "sequential")
    # Verify no mixing of 'bars' / 'duration' with 'at' unless allowed
    has_at = any("at" in c for c in raw_chords)
    has_bars = any("bars" in c or "duration" in c for c in raw_chords)
    if has_at and has_bars:
        raise MusicalUnitError(
            "Cannot mix duration ('bars') and absolute ('at') positioning in progression chords",
            code="INVALID_TIME",
            field=field,
        )

    parsed_chords: list[ProgressionChord] = []
    current_tick = 0
    for idx, c in enumerate(raw_chords):
        f = f"chords[{idx}]"
        # Check for explicit rest entry: {"rest": bars}
        if "rest" in c:
            rest_bars = c["rest"]
            rest_ticks = parse_time(rest_bars, unit="bars", field=f)
            current_tick += rest_ticks
            continue

        if "symbol" in c:
            spec = parse_chord_symbol(c["symbol"], field=f)
            pitches = chord_pitches(spec)
        elif "pitches" in c:
            spec = parse_chord_spec(c, field=f)
            pitches = chord_pitches(spec)
        else:
            raise MusicalUnitError(f"Chord {idx} missing 'symbol' or 'pitches'", code="INVALID_CHORD_SYMBOL", field=f)

        if "at" in c:
            chord_at = parse_time(c["at"], unit="ticks", field=f)
            dur = parse_time(c.get("length", 192), unit="ticks", field=f)
        else:
            chord_at = current_tick
            bars = c.get("bars", 1)
            dur = parse_time(bars, unit="bars", field=f)
            current_tick += dur

        vol = c.get("volume", 100)
        pan = c.get("panning", 0)

        notes = []
        for p in pitches:
            notes.append({
                "key": p,
                "pos": 0,  # relative to occurrence clip
                "length": dur,
                "volume": vol,
                "panning": pan,
                "note_name": midi_to_note_name(p),
            })

        parsed_chords.append(ProgressionChord(
            spec=spec,
            at_ticks=chord_at,
            duration_ticks=dur,
            notes=notes,
        ))

    total_len = max((c.at_ticks + c.duration_ticks for c in parsed_chords), default=192)
    return Progression(name=name, length_ticks=total_len, chords=parsed_chords)


# =====================================================================
# RHYTHM PATTERNS
# =====================================================================

DRUM_MAP: dict[str, int] = {
    "kick": 36,
    "side_stick": 37,
    "snare": 38,
    "clap": 39,
    "closed_hat": 42,
    "pedal_hat": 44,
    "open_hat": 46,
    "low_tom": 45,
    "mid_tom": 47,
    "high_tom": 50,
    "crash": 49,
    "ride": 51,
    "rim": 37,
    "cowbell": 56,
}


@dataclass
class RhythmHit:
    step: int
    pos: int
    key: int
    length: int
    volume: int
    panning: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "pos": self.pos,
            "key": self.key,
            "length": self.length,
            "volume": self.volume,
            "panning": self.panning,
            "note_name": midi_to_note_name(self.key),
        }


@dataclass
class RhythmPattern:
    name: str
    steps: int
    step_ticks: int
    length_ticks: int
    hits: list[RhythmHit]

    def as_dicts(self) -> list[dict[str, Any]]:
        return [h.to_dict() for h in self.hits]


def parse_rhythm_pattern(pat_dict: dict[str, Any], field: Optional[str] = None) -> RhythmPattern:
    """Parse rhythm pattern specification dict."""
    if not isinstance(pat_dict, dict):
        raise MusicalUnitError("Rhythm pattern must be a dict", code="INVALID_RHYTHM", field=field)

    name = pat_dict.get("name", "rhythm")
    steps = pat_dict.get("steps")
    if steps is None or not isinstance(steps, int) or steps < 1:
        raise MusicalUnitError(
            f"Invalid steps count {steps!r}; must be integer >= 1",
            code="INVALID_STEP",
            field="steps",
            value=steps,
        )

    step_ticks = pat_dict.get("step_ticks", TICKS_PER_STEP)
    raw_hits = pat_dict.get("hits")
    if not raw_hits or not isinstance(raw_hits, list):
        raise MusicalUnitError("Hits cannot be empty", code="EMPTY_UNIT", field="hits")

    parsed_hits: list[RhythmHit] = []
    seen_steps_keys: set[tuple[int, int]] = set()

    for idx, hit in enumerate(raw_hits):
        f = f"hits[{idx}]"
        step = hit.get("step")
        if step is None or not isinstance(step, int) or not (0 <= step < steps):
            raise MusicalUnitError(
                f"Hit step {step!r} out of range [0, {steps - 1}]",
                code="INVALID_STEP",
                field=f,
                value=step,
            )

        # Resolve pitch: explicit pitch overrides drum
        if "pitch" in hit and hit["pitch"] is not None:
            key = parse_pitch(hit["pitch"], field=f)
        elif "drum" in hit and hit["drum"] is not None:
            drum_name = hit["drum"].lower().strip()
            if drum_name not in DRUM_MAP:
                raise MusicalUnitError(
                    f"Unknown drum name {hit['drum']!r}",
                    code="UNKNOWN_DRUM",
                    field=f,
                    accepted=sorted(DRUM_MAP.keys()),
                    value=hit["drum"],
                )
            key = DRUM_MAP[drum_name]
        else:
            raise MusicalUnitError(f"Hit {idx} missing 'drum' or 'pitch'", code="INVALID_PITCH", field=f)

        # Check duplicate hit
        if (step, key) in seen_steps_keys:
            raise MusicalUnitError(
                f"Duplicate hit at step {step} for key {key}",
                code="DUPLICATE_HIT",
                field=f,
            )
        seen_steps_keys.add((step, key))

        len_steps = hit.get("length_steps", 1)
        if isinstance(len_steps, bool) or not isinstance(len_steps, int) or len_steps < 1:
            raise MusicalUnitError(f"Hit length_steps {len_steps!r} must be an integer >= 1", code="INVALID_TIME", field=f, value=len_steps)

        length = len_steps * step_ticks
        pos = step * step_ticks

        vol = hit.get("velocity", hit.get("volume", 100))
        if isinstance(vol, bool) or not isinstance(vol, int) or not (0 <= vol <= 200):
            raise MusicalUnitError(f"Hit velocity {vol!r} out of range [0, 200]", code="INVALID_VELOCITY", field=f, value=vol)

        pan = hit.get("panning", 0)
        if isinstance(pan, bool) or not isinstance(pan, int) or not (-100 <= pan <= 100):
            raise MusicalUnitError(f"Hit panning {pan!r} out of range [-100, 100]", code="INVALID_PANNING", field=f, value=pan)

        parsed_hits.append(RhythmHit(
            step=step,
            pos=pos,
            key=key,
            length=length,
            volume=vol,
            panning=pan,
        ))

    parsed_hits.sort(key=lambda h: (h.pos, h.key))
    total_len = steps * step_ticks

    return RhythmPattern(
        name=name,
        steps=steps,
        step_ticks=step_ticks,
        length_ticks=total_len,
        hits=parsed_hits,
    )


# =====================================================================
# PURE TRANSFORMATIONS
# =====================================================================

def transpose_notes(notes: list[dict[str, Any]], semitones: int) -> list[dict[str, Any]]:
    """Pure exact transposition of note dictionaries."""
    if isinstance(semitones, bool) or not isinstance(semitones, int):
        raise MusicalUnitError(f"Transpose {semitones!r} must be an integer", code="INVALID_PITCH", value=semitones)

    result = []
    for n in notes:
        new_n = copy.deepcopy(n)
        new_key = new_n["key"] + semitones
        if not 0 <= new_key <= 127:
            raise MusicalUnitError(
                f"Transposed key {new_key} out of MIDI range [0, 127]",
                code="PITCH_OUT_OF_RANGE",
                value=new_key,
            )
        new_n["key"] = new_key
        if "note_name" in new_n:
            new_n["note_name"] = midi_to_note_name(new_key)
        result.append(new_n)
    return result


def shift_notes(notes: list[dict[str, Any]], ticks: int) -> list[dict[str, Any]]:
    """Pure exact time shift in ticks."""
    if isinstance(ticks, bool) or not isinstance(ticks, int):
        raise MusicalUnitError(f"Time shift {ticks!r} must be an integer", code="INVALID_TIME", value=ticks)

    result = []
    for n in notes:
        new_n = copy.deepcopy(n)
        new_pos = new_n["pos"] + ticks
        if new_pos < 0:
            raise MusicalUnitError(f"Negative note position {new_pos}", code="NEGATIVE_TIME", value=new_pos)
        new_n["pos"] = new_pos
        result.append(new_n)
    return result


def stretch_notes(notes: list[dict[str, Any]], ratio: float) -> list[dict[str, Any]]:
    """Pure time stretch preserving boundary spans without cumulative drift."""
    if (
        isinstance(ratio, bool)
        or not isinstance(ratio, (int, float))
        or math.isnan(ratio)
        or math.isinf(ratio)
        or ratio <= 0
    ):
        raise MusicalUnitError(f"Invalid stretch ratio {ratio!r}", code="INVALID_RATIO", value=ratio)

    result = []
    for n in notes:
        new_n = copy.deepcopy(n)
        pos = new_n["pos"]
        length = new_n["length"]
        new_pos = round_half_up(pos * ratio)
        new_end = round_half_up((pos + length) * ratio)
        new_len = max(1, new_end - new_pos)
        new_n["pos"] = new_pos
        new_n["length"] = new_len
        result.append(new_n)
    return result


def reverse_notes(notes: list[dict[str, Any]], span_ticks: int) -> list[dict[str, Any]]:
    """Pure exact reversal within a fixed span_ticks."""
    if isinstance(span_ticks, bool) or not isinstance(span_ticks, int) or span_ticks < 1:
        raise MusicalUnitError(f"Invalid span_ticks {span_ticks!r}", code="INVALID_TIME", value=span_ticks)

    result = []
    for n in notes:
        new_n = copy.deepcopy(n)
        pos = new_n["pos"]
        length = new_n["length"]
        new_pos = span_ticks - (pos + length)
        if new_pos < 0:
            raise MusicalUnitError(f"Reversed pos {new_pos} outside span", code="NEGATIVE_TIME", value=new_pos)
        new_n["pos"] = new_pos
        result.append(new_n)
    # Sort deterministically
    result.sort(key=lambda n: (n["pos"], n["key"], n["length"]))
    return result


def quantize_notes(
    notes: list[dict[str, Any]],
    grid_ticks: int,
    mode: Literal["start", "start+length", "end"] = "start",
) -> list[dict[str, Any]]:
    """Pure quantization snapping to positive integer grid_ticks."""
    if isinstance(grid_ticks, bool) or not isinstance(grid_ticks, int) or grid_ticks <= 0:
        raise MusicalUnitError(f"Invalid quantize grid {grid_ticks!r}", code="INVALID_GRID", value=grid_ticks)

    result = []
    for n in notes:
        new_n = copy.deepcopy(n)
        pos = new_n["pos"]
        length = new_n["length"]
        end = pos + length

        if mode == "start":
            new_pos = round_half_up(pos / grid_ticks) * grid_ticks
            new_n["pos"] = new_pos
        elif mode == "start+length":
            new_pos = round_half_up(pos / grid_ticks) * grid_ticks
            new_end = round_half_up(end / grid_ticks) * grid_ticks
            new_len = max(grid_ticks, new_end - new_pos)
            new_n["pos"] = new_pos
            new_n["length"] = new_len
        elif mode == "end":
            new_end = round_half_up(end / grid_ticks) * grid_ticks
            new_len = max(1, new_end - pos)
            new_n["length"] = new_len
        else:
            raise MusicalUnitError(f"Unknown quantize mode {mode!r}", code="INVALID_GRID", value=mode)

        result.append(new_n)

    result.sort(key=lambda n: (n["pos"], n["key"], n["length"]))
    return result


def scale_velocity(
    notes: list[dict[str, Any]],
    multiplier: float = 1.0,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Pure velocity scaling and offset: round_half_up(vol * multiplier) + offset, clamped 0..200."""
    if (
        isinstance(multiplier, bool)
        or not isinstance(multiplier, (int, float))
        or math.isnan(multiplier)
        or math.isinf(multiplier)
        or multiplier < 0
    ):
        raise MusicalUnitError(f"Invalid velocity multiplier {multiplier!r}", code="INVALID_VELOCITY", value=multiplier)

    if isinstance(offset, bool) or not isinstance(offset, int):
        raise MusicalUnitError(f"Invalid velocity offset {offset!r}", code="INVALID_VELOCITY", value=offset)

    result = []
    for n in notes:
        new_n = copy.deepcopy(n)
        vol = new_n.get("volume", 100)
        new_vol = round_half_up(vol * multiplier) + offset
        clamped_vol = max(0, min(200, new_vol))
        new_n["volume"] = clamped_vol
        result.append(new_n)
    return result


def apply_transforms(
    notes: list[dict[str, Any]],
    transforms: dict[str, Any],
    span_ticks: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Apply transformation pipeline in fixed deterministic order.

    Order: transpose -> time_shift -> stretch -> reverse -> quantize -> velocity_scale.
    """
    if not transforms:
        return copy.deepcopy(notes)

    cur = copy.deepcopy(notes)

    # 1. Transpose
    if "transpose" in transforms and transforms["transpose"] != 0:
        cur = transpose_notes(cur, transforms["transpose"])

    # 2. Time shift
    if "time_shift" in transforms and transforms["time_shift"] != 0:
        cur = shift_notes(cur, transforms["time_shift"])

    # 3. Time stretch
    if "stretch" in transforms and transforms["stretch"] != 1.0:
        cur = stretch_notes(cur, transforms["stretch"])

    # 4. Reverse
    if transforms.get("reverse", False):
        span = span_ticks or (max((n["pos"] + n["length"] for n in cur), default=192))
        cur = reverse_notes(cur, span_ticks=span)

    # 5. Quantize
    if "quantize" in transforms and transforms["quantize"] is not None:
        grid = transforms["quantize"]
        mode = transforms.get("quantize_mode", "start")
        cur = quantize_notes(cur, grid_ticks=grid, mode=mode)

    # 6. Velocity scale & offset
    mult = transforms.get("velocity_scale", 1.0)
    off = transforms.get("velocity_offset", 0)
    if mult != 1.0 or off != 0:
        cur = scale_velocity(cur, multiplier=mult, offset=off)

    for n in cur:
        if "panning" not in n:
            n["panning"] = 0

    return cur


def transform_notes(
    notes: list[dict[str, Any]],
    transpose: int = 0,
    time_shift: int = 0,
    time_stretch: float = 1.0,
    reverse: bool = False,
    quantize_grid: Optional[int] = None,
    velocity_scale: float = 1.0,
    velocity_offset: int = 0,
    phrase_len: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Convenience functional wrapper around apply_transforms."""
    transforms = {
        "transpose": transpose,
        "time_shift": time_shift,
        "stretch": time_stretch,
        "reverse": reverse,
        "quantize": quantize_grid,
        "velocity_scale": velocity_scale,
        "velocity_offset": velocity_offset,
    }
    return apply_transforms(notes, transforms, span_ticks=phrase_len)


def reverse_automation_points(
    points: list[dict[str, Any]],
    span_ticks: int,
) -> list[dict[str, Any]]:
    """Pure reversal of automation points within span_ticks."""
    result = []
    for p in points:
        new_pos = span_ticks - p["pos"]
        result.append({"pos": new_pos, "value": p["value"]})
    result.sort(key=lambda x: x["pos"])
    return result
