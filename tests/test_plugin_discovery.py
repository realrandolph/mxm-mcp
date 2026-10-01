"""Filesystem-only VST3/LV2 plugin and preset discovery regressions."""

from __future__ import annotations

import base64
import json
import os
import struct
from pathlib import Path

import pytest

from lmms_mcp import discovery_cache, lv2, server, vst3, vst3_presets, xml_parser
from lmms_mcp.project import LMMSProject

CID = "A1B2C3D4E5F60718293A4B4C55667788"
PLUGIN_URI = "https://example.test/plugins/fixture-synth"
PRESET_URI = "https://example.test/presets/warm-pad"


@pytest.fixture(autouse=True)
def clear_cache():
    discovery_cache.clear_discovery_cache()
    yield
    discovery_cache.clear_discovery_cache()


def _write_vstpreset(
    path: Path,
    *,
    cid: str = CID,
    state: bytes = b"opaque component state",
    controller_state: bytes = b"",
):
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = (b'<MetaInfo><Attribute name="Name" value="Warm Pad" />'
                b'<Attribute name="Author" value="Ada Example" />'
                b'<Attribute name="Category" value="Pads" />'
                b'<Attribute name="Tags" value="soft, wide" />'
                b'<Attribute name="Character" value="soft, wide" /></MetaInfo>')
    payload_offset = 48
    controller_offset = payload_offset + len(state)
    meta_offset = controller_offset + len(controller_state)
    chunk_table_offset = meta_offset + len(metadata)
    table = struct.pack(">I", 2 + bool(controller_state))
    table += b"Comp" + struct.pack(">QQ", payload_offset, len(state))
    if controller_state:
        table += b"Cont" + struct.pack(">QQ", controller_offset, len(controller_state))
    table += b"Meta" + struct.pack(">QQ", meta_offset, len(metadata))
    header = (b"VST3" + struct.pack(">I", 1) + cid.encode("ascii")
              + struct.pack(">Q", chunk_table_offset))
    path.write_bytes(header + state + controller_state + metadata + table)
    return state


def _fixture_lv2_bundle(root: Path) -> tuple[Path, Path]:
    bundle = root / "Fixture Synth.lv2"
    bundle.mkdir(parents=True)
    (bundle / "manifest.ttl").write_text(f'''@prefix lv2: <http://lv2plug.in/ns/lv2core#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<{PLUGIN_URI}> a lv2:InstrumentPlugin ;
  lv2:binary <fixture.so> ; rdfs:seeAlso <plugin.ttl>, <presets.ttl> .
''')
    (bundle / "plugin.ttl").write_text(f'''@prefix lv2: <http://lv2plug.in/ns/lv2core#> .
@prefix doap: <http://usefulinc.com/ns/doap#> .
@prefix foaf: <http://xmlns.com/foaf/0.1/> .
<{PLUGIN_URI}> a lv2:InstrumentPlugin ; doap:name "Fixture Synth" ;
  doap:maintainer [ foaf:name "Example Audio" ] ;
  lv2:port [ a lv2:InputPort, lv2:ControlPort ; lv2:index 0 ;
    lv2:symbol "gain" ; lv2:name "Gain" ; lv2:default 0.5 ;
    lv2:minimum 0.0 ; lv2:maximum 1.0 ] .
''')
    preset_file = bundle / "presets.ttl"
    preset_file.write_text(f'''@prefix lv2: <http://lv2plug.in/ns/lv2core#> .
@prefix pset: <http://lv2plug.in/ns/ext/presets#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix dc: <http://purl.org/dc/elements/1.1/> .
<{PRESET_URI}> a pset:Preset ; lv2:appliesTo <{PLUGIN_URI}> ;
  rdfs:label "Warm Pad" ; dc:creator "Ada Example" ; dc:subject "warm", "pad" ;
  lv2:port [ lv2:symbol "gain" ; pset:value 0.75 ] .
''')
    return bundle, preset_file


def _vst3_plugin(bundle: Path) -> dict:
    return {"name": "Fixture Synth", "vendor": "Example Audio",
            "module": str(bundle), "cid": CID, "is_instrument": True,
            "sub_categories": "Instrument|Synth", "version": "1.0",
            "class_flags": 0}


