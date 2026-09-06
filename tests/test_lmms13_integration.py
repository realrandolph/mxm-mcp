"""End-to-end validation against an installed LMMS 1.3 build."""

import json
import os
import subprocess
import wave
from pathlib import Path

import pytest

from lmms_mcp import server
from lmms_mcp import xml_parser
from lmms_mcp.project import LMMSProject


LMMS_EXE = os.environ.get("LMMS_EXECUTABLE", "/home/dolf/.local/bin/lmms")
SURGE_LV2 = Path("/usr/lib/lv2/Surge XT.lv2")


def _audio_peak(path: Path) -> int:
    with wave.open(str(path), "rb") as audio:
        frames = audio.readframes(audio.getnframes())
    width = audio.getsampwidth()
    return max(
        abs(int.from_bytes(frames[offset:offset + width], "little", signed=True))
        for offset in range(0, len(frames), width)
    ) if frames else 0


def _build_validation_project() -> LMMSProject:
    project = LMMSProject()
    project.new(bpm=120)
    server.set_project(project)

    for name in ("Native Kick", "Native Bass", "Native Pad", "Native Lead"):
        project.add_mixer_channel(name)

    native = (
        ("Native Kick", "kicker", 1, 36),
        ("Native Bass", "tripleoscillator", 2, 48),
        ("Native Pad", "organic", 3, 60),
        ("Native Lead", "monstro", 4, 72),
    )
    for name, instrument, mixer_channel, key in native:
        result = project.add_track(
            "instrument", name, instrument=instrument,
            mixer_channel=mixer_channel, volume=85,
        )
        project.add_note(result["track_index"], key=key, length=144)

    surge = json.loads(server.add_surge_xt_track(
        "Surge XT via Carla Rack", str(SURGE_LV2), mixer_channel=4, volume=85,
    ))
    assert surge["bridge"] == "carlarack"
    project.add_note(4, key=67, length=144)

    # Put effects on real mixer buses, not directly on the instrument tracks.
    server.add_effect("mixer", 1, "compressor")
    server.add_effect("mixer", 2, "bassbooster")
    server.add_effect("mixer", 3, "reverbsc")
    server.add_effect("mixer", 4, "delay")
    server.add_effect("mixer", 0, "stereoenhancer")
    return project


@pytest.mark.integration
def test_lmms13_native_and_carla_surge_reopen(tmp_path):
    """LMMS must load native instruments and Carla/Surge together after save."""
    exe = Path(LMMS_EXE)
    if not exe.exists():
        pytest.skip("LMMS executable is not installed")
    if not xml_parser.SURGE_XT_LV2_URI:
        pytest.skip("Surge XT LV2 URI is unavailable")
    if not SURGE_LV2.is_dir():
        pytest.skip("Surge XT LV2 bundle is not installed")

    project = _build_validation_project()
    project_path = tmp_path / "lmms13_native_carla_surge.mmp"
    project.save(project_path, compressed=False)

    root = xml_parser.load_project(project_path)
    tracks = xml_parser.find_tracks(root)
    assert [track.find("instrumenttrack/instrument").get("name") for track in tracks] == [
        "kicker", "tripleoscillator", "organic", "monstro", "carlarack",
    ]
    surge_state = tracks[4].find(
        "instrumenttrack/instrument/carlarack/CARLA-PROJECT"
    )
    assert surge_state is not None
    assert surge_state.findtext("Plugin/Info/URI") == xml_parser.SURGE_XT_LV2_URI
    assert surge_state.findtext("Plugin/Data/Active") == "Yes"
    assert surge_state.findtext("Plugin/Data/ControlChannel") == "1"
    assert surge_state.findtext("Plugin/Data/Options") == "0x3f1"
    assert any(
        item.findtext("Key") == f"{xml_parser.SURGE_XT_LV2_URI}:StateString"
        for item in surge_state.findall("Plugin/Data/CustomData")
    )
    assert all(track.find("instrumenttrack/fxchain") is not None for track in tracks)
    assert all(channel.find("send[@channel='0']") is not None for channel in
               xml_parser.find_mixer_channels(root)[1:])

    rendered = tmp_path / "stems"
    rendered.mkdir()
    proc = subprocess.run(
        [str(exe), "rendertracks", str(project_path), "-o", str(rendered)],
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr

    stems = sorted(rendered.glob("*.wav"))
    assert len(stems) >= 5, proc.stdout + "\n" + proc.stderr
    stem_text = {stem.stem.lower(): stem for stem in stems}
    for name in ("native kick", "native bass", "native pad", "native lead", "surge xt via carla rack"):
        matches = [path for stem, path in stem_text.items() if name in stem]
        assert matches, f"No rendered stem for {name}: {stems}"
        if name.startswith("native"):
            assert _audio_peak(matches[0]) > 0, f"Rendered stem is silent: {matches[0]}"
