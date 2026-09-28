"""Tests for native VST3 hosting support (MXM's ``vst3instrument``).

These tests do not require an installed VST3 plugin: discovery is exercised
with injected probe output, and serialization is pure XML. The end-to-end
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

from lmms_mcp import vst3
from lmms_mcp import vst3_probe
from lmms_mcp import server as srv
from lmms_mcp import xml_parser
from lmms_mcp.project import LMMSProject
from lmms_mcp.xml_parser import (
    add_instrument_track,
    add_note_to_track,
    configure_native_vst3_instrument,
    create_empty_project,
    find_tracks,
    load_project,
    save_project,
)

CID_A = "AB" * 16
CID_B = "CD" * 16

SURGE_MODULE = "/usr/lib/vst3/Surge XT.vst3"
SURGE_CID = "ABCDEF019182FAEB566D624153675854"


def _descriptor(
    name: str = "Synth",
    module: str = SURGE_MODULE,
    cid: str = SURGE_CID,
    sub_categories: str = "Instrument|Synth",
    vendor: str = "Vendor",
) -> dict:
    return {
        "name": name,
        "vendor": vendor,
        "module": module,
        "cid": cid,
        "is_instrument": sub_categories.startswith("Instrument"),
        "sub_categories": sub_categories,
        "version": "1.0",
        "class_flags": 0,
    }


class TestVst3Serialization:
    """The XML shape MXM's Vst3Instrument/MxmPluginBridge expects."""

    def _track(self, instrument: str = "tripleoscillator"):
        root = create_empty_project()
        return add_instrument_track(root, "VST3", instrument=instrument)

    def test_writes_mxm_native_layout(self):
        track = self._track()
        configure_native_vst3_instrument(
            track, SURGE_MODULE, "abcdef019182faeb566d624153675854"
        )
        instrument = track.find("instrumenttrack/instrument")
        assert instrument.get("name") == "vst3instrument"
        wrapper = instrument.find("vst3instrument")
        assert wrapper is not None
        attributes = {
            attr.get("name"): attr.get("value")
            for attr in wrapper.findall("key/attribute")
        }
        assert attributes == {
            "module": SURGE_MODULE,
            "cid": SURGE_CID,  # normalized to upper case
        }

    def test_is_not_carla_and_not_lv2(self):
        track = self._track()
        configure_native_vst3_instrument(track, SURGE_MODULE, SURGE_CID)
        instrument = track.find("instrumenttrack/instrument")
        assert instrument.find("carlarack") is None
        assert instrument.find("lv2controls") is None
        assert instrument.find("vst3instrument") is not None

    def test_replaces_a_previous_plugin(self):
        track = self._track()
        configure_native_vst3_instrument(track, SURGE_MODULE, SURGE_CID)
        instrument = track.find("instrumenttrack/instrument")
        assert instrument.find("tripleoscillator") is None
        assert len(instrument.findall("vst3instrument")) == 1

    def test_requires_a_module_path(self):
        track = self._track()
        with pytest.raises(ValueError, match="module path"):
            configure_native_vst3_instrument(track, "", SURGE_CID)

    def test_rejects_malformed_class_id(self):
        track = self._track()
        with pytest.raises(ValueError, match="32 hexadecimal"):
            configure_native_vst3_instrument(track, SURGE_MODULE, "not-a-cid")

    def test_optional_state_and_models(self):
        track = self._track()
        configure_native_vst3_instrument(
            track, SURGE_MODULE, SURGE_CID, state="QUJD"
        )
        wrapper = track.find("instrumenttrack/instrument/vst3instrument")
        state = wrapper.find("state")
        assert state.get("encoding") == "base64"
        assert state.text == "QUJD"
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
        assert instrument is not None
        attributes = {
            attr.get("name"): attr.get("value")
            for attr in instrument.findall("vst3instrument/key/attribute")
        }
        assert attributes == {"module": SURGE_MODULE, "cid": SURGE_CID}
        assert loaded.find(".//midiclip/note").get("key") == "60"


