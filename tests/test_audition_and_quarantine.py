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

VST3_CID = "A1B2C3D4E5F60718293A4B4C55667788"


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    return tmp_path


def tone(frequency: float, seconds: float, amplitude: float = 0.5, sample_rate: int = 48000, ramp: float = 0.0) -> list[float]:
    return [amplitude * (min(1.0, (i / sample_rate) / ramp) if ramp > 0 else 1.0) * math.sin(2 * math.pi * frequency * (i / sample_rate)) for i in range(int(sample_rate * seconds))]


def _add(target: list[float], start: float, source: list[float]) -> None:
    b = int(start * 48000)
    for off, v in enumerate(source):
        if 0 <= b + off < len(target):
            target[b + off] += v


def synthetic_probe(*, lead_in: float = audition.LEAD_IN_SECONDS, include_c2: bool = False, pre_note_noise: bool = False, held_frequency: float = 261.626) -> list[float]:
    samples = [0.0] * int((lead_in + audition.PROBE_SECONDS + 0.6) * 48000)
    if pre_note_noise: _add(samples, 0.08, tone(900.0, 0.12, amplitude=0.08))
    _add(samples, lead_in + audition.HELD_START, tone(held_frequency, 1.45, 0.5, ramp=0.02))
    for idx, (k, f) in enumerate({36: 65.406, 48: 130.813, 60: 261.626, 72: 523.251}.items()):
        if k != 36 or include_c2: _add(samples, lead_in + audition.OCTAVE_START + idx * audition.OCTAVE_STEP, tone(f, audition.OCTAVE_DURATION, 0.4))
    for k in audition.CHORD_KEYS: _add(samples, lead_in + audition.CHORD_START, tone(440.0 * 2 ** ((k - 69) / 12.0), audition.CHORD_DURATION, 0.12))
    for idx, k in enumerate(audition.RUN_KEYS): _add(samples, lead_in + audition.RUN_START + idx * audition.RUN_STEP, tone(440.0 * 2 ** ((k - 69) / 12.0), audition.RUN_DURATION, 0.35))
    tail_start = lead_in + audition.RUN_LAST_OFF
    _add(samples, tail_start, [0.35 * math.exp(-i / 48000 / 0.06) * math.sin(2 * math.pi * 261.626 * i / 48000) for i in range(int(0.55 * 48000))])
    return samples


def write_wav(path: Path, samples: list[float], sample_rate: int = 48000, channels: int = 1, right: list[float] | None = None) -> Path:
    frames = array("h")
    for idx, v in enumerate(samples):
        frames.append(int(round(max(-1.0, min(1.0, v)) * 32767)))
        if right is not None: frames.append(int(round(max(-1.0, min(1.0, right[idx])) * 32767)))
        elif channels > 1:
            for _ in range(channels - 1): frames.append(int(round(max(-1.0, min(1.0, v)) * 32767)))
    with wave.open(str(path), "wb") as h:
        h.setnchannels(2 if right is not None else channels); h.setsampwidth(2); h.setframerate(sample_rate); h.writeframes(frames.tobytes())
    return path


