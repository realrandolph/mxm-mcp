"""Tests for native VST3 hosting support (MXM's ``vst3instrument``).

No installed plugin is required: discovery is exercised with injected probe
output and a compiled stub module; serialization is pure XML. The end-to-end
render check lives in ``test_lmms13_integration.py``.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from lmms_mcp import vst3, vst3_probe, xml_parser
from lmms_mcp import server as srv
from lmms_mcp.project import LMMSProject
from lmms_mcp.xml_parser import (
    add_instrument_track, add_note_to_track,
    configure_native_vst3_instrument, create_empty_project, find_tracks,
    load_project, save_project,
)

SURGE_MODULE = "/usr/lib/vst3/Surge XT.vst3"
SURGE_CID = "ABCDEF019182FAEB566D624153675854"
CID_A, CID_B = "AB" * 16, "CD" * 16
FAKE_CID = "A1B2C3D4E5F60718293A4B4C00000000"


@pytest.fixture(autouse=True)
def _isolate_mxm_build_options(monkeypatch):
    """Clear the cached MXM ``--version`` result and avoid spawning the binary."""
    from lmms_mcp import lmms_app

    lmms_app.clear_mxm_build_options_cache()
    monkeypatch.setattr(lmms_app, "find_mxm_exe", lambda: None)
    yield
    lmms_app.clear_mxm_build_options_cache()


class _Completed:
    def __init__(self, stdout):
        self.stdout, self.stderr = stdout, ""


def _descriptor(name="Synth", module=SURGE_MODULE, cid=SURGE_CID,
                sub_categories="Instrument|Synth", vendor="Vendor"):
    return {
        "name": name, "vendor": vendor, "module": module, "cid": cid,
        "is_instrument": sub_categories.startswith("Instrument"),
        "sub_categories": sub_categories, "version": "1.0", "class_flags": 0,
    }


def _key_attributes(instrument, prefix="vst3instrument/key"):
    return {a.get("name"): a.get("value") for a in instrument.findall(f"{prefix}/attribute")}


class TestVst3Serialization:
    """The XML shape MXM's Vst3Instrument/MxmPluginBridge expects."""

    def _track(self):
        root = create_empty_project()
        return add_instrument_track(root, "VST3")

    def test_writes_mxm_native_layout(self):
        track = self._track()
        configure_native_vst3_instrument(
            track, SURGE_MODULE, "abcdef019182faeb566d624153675854")
        instrument = track.find("instrumenttrack/instrument")
        assert instrument.get("name") == "vst3instrument"
        assert _key_attributes(instrument, "vst3instrument/key") == {
            "module": SURGE_MODULE, "cid": SURGE_CID}  # cid normalized upper

    def test_replaces_plugin_and_is_not_carla_or_lv2(self):
        track = self._track()
        configure_native_vst3_instrument(track, SURGE_MODULE, SURGE_CID)
        instrument = track.find("instrumenttrack/instrument")
        assert instrument.find("tripleoscillator") is None
        assert len(instrument.findall("vst3instrument")) == 1
        assert instrument.find("carlarack") is None
        assert instrument.find("lv2controls") is None

    def test_rejects_bad_input(self):
        track = self._track()
        with pytest.raises(ValueError, match="module path"):
            configure_native_vst3_instrument(track, "", SURGE_CID)
        with pytest.raises(ValueError, match="32 hex characters"):
            configure_native_vst3_instrument(track, SURGE_MODULE, "not-a-cid")

    def test_optional_state_and_models(self):
        track = self._track()
        configure_native_vst3_instrument(track, SURGE_MODULE, SURGE_CID, state="QUJD")
        wrapper = track.find("instrumenttrack/instrument/vst3instrument")
        assert wrapper.find("state").get("encoding") == "base64"
        assert wrapper.find("state").text == "QUJD"
        assert wrapper.find("models") is not None

    def test_round_trip_preserves_identity_and_notes(self):
        root = create_empty_project()
        track = add_instrument_track(root, "VST3")
        configure_native_vst3_instrument(track, SURGE_MODULE, SURGE_CID)
        add_note_to_track(root, 0, key=60, pos=0, length=96)
        with tempfile.NamedTemporaryFile(suffix=".mmpz", delete=False) as handle:
            path = handle.name
        save_project(path, root)
        loaded = load_project(path)
        Path(path).unlink()
        instrument = loaded.find(".//instrument[@name='vst3instrument']")
        assert _key_attributes(instrument) == {"module": SURGE_MODULE, "cid": SURGE_CID}
        assert loaded.find(".//midiclip/note").get("key") == "60"