class TestVst3Helpers:
    def test_cid_validation(self):
        assert vst3.is_valid_cid(SURGE_CID)
        assert vst3.is_valid_cid(SURGE_CID.lower())
        assert not vst3.is_valid_cid("xyz")
        assert not vst3.is_valid_cid("AB" * 15)

    def test_normalize_cid(self):
        assert vst3.normalize_cid(" abcdef0123456789abcdef0123456789 ") == (
            "ABCDEF0123456789ABCDEF0123456789"
        )


class TestVst3Discovery:
    def test_find_bundles_honours_vst3_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv(vst3.PATH_ONLY_ENV, "1")
        monkeypatch.setenv("VST3_PATH", str(tmp_path))
        bundle = tmp_path / "Extra.vst3"
        bundle.mkdir()
        assert vst3.find_vst3_bundles() == [bundle]

    def test_path_only_ignores_standard_locations(self, tmp_path, monkeypatch):
        standard = tmp_path / "standard"
        standard.mkdir()
        (standard / "Builtin.vst3").mkdir()
        monkeypatch.setattr(vst3, "standard_vst3_dirs", lambda: [standard])
        monkeypatch.delenv("VST3_PATH", raising=False)

        monkeypatch.setenv(vst3.PATH_ONLY_ENV, "1")
        assert vst3.find_vst3_bundles() == []

        monkeypatch.delenv(vst3.PATH_ONLY_ENV)
        found = vst3.find_vst3_bundles()
        assert [path.name for path in found] == ["Builtin.vst3"]

    def test_empty_path_only_env_means_path_only(self, monkeypatch, tmp_path):
        """MXM tests std::getenv presence, so "" still means path-only."""
        standard = tmp_path / "standard"
        standard.mkdir()
        (standard / "Builtin.vst3").mkdir()
        monkeypatch.setattr(vst3, "standard_vst3_dirs", lambda: [standard])
        monkeypatch.setenv(vst3.PATH_ONLY_ENV, "")
        monkeypatch.delenv("VST3_PATH", raising=False)

        assert vst3.path_only_enabled() is True
        assert vst3.find_vst3_bundles() == []

    def test_standard_scan_is_recursive(self, tmp_path, monkeypatch):
        standard = tmp_path / "standard"
        (standard / "sub").mkdir(parents=True)
        (standard / "Top.vst3").mkdir()
        (standard / "sub" / "Inner.vst3").mkdir()
        monkeypatch.setattr(vst3, "standard_vst3_dirs", lambda: [standard])
        monkeypatch.delenv(vst3.PATH_ONLY_ENV, raising=False)
        monkeypatch.delenv("VST3_PATH", raising=False)

        names = {path.name for path in vst3.find_vst3_bundles()}
        assert names == {"Top.vst3", "Inner.vst3"}

    def test_scan_does_not_descend_into_bundles(self, tmp_path, monkeypatch):
        standard = tmp_path / "standard"
        nested = standard / "Top.vst3" / "Nested.vst3"
        nested.mkdir(parents=True)
        monkeypatch.setattr(vst3, "standard_vst3_dirs", lambda: [standard])
        monkeypatch.delenv(vst3.PATH_ONLY_ENV, raising=False)
        monkeypatch.delenv("VST3_PATH", raising=False)

        names = {path.name for path in vst3.find_vst3_bundles()}
        assert names == {"Top.vst3"}

    def test_vst3_path_dir_scan_skips_vst3_files(self, tmp_path, monkeypatch):
        # MXM's discoverPathOrDirectory lists directories only (QDir::Dirs).
        (tmp_path / "Real.vst3").mkdir()
        (tmp_path / "Loose.vst3").write_bytes(b"not a bundle")
        monkeypatch.setenv(vst3.PATH_ONLY_ENV, "1")
        monkeypatch.setenv("VST3_PATH", str(tmp_path))

        found = vst3.find_vst3_bundles()
        assert found == [tmp_path / "Real.vst3"]

    def test_relative_vst3_path_is_made_absolute(self, tmp_path, monkeypatch):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "Inner.vst3").mkdir()
        monkeypatch.setenv(vst3.PATH_ONLY_ENV, "1")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("VST3_PATH", "sub")

        found = vst3.find_vst3_bundles()
        assert found == [tmp_path / "sub" / "Inner.vst3"]
        assert found[0].is_absolute()

    def test_effective_search_paths_honours_path_only(self, monkeypatch, tmp_path):
        standard = tmp_path / "standard"
        monkeypatch.setattr(vst3, "standard_vst3_dirs", lambda: [standard])
        monkeypatch.setenv("VST3_PATH", "/extra/one:/extra/two")

        monkeypatch.delenv(vst3.PATH_ONLY_ENV, raising=False)
        assert vst3.effective_search_paths() == [
            str(standard), "/extra/one", "/extra/two",
        ]

        monkeypatch.setenv(vst3.PATH_ONLY_ENV, "1")
        assert vst3.effective_search_paths() == ["/extra/one", "/extra/two"]

    def test_app_vst3_dir_follows_symlink(self, tmp_path, monkeypatch):
        # Real layout: <appdir>/vst3 next to the resolved executable.
        real_bin = tmp_path / "real" / "bin"
        real_bin.mkdir(parents=True)
        (real_bin / "vst3").mkdir()
        exe = real_bin / "mxm"
        exe.write_bytes(b"#!/bin/true\n")
        link_dir = tmp_path / "launcher"
        link_dir.mkdir()
        link = link_dir / "mxm"
        link.symlink_to(exe)
        monkeypatch.setattr(vst3.lmms_app, "find_mxm_exe", lambda: link)
        monkeypatch.setattr(vst3.lmms_app, "find_lmms_exe", lambda: None)

        assert vst3._app_vst3_dir() == real_bin / "vst3"

    def test_native_vst3_platform_guard(self, monkeypatch):
        monkeypatch.setattr(vst3.sys, "platform", "win32")
        assert vst3.native_vst3_discovery_supported() is False
        assert vst3.discover_vst3_plugins(bundles=[Path("/x.vst3")]) == []

    def test_discover_filters_audio_modules_and_classifies(self):
        def fake_probe(bundle):
            return {
                "module": str(bundle),
                "classes": [
                    {
                        "cid": CID_A,
                        "name": "Synth",
                        "category": "Audio Module Class",
                        "sub_categories": "Instrument|Synth",
                        "vendor": "V",
                        "version": "1",
                        "class_flags": 0,
                    },
                    {
                        # Controller companion class - must be dropped.
                        "cid": CID_B,
                        "name": "Synth",
                        "category": "Component Controller Class",
                        "sub_categories": "Instrument|Synth",
                    },
                    {
                        "cid": "EF" * 16,
                        "name": "Reverb",
                        "category": "Audio Module Class",
                        "sub_categories": "Fx|Reverb",
                        "vendor": "V",
                        "version": "1",
                        "class_flags": 0,
                    },
                ],
            }

        plugins = vst3.discover_vst3_plugins(
            bundles=[Path("/x.vst3")], probe=fake_probe
        )
        assert [plugin["name"] for plugin in plugins] == ["Reverb", "Synth"]
        by_name = {plugin["name"]: plugin for plugin in plugins}
        assert by_name["Synth"]["is_instrument"] is True
        assert by_name["Reverb"]["is_instrument"] is False
        assert by_name["Synth"]["cid"] == CID_A

    def test_discover_skips_errors_and_duplicates(self):
        def fake_probe(bundle):
            if bundle.name == "bad.vst3":
                return {"module": str(bundle), "error": "boom"}
            description = {
                "cid": CID_A,
                "name": "Synth",
                "category": "Audio Module Class",
                "sub_categories": "Instrument|Synth",
                "vendor": "",
                "version": "",
                "class_flags": 0,
            }
            return {"module": str(bundle), "classes": [description, description]}

        plugins = vst3.discover_vst3_plugins(
            bundles=[Path("/bad.vst3"), Path("/good.vst3")], probe=fake_probe
        )
        assert len(plugins) == 1
        assert plugins[0]["name"] == "Synth"

    def test_discover_drops_invalid_class_ids(self):
        def fake_probe(bundle):
            return {
                "module": str(bundle),
                "classes": [{
                    "cid": "1234",
                    "name": "Broken",
                    "category": "Audio Module Class",
                    "sub_categories": "Instrument",
                }],
            }

        assert vst3.discover_vst3_plugins(
            bundles=[Path("/x.vst3")], probe=fake_probe
        ) == []

    def test_resolve_by_exact_name(self):
        plugins = [_descriptor(name="Surge XT"), _descriptor(name="Zebralette3")]
        plugin, candidates = vst3.resolve_vst3_instrument(
            plugins, plugin_name="Surge XT"
        )
        assert plugin["name"] == "Surge XT"
        assert candidates == []

    def test_resolve_ambiguous_name_returns_candidates(self):
        plugins = [_descriptor(name="Surge XT"), _descriptor(name="Surge XT Effects")]
        plugin, candidates = vst3.resolve_vst3_instrument(
            plugins, plugin_name="surge"
        )
        assert plugin is None
        assert [candidate["name"] for candidate in candidates] == [
            "Surge XT", "Surge XT Effects",
        ]

    def test_resolve_unknown_name(self):
        plugins = [_descriptor(name="Surge XT")]
        plugin, candidates = vst3.resolve_vst3_instrument(
            plugins, plugin_name="nonexistent"
        )
        assert plugin is None
        assert candidates == []

    def test_resolve_by_module_and_cid(self):
        plugins = [_descriptor(name="Surge XT")]
        plugin, candidates = vst3.resolve_vst3_instrument(
            plugins, module=SURGE_MODULE, cid=SURGE_CID.lower()
        )
        assert plugin["name"] == "Surge XT"
        assert candidates == []

    def test_resolve_instruments_only_skips_effects(self):
        plugins = [
            _descriptor(name="Shared", sub_categories="Instrument|Synth"),
            _descriptor(name="Shared", sub_categories="Fx|Reverb"),
        ]
        # Without the filter the exact name is ambiguous.
        plugin, candidates = vst3.resolve_vst3_instrument(
            plugins, plugin_name="Shared"
        )
        assert plugin is None
        assert len(candidates) == 2

        plugin, candidates = vst3.resolve_vst3_instrument(
            plugins, plugin_name="Shared", instruments_only=True
        )
        assert plugin is not None
        assert plugin["is_instrument"] is True
        assert candidates == []


