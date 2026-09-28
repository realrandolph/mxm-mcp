"""Tests for native VST3 hosting support (MXM's ``vst3instrument``).

These tests do not require an installed VST3 plugin: discovery is exercised
with injected probe output, and serialization is pure XML. The end-to-end
render check lives in ``test_lmms13_integration.py``.
"""

import json
import tempfile
from pathlib import Path

import pytest

from lmms_mcp import vst3
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
        bundle = tmp_path / "Extra.vst3"
        bundle.mkdir()
        monkeypatch.setenv("VST3_PATH", str(tmp_path))
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
            _descriptor(name="Surge XT Effects", sub_categories="Fx|Reverb"),
        ])
        self._new_project()
        response = json.loads(srv.add_vst3_instrument_track("Lead", plugin="surge"))
        assert "error" in response
        assert [plugin["name"] for plugin in response["matches"]] == [
            "Surge XT", "Surge XT Effects",
        ]

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
