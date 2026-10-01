"""Filesystem-only VST3/LV2 plugin and preset discovery regressions."""

from __future__ import annotations

import asyncio
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


def _run_tool(awaitable):
    return asyncio.run(awaitable)


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
                        lambda plugins=None, **kwargs: [preset_path.parent])
    parsed = vst3_presets.parse_vstpreset(preset_path, include_state=True)
    assert parsed["cid"] == CID
    assert parsed["name"] == "Warm Pad"
    assert parsed["metadata"]["author"] == "Ada Example"
    assert base64.b64decode(parsed["state"]) == component_state
    listed = vst3_presets.parse_vstpreset(preset_path)
    assert listed["loadable"] is True
    assert "state" not in listed

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
    assert "state" not in preset


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
    monkeypatch.setattr(vst3_presets, "preset_search_paths",
                        lambda plugins=None, **kwargs: [root])
    plugin = _vst3_plugin(tmp_path / "Fixture Synth.vst3")
    presets = vst3_presets.discover_vst3_presets([plugin], refresh=True)["presets"]
    by_name = {item["name"]: item for item in presets}
    assert set(by_name) == {"Warm Pad", "User Lead"}
    assert by_name["Warm Pad"]["origin"] == "factory"
    assert by_name["User Lead"]["origin"] == "user"
    assert by_name["User Lead"]["author"] is None
    assert by_name["User Lead"]["loadable"] is False


def test_plugin_scoped_vst3_preset_search_uses_plugin_roots_only(tmp_path, monkeypatch):
    search_root = tmp_path / "u-he"
    target_bundle = tmp_path / "plugins" / "Zebralette3.vst3"
    target_bundle.mkdir(parents=True)
    target_root = search_root / "Zebralette3.data" / "Presets"
    other_root = search_root / "Hive.data" / "Presets"
    _write_vstpreset(target_root / "Factory" / "Warm Pad.vstpreset")
    _write_vstpreset(other_root / "Factory" / "Other.vstpreset")
    plugin = {**_vst3_plugin(target_bundle), "name": "Zebralette3", "vendor": "u-he"}
    monkeypatch.setattr(vst3_presets, "standard_preset_dirs", lambda: [search_root])
    monkeypatch.setattr(vst3, "standard_vst3_dirs", lambda: [])
    for variable in ("VST3_PRESET_PATH", "UHE_PRESET_PATH", "VST3_PATH"):
        monkeypatch.delenv(variable, raising=False)

    paths = vst3_presets.preset_search_paths([plugin], plugin_scoped=True)
    assert search_root not in paths
    assert target_root in paths
    assert other_root not in paths

    index = vst3_presets.discover_vst3_presets(
        [plugin], plugin_scoped=True, refresh=True,
    )
    assert [item["name"] for item in index["presets"]] == ["Warm Pad"]


def test_plugin_scoped_vst3_search_honors_explicit_flat_preset_roots(tmp_path, monkeypatch):
    bundle = tmp_path / "plugins" / "Zebralette3.vst3"
    bundle.mkdir(parents=True)
    configured_root = tmp_path / "custom-preset-root"
    _write_vstpreset(configured_root / "Factory Pad.vstpreset")
    plugin = {**_vst3_plugin(bundle), "name": "Zebralette3", "vendor": "u-he"}
    monkeypatch.setattr(vst3_presets, "standard_preset_dirs", lambda: [])
    monkeypatch.setattr(vst3, "standard_vst3_dirs", lambda: [])
    monkeypatch.setenv("VST3_PRESET_PATH", str(configured_root))
    monkeypatch.delenv("UHE_PRESET_PATH", raising=False)
    monkeypatch.delenv("VST3_PATH", raising=False)

    index = vst3_presets.discover_vst3_presets([plugin], plugin_scoped=True, refresh=True)

    assert str(configured_root) in index["search_paths"]
    assert [item["name"] for item in index["presets"]] == ["Warm Pad"]


