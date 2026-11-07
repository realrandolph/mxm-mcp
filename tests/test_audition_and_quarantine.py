"""Tests for the acoustic audition probe and plugin quarantine records."""

from __future__ import annotations

import base64
import json
import math
import subprocess
import wave
from array import array
from pathlib import Path
from types import SimpleNamespace

import pytest

from mxm_mcp import audition, quarantine, xml_parser


# ---- helpers ----------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Keep quarantine records inside the test's temporary config root."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    return tmp_path


def tone(frequency: float, seconds: float, amplitude: float = 0.5,
         sample_rate: int = 48000, ramp: float = 0.0) -> list[float]:
    count = int(sample_rate * seconds)
    samples = []
    for index in range(count):
        time = index / sample_rate
        envelope = min(1.0, time / ramp) if ramp > 0 else 1.0
        samples.append(amplitude * envelope * math.sin(2 * math.pi * frequency * time))
    return samples


def _add(target: list[float], start: float, source: list[float]) -> None:
    begin = int(start * 48000)
    for offset, value in enumerate(source):
        position = begin + offset
        if 0 <= position < len(target):
            target[position] += value


def synthetic_probe(
    *,
    lead_in: float = audition.LEAD_IN_SECONDS,
    include_c2: bool = False,
    pre_note_noise: bool = False,
    held_frequency: float = 261.626,
) -> list[float]:
    """A probe-shaped buffer: held C4, optional C2, chord, 16ths and a tail."""
    total = lead_in + audition.PROBE_SECONDS + 0.6
    samples = [0.0] * int(total * 48000)
    if pre_note_noise:
        _add(samples, 0.08, tone(900.0, 0.12, amplitude=0.08))
    _add(samples, lead_in + audition.HELD_START,
         tone(held_frequency, 1.45, 0.5, ramp=0.02))
    octave_tones = {36: 65.406, 48: 130.813, 60: 261.626, 72: 523.251}
    for index, (key, frequency) in enumerate(octave_tones.items()):
        if key == 36 and not include_c2:
            continue
        start = lead_in + audition.OCTAVE_START + index * audition.OCTAVE_STEP
        _add(samples, start, tone(frequency, audition.OCTAVE_DURATION, 0.4))
    for key in audition.CHORD_KEYS:
        _add(samples, lead_in + audition.CHORD_START,
             tone(440.0 * 2 ** ((key - 69) / 12.0), audition.CHORD_DURATION, 0.12))
    for index, key in enumerate(audition.RUN_KEYS):
        start = lead_in + audition.RUN_START + index * audition.RUN_STEP
        _add(samples, start, tone(440.0 * 2 ** ((key - 69) / 12.0),
                                  audition.RUN_DURATION, 0.35))
    # A decaying tail after the final 16th.
    tail_start = lead_in + audition.RUN_LAST_OFF
    tail = [
        0.35 * math.exp(-index / 48000 / 0.06) * math.sin(2 * math.pi * 261.626 * index / 48000)
        for index in range(int(0.55 * 48000))
    ]
    _add(samples, tail_start, tail)
    return samples


def write_wav(path: Path, samples: list[float], sample_rate: int = 48000,
              channels: int = 1, right: list[float] | None = None) -> Path:
    frames = array("h")
    for index, value in enumerate(samples):
        clamped = max(-1.0, min(1.0, value))
        integer = int(round(clamped * 32767))
        if right is None:
            for _ in range(channels):
                frames.append(integer)
        else:
            frames.append(integer)
            other = max(-1.0, min(1.0, right[index]))
            frames.append(int(round(other * 32767)))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2 if right is not None else channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(frames.tobytes())
    return path


def _fake_render(returncode: int = 0, stdout: str = "", stderr: str = ""):
    def run(cmd, timeout):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    return run


def _rendering_wav(samples: list[float], sample_rate: int = 48000):
    def run(cmd, timeout):
        out = Path(cmd[cmd.index("-o") + 1])
        write_wav(out, samples, sample_rate=sample_rate, channels=2)
        return SimpleNamespace(returncode=0, stdout="rendered", stderr="")
    return run


VST3_CID = "A1B2C3D4E5F60718293A4B4C55667788"


# ---- quarantine storage -----------------------------------------------------------------------