class TestMxmBuildOptionsCache:
    def test_build_options_are_cached(self, monkeypatch):
        from lmms_mcp import lmms_app

        calls = []

        class Completed:
            stdout = "MXM_HAVE_VST3='TRUE'\nWANT_VST3='ON'\n"
            stderr = ""

        monkeypatch.setattr(lmms_app, "find_mxm_exe", lambda: Path("/fake/mxm"))
        monkeypatch.setattr(
            lmms_app.subprocess, "run",
            lambda *a, **k: calls.append(a) or Completed(),
        )
        lmms_app.clear_mxm_build_options_cache()
        try:
            first = lmms_app.get_mxm_build_options()
            second = lmms_app.get_mxm_build_options()
        finally:
            lmms_app.clear_mxm_build_options_cache()

        assert first == second
        assert first["have_vst3"] is True
        assert len(calls) == 1

    def test_build_options_returns_independent_dicts(self, monkeypatch):
        from lmms_mcp import lmms_app

        class Completed:
            stdout = "MXM_HAVE_VST3='TRUE'\n"
            stderr = ""

        monkeypatch.setattr(lmms_app, "find_mxm_exe", lambda: Path("/fake/mxm"))
        monkeypatch.setattr(lmms_app.subprocess, "run", lambda *a, **k: Completed())
        lmms_app.clear_mxm_build_options_cache()
        try:
            first = lmms_app.get_mxm_build_options()
            first["have_vst3"] = False
            second = lmms_app.get_mxm_build_options()
        finally:
            lmms_app.clear_mxm_build_options_cache()

        assert second["have_vst3"] is True