def test_plugin_filtered_preset_tool_indexes_only_matching_vst3_records(monkeypatch):
    target = {**_vst3_plugin(Path("/plugins/Zebralette3.vst3")),
              "name": "Zebralette3"}
    unrelated = {**_vst3_plugin(Path("/plugins/Other Synth.vst3")),
                 "name": "Other Synth", "cid": "CD" * 16}
    authorized_queries = []
    indexed = []

    async def authorize(_ctx, _action, _arguments, **kwargs):
        authorized_queries.append(kwargs["plugin_query"])
        return [target, unrelated], []

    def discover(records, *, refresh=False, plugin_scoped=False):
        indexed.append((records, refresh, plugin_scoped))
        return {"presets": [{"name": "Warm Pad", "plugin_name": "Zebralette3",
                              "plugin_cid": CID, "plugin_module": target["module"]}]}

    monkeypatch.setattr(server, "_authorized_vst3_plugins", authorize)
    monkeypatch.setattr(vst3_presets, "discover_vst3_presets", discover)

    result = json.loads(_run_tool(server.list_plugin_presets(
        "vst3", plugin="Zebralette3", limit=200,
    )))

    assert authorized_queries == ["Zebralette3"]
    assert indexed == [([target], False, True)]
    assert result["total"] == 1
    assert result["presets"][0]["name"] == "Warm Pad"


def test_plugin_scoped_preset_cache_fingerprints_and_builds_per_plugin(
        tmp_path, monkeypatch):
    global_root = tmp_path / "u-he"
    bundle_root = tmp_path / "plugins"
    first_bundle = bundle_root / "Zebralette3.vst3"
    second_bundle = bundle_root / "Hive.vst3"
    first_bundle.mkdir(parents=True)
    second_bundle.mkdir(parents=True)
    first_preset_root = global_root / "Zebralette3.data" / "Presets"
    second_preset_root = global_root / "Hive.data" / "Presets"
    _write_vstpreset(first_preset_root / "Warm Pad.vstpreset")
    _write_vstpreset(second_preset_root / "Bass.vstpreset", cid="CD" * 16)
    first_plugin = {**_vst3_plugin(first_bundle), "name": "Zebralette3", "vendor": "u-he"}
    second_plugin = {**_vst3_plugin(second_bundle), "name": "Hive", "cid": "CD" * 16,
                     "vendor": "u-he"}
    monkeypatch.setattr(vst3_presets, "standard_preset_dirs", lambda: [global_root])
    monkeypatch.setattr(vst3, "standard_vst3_dirs", lambda: [])
    for variable in ("VST3_PRESET_PATH", "UHE_PRESET_PATH", "VST3_PATH"):
        monkeypatch.delenv(variable, raising=False)
    build_paths = []
    fingerprint_paths = []
    original_collect = vst3_presets._collect_files
    original_fingerprint = discovery_cache.filesystem_fingerprint

    def collect(paths):
        build_paths.append(tuple(paths))
        return original_collect(paths)

    def fingerprint(paths):
        fingerprint_paths.append(tuple(paths))
        return original_fingerprint(paths)

    monkeypatch.setattr(vst3_presets, "_collect_files", collect)
    monkeypatch.setattr(discovery_cache, "filesystem_fingerprint", fingerprint)

    first = vst3_presets.discover_vst3_presets([first_plugin], plugin_scoped=True)
    second = vst3_presets.discover_vst3_presets([second_plugin], plugin_scoped=True)
    first_again = vst3_presets.discover_vst3_presets([first_plugin], plugin_scoped=True)

    assert first_again is first
    assert len(build_paths) == 2  # separate plugin indexes; first one is warm-cached
    assert len(fingerprint_paths) == 3
    assert all(global_root not in paths for paths in fingerprint_paths)
    assert all(global_root not in paths for paths in build_paths)
    assert [item["name"] for item in second["presets"]] == ["Warm Pad"]


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