def test_quarantine_round_trip_add_list_check_remove(isolated_config):
    assert quarantine.list_quarantined() == []
    assert not quarantine.is_quarantined("/plugins/Bad.vst3")

    entry = quarantine.quarantine_plugin(
        "/plugins/Bad.vst3", quarantine.REASON_SEGFAULT, name="Bad Synth"
    )
    assert entry["module"] == "/plugins/Bad.vst3"
    assert entry["name"] == "Bad Synth"
    assert entry["reason"] == "segfault during probe"
    assert entry["timestamp"].endswith("Z")

    stored = quarantine.list_quarantined()
    assert stored == [entry]
    assert quarantine.is_quarantined("/plugins/Bad.vst3")
    assert quarantine.is_quarantined("Bad Synth")
    assert not quarantine.is_quarantined("/plugins/Other.vst3")

    path = isolated_config / "mxm-mcp" / "quarantine.json"
    payload = json.loads(path.read_text())
    assert payload["version"] == 1
    assert list(payload["plugins"].values())[0]["reason"] == "segfault during probe"

    assert quarantine.unquarantine_plugin("/plugins/Bad.vst3") is True
    assert quarantine.list_quarantined() == []
    assert quarantine.unquarantine_plugin("/plugins/Bad.vst3") is False


def test_requarantine_updates_reason_and_keeps_name(isolated_config):
    quarantine.quarantine_plugin("/plugins/Slow.vst3", "timed out during render",
                                 name="Slow One")
    updated = quarantine.quarantine_plugin("/plugins/Slow.vst3", "user blocked")
    assert updated["reason"] == "user blocked"
    assert updated["name"] == "Slow One"
    assert len(quarantine.list_quarantined()) == 1


def test_quarantine_requires_identity(isolated_config):
    with pytest.raises(ValueError):
        quarantine.quarantine_plugin("   ", "segfault during probe")


def test_quarantine_legacy_file_is_read_and_migrated(isolated_config):
    legacy = isolated_config / "lmms-mcp" / "quarantine.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({
        "version": 1,
        "plugins": {
            "/old/Legacy.vst3": {
                "module": "/old/Legacy.vst3",
                "name": "Legacy",
                "reason": "user blocked",
                "timestamp": "2025-01-01T00:00:00Z",
            }
        },
    }))
    assert quarantine.is_quarantined("/old/Legacy.vst3")

    quarantine.quarantine_plugin("/new/Current.vst3", "segfault during probe")
    canonical = isolated_config / "mxm-mcp" / "quarantine.json"
    migrated = json.loads(canonical.read_text())["plugins"]
    modules = {record["module"] for record in migrated.values()}
    assert modules == {"/old/Legacy.vst3", "/new/Current.vst3"}

    # Once the canonical file exists, the stale legacy copy no longer counts.
    assert quarantine.unquarantine_plugin("/old/Legacy.vst3") is True
    assert not quarantine.is_quarantined("/old/Legacy.vst3")
    assert legacy.exists()


def test_corrupt_quarantine_file_is_ignored(isolated_config):
    path = isolated_config / "mxm-mcp" / "quarantine.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not valid json")
    assert quarantine.list_quarantined() == []
    assert quarantine.is_quarantined("/plugins/Anything.vst3") is False

    entry = quarantine.quarantine_plugin("/plugins/Anything.vst3", "user blocked")
    assert entry["module"] == "/plugins/Anything.vst3"
    assert json.loads(path.read_text())["plugins"]


def test_filter_quarantined_checks_every_identity_field(isolated_config):
    quarantine.quarantine_plugin("/plugins/Bad.vst3", quarantine.REASON_SEGFAULT,
                                 name="Bad Synth")
    quarantine.quarantine_plugin("http://example.test/lv2/silent", "user blocked",
                                 name="Silent LV2")
    plugins = [
        {"name": "Good VST3", "module": "/plugins/Good.vst3", "cid": "ABC"},
        {"name": "Bad Synth", "module": "/plugins/Bad.vst3", "cid": VST3_CID},
        {"name": "Silent LV2", "bundle": "/x/silent.lv2",
         "uri": "http://example.test/lv2/silent"},
        {"name": "Empty"},
        "free-form-entry",
    ]
    kept = quarantine.filter_quarantined(plugins)
    assert [entry.get("name") if isinstance(entry, dict) else entry for entry in kept] == [
        "Good VST3", "Empty", "free-form-entry"
    ]


# ---- probe phrase and project builder ---------------------------------------------------------