def _new_project(instrument: str, name: str = "Synth") -> LMMSProject:
    project = LMMSProject()
    project.new()
    result = project.add_track("instrument", name, instrument=instrument)
    track = xml_parser.find_track_element(project.root, result["track_index"])
    return project, track


def test_vst3_plugin_search_paths_include_linux_standard_and_environment(tmp_path, monkeypatch):
    (tmp_path / "FromPath.vst3").mkdir()
    monkeypatch.setattr(vst3, "standard_vst3_dirs", lambda: [tmp_path / "standard"])
    monkeypatch.delenv(vst3.PATH_ONLY_ENV, raising=False)
    monkeypatch.setenv("VST3_PATH", str(tmp_path))
    assert vst3.find_vst3_bundles() == [tmp_path / "FromPath.vst3"]
    assert str(tmp_path / "standard") in vst3.effective_search_paths()


def test_linux_vst3_standard_paths_include_home_usr_and_local(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("lmms_mcp.vst3_platform.LINUX", True)
    monkeypatch.setattr("lmms_mcp.vst3_platform.WINDOWS", False)
    monkeypatch.setattr("lmms_mcp.vst3_platform.MACOS", False)
    dirs = vst3.standard_vst3_dirs()
    assert tmp_path / ".vst3" in dirs
    assert Path("/usr/lib/vst3") in dirs
    assert Path("/usr/local/lib/vst3") in dirs


def test_vst3_plugin_probe_results_are_cached_and_invalidate_on_binary_change(
        tmp_path, monkeypatch):
    bundle = tmp_path / "Cache.vst3"
    binary = bundle / "Contents" / "x86_64-linux" / "Cache.so"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"version one")
    monkeypatch.setattr(vst3, "native_vst3_discovery_supported", lambda: True)
    monkeypatch.setattr(vst3, "standard_vst3_dirs", lambda: [tmp_path])
    monkeypatch.delenv(vst3.PATH_ONLY_ENV, raising=False)
    monkeypatch.delenv("VST3_PATH", raising=False)
    calls = []

    def probe(path):
        calls.append(path)
        return {"module": str(path), "classes": [{
            "cid": CID, "name": "Cache Synth", "category": "Audio Module Class",
            "sub_categories": "Instrument|Synth", "vendor": "Example", "version": "1",
            "class_flags": 0,
        }]}

    monkeypatch.setattr(vst3, "_probe_bundle", probe)
    first = vst3.discover_vst3_plugins()
    assert vst3.discover_vst3_plugins() == first
    assert len(calls) == 1
    binary.write_bytes(b"changed binary")
    os.utime(binary, ns=(binary.stat().st_atime_ns, binary.stat().st_mtime_ns + 1_000_000))
    vst3.discover_vst3_plugins()
    assert len(calls) == 2


def test_vst3_preset_container_metadata_association_and_embedded_component_state(
        tmp_path, monkeypatch):
    plugin_bundle = tmp_path / "Fixture Synth.vst3"
    plugin_bundle.mkdir()
    preset_path = tmp_path / "Factory" / "Warm Pad.vstpreset"
    component_state = _write_vstpreset(preset_path)
    monkeypatch.setattr(vst3_presets, "preset_search_paths",
                        lambda plugins=None: [preset_path.parent])
    parsed = vst3_presets.parse_vstpreset(preset_path)
    assert parsed["cid"] == CID
    assert parsed["name"] == "Warm Pad"
    assert parsed["metadata"]["author"] == "Ada Example"
    assert base64.b64decode(parsed["state"]) == component_state

    index = vst3_presets.discover_vst3_presets(
        [_vst3_plugin(plugin_bundle)], refresh=True)
    preset, = index["presets"]
    assert preset["plugin_name"] == "Fixture Synth"
    assert preset["plugin_cid"] == CID
    assert preset["origin"] == "factory"
    assert preset["state_chunks"] == ["Comp"]
    assert preset["loadable"] is True
    assert preset["category"] == "Pads"
    assert preset["tags"] == ["soft", "wide"]
    assert preset["character"] == "soft, wide"
    assert preset["loadable"] is True


def test_vst3_controller_state_chunk_is_reported_and_not_marked_loadable(tmp_path):
    preset_path = tmp_path / "Warm Pad.vstpreset"
    _write_vstpreset(preset_path, controller_state=b"opaque controller state")
    parsed = vst3_presets.parse_vstpreset(preset_path)
    assert parsed["state_chunks"] == ["Comp", "Cont"]
    assert parsed["requires_controller_state"] is True
    assert parsed["unsupported_state_chunks"] == ["Cont"]
    assert parsed["loadable"] is False