class TestVst3ServerTools:
    def _patch_discovery(self, monkeypatch, plugins):
        monkeypatch.setattr(vst3, "discover_vst3_plugins", lambda *a, **k: plugins)

    def _new_project(self):
        project = LMMSProject()
        project.new()
        srv.set_project(project)
        return project

    def test_list_instruments_only_by_default(self, monkeypatch):
        self._patch_discovery(monkeypatch, [
            _descriptor(name="Surge XT"),
            _descriptor(name="ZamComp", sub_categories="Fx|Dynamics"),
        ])
        payload = json.loads(srv.list_vst3_instruments())
        assert payload["host"] == "vst3instrument"
        assert payload["native"] is True
        assert payload["carla"] is False
        assert payload["instrument_count"] == 1
        assert [plugin["name"] for plugin in payload["plugins"]] == ["Surge XT"]

    def test_list_includes_effects_on_request(self, monkeypatch):
        self._patch_discovery(monkeypatch, [
            _descriptor(name="Surge XT"),
            _descriptor(name="ZamComp", sub_categories="Fx|Dynamics"),
        ])
        payload = json.loads(srv.list_vst3_instruments(include_effects=True))
        assert payload["instrument_count"] == 1
        assert payload["effect_count"] == 1
        assert len(payload["plugins"]) == 2

    def test_add_by_name_writes_native_vst3(self, monkeypatch):
        self._patch_discovery(monkeypatch, [_descriptor(name="Surge XT")])
        project = self._new_project()
        result = json.loads(srv.add_vst3_instrument_track("Lead", plugin="Surge XT"))
        assert result["host"] == "vst3instrument"
        assert result["native"] is True
        assert result["carla"] is False
        assert result["plugin_type"] == "VST3"
        assert result["cid"] == SURGE_CID
        assert result["verified"] is True

        instrument = project.root.find(".//instrument[@name='vst3instrument']")
        assert instrument is not None
        attributes = {
            attr.get("name"): attr.get("value")
            for attr in instrument.findall("vst3instrument/key/attribute")
        }
        assert attributes == {"module": SURGE_MODULE, "cid": SURGE_CID}
        # Never the Carla bridge.
        assert project.root.find(".//instrument[@name='carlarack']") is None

    def test_add_explicit_module_and_cid(self, monkeypatch):
        self._patch_discovery(monkeypatch, [_descriptor(name="Surge XT")])
        self._new_project()
        result = json.loads(srv.add_vst3_instrument_track(
            "Lead", module_path=SURGE_MODULE, cid=SURGE_CID.lower()
        ))
        assert result["cid"] == SURGE_CID

    def test_add_unverified_requires_flag(self, monkeypatch):
        self._patch_discovery(monkeypatch, [])
        self._new_project()
        rejected = json.loads(srv.add_vst3_instrument_track(
            "Lead", module_path=SURGE_MODULE, cid=SURGE_CID
        ))
        assert "error" in rejected
        accepted = json.loads(srv.add_vst3_instrument_track(
            "Lead", module_path=SURGE_MODULE, cid=SURGE_CID,
            allow_unverified=True,
        ))
        assert accepted["host"] == "vst3instrument"
        assert accepted["verified"] is False

    def test_add_rejects_incomplete_explicit_identity(self, monkeypatch):
        self._patch_discovery(monkeypatch, [])
        self._new_project()
        response = json.loads(srv.add_vst3_instrument_track(
            "Lead", module_path=SURGE_MODULE
        ))
        assert "both module_path and cid" in response["error"]

    def test_add_unknown_name_lists_available(self, monkeypatch):
        self._patch_discovery(monkeypatch, [_descriptor(name="Surge XT")])
        self._new_project()
        response = json.loads(srv.add_vst3_instrument_track("Lead", plugin="Nope"))
        assert "error" in response
        assert [plugin["name"] for plugin in response["available"]] == ["Surge XT"]

    def test_add_ambiguous_name_lists_matches(self, monkeypatch):
        self._patch_discovery(monkeypatch, [
            _descriptor(name="Surge XT"),
            _descriptor(name="Surge XT Effects"),
        ])
        self._new_project()
        response = json.loads(srv.add_vst3_instrument_track("Lead", plugin="surge"))
        assert "error" in response
        assert [plugin["name"] for plugin in response["matches"]] == [
            "Surge XT", "Surge XT Effects",
        ]

    def test_add_by_name_ignores_effects(self, monkeypatch):
        self._patch_discovery(monkeypatch, [
            _descriptor(name="ZamComp", sub_categories="Fx|Dynamics"),
        ])
        self._new_project()
        response = json.loads(srv.add_vst3_instrument_track(
            "Lead", plugin="ZamComp"
        ))
        assert "error" in response
        assert response["available"] == []

    def test_add_rolls_back_track_when_configuration_fails(self, monkeypatch):
        self._patch_discovery(monkeypatch, [_descriptor(name="Surge XT")])
        project = self._new_project()
        # Pretend the project was just saved/cleaned so a stray modified flag
        # would be observable.
        project._modified = False

        def boom(*args, **kwargs):
            raise ValueError("serialization failed")

        monkeypatch.setattr(xml_parser, "configure_native_vst3_instrument", boom)
        response = json.loads(srv.add_vst3_instrument_track(
            "Lead", plugin="Surge XT"
        ))
        assert "error" in response
        assert find_tracks(project.root) == []
        # A rolled-back change must not leave the project marked modified.
        assert project.modified is False

    def test_add_requires_a_selection(self, monkeypatch):
        self._patch_discovery(monkeypatch, [])
        self._new_project()
        response = json.loads(srv.add_vst3_instrument_track("Lead"))
        assert "error" in response

    def test_add_instrument_track_redirects_vst3_host(self, monkeypatch):
        self._new_project()
        response = json.loads(srv.add_instrument_track(
            "Lead", instrument="vst3instrument"
        ))
        assert "add_vst3_instrument_track" in response["error"]