def _fake_render(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return lambda cmd, timeout: SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _rendering_wav(samples: list[float], sample_rate: int = 48000):
    return lambda cmd, timeout: (write_wav(Path(cmd[cmd.index("-o") + 1]), samples, sample_rate=sample_rate, channels=2), SimpleNamespace(returncode=0, stdout="rendered", stderr=""))[1]


# ---- tests ----

def test_quarantine_round_trip_add_list_check_remove(isolated_config):
    assert quarantine.list_quarantined() == [] and not quarantine.is_quarantined("/plugins/Bad.vst3")
    entry = quarantine.quarantine_plugin("/plugins/Bad.vst3", quarantine.REASON_SEGFAULT, name="Bad Synth")
    assert entry["module"] == "/plugins/Bad.vst3" and entry["name"] == "Bad Synth"
    assert quarantine.is_quarantined("/plugins/Bad.vst3") and quarantine.is_quarantined("Bad Synth")
    assert not quarantine.is_quarantined("/plugins/Other.vst3")
    path = isolated_config / "mxm-mcp/quarantine.json"
    assert json.loads(path.read_text())["version"] == 1
    assert quarantine.unquarantine_plugin("/plugins/Bad.vst3") is True
    assert quarantine.list_quarantined() == [] and quarantine.unquarantine_plugin("/plugins/Bad.vst3") is False


def test_requarantine_updates_reason_and_keeps_name(isolated_config):
    quarantine.quarantine_plugin("/plugins/Slow.vst3", "timed out during render", name="Slow One")
    updated = quarantine.quarantine_plugin("/plugins/Slow.vst3", "user blocked")
    assert updated["reason"] == "user blocked" and updated["name"] == "Slow One" and len(quarantine.list_quarantined()) == 1


def test_quarantine_requires_identity(isolated_config):
    with pytest.raises(ValueError):
        quarantine.quarantine_plugin("   ", "segfault during probe")


def test_quarantine_legacy_file_is_read_and_migrated(isolated_config):
    legacy = isolated_config / "lmms-mcp/quarantine.json"; legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"version": 1, "plugins": {"/old/Legacy.vst3": {"module": "/old/Legacy.vst3", "name": "Legacy", "reason": "user blocked", "timestamp": "2025-01-01T00:00:00Z"}}}))
    assert quarantine.is_quarantined("/old/Legacy.vst3")
    quarantine.quarantine_plugin("/new/Current.vst3", "segfault during probe")
    migrated = json.loads((isolated_config / "mxm-mcp/quarantine.json").read_text())["plugins"]
    assert {r["module"] for r in migrated.values()} == {"/old/Legacy.vst3", "/new/Current.vst3"}
    assert quarantine.unquarantine_plugin("/old/Legacy.vst3") is True and not quarantine.is_quarantined("/old/Legacy.vst3") and legacy.exists()


def test_corrupt_quarantine_file_is_ignored(isolated_config):
    path = isolated_config / "mxm-mcp/quarantine.json"; path.parent.mkdir(parents=True); path.write_text("{not valid json")
    assert quarantine.list_quarantined() == [] and quarantine.is_quarantined("/plugins/Anything.vst3") is False
    entry = quarantine.quarantine_plugin("/plugins/Anything.vst3", "user blocked")
    assert entry["module"] == "/plugins/Anything.vst3" and json.loads(path.read_text())["plugins"]


def test_filter_quarantined_checks_every_identity_field(isolated_config):
    quarantine.quarantine_plugin("/plugins/Bad.vst3", quarantine.REASON_SEGFAULT, name="Bad Synth")
    quarantine.quarantine_plugin("http://example.test/lv2/silent", "user blocked", name="Silent LV2")
    plugins = [
        {"name": "Good VST3", "module": "/plugins/Good.vst3", "cid": "ABC"},
        {"name": "Bad Synth", "module": "/plugins/Bad.vst3", "cid": VST3_CID},
        {"name": "Silent LV2", "bundle": "/x/silent.lv2", "uri": "http://example.test/lv2/silent"},
        {"name": "Empty"}, "free-form-entry",
    ]
    assert [e.get("name") if isinstance(e, dict) else e for e in quarantine.filter_quarantined(plugins)] == ["Good VST3", "Empty", "free-form-entry"]


def test_probe_events_have_the_specified_pitches_and_timing():
    events = audition.probe_events()
    assert len(events) == 13 and events[0]["part"] == "held" and events[0]["key"] == 60 and events[0]["start"] == 0.0
    assert [e["key"] for e in events if e["part"] == "octaves"] == [36, 48, 60, 72]
    assert [e["key"] for e in events if e["part"] == "chord"] == [60, 63, 67, 70]
    assert [e["key"] for e in events if e["part"] == "run"] == [60, 62, 63, 65]


def test_build_probe_project_writes_editable_notes(tmp_path):
    assert audition._seconds_to_ticks(1.0, 120) == 96
    root = xml_parser.load_project(audition.build_probe_project("tripleoscillator", tmp_path / "probe.mmp", name="Probe"))
    assert root.find(".//head").get("bpm") == "120"
    tracks = xml_parser.find_tracks(root)
    assert len(tracks) == 1 and tracks[0].find("instrumenttrack/instrument").get("name") == "tripleoscillator"
    assert len(tracks[0].find("midiclip").findall("note")) == 13