def test_vst3_user_factory_preset_banks_and_h2p_are_indexed_without_fake_metadata(
        tmp_path, monkeypatch):
    root = tmp_path / "banks"
    factory = root / "Factory" / "Bass"
    user = root / "User" / "Leads"
    _write_vstpreset(factory / "Factory Lead.vstpreset")
    (user / "User Lead.h2p").parent.mkdir(parents=True)
    (user / "User Lead.h2p").write_text("# opaque user preset")
    monkeypatch.setattr(vst3_presets, "preset_search_paths", lambda plugins=None: [root])
    plugin = _vst3_plugin(tmp_path / "Fixture Synth.vst3")
    presets = vst3_presets.discover_vst3_presets([plugin], refresh=True)["presets"]
    by_name = {item["name"]: item for item in presets}
    assert set(by_name) == {"Warm Pad", "User Lead"}
    assert by_name["Warm Pad"]["origin"] == "factory"
    assert by_name["User Lead"]["origin"] == "user"
    assert by_name["User Lead"]["author"] is None
    assert by_name["User Lead"]["loadable"] is False


def test_lv2_manifest_and_referenced_rdf_expose_plugins_ports_and_presets(tmp_path, monkeypatch):
    root = tmp_path / "lv2"
    bundle, _ = _fixture_lv2_bundle(root)
    monkeypatch.setattr(lv2, "standard_lv2_dirs", lambda: [root])
    monkeypatch.delenv("LV2_PATH", raising=False)
    index = lv2.discover_lv2(refresh=True)
    plugin, = index["plugins"]
    preset, = index["presets"]
    assert plugin["uri"] == PLUGIN_URI
    assert plugin["name"] == "Fixture Synth"
    assert plugin["vendor"] == "Example Audio"
    assert plugin["bundle"] == str(bundle)
    assert plugin["is_instrument"] is True
    assert plugin["ports"][0]["symbol"] == "gain"
    assert preset["id"] == PRESET_URI
    assert preset["name"] == "Warm Pad"
    assert preset["author"] == "Ada Example"
    assert preset["tags"] == ["pad", "warm"]
    assert preset["port_values"] == {"gain": 0.75}
    assert preset["loadable"] is True


def test_lv2_path_and_cache_invalidation_on_preset_edit(tmp_path, monkeypatch):
    bundle, preset_path = _fixture_lv2_bundle(tmp_path / "configured")
    monkeypatch.setattr(lv2, "standard_lv2_dirs", lambda: [])
    monkeypatch.setenv("LV2_PATH", str(bundle.parent))
    before = lv2.discover_lv2()
    assert len(before["plugins"]) == 1
    assert lv2.discover_lv2() is before
    preset_path.write_text(preset_path.read_text().replace("0.75", "0.25"))
    after = lv2.discover_lv2()
    assert after is not before
    assert after["presets"][0]["port_values"] == {"gain": 0.25}


def test_vst3_preset_tool_embeds_component_state_in_project(tmp_path, monkeypatch):
    plugin_bundle = tmp_path / "Fixture Synth.vst3"
    plugin_bundle.mkdir()
    preset_path = tmp_path / "Factory" / "Warm Pad.vstpreset"
    state = _write_vstpreset(preset_path)
    plugin = _vst3_plugin(plugin_bundle)
    monkeypatch.setattr(vst3, "discover_vst3_plugins", lambda **kwargs: [plugin])
    monkeypatch.setattr(vst3_presets, "preset_search_paths", lambda plugins=None: [tmp_path])
    project, track = _new_project(xml_parser.NATIVE_VST3_HOST)
    xml_parser.configure_native_vst3_instrument(track, plugin["module"], CID)
    server.set_project(project)
    result = json.loads(server.load_native_plugin_preset(0, str(preset_path)))
    assert result["embedded"] is True
    state_node = project.root.find(".//vst3instrument/state")
    assert base64.b64decode(state_node.text) == state
    assert project.modified
    listed = json.loads(server.list_plugin_presets("vst3", "Fixture Synth"))
    assert "state" not in listed["presets"][0]
    filtered = json.loads(server.list_plugin_presets(
        "vst3", plugin="Fixture Synth", query="warm", bank="Factory",
        category="pads", tags="soft", author="ada", character="wide",
        origin="factory"))
    assert filtered["total"] == 1
    project_path = tmp_path / "vst3-state.mmp"
    project.save(project_path, compressed=False)
    reopened = xml_parser.load_project(project_path)
    assert base64.b64decode(reopened.find(".//vst3instrument/state").text) == state