def test_cid_helpers():
    assert vst3.is_valid_cid(SURGE_CID) and vst3.is_valid_cid(SURGE_CID.lower())
    assert not vst3.is_valid_cid("xyz") and not vst3.is_valid_cid("AB" * 15)
    assert vst3.normalize_cid(" abcdef0123456789abcdef0123456789 ") == (
        "ABCDEF0123456789ABCDEF0123456789")


class TestVst3Discovery:
    def test_vst3_path_and_path_only(self, tmp_path, monkeypatch):
        bundle = tmp_path / "Extra.vst3"
        bundle.mkdir()
        monkeypatch.setenv(vst3.PATH_ONLY_ENV, "1")
        monkeypatch.setenv("VST3_PATH", str(tmp_path))
        assert vst3.find_vst3_bundles() == [bundle]

    def test_path_only_presence_semantics(self, tmp_path, monkeypatch):
        standard = tmp_path / "standard"
        standard.mkdir()
        (standard / "Builtin.vst3").mkdir()
        monkeypatch.setattr(vst3, "standard_vst3_dirs", lambda: [standard])
        monkeypatch.delenv("VST3_PATH", raising=False)
        # An empty value still means path-only (matches std::getenv presence).
        monkeypatch.setenv(vst3.PATH_ONLY_ENV, "")
        assert vst3.path_only_enabled() is True
        assert vst3.find_vst3_bundles() == []
        monkeypatch.delenv(vst3.PATH_ONLY_ENV)
        assert [p.name for p in vst3.find_vst3_bundles()] == ["Builtin.vst3"]

    def test_standard_scan_recursive_case_sensitive_no_descent(self, tmp_path, monkeypatch):
        standard = tmp_path / "standard"
        (standard / "sub").mkdir(parents=True)
        (standard / "Top.vst3" / "Nested.vst3").mkdir(parents=True)
        (standard / "sub" / "Inner.vst3").mkdir()
        (standard / "lower.vst3").mkdir()
        (standard / "upper.VST3").mkdir()
        monkeypatch.setattr(vst3, "standard_vst3_dirs", lambda: [standard])
        monkeypatch.delenv(vst3.PATH_ONLY_ENV, raising=False)
        monkeypatch.delenv("VST3_PATH", raising=False)
        assert {p.name for p in vst3.find_vst3_bundles()} == {
            "Top.vst3", "Inner.vst3", "lower.vst3"}

    def test_vst3_path_dir_scan(self, tmp_path, monkeypatch):
        # discoverPathOrDirectory: dirs only, recursive, no descent, case-sensitive.
        (tmp_path / "Real.vst3").mkdir()
        (tmp_path / "Loose.vst3").write_bytes(b"not a bundle")
        (tmp_path / "Top.vst3" / "Nested.vst3").mkdir(parents=True)
        (tmp_path / "upper.VST3").mkdir()
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "Inner.vst3").mkdir()
        monkeypatch.setenv(vst3.PATH_ONLY_ENV, "1")
        monkeypatch.setenv("VST3_PATH", str(tmp_path))
        assert {p.name for p in vst3.find_vst3_bundles()} == {
            "Real.vst3", "Top.vst3", "Inner.vst3"}

    def test_relative_vst3_path_is_absolute(self, tmp_path, monkeypatch):
        (tmp_path / "sub" / "Inner.vst3").mkdir(parents=True)
        monkeypatch.setenv(vst3.PATH_ONLY_ENV, "1")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("VST3_PATH", "sub")
        found = vst3.find_vst3_bundles()
        assert found == [tmp_path / "sub" / "Inner.vst3"] and found[0].is_absolute()

    def test_direct_vst3_path_match_is_case_sensitive(self, tmp_path, monkeypatch):
        lower = tmp_path / "Lower.vst3"
        lower.mkdir()
        (tmp_path / "Upper.VST3").mkdir()
        monkeypatch.setenv(vst3.PATH_ONLY_ENV, "1")
        monkeypatch.setenv("VST3_PATH", os.pathsep.join(
            [str(lower), str(tmp_path / "Upper.VST3")]))
        assert vst3.find_vst3_bundles() == [lower]

    def test_effective_search_paths_honours_path_only(self, monkeypatch, tmp_path):
        standard = tmp_path / "standard"
        monkeypatch.setattr(vst3, "standard_vst3_dirs", lambda: [standard])
        monkeypatch.setenv("VST3_PATH", "/extra/one:/extra/two")
        monkeypatch.delenv(vst3.PATH_ONLY_ENV, raising=False)
        assert vst3.effective_search_paths() == [str(standard), "/extra/one", "/extra/two"]
        monkeypatch.setenv(vst3.PATH_ONLY_ENV, "1")
        assert vst3.effective_search_paths() == ["/extra/one", "/extra/two"]

    def test_app_vst3_dir_follows_symlink(self, tmp_path, monkeypatch):
        real_bin = tmp_path / "real" / "bin"
        (real_bin / "vst3").mkdir(parents=True)
        exe = real_bin / "mxm"
        exe.write_bytes(b"#!/bin/true\n")
        link = tmp_path / "launcher" / "mxm"
        link.parent.mkdir()
        link.symlink_to(exe)
        monkeypatch.setattr(vst3.lmms_app, "find_mxm_exe", lambda: link)
        monkeypatch.setattr(vst3.lmms_app, "find_lmms_exe", lambda: None)
        assert vst3._app_vst3_dir() == real_bin / "vst3"

    def test_platform_guard_short_circuits_discovery(self, monkeypatch):
        def fake_probe(bundle):
            return {"module": str(bundle), "classes": [{
                "cid": CID_A, "name": "Synth", "category": "Audio Module Class",
                "sub_categories": "Instrument|Synth", "vendor": "V", "version": "1",
                "class_flags": 0}]}

        monkeypatch.setattr(vst3.sys, "platform", "win32")
        assert vst3.native_vst3_discovery_supported() is False
        assert vst3.discover_vst3_plugins(
            bundles=[Path("/x.vst3")], probe=fake_probe) == []
        monkeypatch.setattr(vst3.sys, "platform", "linux")
        assert len(vst3.discover_vst3_plugins(
            bundles=[Path("/x.vst3")], probe=fake_probe)) == 1

    def test_discover_filters_and_classifies(self):
        def fake_probe(bundle):
            return {"module": str(bundle), "classes": [
                {"cid": CID_A, "name": "Synth", "category": "Audio Module Class",
                 "sub_categories": "Instrument|Synth", "vendor": "V", "version": "1",
                 "class_flags": 0},
                {"cid": CID_B, "name": "Synth",
                 "category": "Component Controller Class",
                 "sub_categories": "Instrument|Synth"},  # dropped
                {"cid": "EF" * 16, "name": "Reverb",
                 "category": "Audio Module Class", "sub_categories": "Fx|Reverb",
                 "vendor": "V", "version": "1", "class_flags": 0},
            ]}

        plugins = vst3.discover_vst3_plugins(bundles=[Path("/x.vst3")], probe=fake_probe)
        assert [p["name"] for p in plugins] == ["Reverb", "Synth"]
        by_name = {p["name"]: p for p in plugins}
        assert by_name["Synth"]["is_instrument"] is True
        assert by_name["Reverb"]["is_instrument"] is False
        assert by_name["Synth"]["cid"] == CID_A

    def test_discover_skips_errors_duplicates_and_bad_cids(self):
        good = {"cid": CID_A, "name": "Synth", "category": "Audio Module Class",
                "sub_categories": "Instrument|Synth", "vendor": "", "version": "",
                "class_flags": 0}

        def fake_probe(bundle):
            if bundle.name == "bad.vst3":
                return {"module": str(bundle), "error": "boom"}
            if bundle.name == "invalid.vst3":
                return {"module": str(bundle), "classes": [
                    {"cid": "1234", "name": "Broken",
                     "category": "Audio Module Class", "sub_categories": "Instrument"}]}
            return {"module": str(bundle), "classes": [good, good]}

        plugins = vst3.discover_vst3_plugins(
            bundles=[Path("/bad.vst3"), Path("/invalid.vst3"), Path("/good.vst3")],
            probe=fake_probe)
        assert len(plugins) == 1 and plugins[0]["name"] == "Synth"

    def test_resolve_by_name(self):
        plugins = [_descriptor(name="Surge XT"), _descriptor(name="Surge XT Effects")]
        plugin, candidates = vst3.resolve_vst3_instrument(plugins, plugin_name="Surge XT")
        assert plugin["name"] == "Surge XT" and candidates == []
        plugin, candidates = vst3.resolve_vst3_instrument(plugins, plugin_name="surge")
        assert plugin is None and [c["name"] for c in candidates] == [
            "Surge XT", "Surge XT Effects"]
        plugin, candidates = vst3.resolve_vst3_instrument(plugins, plugin_name="none")
        assert plugin is None and candidates == []

    def test_resolve_by_module_cid_and_instruments_only(self):
        plugins = [
            _descriptor(name="Surge XT"), _descriptor(name="Surge XT",
                                                      sub_categories="Fx|Reverb")]
        plugin, candidates = vst3.resolve_vst3_instrument(
            plugins, module=SURGE_MODULE, cid=SURGE_CID.lower())
        assert plugin["name"] == "Surge XT" and candidates == []
        plugin, candidates = vst3.resolve_vst3_instrument(
            plugins, plugin_name="Surge XT")
        assert plugin is None and len(candidates) == 2
        plugin, candidates = vst3.resolve_vst3_instrument(
            plugins, plugin_name="Surge XT", instruments_only=True)
        assert plugin is not None and plugin["is_instrument"] and candidates == []


