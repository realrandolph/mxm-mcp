"""Acoustic audition probe and metric analysis for MXM plugin patches."""

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

PROBE_SECONDS, LEAD_IN_SECONDS, TAIL_SECONDS, PROBE_TIMEOUT_SECONDS = 4.0, 0.5, 2.0, 120.0
HELD_KEY, HELD_VELOCITY, HELD_START, HELD_DURATION = 60, 90, 0.0, 1.5
OCTAVE_KEYS, OCTAVE_START, OCTAVE_STEP, OCTAVE_DURATION = (36, 48, 60, 72), 1.5, 0.25, 0.24
CHORD_KEYS, CHORD_START, CHORD_DURATION = (60, 63, 67, 70), 2.5, 0.9
RUN_KEYS, RUN_START, RUN_STEP, RUN_DURATION = (60, 62, 63, 65), 3.5, 0.125, 0.08
RUN_LAST_OFF = RUN_START + (len(RUN_KEYS) - 1) * RUN_STEP + RUN_DURATION


def probe_events() -> list[dict]:
    events: list[dict] = []

    def note(part: str, key: int, start: float, duration: float, velocity: int = HELD_VELOCITY) -> None:
        events.append({
            "part": part, "key": int(key), "velocity": int(velocity),
            "start": round(start, 6), "duration": round(duration, 6), "end": round(start + duration, 6),
        })

    note("held", HELD_KEY, HELD_START, HELD_DURATION)
    for idx, key in enumerate(OCTAVE_KEYS):
        note("octaves", key, OCTAVE_START + idx * OCTAVE_STEP, OCTAVE_DURATION)
    for key in CHORD_KEYS:
        note("chord", key, CHORD_START, CHORD_DURATION)
    for idx, key in enumerate(RUN_KEYS):
        note("run", key, RUN_START + idx * RUN_STEP, RUN_DURATION)
    return events


def _seconds_to_ticks(seconds: float, bpm: float) -> int:
    return max(1, int(round(float(seconds) * float(bpm) / 60.0 * TICKS_PER_BEAT)))


def _resolve_kind(plugin: str, kind: str) -> str:
    explicit = str(kind or "").strip().lower()
    if explicit:
        if explicit not in {"builtin", "lv2", "vst3"}:
            raise ValueError("kind must be 'builtin', 'lv2', or 'vst3'")
        return explicit
    low = str(plugin).strip().lower()
    if low.endswith((".vst3", ".vst3/")) or "/vst3/" in low:
        return "vst3"
    if low.endswith(".lv2") or low.startswith(("http://", "https://", "urn:")):
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
    m = re.search(r"<([^>]+)>\s+a\s+lv2:(?:InstrumentPlugin|Plugin|Instrument)", text)
    if m:
        return m.group(1)
    raise ValueError(f"No LV2 instrument URI found in {manifest}")


def _lv2_uri(plugin: str) -> str:
    text = str(plugin).strip()
    return text if text.startswith(("http://", "https://", "urn:")) else _lv2_uri_from_bundle(Path(text).expanduser())


