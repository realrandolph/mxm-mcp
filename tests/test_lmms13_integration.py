"""End-to-end native Surge XT validation against the installed MXM."""

import asyncio
import json
import subprocess
import wave
from pathlib import Path

import pytest

from lmms_mcp import lmms_app, server, xml_parser
from lmms_mcp.project import LMMSProject


MXM_BINARY = lmms_app.find_mxm_binary()
SURGE_LV2 = Path("/usr/lib/lv2/Surge XT.lv2")
TALKINGBASS_LV2 = Path.home() / ".lv2" / "talkingbass.lv2"


def _audio_peak(path: Path) -> int:
    with wave.open(str(path), "rb") as audio:
        frames = audio.readframes(audio.getnframes())
        width = audio.getsampwidth()
    return max(
        abs(int.from_bytes(frames[offset:offset + width], "little", signed=True))
        for offset in range(0, len(frames), width)
    ) if frames else 0


def build_native_surge_validation_project() -> LMMSProject:
    project = LMMSProject()
    project.new(bpm=120)
    server.set_project(project)
    result = json.loads(server.add_surge_xt_track("Native Surge XT", str(SURGE_LV2)))
    assert result["host"] == "lv2instrument"
    for key, pos in ((60, 0), (64, 48), (67, 96), (72, 144)):
        project.add_note(0, key=key, pos=pos, length=42, volume=90)
    return project