class TestMxmBuildOptionsCache:
    def test_cached_and_returns_independent_dicts(self, monkeypatch):
        from lmms_mcp import lmms_app

        calls = []
        monkeypatch.setattr(lmms_app, "find_mxm_exe", lambda: Path("/fake/mxm"))
        monkeypatch.setattr(lmms_app.subprocess, "run",
                            lambda *a, **k: calls.append(a) or _Completed(
                                "MXM_HAVE_VST3='TRUE'\nWANT_VST3='ON'\n"))
        first = lmms_app.get_mxm_build_options()
        first["have_vst3"] = False
        second = lmms_app.get_mxm_build_options()
        assert second["have_vst3"] is True and second.get("vst3") is True
        assert len(calls) == 1


class TestVst3ServerTools:
    def _patch(self, monkeypatch, plugins):
        monkeypatch.setattr(vst3, "discover_vst3_plugins", lambda *a, **k: plugins)

    def _new_project(self):
        project = LMMSProject()
        project.new()
        srv.set_project(project)
        return project

    def test_list_instruments_and_effects(self, monkeypatch):
        self._patch(monkeypatch, [_descriptor(name="Surge XT"),
                                  _descriptor(name="ZamComp", sub_categories="Fx|Dynamics")])
        payload = json.loads(srv.list_vst3_instruments())
        assert payload["host"] == "vst3instrument"
        assert payload["native"] is True and payload["carla"] is False
        assert payload["instrument_count"] == 1
        assert [p["name"] for p in payload["plugins"]] == ["Surge XT"]
        payload = json.loads(srv.list_vst3_instruments(include_effects=True))
        assert payload["effect_count"] == 1 and len(payload["plugins"]) == 2

    def test_add_by_name_writes_native_vst3(self, monkeypatch):
        self._patch(monkeypatch, [_descriptor(name="Surge XT")])
        project = self._new_project()
        result = json.loads(srv.add_vst3_instrument_track("Lead", plugin="Surge XT"))
        assert result["host"] == "vst3instrument" and result["verified"] is True
        assert result["native"] is True and result["carla"] is False
        assert result["plugin_type"] == "VST3" and result["cid"] == SURGE_CID
        instrument = project.root.find(".//instrument[@name='vst3instrument']")
        assert _key_attributes(instrument) == {"module": SURGE_MODULE, "cid": SURGE_CID}
        assert project.root.find(".//instrument[@name='carlarack']") is None

    def test_add_explicit_and_unverified(self, monkeypatch):
        self._patch(monkeypatch, [_descriptor(name="Surge XT")])
        self._new_project()
        result = json.loads(srv.add_vst3_instrument_track(
            "Lead", module_path=SURGE_MODULE, cid=SURGE_CID.lower()))
        assert result["cid"] == SURGE_CID
        # Unverified skip: a probe that would explode proves the sweep is skipped.
        self._patch(monkeypatch, [])
        rejected = json.loads(srv.add_vst3_instrument_track(
            "Pad", module_path=SURGE_MODULE, cid=SURGE_CID))
        assert "error" in rejected
        monkeypatch.setattr(vst3, "discover_vst3_plugins", lambda *a, **k: (
            (_ for _ in ()).throw(AssertionError("discovery must be skipped"))))
        accepted = json.loads(srv.add_vst3_instrument_track(
            "Pad", module_path=SURGE_MODULE, cid=SURGE_CID, allow_unverified=True))
        assert accepted["host"] == "vst3instrument" and accepted["verified"] is False

    def test_named_and_verified_paths_still_discover(self, monkeypatch):
        calls = []

        def spy(*args, **kwargs):
            calls.append(args)
            return [_descriptor(name="Surge XT")]

        monkeypatch.setattr(vst3, "discover_vst3_plugins", spy)
        self._new_project()
        assert json.loads(srv.add_vst3_instrument_track(
            "Lead", plugin="Surge XT")).get("verified") is True
        assert json.loads(srv.add_vst3_instrument_track(
            "Pad", module_path=SURGE_MODULE, cid=SURGE_CID)).get("verified") is True
        assert len(calls) == 2

    def test_add_resolution_errors(self, monkeypatch):
        self._new_project()
        self._patch(monkeypatch, [_descriptor(name="Surge XT"),
                                  _descriptor(name="Surge XT Effects")])
        response = json.loads(srv.add_vst3_instrument_track("Lead"))
        assert "error" in response  # no selection
        response = json.loads(srv.add_vst3_instrument_track("Lead", plugin="Nope"))
        assert [p["name"] for p in response["available"]] == ["Surge XT", "Surge XT Effects"]
        response = json.loads(srv.add_vst3_instrument_track("Lead", plugin="surge"))
        assert [p["name"] for p in response["matches"]] == ["Surge XT", "Surge XT Effects"]
        self._patch(monkeypatch, [_descriptor(name="ZamComp", sub_categories="Fx|Dynamics")])
        response = json.loads(srv.add_vst3_instrument_track("Lead", plugin="ZamComp"))
        assert "error" in response and response["available"] == []
        response = json.loads(srv.add_vst3_instrument_track("Lead", module_path=SURGE_MODULE))
        assert "both module_path and cid" in response["error"]

    @pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
    def test_add_rolls_back_on_configuration_failure(self, monkeypatch, error_type):
        self._patch(monkeypatch, [_descriptor(name="Surge XT")])
        project = self._new_project()
        project._modified = False

        def boom(*args, **kwargs):
            raise error_type("serialization failed")

        monkeypatch.setattr(xml_parser, "configure_native_vst3_instrument", boom)
        response = json.loads(srv.add_vst3_instrument_track("Lead", plugin="Surge XT"))
        assert "error" in response and find_tracks(project.root) == []
        assert project.modified is False

    def test_add_instrument_track_redirects_vst3_host(self):
        self._new_project()
        response = json.loads(srv.add_instrument_track("Lead", instrument="vst3instrument"))
        assert "add_vst3_instrument_track" in response["error"]