def test_lv2_bundle_symlinks_stay_inside_configured_root(tmp_path, monkeypatch):
    root = tmp_path / "lv2-root"
    root.mkdir()
    inside, _ = _fixture_lv2_bundle(root)
    outside, _ = _fixture_lv2_bundle(tmp_path / "outside")
    (root / "inside-alias.lv2").symlink_to(inside, target_is_directory=True)
    (root / "outside-alias.lv2").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(lv2, "standard_lv2_dirs", lambda: [root])
    monkeypatch.delenv("LV2_PATH", raising=False)

    bundles = lv2._find_bundles([root])
    assert [bundle for bundle, _ in bundles] == [inside]
    assert len(lv2.discover_lv2(refresh=True)["plugins"]) == 1


def test_lv2_rdf_file_and_triple_limits_are_enforced(tmp_path, monkeypatch):
    root = tmp_path / "lv2"
    bundle, _ = _fixture_lv2_bundle(root)
    extra = bundle / "extra.ttl"
    extra.write_text("@prefix ex: <https://example.test/> .\n" +
                     "\n".join(f"ex:s{i} ex:p ex:o ." for i in range(30)))
    monkeypatch.setattr(lv2, "standard_lv2_dirs", lambda: [root])
    monkeypatch.delenv("LV2_PATH", raising=False)
    monkeypatch.setattr(lv2, "_MAX_RDF_FILE_TRIPLES", 15)
    monkeypatch.setattr(lv2, "_MAX_RDF_FILE_BYTES", 1_000_000)
    index = lv2.discover_lv2(refresh=True)
    assert len(index["plugins"]) == 1
    assert len(index["presets"]) == 1
    graph, _ = lv2._load_bundle(bundle, root, lv2._RdfBudget())
    assert not any(str(subject).endswith("s0") for subject in graph.subjects())

    oversized = bundle / "oversized.ttl"
    oversized.write_bytes(b" " * 1_000_001)
    monkeypatch.setattr(lv2, "_MAX_RDF_FILE_BYTES", 1_000_000)
    budget = lv2._RdfBudget()
    assert lv2._load_bundle(bundle, root, budget)[0]
    assert budget.bytes < oversized.stat().st_size

    monkeypatch.setattr(lv2, "_MAX_RDF_FILES", 1)
    budget = lv2._RdfBudget()
    lv2._load_bundle(bundle, root, budget)
    assert budget.files == 1

    monkeypatch.setattr(lv2, "_MAX_RDF_FILES", 20)
    monkeypatch.setattr(lv2, "_MAX_RDF_TOTAL_TRIPLES", 5)
    budget = lv2._RdfBudget()
    graph, _ = lv2._load_bundle(bundle, root, budget)
    assert len(graph) <= 5 and budget.triples <= 5


def test_discovery_cache_does_not_fingerprint_symlink_targets_outside_root(tmp_path):
    root = tmp_path / "configured"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    target = outside / "target.lv2"
    target.mkdir()
    payload = target / "plugin.ttl"
    payload.write_text("first")
    (root / "external.lv2").symlink_to(target, target_is_directory=True)
    before = discovery_cache.filesystem_fingerprint([root])
    payload.write_text("second")
    os.utime(payload, ns=(payload.stat().st_atime_ns, payload.stat().st_mtime_ns + 1_000_000))
    assert discovery_cache.filesystem_fingerprint([root]) == before


def test_lv2_mixed_valid_and_malformed_port_state_is_not_loadable(tmp_path, monkeypatch):
    root = tmp_path / "lv2"
    _, preset_path = _fixture_lv2_bundle(root)
    preset_path.write_text(preset_path.read_text() + f'''
<{PRESET_URI}> lv2:port [ lv2:symbol "broken" ; pset:value "not-a-number" ] .
''')
    monkeypatch.setattr(lv2, "standard_lv2_dirs", lambda: [root])
    monkeypatch.delenv("LV2_PATH", raising=False)
    preset, = lv2.discover_lv2(refresh=True)["presets"]
    assert preset["port_values"] == {"gain": 0.75}
    assert preset["port_state_count"] == 2
    assert preset["invalid_port_states"] == 1
    assert preset["loadable"] is False