_FAKE_MODULE_C = textwrap.dedent(
    r"""
    #include <stdint.h>
    #include <string.h>

    typedef int32_t tresult;
    typedef int32_t int32;
    typedef uint32_t uint32;

    typedef struct {
        uint8_t cid[16];
        int32 cardinality;
        char category[32];
        char name[64];
        uint32 class_flags;
        char sub_categories[128];
        char vendor[64];
        char version[64];
        char sdk_version[64];
    } PClassInfo2;

    typedef struct FactoryVtbl {
        tresult (*queryInterface)(void*, const char*, void**);
        uint32 (*addRef)(void*);
        uint32 (*release)(void*);
        tresult (*getFactoryInfo)(void*, void*);
        int32 (*countClasses)(void*);
        tresult (*getClassInfo)(void*, int32, void*);
        tresult (*createInstance)(void*, const char*, const char*, void**);
        tresult (*getClassInfo2)(void*, int32, PClassInfo2*);
    } FactoryVtbl;

    typedef struct { const FactoryVtbl* vtbl; } Factory;

    static tresult f_query(void* self, const char* iid, void** obj) {
        (void)iid; *obj = self; return 0;
    }
    static uint32 f_addref(void* self) { (void)self; return 1; }
    static uint32 f_release(void* self) { (void)self; return 0; }
    static tresult f_info(void* self, void* info) {
        (void)self; memset(info, 0, 64 + 256 + 128 + 4);
        strcpy((char*)info, "Fake Vendor"); return 0;
    }
    static int32 f_count(void* self) { (void)self; return 1; }
    static tresult f_get(void* self, int32 i, void* info) {
        (void)self; (void)i; (void)info; return 1;
    }
    static tresult f_create(void* self, const char* cid, const char* iid, void** obj) {
        (void)self; (void)cid; (void)iid; (void)obj; return 1;
    }
    static tresult f_get2(void* self, int32 i, PClassInfo2* info) {
        (void)self;
        if (i != 0) return 1;
        memset(info, 0, sizeof(*info));
        info->cid[0] = 0xA1; info->cid[1] = 0xB2;
        info->cid[2] = 0xC3; info->cid[3] = 0xD4;
        info->cid[4] = 0xE5; info->cid[5] = 0xF6;
        info->cid[6] = 0x07; info->cid[7] = 0x18;
        info->cid[8] = 0x29; info->cid[9] = 0x3A;
        info->cid[10] = 0x4B; info->cid[11] = 0x4C;
        /* bytes 12-15 stay zero: guards against NUL truncation */
        info->cardinality = 0x7FFFFFFF;
        strcpy(info->category, "Audio Module Class");
        strcpy(info->name, "Fake Synth");
        info->class_flags = 0;
        strcpy(info->sub_categories, "Instrument|Synth");
        strcpy(info->vendor, "Fake Vendor");
        strcpy(info->version, "1.2.3");
        strcpy(info->sdk_version, "VST 3.7");
        return 0;
    }

    static const FactoryVtbl g_vtbl = {
        f_query, f_addref, f_release, f_info, f_count, f_get, f_create, f_get2
    };
    static Factory g_factory = { &g_vtbl };

    int ModuleEntry(void* handle) { (void)handle; return 1; }
    int ModuleExit(void) { return 1; }
    Factory* GetPluginFactory(void) { return &g_factory; }
    """
).strip()