_FAKE_MODULE_C = textwrap.dedent(r"""
    #include <stdint.h>
    #include <string.h>
    typedef struct { uint8_t cid[16]; int32_t cardinality; char category[32];
        char name[64]; uint32_t class_flags; char sub_categories[128];
        char vendor[64]; char version[64]; char sdk_version[64]; } PClassInfo2;
    typedef struct FactoryVtbl { int32_t (*queryInterface)(void*,const char*,void**);
        uint32_t (*addRef)(void*); uint32_t (*release)(void*);
        int32_t (*getFactoryInfo)(void*,void*); int32_t (*countClasses)(void*);
        int32_t (*getClassInfo)(void*,int32_t,void*);
        int32_t (*createInstance)(void*,const char*,const char*,void**);
        int32_t (*getClassInfo2)(void*,int32_t,PClassInfo2*); } FactoryVtbl;
    typedef struct { const FactoryVtbl* vtbl; } Factory;
    static int32_t f_query(void* s,const char* i,void** o){(void)i;*o=s;return 0;}
    static uint32_t f_addref(void* s){(void)s;return 1;}
    static uint32_t f_release(void* s){(void)s;return 0;}
    static int32_t f_info(void* s,void* i){(void)s;memset(i,0,452);
        strcpy((char*)i,"Fake Vendor");return 0;}
    static int32_t f_count(void* s){(void)s;return 1;}
    static int32_t f_get(void* s,int32_t i,void* o){(void)s;(void)i;(void)o;return 1;}
    static int32_t f_create(void* s,const char* c,const char* i,void** o){
        (void)s;(void)c;(void)i;(void)o;return 1;}
    static int32_t f_get2(void* s,int32_t i,PClassInfo2* p){
        static const uint8_t cid[16]={0xA1,0xB2,0xC3,0xD4,0xE5,0xF6,0x07,0x18,0x29,0x3A,0x4B,0x4C};
        (void)s; if(i!=0) return 1; memset(p,0,sizeof(*p));
        memcpy(p->cid,cid,16); p->cardinality=0x7FFFFFFF;
        strcpy(p->category,"Audio Module Class"); strcpy(p->name,"Fake Synth");
        strcpy(p->sub_categories,"Instrument|Synth"); strcpy(p->vendor,"Fake Vendor");
        strcpy(p->version,"1.2.3"); strcpy(p->sdk_version,"VST 3.7"); return 0;}
    static const FactoryVtbl g_vtbl={f_query,f_addref,f_release,f_info,f_count,f_get,f_create,f_get2};
    static Factory g_factory={&g_vtbl};
    int ModuleEntry(void* h){(void)h;return 1;}
    int ModuleExit(void){return 1;}
    Factory* GetPluginFactory(void){return &g_factory;}
""").strip()