def test_probe_events_have_the_specified_pitches_and_timing():
    events = audition.probe_events()
    assert len(events) == 13

    held = events[0]
    assert held["part"] == "held"
    assert held["key"] == 60
    assert held["velocity"] == 90
    assert held["start"] == pytest.approx(0.0)
    assert held["end"] == pytest.approx(1.5)

    octaves = [event for event in events if event["part"] == "octaves"]
    assert [event["key"] for event in octaves] == [36, 48, 60, 72]
    assert [event["start"] for event in octaves] == pytest.approx([1.5, 1.75, 2.0, 2.25])

    chord = [event for event in events if event["part"] == "chord"]
    assert [event["key"] for event in chord] == [60, 63, 67, 70]
    assert all(event["start"] == pytest.approx(2.5) for event in chord)
    assert all(event["duration"] == pytest.approx(0.9) for event in chord)

    run = [event for event in events if event["part"] == "run"]
    assert [event["key"] for event in run] == [60, 62, 63, 65]
    assert [event["start"] for event in run] == pytest.approx([3.5, 3.625, 3.75, 3.875])
    assert all(event["duration"] == pytest.approx(0.08) for event in run)


def test_build_probe_project_writes_editable_notes(tmp_path):
    # 120 BPM at 48 ticks per beat is 96 ticks per second.
    assert audition._seconds_to_ticks(1.0, 120) == 96
    path = audition.build_probe_project("tripleoscillator", tmp_path / "probe.mmp",
                                        name="Probe")
    root = xml_parser.load_project(path)
    assert root.find(".//head").get("bpm") == "120"

    tracks = xml_parser.find_tracks(root)
    assert len(tracks) == 1
    track = tracks[0]
    assert track.find("instrumenttrack/instrument").get("name") == "tripleoscillator"

    clip = track.find("midiclip")
    assert clip is not None
    assert int(clip.get("len")) == audition._seconds_to_ticks(6.5, 120)
    assert clip.get("autoresize") == "0"

    notes = clip.findall("note")
    assert len(notes) == 13
    held_pos = audition._seconds_to_ticks(audition.LEAD_IN_SECONDS, 120)
    held = next(note for note in notes if int(note.get("key")) == 60
                and int(note.get("pos")) == held_pos)
    assert int(held.get("len")) == audition._seconds_to_ticks(1.5, 120)
    assert int(held.get("vol")) == 90
    c2 = next(note for note in notes if int(note.get("key")) == 36)
    assert int(c2.get("pos")) == audition._seconds_to_ticks(
        audition.LEAD_IN_SECONDS + 1.5, 120
    )


def test_build_probe_project_targets_vst3_state(tmp_path):
    state = base64.b64encode(b"opaque state").decode("ascii")
    module = str(tmp_path / "Fake Synth.vst3")
    path = audition.build_probe_project(
        module, tmp_path / "probe.mmp", kind="vst3", cid=VST3_CID, state=state
    )
    root = xml_parser.load_project(path)
    instrument = root.find(".//instrument[@name='vst3instrument']")
    assert instrument is not None
    wrapper = instrument.find("vst3instrument")
    assert wrapper.find("state").text == state
    attributes = {item.get("name"): item.get("value")
                  for item in wrapper.find("key").findall("attribute")}
    assert attributes["module"] == module
    assert attributes["cid"] == VST3_CID


def test_build_probe_project_resolves_lv2_bundle_uri(tmp_path):
    bundle = tmp_path / "Example Synth.lv2"
    bundle.mkdir()
    (bundle / "manifest.ttl").write_text(
        "@prefix lv2: <http://lv2plug.in/ns/lv2core#> .\n"
        "<http://example.test/synth> a lv2:InstrumentPlugin .\n"
    )
    path = audition.build_probe_project(bundle, tmp_path / "probe.mmp")
    root = xml_parser.load_project(path)
    instrument = root.find(".//instrument[@name='lv2instrument']")
    attribute = instrument.find("lv2controls/key/attribute[@name='uri']")
    assert attribute.get("value") == "http://example.test/synth"


def test_build_probe_project_requires_vst3_cid(tmp_path):
    with pytest.raises(ValueError, match="class id"):
        audition.build_probe_project(str(tmp_path / "x.vst3"), tmp_path / "p.mmp",
                                     kind="vst3")


# ---- audio analysis ---------------------------------------------------------------------------


def test_measure_attack_time_tracks_a_ramp():
    samples = tone(440.0, 0.4, ramp=0.02)
    attack = audition.measure_attack_time(samples, 48000)
    assert 5.0 <= attack <= 30.0
    instant = audition.measure_attack_time(tone(440.0, 0.4), 48000)
    assert instant <= 5.0