@pytest.mark.integration
def test_lmms13_native_surge_renders_audibly(tmp_path):
    exe = MXM_BINARY
    if exe is None:
        pytest.skip("MXM binary is not installed")
    if not SURGE_LV2.is_dir():
        pytest.skip("Surge XT LV2 bundle is not installed")

    project = build_native_surge_validation_project()
    project_path = tmp_path / "native_surge_xt.mmp"
    output_path = tmp_path / "native_surge_xt.wav"
    project.save(project_path, compressed=False)

    root = xml_parser.load_project(project_path)
    instrument = root.find(".//instrument[@name='lv2instrument']")
    assert instrument is not None
    assert instrument.find("carlarack") is None
    assert instrument.find("lv2controls/key/attribute[@name='uri']").get("value") == (
        xml_parser.SURGE_XT_LV2_URI
    )

    proc = subprocess.run(
        [str(exe), "render", str(project_path), "-o", str(output_path)],
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    assert _audio_peak(output_path) > 0


def build_talking_bass_validation_project() -> LMMSProject:
    project = LMMSProject()
    project.new(bpm=150)
    server.set_project(project)
    result = json.loads(server.add_talking_bass_track("TalkBass"))
    assert result["host"] == "lv2instrument"
    assert result["plugin_id"] == xml_parser.TALKINGBASS_LV2_URI
    for key, pos in ((36, 0), (40, 48), (43, 96), (36, 144)):
        project.add_note(0, key=key, pos=pos, length=42, volume=100)
    return project


@pytest.mark.integration
def test_lmms13_talking_bass_renders_audibly(tmp_path):
    exe = MXM_BINARY
    if exe is None:
        pytest.skip("MXM binary is not installed")
    if not (TALKINGBASS_LV2 / "talkingbass.so").is_file():
        pytest.skip("Talking Bass LV2 is not installed")

    project = build_talking_bass_validation_project()
    project_path = tmp_path / "talking_bass.mmp"
    output_path = tmp_path / "talking_bass.wav"
    project.save(project_path, compressed=False)

    root = xml_parser.load_project(project_path)
    instrument = root.find(".//instrument[@name='lv2instrument']")
    assert instrument is not None
    assert instrument.find("lv2controls/key/attribute[@name='uri']").get("value") == (
        xml_parser.TALKINGBASS_LV2_URI
    )
    assert root.find(".//effect[@name='lv2effect']") is None

    proc = subprocess.run(
        [str(exe), "render", str(project_path), "-o", str(output_path)],
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    assert proc.returncode != 139
    assert _audio_peak(output_path) > 0


def _wav_stats(path: Path) -> dict:
    import math
    with wave.open(str(path), "rb") as audio:
        channels = audio.getnchannels()
        rate = audio.getframerate()
        frames = audio.getnframes()
        width = audio.getsampwidth()
        raw = audio.readframes(frames)
    peak = 0
    total = 0.0
    count = 0
    max_amp = float(1 << (8 * width - 1))
    for offset in range(0, len(raw), width):
        sample = int.from_bytes(raw[offset:offset + width], "little", signed=True)
        peak = max(peak, abs(sample))
        total += sample * sample
        count += 1
    rms = math.sqrt(total / count) / max_amp if count else 0.0
    peak_n = peak / max_amp if max_amp else 0.0
    return {
        "channels": channels,
        "sample_rate": rate,
        "frames": frames,
        "duration": frames / rate if rate else 0,
        "rms": rms,
        "peak": peak_n,
    }


def build_effect_controls_validation_project() -> LMMSProject:
    project = LMMSProject()
    project.new(bpm=140)
    server.set_project(project)
    server.add_instrument_track("Kick", instrument="kicker")
    server.add_instrument_track("Bass", instrument="tripleoscillator")
    server.add_instrument_track("Lead", instrument="tripleoscillator")
    json.loads(server.add_mixer_channel("Drums", 1.0))
    json.loads(server.add_mixer_channel("Music", 1.0))
    kick = xml_parser.find_tracks(project.root)[0].find("instrumenttrack")
    bass = xml_parser.find_tracks(project.root)[1].find("instrumenttrack")
    lead = xml_parser.find_tracks(project.root)[2].find("instrumenttrack")
    kick.set("mixch", "1")
    bass.set("mixch", "2")
    lead.set("mixch", "2")
    kick.set("vol", "72")
    bass.set("vol", "64")
    lead.set("vol", "58")
    server.add_effect("track", 0, "compressor", params={"threshold": -12, "ratio": 3})
    server.add_effect("track", 1, "dualfilter", params={"cut1": 1800, "mix": 0.25})
    server.add_effect("track", 1, "waveshaper", params={"inputGain": 1.2})
    server.add_effect("track", 2, "delay", wet=0.35, params={"DelayTimeSamples": 0.25, "FeebackAmount": 0.22})
    server.add_effect("mixer", 1, "eq", params={"Peak1active": 1, "Peak1gain": 2.5, "Peak1freq": 80})
    server.add_effect("mixer", 2, "reverbsc", wet=0.28, params={"size": 0.62, "color": 8500})
    server.add_effect("mixer", 0, "stereoenhancer", params={"width": 25})
    for pos in (0, 48, 96, 144):
        project.add_note(0, key=36, pos=pos, length=24, volume=100)
        project.add_note(1, key=40, pos=pos, length=42, volume=90)
    project.add_note(2, key=64, pos=0, length=96, volume=85)
    project.add_note(2, key=67, pos=96, length=96, volume=85)
    return project


def _assert_complete_effect_controls(root):
    from lmms_mcp.effects import EFFECT_SPECS
    effects = list(root.findall(".//effect"))
    assert effects
    for effect in effects:
        name = effect.get("name")
        spec = EFFECT_SPECS[name]
        controls = effect.find(spec.controls_tag)
        assert controls is not None, name
        assert controls.attrib, f"{name} has an empty control block"
        required = {param.name for param in spec.params}
        assert required <= set(controls.attrib), name


@pytest.mark.integration
def test_lmms13_effect_controls_render_audibly(tmp_path):
    exe = MXM_BINARY
    if exe is None:
        pytest.skip("MXM binary is not installed")

    project = build_effect_controls_validation_project()
    project_path = tmp_path / "effect_controls.mmp"
    output_path = tmp_path / "effect_controls.wav"
    project.save(project_path, compressed=False)
    _assert_complete_effect_controls(xml_parser.load_project(project_path))

    proc = subprocess.run(
        [str(exe), "render", str(project_path), "-o", str(output_path),
         "-f", "wav", "-s", "48000"],
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    stats = _wav_stats(output_path)
    assert stats["sample_rate"] == 48000
    assert stats["channels"] == 2
    assert stats["peak"] > 0.01
    assert stats["peak"] <= 1.0
    assert stats["rms"] > 0.005
    assert stats["rms"] < 0.45
    assert stats["rms"] / max(stats["peak"], 1e-9) < 0.7


def _find_mxm_binary() -> Path | None:
    """Locate the MXM binary (LMMS fork with native VST3 hosting)."""
    build = Path.home() / "lmms" / "build" / "mxm"
    return MXM_BINARY or (build if build.is_file() else None)


@pytest.mark.integration
def test_mxm_native_vst3_renders_audibly(tmp_path):
    from lmms_mcp import vst3

    exe = _find_mxm_binary()
    if exe is None:
        pytest.skip("MXM binary is not installed")
    instruments = [p for p in vst3.discover_vst3_plugins() if p["is_instrument"]]
    if not instruments:
        pytest.skip("No native VST3 instrument is installed")
    chosen = next((p for p in instruments if p["name"] == "Surge XT"), instruments[0])

    project = LMMSProject()
    project.new(bpm=120)
    server.set_project(project)
    result = json.loads(asyncio.run(
        server.add_vst3_instrument_track("Native VST3", plugin=chosen["name"])))
    assert result.get("host") == "vst3instrument" and result.get("verified") is True
    assert result["native"] is True and result["carla"] is False
    for key, pos in ((60, 0), (64, 48), (67, 96), (72, 144)):
        project.add_note(0, key=key, pos=pos, length=42, volume=90)

    project_path = tmp_path / "native_vst3.mmp"
    output_path = tmp_path / "native_vst3.wav"
    project.save(project_path, compressed=False)
    root = xml_parser.load_project(project_path)
    instrument = root.find(".//instrument[@name='vst3instrument']")
    assert instrument is not None
    assert root.find(".//instrument[@name='carlarack']") is None
    assert root.find(".//lv2controls") is None
    attrs = {a.get("name"): a.get("value")
             for a in instrument.findall("vst3instrument/key/attribute")}
    assert attrs == {"module": chosen["module"], "cid": chosen["cid"]}
    assert root.find(".//midiclip/note") is not None

    proc = subprocess.run(
        [str(exe), "render", str(project_path), "-o", str(output_path),
         "-f", "wav", "-s", "48000"],
        capture_output=True, text=True, timeout=240,
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    assert _audio_peak(output_path) > 0, "native VST3 render was silent"
