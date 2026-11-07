"""Acoustic audition probe and metric analysis for MXM plugin patches.

An agent cannot hear a synth patch, so the probe renders a short, fixed phrase
and turns the result into measurements: how fast it starts, how long it rings,
what pitch it actually plays, and where its energy sits. That is enough to
catch the common host-level surprises before a patch reaches a song: a silent
low register, an octave-transposed sample set, a patch that plays by itself,
or an attack so slow that the note arrives late.

The phrase is four seconds long (plus a short lead-in and tail):

* 0.0-1.5 s  held C4 (MIDI 60, velocity 90) for sustain, harmonics and pitch
* 1.5-2.5 s  C2, C3, C4, C5 (MIDI 36, 48, 60, 72) for register response
* 2.5-3.5 s  C4-Eb4-G4-Bb4 chord for polyphony and intermodulation
* 3.5-4.0 s  four 16th-note staccato notes for attack and release

The project builder targets either a stock MXM instrument, a native LV2
instrument URI, or MXM's native VST3 host with a module, class id and optional
state. The render runs through the installed MXM binary. A crash or timeout
quarantines the plugin identity so the same failure is not repeated blindly.
"""

from __future__ import annotations

import array
import base64
import math
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

from . import mxm_app, quarantine, xml_parser
from .musical_units import TICKS_PER_BEAT
from .project import LMMSProject

# ---- the probe phrase -------------------------------------------------------------------------

PROBE_SECONDS = 4.0
LEAD_IN_SECONDS = 0.5
TAIL_SECONDS = 2.0
PROBE_TIMEOUT_SECONDS = 120.0

HELD_KEY = 60
HELD_VELOCITY = 90
HELD_START = 0.0
HELD_DURATION = 1.5

OCTAVE_KEYS = (36, 48, 60, 72)
OCTAVE_START = 1.5
OCTAVE_STEP = 0.25
OCTAVE_DURATION = 0.24

CHORD_KEYS = (60, 63, 67, 70)
CHORD_START = 2.5
CHORD_DURATION = 0.9

RUN_KEYS = (60, 62, 63, 65)
RUN_START = 3.5
RUN_STEP = 0.125
RUN_DURATION = 0.08
RUN_LAST_OFF = RUN_START + (len(RUN_KEYS) - 1) * RUN_STEP + RUN_DURATION


# ---- probe phrase events ----------------------------------------------------------------------


def probe_events() -> list[dict]:
    """Return the probe phrase as timed note events (seconds, phrase-relative).

    Each event has ``part``, ``key``, ``velocity``, ``start``, ``duration`` and
    ``end``. Times are relative to the first note, which starts at 0.0; the
    project builder shifts them by the lead-in.
    """
    events: list[dict] = []

    def note(part: str, key: int, start: float, duration: float, velocity: int = HELD_VELOCITY) -> None:
        events.append({
            "part": part,
            "key": int(key),
            "velocity": int(velocity),
            "start": round(start, 6),
            "duration": round(duration, 6),
            "end": round(start + duration, 6),
        })

    note("held", HELD_KEY, HELD_START, HELD_DURATION)
    for index, key in enumerate(OCTAVE_KEYS):
        note("octaves", key, OCTAVE_START + index * OCTAVE_STEP, OCTAVE_DURATION)
    for key in CHORD_KEYS:
        note("chord", key, CHORD_START, CHORD_DURATION)
    for index, key in enumerate(RUN_KEYS):
        note("run", key, RUN_START + index * RUN_STEP, RUN_DURATION)
    return events


def _seconds_to_ticks(seconds: float, bpm: float) -> int:
    """Convert seconds to LMMS ticks (48 ticks per beat in 4/4)."""
    return max(1, int(round(float(seconds) * float(bpm) / 60.0 * TICKS_PER_BEAT)))


# ---- project construction ---------------------------------------------------------------------


def _resolve_kind(plugin: str, kind: str) -> str:
    explicit = str(kind or "").strip().lower()
    if explicit:
        if explicit not in {"builtin", "lv2", "vst3"}:
            raise ValueError("kind must be 'builtin', 'lv2', or 'vst3'")
        return explicit
    text = str(plugin).strip()
    lowered = text.lower()
    if lowered.endswith((".vst3", ".vst3/")) or "/vst3/" in lowered:
        return "vst3"
    if lowered.endswith(".lv2") or lowered.startswith(("http://", "https://", "urn:")):
        return "lv2"
    return "builtin"