def test_build_probe_project_targets_vst3_state(tmp_path):
    state = base64.b64encode(b"opaque state").decode("ascii")
    root = xml_parser.load_project(audition.build_probe_project(str(tmp_path / "Fake Synth.vst3"), tmp_path / "probe.mmp", kind="vst3", cid=VST3_CID, state=state))
    inst = root.find(".//instrument[@name='vst3instrument']")
    assert inst is not None and inst.find("vst3instrument/state").text == state


def test_build_probe_project_resolves_lv2_bundle_uri(tmp_path):
    bundle = tmp_path / "Example Synth.lv2"; bundle.mkdir()
    (bundle / "manifest.ttl").write_text("<http://example.test/synth> a lv2:InstrumentPlugin .\n")
    root = xml_parser.load_project(audition.build_probe_project(bundle, tmp_path / "probe.mmp"))
    assert root.find(".//instrument[@name='lv2instrument']/lv2controls/key/attribute[@name='uri']").get("value") == "http://example.test/synth"


def test_build_probe_project_requires_vst3_cid(tmp_path):
    with pytest.raises(ValueError, match="class id"):
        audition.build_probe_project(str(tmp_path / "x.vst3"), tmp_path / "p.mmp", kind="vst3")


def test_measure_attack_time_tracks_a_ramp():
    assert 5.0 <= audition.measure_attack_time(tone(440.0, 0.4, ramp=0.02), 48000) <= 30.0
    assert audition.measure_attack_time(tone(440.0, 0.4), 48000) <= 5.0


def test_measure_tail_duration_tracks_the_decay():
    held = tone(440.0, 0.5, amplitude=0.5)
    decay = [0.5 * math.exp(-i / 48000 / 0.06) * math.sin(2 * math.pi * 440.0 * i / 48000) for i in range(int(0.6 * 48000))]
    assert 100.0 <= audition.measure_tail_duration(held + decay, 48000, 0.5) <= 400.0
    assert audition.measure_tail_duration([0.0] * 48000, 48000, 0.1) == 0.0


def test_estimate_fundamental_detects_octave_drift():
    pitch = audition.estimate_fundamental(tone(261.626, 1.0), 48000)
    assert pitch["frequency_hz"] == pytest.approx(261.626, rel=1e-3)
    assert pitch["semitones_from_c4"] == pytest.approx(0.0, abs=0.05) and pitch["confidence"] > 0.9
    assert audition.estimate_fundamental(tone(130.813, 1.0), 48000)["semitones_from_c4"] == pytest.approx(-12.0, abs=0.05)
    assert audition.estimate_fundamental(tone(523.251, 1.0), 48000)["semitones_from_c4"] == pytest.approx(12.0, abs=0.05)
    assert audition.estimate_fundamental([0.0] * 48000, 48000)["frequency_hz"] is None


@pytest.mark.parametrize(("frequency", "band"), [(40.0, "sub"), (200.0, "low_mid"), (3000.0, "high_mid"), (10000.0, "brightness")])
def test_band_balance_finds_the_dominant_band(frequency, band):
    bal = audition.band_balance(tone(frequency, 1.0), 48000)
    assert bal["dominant_band"] == band and bal[f"{band}_share"] > 0.9 and bal[f"{band}_db"] > -20.0


def test_peak_and_rms_levels():
    analysis = audition.analyze_probe_audio((tone(440.0, 1.0, amplitude=0.5), 48000), lead_in=0.0)
    assert analysis["peak_dbfs"] == pytest.approx(-6.02, abs=0.1)
    assert analysis["rms_dbfs"] == pytest.approx(-9.03, abs=0.2) and analysis["channels"] == 1


def test_analyze_probe_flags_silent_low_notes_and_note_pre_roll():
    a1 = audition.analyze_probe_audio((synthetic_probe(include_c2=False, pre_note_noise=False), 48000))
    assert a1["silent_low_notes"] is True and a1["pre_note_noise"] is False
    assert any("C2 is silent" in w for w in a1["warnings"])
    a2 = audition.analyze_probe_audio((synthetic_probe(include_c2=False, pre_note_noise=True), 48000))
    assert a2["pre_note_noise"] is True and any("before the first note" in w for w in a2["warnings"])


def test_analyze_probe_accepts_a_low_register_patch():
    assert audition.analyze_probe_audio((synthetic_probe(include_c2=True, pre_note_noise=False), 48000))["silent_low_notes"] is False