@pytest.mark.parametrize("replacement", [
    ('lv2:symbol "gain" ; pset:value 0.75',
     'lv2:symbol "gain", "other" ; pset:value 0.75'),
    ('lv2:symbol "gain" ; pset:value 0.75',
     'lv2:symbol "gain" ; pset:value 0.75, 0.25'),
])
def test_lv2_multivalued_port_state_is_not_loadable(tmp_path, monkeypatch, replacement):
    root = tmp_path / "lv2"
    _, preset_path = _fixture_lv2_bundle(root)
    contents = preset_path.read_text()
    preset_path.write_text(contents.replace(*replacement))
    monkeypatch.setattr(lv2, "standard_lv2_dirs", lambda: [root])
    monkeypatch.delenv("LV2_PATH", raising=False)
    preset, = lv2.discover_lv2(refresh=True)["presets"]
    assert preset["port_state_count"] == 1
    assert preset["invalid_port_states"] == 1
    assert preset["loadable"] is False


def test_lv2_preset_can_live_in_a_separate_bundle_from_its_plugin(tmp_path, monkeypatch):
    root = tmp_path / "lv2"
    plugin_bundle, preset_file = _fixture_lv2_bundle(root)
    separate_preset_bundle = root / "Shared Presets.lv2"
    separate_preset_bundle.mkdir()
    (separate_preset_bundle / "shared-presets.ttl").write_text(preset_file.read_text())
    preset_file.unlink()
    monkeypatch.setattr(lv2, "standard_lv2_dirs", lambda: [root])
    monkeypatch.delenv("LV2_PATH", raising=False)
    index = lv2.discover_lv2(refresh=True)
    plugin, = index["plugins"]
    preset, = index["presets"]
    assert plugin["bundle"] == str(plugin_bundle)
    assert preset["plugin_uri"] == plugin["uri"]
    assert preset["loadable"] is True


def test_vst3_preset_tool_embeds_component_state_in_project(tmp_path, monkeypatch):
    plugin_bundle = tmp_path / "Fixture Synth.vst3"
    plugin_bundle.mkdir()
    preset_path = tmp_path / "Factory" / "Warm Pad.vstpreset"
    state = _write_vstpreset(preset_path)
    plugin = _vst3_plugin(plugin_bundle)
    monkeypatch.setattr(vst3, "discover_vst3_plugins", lambda **kwargs: [plugin])
    monkeypatch.setattr(vst3, "find_vst3_bundles", lambda: [])
    monkeypatch.setattr(vst3_presets, "preset_search_paths",
                        lambda plugins=None, **kwargs: [tmp_path])
    project, track = _new_project(xml_parser.NATIVE_VST3_HOST)
    xml_parser.configure_native_vst3_instrument(track, plugin["module"], CID)
    server.set_project(project)
    result = json.loads(_run_tool(server.load_native_plugin_preset(0, str(preset_path))))
    assert result["embedded"] is True
    state_node = project.root.find(".//vst3instrument/state")
    assert base64.b64decode(state_node.text) == state
    assert project.modified
    listed = json.loads(_run_tool(server.list_plugin_presets("vst3", "Fixture Synth")))
    assert "state" not in listed["presets"][0]
    filtered = json.loads(_run_tool(server.list_plugin_presets(
        "vst3", plugin="Fixture Synth", query="warm", bank="Factory",
        category="pads", tags="soft", author="ada", character="wide",
        origin="factory")))
    assert filtered["total"] == 1
    project_path = tmp_path / "vst3-state.mmp"
    project.save(project_path, compressed=False)
    reopened = xml_parser.load_project(project_path)
    assert base64.b64decode(reopened.find(".//vst3instrument/state").text) == state


def test_vst3_preset_state_load_is_rooted_and_capped(tmp_path, monkeypatch):
    root = tmp_path / "presets"
    preset_path = root / "Warm Pad.vstpreset"
    _write_vstpreset(preset_path, state=b"selected state")
    plugin = _vst3_plugin(tmp_path / "Fixture Synth.vst3")
    monkeypatch.setattr(vst3_presets, "preset_search_paths",
                        lambda plugins=None, **kwargs: [root])
    indexed = vst3_presets.discover_vst3_presets([plugin], refresh=True)
    preset, = indexed["presets"]
    assert "state" not in preset
    assert vst3_presets.load_vst3preset_state(preset, indexed["search_paths"])
    monkeypatch.setattr(vst3_presets, "_MAX_COMPONENT_BYTES", 4)
    assert vst3_presets.load_vst3preset_state(preset, indexed["search_paths"]) is None