def _lv2_uri_from_bundle(bundle: Path) -> str:
    manifest = bundle / "manifest.ttl"
    if not manifest.is_file():
        raise ValueError(f"LV2 bundle has no manifest.ttl: {bundle}")
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ValueError(f"Cannot read LV2 manifest {manifest}: {exc}") from exc
    for pattern in (
        r"<([^>]+)>\s+a\s+lv2:InstrumentPlugin",
        r"<([^>]+)>\s+a\s+lv2:Plugin",
        r"<([^>]+)>\s+a\s+lv2:Instrument",
    ):
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    raise ValueError(f"No LV2 instrument URI found in {manifest}")


def _lv2_uri(plugin: str) -> str:
    text = str(plugin).strip()
    if text.startswith(("http://", "https://", "urn:")):
        return text
    return _lv2_uri_from_bundle(Path(text).expanduser())


def _plugin_state(state: str, cid: str = "") -> str:
    """Return base64 VST3 state from a base64 string or a state/preset file."""
    text = str(state or "").strip()
    if not text:
        return ""
    candidate = Path(text).expanduser()
    if candidate.is_file():
        if candidate.suffix.lower() == ".vstpreset":
            from .vst3_presets import parse_vstpreset

            parsed = parse_vstpreset(candidate, include_state=True)
            if not parsed.get("loadable") or not parsed.get("state"):
                raise ValueError(f"VST3 preset is not loadable: {candidate}")
            preset_cid = str(parsed.get("cid") or "")
            if cid and preset_cid and preset_cid.upper() != str(cid).upper():
                raise ValueError(
                    f"VST3 preset class id {preset_cid} does not match {cid}"
                )
            return str(parsed["state"])
        return base64.b64encode(candidate.read_bytes()).decode("ascii")
    try:
        base64.b64decode(text, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("state must be base64 text or a file path") from exc
    return text


def build_probe_project(
    plugin: str,
    project_path: str | Path,
    *,
    kind: str = "",
    cid: str = "",
    state: str = "",
    name: str = "",
    bpm: int = 120,
    lead_in: float = LEAD_IN_SECONDS,
    tail_seconds: float = TAIL_SECONDS,
) -> Path:
    """Write a minimal MXM project that plays the probe phrase on *plugin*.

    *plugin* is a stock instrument name, an LV2 URI/bundle path, or a VST3
    module path. *state* accepts base64 VST3 state, a ``.vstpreset`` file, or a
    raw state file. The project is plain XML so it can be inspected and edited.
    """
    resolved_kind = _resolve_kind(plugin, kind)
    target = Path(project_path)
    if target.suffix.lower() not in {".mmp", ".mmpz"}:
        target = target.with_suffix(".mmp")
    target.parent.mkdir(parents=True, exist_ok=True)

    project = LMMSProject()
    project.new(bpm=bpm)
    project.set_tempo(int(bpm))
    track_name = str(name).strip() or "Probe"

    if resolved_kind == "lv2":
        uri = _lv2_uri(plugin)
        project.add_track("instrument", track_name, instrument="lv2instrument")
        track = xml_parser.find_track_element(project.root, 0)
        xml_parser.configure_native_lv2_instrument(track, uri)
    elif resolved_kind == "vst3":
        if not str(cid).strip():
            raise ValueError("VST3 probing requires the plugin class id (cid)")
        module = str(plugin).strip()
        project.add_track("instrument", track_name, instrument=xml_parser.NATIVE_VST3_HOST)
        track = xml_parser.find_track_element(project.root, 0)
        xml_parser.configure_native_vst3_instrument(
            track, module, str(cid), _plugin_state(state, str(cid))
        )
    else:
        project.add_track("instrument", track_name, instrument=str(plugin).strip())

    for event in probe_events():
        project.add_note(
            0,
            key=event["key"],
            pos=_seconds_to_ticks(lead_in + event["start"], bpm),
            length=_seconds_to_ticks(event["duration"], bpm),
            volume=event["velocity"],
        )

    # A silent tail is part of the phrase: it lets releases, delays and reverbs
    # ring out so the decay measurement has something to observe.
    track = xml_parser.find_track_element(project.root, 0)
    clip = track.find("midiclip")
    if clip is not None:
        total = lead_in + PROBE_SECONDS + max(0.0, float(tail_seconds))
        clip.set("len", str(_seconds_to_ticks(total, bpm)))
        clip.set("autoresize", "0")

    project.save(target, compressed=False)
    return target


# ---- render -----------------------------------------------------------------------------------


def _run_render_process(cmd: list[str], timeout: float):
    """Run one MXM render command (wrapped for tests)."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _render_failure_reason(returncode: int) -> str:
    if returncode in (-11, 139, 0xC0000005):  # SIGSEGV / Windows access violation
        return quarantine.REASON_SEGFAULT
    if returncode < 0 or returncode in (128 + 6, 128 + 11, 0xC0000409, 0xC00000FD):
        return quarantine.REASON_CRASH
    return f"render exited with code {returncode} during probe"


def run_probe_render(
    project_path: str | Path,
    wav_path: str | Path,
    *,
    binary: str | Path | None = None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> dict:
    """Render a probe project headlessly to a 48 kHz stereo WAV.

    The returned dictionary always has ``ok``. A non-zero exit carries a
    ``reason`` suitable for :func:`quarantine.quarantine_plugin`; a missing
    binary or an OS-level launch error carries ``reason=None`` because the
    plugin itself did not fail.
    """
    executable = Path(binary) if binary else mxm_app.find_mxm_binary()
    if executable is None:
        return {
            "ok": False,
            "error": "MXM binary not found. Set MXM_EXECUTABLE to the MXM executable.",
            "reason": None,
            "returncode": None,
            "command": None,
        }

    out = Path(wav_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(executable),
        "render",
        str(project_path),
        "-o",
        str(out),
        "-f",
        "wav",
        "-s",
        "48000",
        "-b",
        "160",
    ]
    started = time.monotonic()
    try:
        proc = _run_render_process(command, timeout)
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"MXM render timed out after {timeout:g}s",
            "reason": quarantine.REASON_TIMEOUT,
            "returncode": None,
            "command": command,
            "wav": str(out),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    except OSError as exc:
        return {
            "ok": False,
            "error": f"Could not start MXM: {exc}",
            "reason": None,
            "returncode": None,
            "command": command,
            "wav": str(out),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }

    elapsed = round(time.monotonic() - started, 3)
    returncode = int(proc.returncode)
    if returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return {
            "ok": False,
            "error": f"MXM render failed (exit {returncode})" + (f": {detail}" if detail else ""),
            "reason": _render_failure_reason(returncode),
            "returncode": returncode,
            "command": command,
            "wav": str(out),
            "elapsed_seconds": elapsed,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    if not out.is_file() or out.stat().st_size <= 44:
        return {
            "ok": False,
            "error": "MXM reported success but produced no usable WAV file",
            "reason": quarantine.REASON_NO_AUDIO,
            "returncode": returncode,
            "command": command,
            "wav": str(out),
            "elapsed_seconds": elapsed,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    try:
        info = wav_info(out)
    except (OSError, EOFError, wave.Error) as exc:
        return {
            "ok": False,
            "error": f"MXM produced an unreadable WAV file: {exc}",
            "reason": None,
            "returncode": returncode,
            "command": command,
            "wav": str(out),
            "elapsed_seconds": elapsed,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    return {
        "ok": True,
        "returncode": returncode,
        "command": command,
        "wav": str(out),
        "wav_info": info,
        "elapsed_seconds": elapsed,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


# ---- WAV decoding -----------------------------------------------------------------------------


def read_wav(path: str | Path) -> tuple[list[float], list[float], int]:
    """Decode a PCM WAV file to (left, right, sample_rate) float samples."""
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        sample_rate = handle.getframerate()
        frames = handle.getnframes()
        raw = handle.readframes(frames)

    if width == 1:
        values = [(byte - 128) / 128.0 for byte in raw]
    elif width == 2:
        samples = array.array("h")
        samples.frombytes(raw)
        if sys.byteorder == "big":
            samples.byteswap()
        values = [sample / 32768.0 for sample in samples]
    elif width == 3:
        values = []
        for offset in range(0, len(raw) - 2, 3):
            value = raw[offset] | (raw[offset + 1] << 8) | (raw[offset + 2] << 16)
            if value & 0x800000:
                value -= 0x1000000
            values.append(value / 8388608.0)
    elif width == 4:
        samples = array.array("i")
        samples.frombytes(raw)
        if sys.byteorder == "big":
            samples.byteswap()
        values = [sample / 2147483648.0 for sample in samples]
    else:
        raise ValueError(f"Unsupported WAV sample width: {width * 8} bits")

    if channels <= 1:
        return values, values, sample_rate
    return values[0::channels], values[1::channels], sample_rate


def wav_info(path: str | Path) -> dict:
    """Return header facts about a WAV file without decoding its samples."""
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        frames = handle.getnframes()
        return {
            "channels": handle.getnchannels(),
            "sample_rate": rate,
            "frames": frames,
            "sample_width_bits": handle.getsampwidth() * 8,
            "duration_seconds": round(frames / rate, 3) if rate else 0.0,
        }


def _mono(left: list[float], right: list[float]) -> list[float]:
    if len(right) != len(left):
        right = right[: len(left)]
    return [(a + b) * 0.5 for a, b in zip(left, right)]


def _db(level: float) -> float:
    if level <= 0.0:
        return -120.0
    return max(-120.0, 20.0 * math.log10(level))


def _rms(samples: list[float]) -> float:
    if not samples:
        return 0.0
    return math.sqrt(sum(value * value for value in samples) / len(samples))


def _slice(samples: list[float], sample_rate: int, start: float, end: float | None) -> list[float]:
    begin = max(0, int(float(start) * sample_rate))
    if end is None:
        return samples[begin:]
    finish = min(len(samples), int(float(end) * sample_rate))
    if finish <= begin:
        return []
    return samples[begin:finish]


def _rms_db_window(samples: list[float], sample_rate: int, start: float, end: float) -> float:
    return _db(_rms(_slice(samples, sample_rate, start, end)))


# ---- metrics ----------------------------------------------------------------------------------


def measure_attack_time(
    samples: list[float],
    sample_rate: int,
    note_on: float = 0.0,
    *,
    search_seconds: float = 0.5,
    frame_ms: float = 5.0,
    threshold_db: float = -3.0,
) -> float:
    """Milliseconds from *note_on* until the envelope reaches 3 dB under peak.

    The peak is taken inside the search window, so a slow swell registers as a
    late attack while a percussive patch registers near zero.
    """
    frame = max(1, int(sample_rate * frame_ms / 1000.0))
    start = max(0, int(note_on * sample_rate))
    end = min(len(samples), start + int(search_seconds * sample_rate))
    if end - start < frame:
        return 0.0
    levels = [
        _rms(samples[position: position + frame])
        for position in range(start, end - frame + 1, frame)
    ]
    peak = max(levels, default=0.0)
    if peak <= 1e-6:
        return 0.0
    target = peak * (10.0 ** (threshold_db / 20.0))
    for index, level in enumerate(levels):
        if level >= target:
            return index * frame / sample_rate * 1000.0
    return 0.0


def measure_tail_duration(
    samples: list[float],
    sample_rate: int,
    note_off: float,
    *,
    threshold_dbfs: float = -40.0,
    frame_ms: float = 10.0,
) -> float:
    """Milliseconds after *note_off* until the signal stays under *threshold_dbfs*."""
    start = max(0, int(note_off * sample_rate))
    if start >= len(samples):
        return 0.0
    frame = max(1, int(sample_rate * frame_ms / 1000.0))
    threshold = 10.0 ** (threshold_dbfs / 20.0)
    last_above = -1
    for position in range(start, len(samples), frame):
        if _rms(samples[position: position + frame]) > threshold:
            last_above = position
    if last_above < 0:
        return 0.0
    return min(
        (last_above + frame - start) / sample_rate * 1000.0,
        (len(samples) - start) / sample_rate * 1000.0,
    )


def estimate_fundamental(
    samples: list[float],
    sample_rate: int,
    start: float = 0.0,
    end: float | None = None,
    *,
    min_hz: float = 40.0,
    max_hz: float = 2000.0,
) -> dict:
    """Estimate the fundamental of a monophonic window by normalized autocorrelation.

    Returns ``frequency_hz``, ``midi_note``, ``semitones_from_c4`` and
    ``confidence``. When the window is silent or unpitched the frequency is
    ``None`` and confidence is 0.
    """
    segment = _slice(samples, sample_rate, start, end)
    empty = {
        "frequency_hz": None,
        "midi_note": None,
        "semitones_from_c4": None,
        "confidence": 0.0,
    }
    if len(segment) < int(sample_rate * 0.02):
        return empty

    # Autocorrelation is quadratic; 12 kHz loses no pitch information below
    # 2 kHz and keeps a 1 s window cheap in pure Python.
    factor = max(1, int(sample_rate // 12000))
    if factor > 1:
        decimated = [
            sum(segment[position: position + factor]) / factor
            for position in range(0, len(segment) - factor + 1, factor)
        ]
        segment = decimated
        effective_rate = sample_rate / factor
    else:
        effective_rate = float(sample_rate)

    segment = segment[-4096:]
    mean = sum(segment) / len(segment)
    segment = [value - mean for value in segment]
    energy = sum(value * value for value in segment)
    if energy <= len(segment) * 1e-10:
        return empty

    min_lag = max(2, int(effective_rate / max_hz))
    max_lag = min(len(segment) // 2, int(effective_rate / min_hz))
    if max_lag <= min_lag:
        return empty

    prefix = [0.0] * (len(segment) + 1)
    for index, value in enumerate(segment):
        prefix[index + 1] = prefix[index] + value * value
    correlation: dict[int, float] = {}
    total_energy = prefix[-1]
    for lag in range(min_lag, max_lag + 1):
        numerator = sum(
            segment[index] * segment[index + lag]
            for index in range(len(segment) - lag)
        )
        left_energy = prefix[len(segment) - lag]
        right_energy = total_energy - prefix[lag]
        denominator = math.sqrt(max(left_energy * right_energy, 1e-24))
        correlation[lag] = numerator / denominator

    best = max(correlation.values())
    if best < 0.3:
        return empty

    chosen = None
    for lag in range(min_lag + 1, max_lag):
        value = correlation[lag]
        if (
            value >= 0.9 * best
            and value >= correlation[lag - 1]
            and value >= correlation[lag + 1]
        ):
            chosen = lag
            break
    if chosen is None:
        chosen = max(correlation, key=correlation.get)

    shift = 0.0
    if chosen - 1 in correlation and chosen + 1 in correlation:
        denominator = (
            correlation[chosen - 1] - 2.0 * correlation[chosen] + correlation[chosen + 1]
        )
        if abs(denominator) > 1e-12:
            shift = 0.5 * (correlation[chosen - 1] - correlation[chosen + 1]) / denominator
    frequency = effective_rate / (chosen + shift)
    if not min_hz <= frequency <= max_hz:
        frequency = effective_rate / chosen
    midi = 69.0 + 12.0 * math.log2(frequency / 440.0)
    return {
        "frequency_hz": round(frequency, 3),
        "midi_note": round(midi, 3),
        "semitones_from_c4": round(midi - 60.0, 3),
        "confidence": round(max(0.0, min(1.0, correlation.get(chosen, best))), 3),
    }


def _fft(values: list[complex]) -> list[complex]:
    """Iterative radix-2 Cooley-Tukey FFT (length must be a power of two)."""
    count = len(values)
    data = list(values)
    j = 0
    for i in range(1, count):
        bit = count >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            data[i], data[j] = data[j], data[i]
    length = 2
    while length <= count:
        angle = -2.0 * math.pi / length
        step = complex(math.cos(angle), math.sin(angle))
        half = length >> 1
        for start in range(0, count, length):
            factor = 1.0 + 0.0j
            for index in range(start, start + half):
                even = data[index]
                odd = data[index + half] * factor
                data[index] = even + odd
                data[index + half] = even - odd
                factor *= step
        length <<= 1
    return data


_HANN_CACHE: dict[int, list[float]] = {}


def _hann(length: int) -> list[float]:
    cached = _HANN_CACHE.get(length)
    if cached is None:
        denominator = max(1, length - 1)
        cached = [
            0.5 - 0.5 * math.cos(2.0 * math.pi * index / denominator)
            for index in range(length)
        ]
        _HANN_CACHE[length] = cached
    return cached


def _band_shares(segment: list[float], sample_rate: int) -> dict[str, float]:
    """Power shares for sub, low-mid, high-mid and brightness bands."""
    size = 256
    limit = min(4096, len(segment))
    while size * 2 <= limit:
        size *= 2
    window = _hann(size)
    powers = {"sub": 0.0, "low_mid": 0.0, "high_mid": 0.0, "brightness": 0.0}
    frames = 0
    for offset in range(0, len(segment) - size + 1, size):
        buffer = [
            complex(segment[offset + index] * window[index], 0.0)
            for index in range(size)
        ]
        spectrum = _fft(buffer)
        for bin_index in range(1, size // 2 + 1):
            frequency = bin_index * sample_rate / size
            power = abs(spectrum[bin_index]) ** 2
            if bin_index != size // 2:
                power *= 2.0
            if frequency < 60.0:
                powers["sub"] += power
            elif frequency < 1000.0:
                powers["low_mid"] += power
            elif frequency < 5000.0:
                powers["high_mid"] += power
            else:
                powers["brightness"] += power
        frames += 1
    total = sum(powers.values())
    if frames == 0 or total <= 0.0:
        return {"sub": 0.0, "low_mid": 0.0, "high_mid": 0.0, "brightness": 0.0}
    return {name: value / total for name, value in powers.items()}


def band_balance(
    samples: list[float],
    sample_rate: int,
    start: float = 0.0,
    end: float | None = None,
) -> dict:
    """Energy distribution across sub, low-mid, high-mid and brightness.

    Band levels are dBFS relative to the window's mean square; shares sum to
    one. ``dominant_band`` names the largest share, which is what an agent
    reads to decide whether a patch is sub-heavy, warm, biting, or airy.
    """
    segment = _slice(samples, sample_rate, start, end)
    if not segment:
        shares = {"sub": 0.0, "low_mid": 0.0, "high_mid": 0.0, "brightness": 0.0}
        mean_square = 0.0
    else:
        shares = _band_shares(segment, sample_rate)
        mean_square = sum(value * value for value in segment) / len(segment)
    result: dict[str, float | str | None] = {}
    for name, share in shares.items():
        result[f"{name}_share"] = round(share, 6)
        result[f"{name}_db"] = round(
            10.0 * math.log10(max(share * mean_square, 1e-24)), 3
        )
    dominant = max(shares, key=shares.get) if any(shares.values()) else None
    result["dominant_band"] = dominant
    return result


def _count_onsets(
    samples: list[float],
    sample_rate: int,
    start: float,
    end: float,
    *,
    frame_ms: float = 5.0,
    threshold_db: float = -12.0,
    minimum_gap_ms: float = 30.0,
) -> int:
    frame = max(1, int(sample_rate * frame_ms / 1000.0))
    segment = _slice(samples, sample_rate, start, end)
    if len(segment) < frame:
        return 0
    levels = [
        _db(_rms(segment[position: position + frame]))
        for position in range(0, len(segment) - frame + 1, frame)
    ]
    peak = max(levels, default=-120.0)
    if peak < -70.0:
        return 0
    gate = peak + threshold_db
    onsets = 0
    above_before = False
    last_onset = -math.inf
    for index, level in enumerate(levels):
        above = level >= gate
        now = index * frame / sample_rate
        if above and not above_before and (now - last_onset) * 1000.0 >= minimum_gap_ms:
            onsets += 1
            last_onset = now
        above_before = above
    return onsets


# ---- full probe analysis ----------------------------------------------------------------------


def analyze_probe_audio(
    source,
    *,
    sample_rate: int | None = None,
    lead_in: float = LEAD_IN_SECONDS,
) -> dict:
    """Measure a rendered probe.

    *source* is a WAV path, ``(samples, sample_rate)``, or a mono sample list
    with an explicit *sample_rate*. *lead_in* is the silence before the first
    note; the builder inserts :data:`LEAD_IN_SECONDS`.
    """
    channels = 1
    if isinstance(source, (str, Path)):
        left, right, rate = read_wav(source)
        channels = 1 if left is right else 2
    elif isinstance(source, (tuple, list)) and len(source) == 2 and isinstance(source[1], int):
        samples, rate = source
        left = list(samples)
        right = left
    elif isinstance(source, (list, array.array)):
        if sample_rate is None:
            raise ValueError("sample_rate is required for a sample list")
        rate = int(sample_rate)
        left = list(source)
        right = left
    else:
        raise TypeError(f"Unsupported audio source: {type(source)!r}")
    rate = int(rate)
    if not left:
        raise ValueError("Probe audio is empty")

    mono = _mono(left, right)
    if not mono:
        raise ValueError("Probe audio is empty")

    peak = max(max(abs(value) for value in left), max(abs(value) for value in right))
    combined = list(left) + list(right)
    rms = _rms(combined)

    held_db = _rms_db_window(mono, rate, lead_in + 0.20, lead_in + 1.40)
    c2_db = _rms_db_window(mono, rate, lead_in + 1.51, lead_in + 1.73)
    octave_db = _rms_db_window(mono, rate, lead_in + OCTAVE_START, lead_in + CHORD_START)
    chord_db = _rms_db_window(mono, rate, lead_in + CHORD_START, lead_in + RUN_START)
    run_db = _rms_db_window(mono, rate, lead_in + RUN_START, lead_in + PROBE_SECONDS)

    pitch = estimate_fundamental(
        mono, rate, lead_in + 0.25, lead_in + 1.45
    )
    if pitch["confidence"] < 0.3:
        drift = None
        octave = None
        fundamental = None
        midi_note = None
    else:
        drift = pitch["semitones_from_c4"]
        nearest_octave = round(drift / 12.0)
        octave = (
            int(nearest_octave)
            if abs(drift - nearest_octave * 12.0) < 0.5
            else 0
        )
        fundamental = pitch["frequency_hz"]
        midi_note = pitch["midi_note"]

    attack = measure_attack_time(mono, rate, lead_in)
    tail = measure_tail_duration(mono, rate, lead_in + RUN_LAST_OFF)
    bands = band_balance(mono, rate, lead_in + 0.20, lead_in + 1.40)
    run_onsets = _count_onsets(mono, rate, lead_in + RUN_START, lead_in + PROBE_SECONDS)

    silent_low_notes = c2_db <= -55.0 or (held_db > -70.0 and c2_db < held_db - 40.0)

    pre_note_end = lead_in - 0.05
    if pre_note_end >= 0.05:
        noise_db = _rms_db_window(mono, rate, 0.0, pre_note_end)
        pre_note_noise = noise_db > -50.0 and (
            held_db <= -70.0 or noise_db > held_db - 30.0
        )
    else:
        noise_db = -120.0
        pre_note_noise = False

    warnings: list[str] = []
    if _db(peak) < -70.0 or held_db < -70.0:
        warnings.append("silent on a held C4")
    elif pitch["confidence"] < 0.3:
        warnings.append("no clear pitch (noise, drum or heavily detuned)")
    elif drift is not None:
        cents = (drift - round(drift)) * 100.0
        if octave:
            warnings.append(
                f"C4 sounds {octave:+d} octave(s) from written pitch "
                f"({drift:+.2f} semitones); transpose by {-drift:+.2f} or "
                "retune the patch"
            )
        elif abs(drift) >= 0.5:
            warnings.append(
                f"C4 sounds {drift:+.2f} semitones from written pitch; "
                f"transpose the patch by {-drift:+.2f}"
            )
        if abs(cents) >= 25.0:
            warnings.append(f"out of tune by {cents:+.0f} cents")
    if silent_low_notes:
        warnings.append("C2 is silent (outside the patch's range?)")
    if pre_note_noise:
        warnings.append(
            f"sound before the first note ({noise_db:.0f} dBFS): "
            "self-playing or noisy"
        )
    if attack >= 250.0:
        warnings.append(f"slow attack {attack:.0f} ms")
    if tail >= 800.0:
        warnings.append(f"long tail {tail / 1000.0:.1f} s after note-off")
    if run_onsets and run_onsets < len(RUN_KEYS) - 1:
        warnings.append(
            f"16th run blurs: {run_onsets} of {len(RUN_KEYS)} onsets heard"
        )
    if _db(peak) >= -0.1:
        warnings.append("probe peaks at full scale (patch may clip)")

    return {
        "duration_seconds": round(len(mono) / rate, 3),
        "sample_rate": rate,
        "channels": channels,
        "peak_dbfs": round(_db(peak), 2),
        "rms_dbfs": round(_db(rms), 2),
        "attack_time_ms": round(attack, 2),
        "tail_duration_ms": round(tail, 2),
        "pitch_drift_semitones": None if drift is None else round(drift, 3),
        "detected_fundamental_hz": fundamental,
        "detected_midi_note": midi_note,
        "octave_offset": octave,
        "pitch_confidence": pitch["confidence"],
        "band_balance": bands,
        "silent_low_notes": bool(silent_low_notes),
        "pre_note_noise": bool(pre_note_noise),
        "run_onsets": run_onsets,
        "section_levels_dbfs": {
            "held": round(held_db, 2),
            "octaves": round(octave_db, 2),
            "chord": round(chord_db, 2),
            "run": round(run_db, 2),
            "pre_note": round(noise_db, 2),
            "c2": round(c2_db, 2),
        },
        "warnings": warnings,
    }


# ---- orchestration ----------------------------------------------------------------------------


def audition_plugin(
    plugin: str,
    *,
    kind: str = "",
    cid: str = "",
    state: str = "",
    name: str = "",
    binary: str | Path | None = None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    allow_quarantined: bool = False,
    output_dir: str | Path | None = None,
    keep_audio: bool | None = None,
) -> dict:
    """Render the probe for *plugin* and return its acoustic profile.

    A quarantined identity is refused before any plugin code runs unless
    *allow_quarantined* is set. A crash, timeout, or empty render quarantines
    the identity automatically and returns the record. Set *output_dir* (or
    *keep_audio*) to keep the temporary project and WAV for inspection.
    """
    identity = str(plugin).strip()
    if not identity:
        return {"ok": False, "plugin": identity, "error": "A plugin is required"}

    if quarantine.is_quarantined(identity) and not allow_quarantined:
        record = quarantine.quarantined_entry(identity)
        reason = record.get("reason") if record else ""
        return {
            "ok": False,
            "plugin": identity,
            "error": "plugin is quarantined" + (f" ({reason})" if reason else ""),
            "quarantined": True,
            "quarantine": record,
            "hint": "unquarantine_plugin() after fixing or reinstalling the plugin",
        }

    keep = bool(output_dir) or bool(keep_audio)
    if output_dir:
        work_dir = Path(output_dir).expanduser()
        work_dir.mkdir(parents=True, exist_ok=True)
    else:
        work_dir = Path(tempfile.mkdtemp(prefix="mxm-audition-"))

    display_name = str(name).strip()
    try:
        try:
            project_path = build_probe_project(
                plugin,
                work_dir / "probe.mmp",
                kind=kind,
                cid=cid,
                state=state,
                name=display_name,
            )
        except (OSError, ValueError) as exc:
            return {
                "ok": False,
                "plugin": identity,
                "error": f"probe project could not be built: {exc}",
                "quarantined": False,
                "quarantine": None,
                "work_dir": str(work_dir) if keep else None,
            }
        wav_path = work_dir / "probe.wav"
        render = run_probe_render(
            project_path, wav_path, binary=binary, timeout=timeout
        )
        if not render["ok"]:
            reason = render.get("reason")
            record = None
            quarantine_error = None
            if reason:
                try:
                    record = quarantine.quarantine_plugin(
                        identity, str(reason), display_name
                    )
                except OSError as exc:
                    quarantine_error = str(exc)
            result = {
                "ok": False,
                "plugin": identity,
                "error": render.get("error") or "probe render failed",
                "quarantined": record is not None,
                "quarantine": record,
                "render": render,
                "work_dir": str(work_dir) if keep else None,
            }
            if quarantine_error:
                result["quarantine_error"] = quarantine_error
            return result
        try:
            metrics = analyze_probe_audio(wav_path)
        except (OSError, ValueError, TypeError, wave.Error) as exc:
            return {
                "ok": False,
                "plugin": identity,
                "error": f"probe audio could not be analyzed: {exc}",
                "quarantined": False,
                "quarantine": None,
                "render": render,
                "work_dir": str(work_dir) if keep else None,
            }
        info = render.get("wav_info") or {}
        if (
            (info.get("channels") and info["channels"] != 2)
            or (info.get("sample_rate") and info["sample_rate"] != 48000)
        ):
            metrics["warnings"].append(
                f"render is {info.get('channels')}-channel at "
                f"{info.get('sample_rate')} Hz (asked for 48 kHz stereo)"
            )
        return {
            "ok": True,
            "plugin": identity,
            "name": display_name,
            "quarantined": False,
            "quarantine": None,
            "probe_seconds": PROBE_SECONDS,
            "render": {
                key: render.get(key)
                for key in ("returncode", "wav", "wav_info", "elapsed_seconds", "command")
            },
            "metrics": metrics,
            "warnings": metrics["warnings"],
            "work_dir": str(work_dir) if keep else None,
        }
    finally:
        if not keep:
            shutil.rmtree(work_dir, ignore_errors=True)