def test_non_octave_transposition_is_not_reported_as_an_octave():
    a = audition.analyze_probe_audio((synthetic_probe(include_c2=True, held_frequency=261.626 * 2 ** (9 / 12)), 48000))
    assert a["pitch_drift_semitones"] == pytest.approx(9.0, abs=0.1) and a["octave_offset"] == 0
    assert any("semitones from written pitch" in w for w in a["warnings"]) and not any("octave(s)" in w for w in a["warnings"])


def test_read_wav_preserves_stereo(tmp_path):
    l, r = tone(440.0, 0.1, amplitude=0.4), tone(880.0, 0.1, amplitude=0.2)
    path = write_wav(tmp_path / "stereo.wav", l, right=r)
    dl, dr, rate = audition.read_wav(path)
    assert rate == 48000 and len(dl) == len(dr) == len(l)
    assert dl[0] == pytest.approx(l[0], abs=1e-3) and dr[0] == pytest.approx(r[0], abs=1e-3)


def test_build_and_render_success_returns_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(audition, "_run_render_process", _rendering_wav(synthetic_probe(include_c2=True)))
    res = audition.audition_plugin("tripleoscillator", binary=tmp_path / "mxm", output_dir=tmp_path / "probe-out", name="Test Synth")
    assert res["ok"] is True and res["quarantined"] is False and (tmp_path / "probe-out/probe.wav").is_file() and (tmp_path / "probe-out/probe.mmp").is_file()


def test_render_segfault_quarantines_the_plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(audition, "_run_render_process", _fake_render(-11, stderr="Segmentation fault"))
    mod = str(tmp_path / "Bad Synth.vst3")
    res = audition.audition_plugin(mod, kind="vst3", cid=VST3_CID, binary=tmp_path / "mxm", name="Bad Synth")
    assert res["ok"] is False and res["quarantined"] is True and quarantine.is_quarantined(mod) and quarantine.is_quarantined("Bad Synth")


def test_render_timeout_quarantines_the_plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(audition, "_run_render_process", lambda c, t: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd="mxm", timeout=t)))
    res = audition.audition_plugin("tripleoscillator", binary=tmp_path / "mxm", timeout=1.0)
    assert res["ok"] is False and res["quarantined"] is True and quarantine.is_quarantined("tripleoscillator")


def test_render_nonzero_exit_quarantines_with_exit_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(audition, "_run_render_process", _fake_render(3, stderr="boom"))
    res = audition.audition_plugin("tripleoscillator", binary=tmp_path / "mxm")
    assert res["ok"] is False and res["quarantined"] is True and "code 3" in res["quarantine"]["reason"]


def test_missing_binary_fails_without_quarantine(tmp_path, monkeypatch):
    monkeypatch.setattr(audition.mxm_app, "find_mxm_binary", lambda: None)
    res = audition.audition_plugin("tripleoscillator")
    assert res["ok"] is False and res["quarantined"] is False and quarantine.list_quarantined() == []


def test_invalid_probe_target_reports_build_error(tmp_path, monkeypatch):
    monkeypatch.setattr(audition, "_run_render_process", lambda c, t: (_ for _ in ()).throw(AssertionError("should not run")))
    res = audition.audition_plugin(str(tmp_path / "No Cid.vst3"), kind="vst3", binary=tmp_path / "mxm")
    assert res["ok"] is False and "class id" in res["error"] and quarantine.list_quarantined() == []


def test_quarantined_plugin_is_refused_before_rendering(tmp_path, monkeypatch):
    quarantine.quarantine_plugin("tripleoscillator", quarantine.REASON_SEGFAULT, name="TripleOsc")
    monkeypatch.setattr(audition, "_run_render_process", lambda c, t: (_ for _ in ()).throw(AssertionError("should not run")))
    res = audition.audition_plugin("tripleoscillator", binary=tmp_path / "mxm")
    assert res["ok"] is False and res["quarantined"] is True and "quarantined" in res["error"]


def test_unquarantine_allows_a_retry(tmp_path, monkeypatch):
    quarantine.quarantine_plugin("tripleoscillator", quarantine.REASON_SEGFAULT)
    assert quarantine.unquarantine_plugin("tripleoscillator") is True
    monkeypatch.setattr(audition, "_run_render_process", _rendering_wav(synthetic_probe(include_c2=True)))
    assert audition.audition_plugin("tripleoscillator", binary=tmp_path / "mxm")["ok"] is True