def _build_fake_bundle(tmp_path: Path) -> Path:
    """Compile a minimal VST3 module so the ctypes probe can be tested."""
    if not sys.platform.startswith("linux"):
        pytest.skip("native VST3 discovery is Linux-only in this MCP")
    compiler = shutil.which("cc") or shutil.which("gcc")
    if compiler is None:
        pytest.skip("no C compiler available to build a stub VST3 module")
    bundle = tmp_path / "Fake.vst3"
    so_dir = bundle / "Contents" / f"{os.uname().machine}-linux"
    so_dir.mkdir(parents=True)
    source = tmp_path / "fake_module.c"
    source.write_text(_FAKE_MODULE_C)
    so_path = so_dir / "Fake.so"
    proc = subprocess.run(
        [compiler, "-shared", "-fPIC", "-O0", "-o", str(so_path), str(source)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        pytest.skip(f"could not build stub VST3 module: {proc.stderr.strip()}")
    return bundle


class TestVst3ProbeCtypes:
    """Deterministic coverage for the riskiest code in vst3_probe.py."""

    def test_bundle_so_path_locates_linux_binary(self, tmp_path):
        bundle = tmp_path / "X.vst3"
        so_dir = bundle / "Contents" / f"{os.uname().machine}-linux"
        so_dir.mkdir(parents=True)
        binary = so_dir / "X.so"
        binary.write_bytes(b"")
        assert vst3_probe.vst3_bundle_so_path(bundle) == binary

    def test_bundle_so_path_missing(self, tmp_path):
        bundle = tmp_path / "Empty.vst3"
        bundle.mkdir()
        assert vst3_probe.vst3_bundle_so_path(bundle) is None

    def test_probe_reports_missing_binary(self, tmp_path):
        bundle = tmp_path / "Empty.vst3"
        bundle.mkdir()
        result = vst3_probe.probe_bundle(bundle)
        assert "error" in result

    def test_probe_reads_stub_factory(self, tmp_path):
        bundle = _build_fake_bundle(tmp_path)
        result = vst3_probe.probe_bundle(bundle)
        assert "error" not in result, result
        classes = result["classes"]
        assert len(classes) == 1
        info = classes[0]
        # Trailing zero bytes must be preserved (regression guard).
        assert info["cid"] == "A1B2C3D4E5F60718293A4B4C00000000"
        assert info["name"] == "Fake Synth"
        assert info["category"] == "Audio Module Class"
        assert info["sub_categories"] == "Instrument|Synth"
        assert info["vendor"] == "Fake Vendor"
        assert info["version"] == "1.2.3"

    def test_stub_module_flows_through_discovery(self, tmp_path):
        bundle = _build_fake_bundle(tmp_path)
        plugins = vst3.discover_vst3_plugins(
            bundles=[bundle], probe=vst3_probe.probe_bundle
        )
        assert len(plugins) == 1
        assert plugins[0]["is_instrument"] is True
        assert plugins[0]["cid"] == "A1B2C3D4E5F60718293A4B4C00000000"
        assert plugins[0]["module"] == str(bundle)

    def test_probe_main_emits_json_line(self, tmp_path, capsys):
        bundle = _build_fake_bundle(tmp_path)
        assert vst3_probe.main([str(bundle)]) == 0
        lines = [
            line for line in capsys.readouterr().out.splitlines() if line.strip()
        ]
        payload = json.loads(lines[-1])
        assert payload["classes"][0]["name"] == "Fake Synth"