def test_measure_tail_duration_tracks_the_decay():
    held = tone(440.0, 0.5, amplitude=0.5)
    decay = [
        0.5 * math.exp(-index / 48000 / 0.06) * math.sin(2 * math.pi * 440.0 * index / 48000)
        for index in range(int(0.6 * 48000))
    ]
    tail = audition.measure_tail_duration(held + decay, 48000, 0.5)
    assert 100.0 <= tail <= 400.0
    assert audition.measure_tail_duration([0.0] * 48000, 48000, 0.1) == 0.0


def test_estimate_fundamental_detects_octave_drift():
    pitch = audition.estimate_fundamental(tone(261.626, 1.0), 48000)
    assert pitch["frequency_hz"] == pytest.approx(261.626, rel=1e-3)
    assert pitch["semitones_from_c4"] == pytest.approx(0.0, abs=0.05)
    assert pitch["confidence"] > 0.9

    down = audition.estimate_fundamental(tone(130.813, 1.0), 48000)
    assert down["semitones_from_c4"] == pytest.approx(-12.0, abs=0.05)

    up = audition.estimate_fundamental(tone(523.251, 1.0), 48000)
    assert up["semitones_from_c4"] == pytest.approx(12.0, abs=0.05)

    silent = audition.estimate_fundamental([0.0] * 48000, 48000)
    assert silent["frequency_hz"] is None
    assert silent["confidence"] == 0.0


@pytest.mark.parametrize(("frequency", "band"), [
    (40.0, "sub"),
    (200.0, "low_mid"),
    (3000.0, "high_mid"),
    (10000.0, "brightness"),
])
def test_band_balance_finds_the_dominant_band(frequency, band):
    balance = audition.band_balance(tone(frequency, 1.0), 48000)
    assert balance["dominant_band"] == band
    assert balance[f"{band}_share"] > 0.9
    assert balance[f"{band}_db"] > -20.0


def test_peak_and_rms_levels():
    samples = tone(440.0, 1.0, amplitude=0.5)
    analysis = audition.analyze_probe_audio((samples, 48000), lead_in=0.0)
    assert analysis["peak_dbfs"] == pytest.approx(-6.02, abs=0.1)
    assert analysis["rms_dbfs"] == pytest.approx(-9.03, abs=0.2)
    assert analysis["channels"] == 1


def test_analyze_probe_flags_silent_low_notes_and_note_pre_roll():
    samples = synthetic_probe(include_c2=False, pre_note_noise=False)
    analysis = audition.analyze_probe_audio((samples, 48000))
    assert analysis["silent_low_notes"] is True
    assert analysis["pre_note_noise"] is False
    assert any("C2 is silent" in warning for warning in analysis["warnings"])
    assert analysis["attack_time_ms"] < 100.0
    assert analysis["band_balance"]["dominant_band"] == "low_mid"
    assert analysis["detected_fundamental_hz"] == pytest.approx(261.626, rel=1e-2)

    noisy = synthetic_probe(include_c2=False, pre_note_noise=True)
    noisy_analysis = audition.analyze_probe_audio((noisy, 48000))
    assert noisy_analysis["pre_note_noise"] is True
    assert any("before the first note" in warning for warning in noisy_analysis["warnings"])


def test_analyze_probe_accepts_a_low_register_patch():
    samples = synthetic_probe(include_c2=True, pre_note_noise=False)
    analysis = audition.analyze_probe_audio((samples, 48000))
    assert analysis["silent_low_notes"] is False


def test_non_octave_transposition_is_not_reported_as_an_octave():
    frequency = 261.626 * 2 ** (9 / 12)  # a fifth up, not an octave
    samples = synthetic_probe(include_c2=True, held_frequency=frequency)
    analysis = audition.analyze_probe_audio((samples, 48000))
    assert analysis["pitch_drift_semitones"] == pytest.approx(9.0, abs=0.1)
    assert analysis["octave_offset"] == 0
    assert any("semitones from written pitch" in warning
               for warning in analysis["warnings"])
    assert not any("octave(s)" in warning for warning in analysis["warnings"])


def test_read_wav_preserves_stereo(tmp_path):
    left = tone(440.0, 0.1, amplitude=0.4)
    right = tone(880.0, 0.1, amplitude=0.2)
    path = write_wav(tmp_path / "stereo.wav", left, right=right)
    decoded_left, decoded_right, rate = audition.read_wav(path)
    assert rate == 48000
    assert len(decoded_left) == len(decoded_right) == len(left)
    assert decoded_left[0] == pytest.approx(left[0], abs=1e-3)
    assert decoded_right[0] == pytest.approx(right[0], abs=1e-3)


# ---- render orchestration and automatic quarantine -------------------------------------------