def test_vst3_preset_symlink_outside_search_root_is_not_indexed(tmp_path):
    root = tmp_path / "configured"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    preset = outside / "External.vstpreset"
    _write_vstpreset(preset)
    (root / "external.vstpreset").symlink_to(preset)
    assert vst3_presets._collect_files([root]) == []


def test_vst3_preset_scan_has_file_count_and_aggregate_byte_caps(tmp_path, monkeypatch):
    root = tmp_path / "presets"
    _write_vstpreset(root / "A.vstpreset")
    _write_vstpreset(root / "B.vstpreset")
    monkeypatch.setattr(vst3_presets, "_MAX_PRESET_FILES", 1)
    assert len(vst3_presets._collect_files([root])) == 1
    monkeypatch.setattr(vst3_presets, "_MAX_PRESET_FILES", 10)
    monkeypatch.setattr(vst3_presets, "_MAX_PRESET_TOTAL_BYTES", 1)
    assert vst3_presets._collect_files([root]) == []


def test_vst3_controller_state_preset_fails_without_mutating_project(tmp_path, monkeypatch):
    plugin_bundle = tmp_path / "Fixture Synth.vst3"
    plugin_bundle.mkdir()
    preset_path = tmp_path / "Warm Pad.vstpreset"
    _write_vstpreset(preset_path, controller_state=b"controller state")
    plugin = _vst3_plugin(plugin_bundle)
    monkeypatch.setattr(vst3, "discover_vst3_plugins", lambda **kwargs: [plugin])
    monkeypatch.setattr(vst3, "find_vst3_bundles", lambda: [])
    monkeypatch.setattr(vst3_presets, "preset_search_paths",
                        lambda plugins=None, **kwargs: [tmp_path])
    project, track = _new_project(xml_parser.NATIVE_VST3_HOST)
    xml_parser.configure_native_vst3_instrument(track, plugin["module"], CID)
    project._modified = False
    before = xml_parser.ET.tostring(project.root)
    server.set_project(project)
    result = json.loads(_run_tool(server.load_native_plugin_preset(0, str(preset_path))))
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
    added = json.loads(_run_tool(server.add_lv2_instrument_track("Fixture", PLUGIN_URI)))
    assert added["plugin_id"] == PLUGIN_URI
    assert added["host"] == "lv2instrument"
    result = json.loads(_run_tool(server.load_native_plugin_preset(0, str(preset_path))))
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
    monkeypatch.setattr(vst3, "find_vst3_bundles", lambda: [])
    monkeypatch.setattr(vst3, "effective_search_paths", lambda: [])
    monkeypatch.setattr(vst3_presets, "preset_search_paths",
                        lambda plugins=None, **kwargs: [])
    monkeypatch.delenv("LV2_PATH", raising=False)
    plugins = json.loads(_run_tool(server.list_native_plugins("lv2")))
    assert plugins["count"] == 1
    assert plugins["plugins"][0]["uri"] == PLUGIN_URI
    presets = json.loads(_run_tool(server.list_plugin_presets(
        "lv2", plugin=PLUGIN_URI, tags="warm", author="ada")))
    assert presets["total"] == 1
    assert presets["presets"][0]["name"] == "Warm Pad"
    refreshed = json.loads(_run_tool(server.refresh_native_plugin_discovery()))
    assert refreshed["lv2_presets"] == 1


def test_lv2_model_values_validate_control_symbols():
    project, track = _new_project("lv2instrument")
    with pytest.raises(ValueError, match="symbol"):
        xml_parser.configure_native_lv2_instrument(track, PLUGIN_URI, {"bad symbol": 1})
    with pytest.raises(ValueError, match="non-finite"):
        xml_parser.configure_native_lv2_instrument(track, PLUGIN_URI, {"gain": float("nan")})