def _build_fake_bundle(tmp_path):
    """Compile a minimal VST3 module so the ctypes probe can be tested."""
    if not sys.platform.startswith("linux"):
        pytest.skip("native VST3 discovery is Linux-only in this MCP")
    compiler = shutil.which("cc") or shutil.which("gcc")
    if compiler is None:
        pytest.skip("no C compiler available to build a stub VST3 module")
    so_dir = tmp_path / "Fake.vst3" / "Contents" / f"{os.uname().machine}-linux"
    so_dir.mkdir(parents=True)
    source = tmp_path / "fake_module.c"
    source.write_text(_FAKE_MODULE_C)
    proc = subprocess.run([compiler, "-shared", "-fPIC", "-O0",
                           "-o", str(so_dir / "Fake.so"), str(source)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip(f"could not build stub VST3 module: {proc.stderr.strip()}")
    return tmp_path / "Fake.vst3"


class TestVst3ProbeCtypes:
    """Deterministic coverage for the riskiest code in vst3_probe.py."""

    def test_bundle_so_path(self, tmp_path):
        bundle = tmp_path / "X.vst3"
        binary = bundle / "Contents" / f"{os.uname().machine}-linux" / "X.so"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"")
        assert vst3_probe.vst3_bundle_so_path(bundle) == binary
        assert vst3_probe.vst3_bundle_so_path(tmp_path / "Empty.vst3") is None

    def test_probe_reports_missing_binary(self, tmp_path):
        (tmp_path / "Empty.vst3").mkdir()
        assert "error" in vst3_probe.probe_bundle(tmp_path / "Empty.vst3")

    def test_probe_reads_stub_factory(self, tmp_path):
        result = vst3_probe.probe_bundle(_build_fake_bundle(tmp_path))
        assert "error" not in result, result
        info = result["classes"][0]
        # Trailing zero bytes must be preserved (regression guard).
        assert info["cid"] == FAKE_CID
        assert info["name"] == "Fake Synth"
        assert info["category"] == "Audio Module Class"
        assert info["sub_categories"] == "Instrument|Synth"
        assert info["vendor"] == "Fake Vendor" and info["version"] == "1.2.3"

    def test_stub_module_flows_through_discovery_and_main(self, tmp_path, capsys):
        bundle = _build_fake_bundle(tmp_path)
        plugins = vst3.discover_vst3_plugins(bundles=[bundle], probe=vst3_probe.probe_bundle)
        assert len(plugins) == 1 and plugins[0]["is_instrument"] is True
        assert plugins[0]["cid"] == FAKE_CID and plugins[0]["module"] == str(bundle)
        assert vst3_probe.main([str(bundle)]) == 0
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["classes"][0]["name"] == "Fake Synth"