def test_build_and_render_success_returns_profile(tmp_path, monkeypatch):
    samples = synthetic_probe(include_c2=True)
    monkeypatch.setattr(audition, "_run_render_process", _rendering_wav(samples))
    result = audition.audition_plugin(
        "tripleoscillator", binary=tmp_path / "mxm",
        output_dir=tmp_path / "probe-out", name="Test Synth",
    )
    assert result["ok"] is True, result
    assert result["quarantined"] is False
    assert result["metrics"]["sample_rate"] == 48000
    assert result["metrics"]["channels"] == 2
    assert (tmp_path / "probe-out" / "probe.wav").is_file()
    assert (tmp_path / "probe-out" / "probe.mmp").is_file()
    assert quarantine.list_quarantined() == []


def test_render_segfault_quarantines_the_plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(audition, "_run_render_process", _fake_render(-11, stderr="Segmentation fault"))
    module = str(tmp_path / "Bad Synth.vst3")
    result = audition.audition_plugin(
        module, kind="vst3", cid=VST3_CID, binary=tmp_path / "mxm", name="Bad Synth"
    )
    assert result["ok"] is False
    assert result["quarantined"] is True
    assert result["quarantine"]["reason"] == "segfault during probe"
    assert result["quarantine"]["name"] == "Bad Synth"
    assert quarantine.is_quarantined(module)
    assert quarantine.is_quarantined("Bad Synth")
    assert "work_dir" in result and result["work_dir"] is None


def test_render_timeout_quarantines_the_plugin(tmp_path, monkeypatch):
    def timeout(cmd, timeout_seconds):
        raise subprocess.TimeoutExpired(cmd="mxm", timeout=timeout_seconds)

    monkeypatch.setattr(audition, "_run_render_process", timeout)
    result = audition.audition_plugin(
        "tripleoscillator", binary=tmp_path / "mxm", timeout=1.0
    )
    assert result["ok"] is False
    assert result["quarantined"] is True
    assert result["quarantine"]["reason"] == "timed out during probe"
    assert quarantine.is_quarantined("tripleoscillator")


def test_render_nonzero_exit_quarantines_with_exit_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(audition, "_run_render_process", _fake_render(3, stderr="boom"))
    result = audition.audition_plugin(
        "tripleoscillator", binary=tmp_path / "mxm"
    )
    assert result["ok"] is False
    assert result["quarantined"] is True
    assert "code 3" in result["quarantine"]["reason"]


def test_missing_binary_fails_without_quarantine(tmp_path, monkeypatch):
    monkeypatch.setattr(audition.mxm_app, "find_mxm_binary", lambda: None)
    result = audition.audition_plugin("tripleoscillator")
    assert result["ok"] is False
    assert result["quarantined"] is False
    assert result["quarantine"] is None
    assert quarantine.list_quarantined() == []


def test_invalid_probe_target_reports_build_error(tmp_path, monkeypatch):
    def explode(cmd, timeout):
        raise AssertionError("render must not run for an invalid target")

    monkeypatch.setattr(audition, "_run_render_process", explode)
    result = audition.audition_plugin(
        str(tmp_path / "No Cid.vst3"), kind="vst3", binary=tmp_path / "mxm"
    )
    assert result["ok"] is False
    assert result["quarantined"] is False
    assert "class id" in result["error"]
    assert quarantine.list_quarantined() == []


def test_quarantined_plugin_is_refused_before_rendering(tmp_path, monkeypatch):
    quarantine.quarantine_plugin("tripleoscillator", quarantine.REASON_SEGFAULT,
                                 name="TripleOsc")

    def explode(cmd, timeout):
        raise AssertionError("render must not run for a quarantined plugin")

    monkeypatch.setattr(audition, "_run_render_process", explode)
    result = audition.audition_plugin("tripleoscillator", binary=tmp_path / "mxm")
    assert result["ok"] is False
    assert result["quarantined"] is True
    assert "quarantined" in result["error"]
    assert "segfault during probe" in result["error"]


def test_unquarantine_allows_a_retry(tmp_path, monkeypatch):
    quarantine.quarantine_plugin("tripleoscillator", quarantine.REASON_SEGFAULT)
    assert quarantine.unquarantine_plugin("tripleoscillator") is True
    monkeypatch.setattr(
        audition, "_run_render_process", _rendering_wav(synthetic_probe(include_c2=True))
    )
    result = audition.audition_plugin("tripleoscillator", binary=tmp_path / "mxm")
    assert result["ok"] is True, result