def test_vst3_controller_state_preset_fails_without_mutating_project(tmp_path, monkeypatch):
    plugin_bundle = tmp_path / "Fixture Synth.vst3"
    plugin_bundle.mkdir()
    preset_path = tmp_path / "Warm Pad.vstpreset"
    _write_vstpreset(preset_path, controller_state=b"controller state")
    plugin = _vst3_plugin(plugin_bundle)
    monkeypatch.setattr(vst3, "discover_vst3_plugins", lambda **kwargs: [plugin])
    monkeypatch.setattr(vst3_presets, "preset_search_paths", lambda plugins=None: [tmp_path])
    project, track = _new_project(xml_parser.NATIVE_VST3_HOST)
    xml_parser.configure_native_vst3_instrument(track, plugin["module"], CID)
    project._modified = False
    before = xml_parser.ET.tostring(project.root)
    server.set_project(project)
    result = json.loads(server.load_native_plugin_preset(0, str(preset_path)))
    assert "controller state" in result["error"]
    assert xml_parser.ET.tostring(project.root) == before
    assert project.modified is False


def test_lv2_preset_tool_serializes_native_port_values_and_roundtrips(tmp_path, monkeypatch):
    root = tmp_path / "lv2"
    _, preset_path = _fixture_lv2_bundle(root)
    monkeypatch.setattr(lv2, "standard_lv2_dirs", lambda: [root])
    monkeypatch.delenv("LV2_PATH", raising=False)
    project = LMMSProject()
    project.new()
    server.set_project(project)
    added = json.loads(server.add_lv2_instrument_track("Fixture", PLUGIN_URI))
    assert added["plugin_id"] == PLUGIN_URI
    assert added["host"] == "lv2instrument"
    result = json.loads(server.load_native_plugin_preset(0, str(preset_path)))
    assert result["embedded"] is True
    assert result["ports"] == 1
    models = project.root.find(".//lv2controls/models")
    assert models.get("gain") == "0.75"
    project_path = tmp_path / "preset-roundtrip.mmp"
    project.save(project_path, compressed=False)
    reopened = xml_parser.load_project(project_path)
    assert reopened.find(".//lv2controls/models").get("gain") == "0.75"


def test_native_plugin_and_preset_tools_filter_and_refresh(tmp_path, monkeypatch):
    root = tmp_path / "lv2"
    _fixture_lv2_bundle(root)
    monkeypatch.setattr(lv2, "standard_lv2_dirs", lambda: [root])
    monkeypatch.setattr(vst3, "discover_vst3_plugins", lambda **kwargs: [])
    monkeypatch.setattr(vst3, "effective_search_paths", lambda: [])
    monkeypatch.setattr(vst3_presets, "preset_search_paths", lambda plugins=None: [])
    monkeypatch.delenv("LV2_PATH", raising=False)
    plugins = json.loads(server.list_native_plugins("lv2"))
    assert plugins["count"] == 1
    assert plugins["plugins"][0]["uri"] == PLUGIN_URI
    presets = json.loads(server.list_plugin_presets(
        "lv2", plugin=PLUGIN_URI, tags="warm", author="ada"))
    assert presets["total"] == 1
    assert presets["presets"][0]["name"] == "Warm Pad"
    refreshed = json.loads(server.refresh_native_plugin_discovery())
    assert refreshed["lv2_presets"] == 1


def test_lv2_model_values_validate_control_symbols():
    project, track = _new_project("lv2instrument")
    with pytest.raises(ValueError, match="symbol"):
        xml_parser.configure_native_lv2_instrument(track, PLUGIN_URI, {"bad symbol": 1})
    with pytest.raises(ValueError, match="non-finite"):
        xml_parser.configure_native_lv2_instrument(track, PLUGIN_URI, {"gain": float("nan")})