def _plugin_state(state: str, cid: str = "") -> str:
    text = str(state or "").strip()
    if not text:
        return ""
    cand = Path(text).expanduser()
    if cand.is_file():
        if cand.suffix.lower() == ".vstpreset":
            from .vst3_presets import parse_vstpreset
            parsed = parse_vstpreset(cand, include_state=True)
            if not parsed.get("loadable") or not parsed.get("state"):
                raise ValueError(f"VST3 preset is not loadable: {cand}")
            preset_cid = str(parsed.get("cid") or "")
            if cid and preset_cid and preset_cid.upper() != str(cid).upper():
                raise ValueError(f"VST3 preset class id {preset_cid} does not match {cid}")
            return str(parsed["state"])
        return base64.b64encode(cand.read_bytes()).decode("ascii")
    try:
        base64.b64decode(text, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("state must be base64 text or a file path") from exc
    return text


def build_probe_project(
    plugin: str, project_path: str | Path, *, kind: str = "", cid: str = "", state: str = "",
    name: str = "", bpm: int = 120, lead_in: float = LEAD_IN_SECONDS, tail_seconds: float = TAIL_SECONDS,
) -> Path:
    r_kind = _resolve_kind(plugin, kind)
    target = Path(project_path)
    if target.suffix.lower() not in {".mmp", ".mmpz"}:
        target = target.with_suffix(".mmp")
    target.parent.mkdir(parents=True, exist_ok=True)

    project = LMMSProject()
    project.new(bpm=bpm)
    project.set_tempo(int(bpm))
    track_name = str(name).strip() or "Probe"

    if r_kind == "lv2":
        uri = _lv2_uri(plugin)
        project.add_track("instrument", track_name, instrument="lv2instrument")
        track = xml_parser.find_track_element(project.root, 0)
        xml_parser.configure_native_lv2_instrument(track, uri)
    elif r_kind == "vst3":
        if not str(cid).strip():
            raise ValueError("VST3 probing requires the plugin class id (cid)")
        project.add_track("instrument", track_name, instrument=xml_parser.NATIVE_VST3_HOST)
        track = xml_parser.find_track_element(project.root, 0)
        xml_parser.configure_native_vst3_instrument(track, str(plugin).strip(), str(cid), _plugin_state(state, str(cid)))
    else:
        project.add_track("instrument", track_name, instrument=str(plugin).strip())

    for ev in probe_events():
        project.add_note(0, key=ev["key"], pos=_seconds_to_ticks(lead_in + ev["start"], bpm),
                         length=_seconds_to_ticks(ev["duration"], bpm), volume=ev["velocity"])

    track = xml_parser.find_track_element(project.root, 0)
    clip = track.find("midiclip")
    if clip is not None:
        total = lead_in + PROBE_SECONDS + max(0.0, float(tail_seconds))
        clip.set("len", str(_seconds_to_ticks(total, bpm)))
        clip.set("autoresize", "0")

    project.save(target, compressed=False)
    return target


def _run_render_process(cmd: list[str], timeout: float):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _render_failure_reason(returncode: int) -> str:
    if returncode in (-11, 139, 0xC0000005):
        return quarantine.REASON_SEGFAULT
    if returncode < 0 or returncode in (128 + 6, 128 + 11, 0xC0000409, 0xC00000FD):
        return quarantine.REASON_CRASH
    return f"render exited with code {returncode} during probe"


def run_probe_render(
    project_path: str | Path, wav_path: str | Path, *, binary: str | Path | None = None, timeout: float = PROBE_TIMEOUT_SECONDS,
) -> dict:
    exe = Path(binary) if binary else mxm_app.find_mxm_binary()
    if exe is None:
        return {"ok": False, "error": "MXM binary not found. Set MXM_EXECUTABLE to the MXM executable.", "reason": None, "returncode": None, "command": None}
    out = Path(wav_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    command = [str(exe), "render", str(project_path), "-o", str(out), "-f", "wav", "-s", "48000", "-b", "160"]
    started = time.monotonic()
    try:
        proc = _run_render_process(command, timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"MXM render timed out after {timeout:g}s", "reason": quarantine.REASON_TIMEOUT, "returncode": None, "command": command, "wav": str(out), "elapsed_seconds": round(time.monotonic() - started, 3)}
    except OSError as exc:
        return {"ok": False, "error": f"Could not start MXM: {exc}", "reason": None, "returncode": None, "command": command, "wav": str(out), "elapsed_seconds": round(time.monotonic() - started, 3)}

    elapsed = round(time.monotonic() - started, 3)
    ret = int(proc.returncode)
    if ret != 0:
        det = (proc.stderr or proc.stdout or "").strip()
        return {"ok": False, "error": f"MXM render failed (exit {ret})" + (f": {det}" if det else ""), "reason": _render_failure_reason(ret), "returncode": ret, "command": command, "wav": str(out), "elapsed_seconds": elapsed, "stdout": proc.stdout, "stderr": proc.stderr}
    if not out.is_file() or out.stat().st_size <= 44:
        return {"ok": False, "error": "MXM reported success but produced no usable WAV file", "reason": quarantine.REASON_NO_AUDIO, "returncode": ret, "command": command, "wav": str(out), "elapsed_seconds": elapsed, "stdout": proc.stdout, "stderr": proc.stderr}
    try:
        info = wav_info(out)
    except (OSError, EOFError, wave.Error) as exc:
        return {"ok": False, "error": f"MXM produced an unreadable WAV file: {exc}", "reason": None, "returncode": ret, "command": command, "wav": str(out), "elapsed_seconds": elapsed, "stdout": proc.stdout, "stderr": proc.stderr}
    return {"ok": True, "returncode": ret, "command": command, "wav": str(out), "wav_info": info, "elapsed_seconds": elapsed, "stdout": proc.stdout, "stderr": proc.stderr}


def read_wav(path: str | Path) -> tuple[list[float], list[float], int]:
    with wave.open(str(path), "rb") as h:
        channels, width, rate, frames = h.getnchannels(), h.getsampwidth(), h.getframerate(), h.getnframes()
        raw = h.readframes(frames)
    if width == 1:
        vals = [(b - 128) / 128.0 for b in raw]
    elif width in (2, 4):
        s = array.array("h" if width == 2 else "i")
        s.frombytes(raw)
        if sys.byteorder == "big": s.byteswap()
        div = 32768.0 if width == 2 else 2147483648.0
        vals = [v / div for v in s]
    elif width == 3:
        vals = [((raw[o] | (raw[o + 1] << 8) | (raw[o + 2] << 16)) - (0x1000000 if raw[o + 2] & 0x80 else 0)) / 8388608.0 for o in range(0, len(raw) - 2, 3)]
    else:
        raise ValueError(f"Unsupported WAV sample width: {width * 8} bits")
    return (vals, vals, rate) if channels <= 1 else (vals[0::channels], vals[1::channels], rate)
    return (vals, vals, rate) if channels <= 1 else (vals[0::channels], vals[1::channels], rate)


def wav_info(path: str | Path) -> dict:
    with wave.open(str(path), "rb") as h:
        r, f = h.getframerate(), h.getnframes()
        return {"channels": h.getnchannels(), "sample_rate": r, "frames": f, "sample_width_bits": h.getsampwidth() * 8, "duration_seconds": round(f / r, 3) if r else 0.0}


def _mono(left: list[float], right: list[float]) -> list[float]:
    r = right[:len(left)] if len(right) != len(left) else right
    return [(a + b) * 0.5 for a, b in zip(left, r)]


def _db(level: float) -> float:
    return -120.0 if level <= 0.0 else max(-120.0, 20.0 * math.log10(level))


def _rms(samples: list[float]) -> float:
    return math.sqrt(sum(v * v for v in samples) / len(samples)) if samples else 0.0


def _slice(samples: list[float], sample_rate: int, start: float, end: float | None) -> list[float]:
    b = max(0, int(float(start) * sample_rate))
    if end is None:
        return samples[b:]
    f = min(len(samples), int(float(end) * sample_rate))
    return samples[b:f] if f > b else []


def _rms_db_window(samples: list[float], sample_rate: int, start: float, end: float) -> float:
    return _db(_rms(_slice(samples, sample_rate, start, end)))


def measure_attack_time(samples: list[float], sample_rate: int, note_on: float = 0.0, *, search_seconds: float = 0.5, frame_ms: float = 5.0, threshold_db: float = -3.0) -> float:
    frame = max(1, int(sample_rate * frame_ms / 1000.0))
    start = max(0, int(note_on * sample_rate))
    end = min(len(samples), start + int(search_seconds * sample_rate))
    if end - start < frame:
        return 0.0
    levels = [_rms(samples[p:p + frame]) for p in range(start, end - frame + 1, frame)]
    peak = max(levels, default=0.0)
    if peak <= 1e-6:
        return 0.0
    target = peak * (10.0 ** (threshold_db / 20.0))
    for idx, lvl in enumerate(levels):
        if lvl >= target:
            return idx * frame / sample_rate * 1000.0
    return 0.0


def measure_tail_duration(samples: list[float], sample_rate: int, note_off: float, *, threshold_dbfs: float = -40.0, frame_ms: float = 10.0) -> float:
    start = max(0, int(note_off * sample_rate))
    if start >= len(samples):
        return 0.0
    frame = max(1, int(sample_rate * frame_ms / 1000.0))
    thresh = 10.0 ** (threshold_dbfs / 20.0)
    last_above = -1
    for p in range(start, len(samples), frame):
        if _rms(samples[p:p + frame]) > thresh:
            last_above = p
    return 0.0 if last_above < 0 else min((last_above + frame - start) / sample_rate * 1000.0, (len(samples) - start) / sample_rate * 1000.0)


def estimate_fundamental(samples: list[float], sample_rate: int, start: float = 0.0, end: float | None = None, *, min_hz: float = 40.0, max_hz: float = 2000.0) -> dict:
    empty = {"frequency_hz": None, "midi_note": None, "semitones_from_c4": None, "confidence": 0.0}
    seg = _slice(samples, sample_rate, start, end)
    if len(seg) < int(sample_rate * 0.02):
        return empty
    factor = max(1, int(sample_rate // 12000))
    if factor > 1:
        seg = [sum(seg[p:p + factor]) / factor for p in range(0, len(seg) - factor + 1, factor)]
        eff_rate = sample_rate / factor
    else:
        eff_rate = float(sample_rate)
    seg = seg[-4096:]
    mean = sum(seg) / len(seg)
    seg = [v - mean for v in seg]
    if sum(v * v for v in seg) <= len(seg) * 1e-10:
        return empty
    min_lag, max_lag = max(2, int(eff_rate / max_hz)), min(len(seg) // 2, int(eff_rate / min_hz))
    if max_lag <= min_lag:
        return empty
    prefix = [0.0] * (len(seg) + 1)
    for idx, v in enumerate(seg):
        prefix[idx + 1] = prefix[idx] + v * v
    tot = prefix[-1]
    corr: dict[int, float] = {}
    for lag in range(min_lag, max_lag + 1):
        num = sum(seg[i] * seg[i + lag] for i in range(len(seg) - lag))
        den = math.sqrt(max(prefix[len(seg) - lag] * (tot - prefix[lag]), 1e-24))
        corr[lag] = num / den
    best = max(corr.values())
    if best < 0.3:
        return empty
    chosen = next((l for l in range(min_lag + 1, max_lag) if corr[l] >= 0.9 * best and corr[l] >= corr[l - 1] and corr[l] >= corr[l + 1]), None)
    if chosen is None:
        chosen = max(corr, key=corr.get)
    shift = 0.0
    if chosen - 1 in corr and chosen + 1 in corr:
        den = corr[chosen - 1] - 2.0 * corr[chosen] + corr[chosen + 1]
        if abs(den) > 1e-12:
            shift = 0.5 * (corr[chosen - 1] - corr[chosen + 1]) / den
    freq = eff_rate / (chosen + shift)
    if not min_hz <= freq <= max_hz:
        freq = eff_rate / chosen
    midi = 69.0 + 12.0 * math.log2(freq / 440.0)
    return {
        "frequency_hz": round(freq, 3), "midi_note": round(midi, 3),
        "semitones_from_c4": round(midi - 60.0, 3), "confidence": round(max(0.0, min(1.0, corr.get(chosen, best))), 3),
    }


def _fft(values: list[complex]) -> list[complex]:
    n = len(values)
    data = list(values)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            data[i], data[j] = data[j], data[i]
    length = 2
    while length <= n:
        ang = -2.0 * math.pi / length
        step = complex(math.cos(ang), math.sin(ang))
        half = length >> 1
        for start in range(0, n, length):
            factor = 1.0 + 0.0j
            for idx in range(start, start + half):
                even, odd = data[idx], data[idx + half] * factor
                data[idx] = even + odd
                data[idx + half] = even - odd
                factor *= step
        length <<= 1
    return data


_HANN_CACHE: dict[int, list[float]] = {}


def _hann(length: int) -> list[float]:
    if length not in _HANN_CACHE:
        den = max(1, length - 1)
        _HANN_CACHE[length] = [0.5 - 0.5 * math.cos(2.0 * math.pi * i / den) for i in range(length)]
    return _HANN_CACHE[length]


def _band_shares(segment: list[float], sample_rate: int) -> dict[str, float]:
    size = 256
    limit = min(4096, len(segment))
    while size * 2 <= limit:
        size *= 2
    win = _hann(size)
    powers = {"sub": 0.0, "low_mid": 0.0, "high_mid": 0.0, "brightness": 0.0}
    frames = 0
    for off in range(0, len(segment) - size + 1, size):
        buf = [complex(segment[off + i] * win[i], 0.0) for i in range(size)]
        spec = _fft(buf)
        for b_idx in range(1, size // 2 + 1):
            f = b_idx * sample_rate / size
            p = (abs(spec[b_idx]) ** 2) * (1.0 if b_idx == size // 2 else 2.0)
            if f < 60.0:
                powers["sub"] += p
            elif f < 1000.0:
                powers["low_mid"] += p
            elif f < 5000.0:
                powers["high_mid"] += p
            else:
                powers["brightness"] += p
        frames += 1
    tot = sum(powers.values())
    return {k: 0.0 for k in powers} if frames == 0 or tot <= 0.0 else {k: v / tot for k, v in powers.items()}


def band_balance(samples: list[float], sample_rate: int, start: float = 0.0, end: float | None = None) -> dict:
    seg = _slice(samples, sample_rate, start, end)
    shares = _band_shares(seg, sample_rate) if seg else {"sub": 0.0, "low_mid": 0.0, "high_mid": 0.0, "brightness": 0.0}
    ms = sum(v * v for v in seg) / len(seg) if seg else 0.0
    res: dict[str, float | str | None] = {}
    for name, share in shares.items():
        res[f"{name}_share"] = round(share, 6)
        res[f"{name}_db"] = round(10.0 * math.log10(max(share * ms, 1e-24)), 3)
    res["dominant_band"] = max(shares, key=shares.get) if any(shares.values()) else None
    return res


def _count_onsets(samples: list[float], sample_rate: int, start: float, end: float, *, frame_ms: float = 5.0, threshold_db: float = -12.0, minimum_gap_ms: float = 30.0) -> int:
    frame = max(1, int(sample_rate * frame_ms / 1000.0))
    seg = _slice(samples, sample_rate, start, end)
    if len(seg) < frame:
        return 0
    levels = [_db(_rms(seg[p:p + frame])) for p in range(0, len(seg) - frame + 1, frame)]
    peak = max(levels, default=-120.0)
    if peak < -70.0:
        return 0
    gate, onsets, above_prev, last_on = peak + threshold_db, 0, False, -math.inf
    for idx, lvl in enumerate(levels):
        ab = lvl >= gate
        now = idx * frame / sample_rate
        if ab and not above_prev and (now - last_on) * 1000.0 >= minimum_gap_ms:
            onsets += 1
            last_on = now
        above_prev = ab
    return onsets


def analyze_probe_audio(source, *, sample_rate: int | None = None, lead_in: float = LEAD_IN_SECONDS) -> dict:
    channels = 1
    if isinstance(source, (str, Path)):
        left, right, rate = read_wav(source)
        channels = 1 if left is right else 2
    elif isinstance(source, (tuple, list)) and len(source) == 2 and isinstance(source[1], int):
        left = list(source[0])
        right, rate = left, int(source[1])
    elif isinstance(source, (list, array.array)):
        if sample_rate is None:
            raise ValueError("sample_rate is required for a sample list")
        left = list(source)
        right, rate = left, int(sample_rate)
    else:
        raise TypeError(f"Unsupported audio source: {type(source)!r}")
    if not left:
        raise ValueError("Probe audio is empty")
    mono = _mono(left, right)
    if not mono:
        raise ValueError("Probe audio is empty")

    peak = max(max(abs(v) for v in left), max(abs(v) for v in right))
    rms = _rms(list(left) + list(right))
    held_db = _rms_db_window(mono, rate, lead_in + 0.20, lead_in + 1.40)
    c2_db = _rms_db_window(mono, rate, lead_in + 1.51, lead_in + 1.73)
    octave_db = _rms_db_window(mono, rate, lead_in + OCTAVE_START, lead_in + CHORD_START)
    chord_db = _rms_db_window(mono, rate, lead_in + CHORD_START, lead_in + RUN_START)
    run_db = _rms_db_window(mono, rate, lead_in + RUN_START, lead_in + PROBE_SECONDS)

    pitch = estimate_fundamental(mono, rate, lead_in + 0.25, lead_in + 1.45)
    if pitch["confidence"] < 0.3:
        drift = octave = fundamental = midi_note = None
    else:
        drift = pitch["semitones_from_c4"]
        n_oct = round(drift / 12.0)
        octave = int(n_oct) if abs(drift - n_oct * 12.0) < 0.5 else 0
        fundamental, midi_note = pitch["frequency_hz"], pitch["midi_note"]

    attack = measure_attack_time(mono, rate, lead_in)
    tail = measure_tail_duration(mono, rate, lead_in + RUN_LAST_OFF)
    bands = band_balance(mono, rate, lead_in + 0.20, lead_in + 1.40)
    run_onsets = _count_onsets(mono, rate, lead_in + RUN_START, lead_in + PROBE_SECONDS)
    silent_low_notes = c2_db <= -55.0 or (held_db > -70.0 and c2_db < held_db - 40.0)

    pre_note_end = lead_in - 0.05
    if pre_note_end >= 0.05:
        noise_db = _rms_db_window(mono, rate, 0.0, pre_note_end)
        pre_note_noise = noise_db > -50.0 and (held_db <= -70.0 or noise_db > held_db - 30.0)
    else:
        noise_db, pre_note_noise = -120.0, False

    warnings: list[str] = []
    if _db(peak) < -70.0 or held_db < -70.0:
        warnings.append("silent on a held C4")
    elif pitch["confidence"] < 0.3:
        warnings.append("no clear pitch (noise, drum or heavily detuned)")
    elif drift is not None:
        cents = (drift - round(drift)) * 100.0
        if octave:
            warnings.append(f"C4 sounds {octave:+d} octave(s) from written pitch ({drift:+.2f} semitones); transpose by {-drift:+.2f} or retune the patch")
        elif abs(drift) >= 0.5: warnings.append(f"C4 sounds {drift:+.2f} semitones from written pitch; transpose the patch by {-drift:+.2f}")
        if abs(cents) >= 25.0: warnings.append(f"out of tune by {cents:+.0f} cents")
    if silent_low_notes: warnings.append("C2 is silent (outside the patch's range?)")
    if pre_note_noise: warnings.append(f"sound before the first note ({noise_db:.0f} dBFS): self-playing or noisy")
    if attack >= 250.0: warnings.append(f"slow attack {attack:.0f} ms")
    if tail >= 800.0: warnings.append(f"long tail {tail / 1000.0:.1f} s after note-off")
    if run_onsets and run_onsets < len(RUN_KEYS) - 1: warnings.append(f"16th run blurs: {run_onsets} of {len(RUN_KEYS)} onsets heard")
    if _db(peak) >= -0.1: warnings.append("probe peaks at full scale (patch may clip)")

    return {
        "duration_seconds": round(len(mono) / rate, 3), "sample_rate": rate, "channels": channels,
        "peak_dbfs": round(_db(peak), 2), "rms_dbfs": round(_db(rms), 2),
        "attack_time_ms": round(attack, 2), "tail_duration_ms": round(tail, 2),
        "pitch_drift_semitones": None if drift is None else round(drift, 3),
        "detected_fundamental_hz": fundamental, "detected_midi_note": midi_note,
        "octave_offset": octave, "pitch_confidence": pitch["confidence"],
        "band_balance": bands, "silent_low_notes": bool(silent_low_notes),
        "pre_note_noise": bool(pre_note_noise), "run_onsets": run_onsets,
        "section_levels_dbfs": {
            "held": round(held_db, 2), "octaves": round(octave_db, 2), "chord": round(chord_db, 2),
            "run": round(run_db, 2), "pre_note": round(noise_db, 2), "c2": round(c2_db, 2),
        },
        "warnings": warnings,
    }


def audition_plugin(
    plugin: str, *, kind: str = "", cid: str = "", state: str = "", preset: str = "", name: str = "",
    binary: str | Path | None = None, timeout: float = PROBE_TIMEOUT_SECONDS,
    allow_quarantined: bool = False, output_dir: str | Path | None = None, keep_audio: bool | None = None,
) -> dict:
    identity = str(plugin).strip()
    if not identity: return {"ok": False, "plugin": identity, "error": "A plugin is required"}
    state = state or preset
    if quarantine.is_quarantined(identity) and not allow_quarantined:
        rec = quarantine.quarantined_entry(identity)
        reason = rec.get("reason") if rec else ""
        return {"ok": False, "plugin": identity, "error": "plugin is quarantined" + (f" ({reason})" if reason else ""), "quarantined": True, "quarantine": rec, "hint": "unquarantine_plugin() after fixing or reinstalling the plugin"}
    keep = bool(output_dir or keep_audio)
    work_dir = Path(output_dir).expanduser() if output_dir else Path(tempfile.mkdtemp(prefix="mxm-audition-"))
    work_dir.mkdir(parents=True, exist_ok=True)
    disp = str(name).strip()
    try:
        try:
            p_path = build_probe_project(plugin, work_dir / "probe.mmp", kind=kind, cid=cid, state=state, name=disp)
        except (OSError, ValueError) as exc:
            return {"ok": False, "plugin": identity, "error": f"probe project could not be built: {exc}", "quarantined": False, "quarantine": None, "work_dir": str(work_dir) if keep else None}
        render = run_probe_render(p_path, work_dir / "probe.wav", binary=binary, timeout=timeout)
        if not render["ok"]:
            rec, q_err = None, None
            if render.get("reason"):
                try: rec = quarantine.quarantine_plugin(identity, str(render["reason"]), disp)
                except OSError as exc: q_err = str(exc)
            res = {"ok": False, "plugin": identity, "error": render.get("error") or "probe render failed", "quarantined": rec is not None, "quarantine": rec, "render": render, "work_dir": str(work_dir) if keep else None}
            if q_err: res["quarantine_error"] = q_err
            return res
        try:
            metrics = analyze_probe_audio(work_dir / "probe.wav")
        except (OSError, ValueError, TypeError, wave.Error) as exc:
            return {"ok": False, "plugin": identity, "error": f"probe audio could not be analyzed: {exc}", "quarantined": False, "quarantine": None, "render": render, "work_dir": str(work_dir) if keep else None}
        info = render.get("wav_info") or {}
        if (info.get("channels") and info["channels"] != 2) or (info.get("sample_rate") and info["sample_rate"] != 48000):
            metrics["warnings"].append(f"render is {info.get('channels')}-channel at {info.get('sample_rate')} Hz (asked for 48 kHz stereo)")
        return {
            "ok": True, "plugin": identity, "name": disp, "quarantined": False, "quarantine": None, "probe_seconds": PROBE_SECONDS,
            "render": {k: render.get(k) for k in ("returncode", "wav", "wav_info", "elapsed_seconds", "command")},
            "metrics": metrics, "warnings": metrics["warnings"], "work_dir": str(work_dir) if keep else None,
        }
    finally:
        if not keep: shutil.rmtree(work_dir, ignore_errors=True)
