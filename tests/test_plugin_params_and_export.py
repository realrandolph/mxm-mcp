"""Tests for VST3 parameter introspection and natural-text preset export (Issue #20).

The probe's ctypes layer is exercised against real bundles by the manual
integration checks; these tests drive the pure logic and the two capability
functions with a synthetic probe, so no installed plugin is required.
"""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path

import pytest

from mxm_mcp import plugin_params, vst3_probe

SYNTHETIC_CID = "A1B2C3D4E5F60718293A4B4C00000000"
BUNDLE = Path("/fixtures/Synthetic.vst3")


def synthetic_parameters() -> list[dict]:
    """The parameter records a probe returns for a small synthetic synth."""
    return [
        {
            "id": 1,
            "name": "Filter Cutoff",
            "short_name": "Cutoff",
            "module": "Filter",
            "units": "Hz",
            "min": 20.0,
            "max": 20000.0,
            "default": 800.0,
            "current": 800.0,
            "normalized": 0.534,
            "default_normalized": 0.534,
            "step_count": 0,
            "stepped": False,
            "readonly": False,
            "hidden": False,
            "can_automate": True,
            "is_list": False,
            "is_program_change": False,
            "is_bypass": False,
            "unit_id": 1,
            "display": "800 Hz",
        },
        {
            "id": 2,
            "name": "Resonance",
            "short_name": "Res",
            "module": "Filter",
            "units": "",
            "min": 0.0,
            "max": 1.0,
            "default": 0.2,
            "current": 0.2,
            "normalized": 0.2,
            "default_normalized": 0.2,
            "step_count": 0,
            "stepped": False,
            "readonly": False,
            "hidden": False,
            "can_automate": True,
            "is_list": False,
            "is_program_change": False,
            "is_bypass": False,
            "unit_id": 1,
            "display": "0.200",
        },
        {
            "id": 3,
            "name": "Attack",
            "short_name": "A",
            "module": "Amp Envelope",
            "units": "ms",
            "min": 0.0,
            "max": 5000.0,
            "default": 10.0,
            "current": 10.0,
            "normalized": 0.002,
            "default_normalized": 0.002,
            "step_count": 0,
            "stepped": False,
            "readonly": False,
            "hidden": False,
            "can_automate": True,
            "is_list": False,
            "is_program_change": False,
            "is_bypass": False,
            "unit_id": 2,
            "display": "10 ms",
        },
        {
            "id": 4,
            "name": "Mode",
            "short_name": "Mode",
            "module": "",
            "units": "",
            "min": 0.0,
            "max": 3.0,
            "default": 0.0,
            "current": 0.0,
            "normalized": 0.0,
            "default_normalized": 0.0,
            "step_count": 3,
            "stepped": True,
            "readonly": False,
            "hidden": False,
            "can_automate": True,
            "is_list": True,
            "is_program_change": False,
            "is_bypass": False,
            "unit_id": 0,
            "display": "Lowpass",
        },
        {
            "id": 5,
            "name": "Output Gain",
            "short_name": "Gain",
            "module": "Output",
            "units": "dB",
            "min": -24.0,
            "max": 24.0,
            "default": 0.0,
            "current": 0.0,
            "normalized": 0.5,
            "default_normalized": 0.5,
            "step_count": 0,
            "stepped": False,
            "readonly": False,
            "hidden": False,
            "can_automate": True,
            "is_list": False,
            "is_program_change": False,
            "is_bypass": False,
            "unit_id": 3,
            "display": "0.0 dB",
        },
    ]


class FakeProbe:
    """A synthetic ``python -m mxm_mcp.vst3_probe`` process.

    ``text_values`` maps a parameter id to the conversion it accepts; any other
    text is rejected, which exercises the local fallback path.
    """

    def __init__(self, parameters=None, text_values=None, verification=None):
        self.parameters = [dict(item) for item in (parameters or synthetic_parameters())]
        self.text_values = text_values or {}
        self.verification = verification
        self.calls: list[list[str]] = []
        self.state_files: list[bytes] = []
        self.saved: dict[int, float] = {}

    def _parameters(self, preset=None):
        parameters = [dict(item) for item in self.parameters]
        values = self.verification if (preset and self.verification is not None) else self.saved
        for item in parameters:
            if int(item["id"]) in values:
                item["normalized"] = values[int(item["id"])]
                item["current"] = values[int(item["id"])]
        return parameters

    def __call__(self, arguments):
        arguments = list(arguments)
        self.calls.append(arguments)
        if "--state-file" in arguments:
            self.state_files.append(
                Path(arguments[arguments.index("--state-file") + 1]).read_bytes())
        if not arguments:
            return {"error": "no arguments"}
        if arguments[0] == "--params":
            preset = arguments[arguments.index("--preset") + 1] if "--preset" in arguments else None
            return {
                "module": arguments[1],
                "class": {"cid": SYNTHETIC_CID, "name": "Synthetic Synth"},
                "parameters": self._parameters(preset),
                "count": len(self.parameters),
                "warnings": [],
            }
        if arguments[0] == "--save-preset":
            out = Path(arguments[arguments.index("--out") + 1])
            applied: dict[int, float] = {}
            results: list[dict] = []
            for index, argument in enumerate(arguments):
                if argument == "--set":
                    key, value = _split(arguments[index + 1])
                    applied[int(key)] = float(value)
            texts = [arguments[i + 1] for i, a in enumerate(arguments) if a == "--set-text"]
            fallbacks = {
                int(_split(arguments[i + 1])[0]): float(_split(arguments[i + 1])[1])
                for i, a in enumerate(arguments) if a == "--set-fallback"
            }
            for entry in texts:
                key, text = _split(entry)
                param_id = int(key)
                accepted = self.text_values.get(param_id, {}).get(text)
                if accepted is not None:
                    applied[param_id] = float(accepted)
                    results.append({"id": param_id, "text": text, "source": "text"})
                elif param_id in fallbacks:
                    applied[param_id] = fallbacks[param_id]
                    results.append({"id": param_id, "text": text, "source": "fallback"})
            self.saved = dict(applied)
            vst3_probe._atomic_write(
                out, vst3_probe.build_vstpreset(SYNTHETIC_CID, {"Comp": b"synthetic-state"}))
            return {
                "module": arguments[1],
                "path": str(out),
                "bytes": out.stat().st_size,
                "class": {"cid": SYNTHETIC_CID, "name": "Synthetic Synth"},
                "parameters_applied": len(applied),
                "applied": [{"id": param_id, "normalized": value}
                            for param_id, value in sorted(applied.items())],
                "parameter_results": results,
                "warnings": [],
            }
        return {"error": f"unexpected probe mode {arguments[0]!r}"}


def _split(text: str) -> tuple[str, str]:
    key, _, value = text.partition("=")
    return key, value


@pytest.fixture
def fake_probe(monkeypatch):
    probe = FakeProbe(
        text_values={1: {"800 Hz": 0.534, "1200 Hz": 0.61}},
    )
    monkeypatch.setattr(plugin_params, "resolve_plugin_bundle", lambda _plugin: BUNDLE)
    return probe


# ---- capability: get_plugin_parameters -----------------------------------------------------

class TestGetPluginParameters:
    def test_enumerates_core_fields(self, fake_probe):
        result = plugin_params.get_plugin_parameters("Synthetic", probe=fake_probe)
        assert result["plugin"] == "Synthetic"
        assert result["module"] == str(BUNDLE)
        assert result["count"] == 5
        cutoff = result["parameters"][0]
        assert cutoff["id"] == 1
        assert cutoff["name"] == "Filter Cutoff"
        assert cutoff["module"] == "Filter"
        assert cutoff["min"] == 20.0 and cutoff["max"] == 20000.0
        assert cutoff["default"] == 800.0 and cutoff["units"] == "Hz"
        assert cutoff["display"] == "800 Hz"
        assert cutoff["stepped"] is False
        # The compact response keeps flags out unless details are requested.
        assert "can_automate" not in cutoff
        assert fake_probe.calls[0][:2] == ["--params", str(BUNDLE)]

    def test_details_keeps_flags_and_short_names(self, fake_probe):
        result = plugin_params.get_plugin_parameters("Synthetic", details=True, probe=fake_probe)
        mode = result["parameters"][3]
        assert mode["short_name"] == "Mode"
        assert mode["step_count"] == 3 and mode["stepped"] is True
        assert mode["is_list"] is True and mode["can_automate"] is True
        assert mode["unit_id"] == 0

    @pytest.mark.parametrize("query,expected", [
        ("cutoff", [1]),
        ("Filter", [1, 2]),
        ("Filter/Cutoff", [1]),
        ("#2", [2]),
        ("amp envelope", [3]),
        ("filter cutoff", [1]),
        ("nothing-here", []),
    ])
    def test_search_matches_name_id_and_module(self, fake_probe, query, expected):
        result = plugin_params.get_plugin_parameters("Synthetic", search=query, probe=fake_probe)
        assert [item["id"] for item in result["parameters"]] == expected
        assert result["count"] == len(expected)

    def test_search_requires_every_token(self, fake_probe):
        result = plugin_params.get_plugin_parameters(
            "Synthetic", search="filter resonance", probe=fake_probe)
        assert [item["id"] for item in result["parameters"]] == [2]

    def test_preset_is_loaded_before_inspection(self, fake_probe, tmp_path):
        preset = tmp_path / "patch.vstpreset"
        preset.write_bytes(vst3_probe.build_vstpreset(SYNTHETIC_CID, {"Comp": b"x"}))
        result = plugin_params.get_plugin_parameters(
            "Synthetic", preset=str(preset), probe=fake_probe)
        assert ["--preset", str(preset)] == fake_probe.calls[0][2:4]
        assert result["preset"] == str(preset)

    def test_missing_preset_is_reported(self, fake_probe, tmp_path):
        with pytest.raises(plugin_params.PluginParameterError, match="preset not found"):
            plugin_params.get_plugin_parameters(
                "Synthetic", preset=str(tmp_path / "nope.vstpreset"), probe=fake_probe)

    def test_probe_error_is_raised(self, monkeypatch):
        monkeypatch.setattr(plugin_params, "resolve_plugin_bundle", lambda _p: BUNDLE)
        probe = lambda _arguments: {"error": "boom"}
        with pytest.raises(plugin_params.PluginParameterError, match="boom"):
            plugin_params.get_plugin_parameters("Synthetic", probe=probe)


# ---- natural text --------------------------------------------------------------------------

class TestNaturalText:
    @pytest.mark.parametrize("text,plain", [
        ("800 Hz", 800.0),
        ("1.2 kHz", 1200.0),
        ("-6 dB", -6.0),
        ("50%", 0.5),
        ("250 ms", 250.0),
        ("0.65", 0.65),
    ])
    def test_parse_natural_value(self, text, plain):
        cutoff, resonance, attack, _mode, gain = synthetic_parameters()
        if text.endswith("ms"):
            param = attack
        elif text == "50%":
            param = resonance
        elif text == "0.65":
            param = resonance
        elif text.endswith("dB"):
            param = gain
        else:
            param = cutoff
        assert plugin_params.parse_natural_value(text, param) == pytest.approx(plain)

    def test_ratio_text_is_left_to_the_plugin(self):
        mode = synthetic_parameters()[3]
        assert plugin_params.parse_natural_value("1/16", mode) is None
        assert plugin_params.text_to_normalized("1/16", mode) is None

    def test_frequency_conversion_is_logarithmic(self):
        cutoff = synthetic_parameters()[0]
        normalized = plugin_params.text_to_normalized("800 Hz", cutoff)
        expected = math.log(800 / 20) / math.log(20000 / 20)
        assert normalized == pytest.approx(expected)

    def test_linear_conversion_clamps_to_range(self):
        resonance = synthetic_parameters()[1]
        assert plugin_params.text_to_normalized("0.25", resonance) == pytest.approx(0.25)
        assert plugin_params.text_to_normalized("5", resonance) == 1.0
        assert plugin_params.text_to_normalized("-5", resonance) == 0.0

    @pytest.mark.parametrize("text,units,minimum,maximum,parsed", [
        ("1.2 kHz", "Hz", 20.0, 20000.0, "1200 Hz"),
        ("3 kHz", "", 0.0, 1.0, "3000 Hz"),
        ("250 ms", "s", 0.0, 1.0, "0.25 s"),
        ("50%", "", 0.0, 1.0, "0.5"),
        ("50%", "%", 0.0, 100.0, "50%"),
        ("10 ct", "st", -24.0, 24.0, "0.1 st"),
        ("1/16", "", 0.0, 15.0, "1/16"),
        ("lowpass", "", 0.0, 1.0, "lowpass"),
    ])
    def test_normalize_value_text_rewrites_units(self, text, units, minimum, maximum, parsed):
        assert vst3_probe.normalize_value_text(text, units, minimum, maximum) == parsed

    @pytest.mark.parametrize("text,units,expected", [
        ("800", "Hz", "800 Hz"),
        ("800 Hz", "Hz", "800 Hz"),
        ("1.2", "kHz", "1.2 kHz"),
        ("", "Hz", ""),
        ("0.5", "", "0.5"),
    ])
    def test_display_appends_units_once(self, text, units, expected):
        assert vst3_probe.format_display(text, units) == expected


# ---- capability: save_plugin_preset_file ---------------------------------------------------

class TestSavePluginPreset:
    def test_applies_text_and_numeric_overrides(self, fake_probe, tmp_path):
        out = tmp_path / "patch.vstpreset"
        result = plugin_params.save_plugin_preset_file(
            "Synthetic", str(out),
            params={"Filter Cutoff": "800 Hz", "#2": 0.65, "Amp Envelope/Attack": "250 ms"},
            probe=fake_probe,
        )
        assert result["parameters_applied"] == 3
        assert result["bytes"] == out.stat().st_size
        assert result["verified"] is True
        save_arguments = next(call for call in fake_probe.calls if call[0] == "--save-preset")
        assert "--set" in save_arguments and "2=0.65" in save_arguments
        assert "--set-text" in save_arguments and "1=800 Hz" in save_arguments
        # "250 ms" is not the plugin's format; the local parser supplies 250 ms
        # as a plain value, which for Attack normalizes to 0.05.
        assert "3=0.05" in save_arguments  # --set-fallback
        preset = vst3_probe.read_vstpreset(out)
        assert preset["class_id"] == SYNTHETIC_CID
        assert preset["chunks"]["Comp"] == b"synthetic-state"

    def test_numeric_values_must_be_normalized(self, fake_probe, tmp_path):
        with pytest.raises(plugin_params.PluginParameterError, match="normalized 0..1"):
            plugin_params.save_plugin_preset_file(
                "Synthetic", str(tmp_path / "x.vstpreset"),
                params={"Resonance": 2.5}, probe=fake_probe)

    def test_unknown_parameter_is_rejected(self, fake_probe, tmp_path):
        with pytest.raises(plugin_params.PluginParameterError, match="no parameter matches"):
            plugin_params.save_plugin_preset_file(
                "Synthetic", str(tmp_path / "x.vstpreset"),
                params={"Nope": 0.5}, probe=fake_probe)

    def test_ambiguous_parameter_is_rejected(self, fake_probe, tmp_path):
        with pytest.raises(plugin_params.PluginParameterError, match="matches 2 parameters"):
            plugin_params.save_plugin_preset_file(
                "Synthetic", str(tmp_path / "x.vstpreset"),
                params={"Filter": 0.5}, probe=fake_probe)

    def test_extension_and_parent_directories_are_created(self, fake_probe, tmp_path):
        out = tmp_path / "nested" / "dir" / "patch"
        result = plugin_params.save_plugin_preset_file(
            "Synthetic", str(out), params={"Resonance": 0.4}, probe=fake_probe)
        assert result["path"] == str(out.with_suffix(".vstpreset"))
        assert out.with_suffix(".vstpreset").is_file()

    def test_base_preset_and_state_are_forwarded(self, fake_probe, tmp_path):
        base = tmp_path / "base.vstpreset"
        base.write_bytes(vst3_probe.build_vstpreset(SYNTHETIC_CID, {"Comp": b"base"}))
        state = tmp_path / "state.bin"
        state.write_bytes(b"raw-state")
        plugin_params.save_plugin_preset_file(
            "Synthetic", str(tmp_path / "out.vstpreset"),
            base_preset=str(base), base_state="cmF3LXN0YXRl", params={"Resonance": 0.3},
            probe=fake_probe)
        for call in fake_probe.calls[:2]:
            assert call[call.index("--preset"):call.index("--preset") + 2] == [
                "--preset", str(base)]
        assert fake_probe.state_files[:2] == [b"raw-state", b"raw-state"]

    def test_invalid_base64_state_is_rejected(self, fake_probe, tmp_path):
        with pytest.raises(plugin_params.PluginParameterError, match="base64"):
            plugin_params.save_plugin_preset_file(
                "Synthetic", str(tmp_path / "x.vstpreset"),
                base_state="not base64!", params={"Resonance": 0.3}, probe=fake_probe)

    def test_verification_failure_is_reported(self, monkeypatch, tmp_path):
        monkeypatch.setattr(plugin_params, "resolve_plugin_bundle", lambda _plugin: BUNDLE)
        probe = FakeProbe(text_values={}, verification={1: 0.1})
        result = plugin_params.save_plugin_preset_file(
            "Synthetic", str(tmp_path / "out.vstpreset"),
            params={"Filter Cutoff": 0.6}, probe=probe)
        assert result["verified"] is False
        assert result["unverified_parameters"] == [1]
        assert any("does not reload" in warning for warning in result["warnings"])

    def test_fallback_conversion_is_flagged(self, fake_probe, tmp_path):
        result = plugin_params.save_plugin_preset_file(
            "Synthetic", str(tmp_path / "out.vstpreset"),
            params={"Amp Envelope/Attack": "250 ms"}, probe=fake_probe)
        assert result["verified"] is True
        assert any("local text parser" in warning for warning in result["warnings"])

    def test_probe_error_is_raised(self, monkeypatch, tmp_path):
        monkeypatch.setattr(plugin_params, "resolve_plugin_bundle", lambda _plugin: BUNDLE)
        probe = lambda arguments: (
            {"error": "save failed"} if "--save-preset" in arguments
            else {"module": str(BUNDLE), "parameters": synthetic_parameters(), "count": 4})
        with pytest.raises(plugin_params.PluginParameterError, match="save failed"):
            plugin_params.save_plugin_preset_file(
                "Synthetic", str(tmp_path / "out.vstpreset"),
                params={"Resonance": 0.5}, probe=probe)


# ---- bundle resolution ---------------------------------------------------------------------

class TestResolvePluginBundle:
    def test_direct_path_wins(self, tmp_path):
        bundle = tmp_path / "Direct.vst3"
        bundle.mkdir()
        assert plugin_params.resolve_plugin_bundle(str(bundle)) == bundle.resolve()

    def test_exact_name_in_configured_root(self, tmp_path, monkeypatch):
        (tmp_path / "Surge XT.vst3").mkdir()
        (tmp_path / "Surge XT Effects.vst3").mkdir()
        monkeypatch.setattr(plugin_params.vst3, "effective_search_paths",
                            lambda: [str(tmp_path)])
        assert plugin_params.resolve_plugin_bundle("Surge XT") == (
            tmp_path / "Surge XT.vst3").resolve()

    def test_ambiguous_partial_name_is_rejected(self, tmp_path, monkeypatch):
        (tmp_path / "A Synth.vst3").mkdir()
        (tmp_path / "B Synth.vst3").mkdir()
        monkeypatch.setattr(plugin_params.vst3, "effective_search_paths",
                            lambda: [str(tmp_path)])
        with pytest.raises(plugin_params.PluginParameterError, match="several VST3 bundles"):
            plugin_params.resolve_plugin_bundle("Synth")

    def test_cached_descriptor_name_is_used(self, tmp_path, monkeypatch):
        bundle = tmp_path / "Product.vst3"
        bundle.mkdir()
        monkeypatch.setattr(plugin_params.vst3, "effective_search_paths", lambda: [])
        monkeypatch.setattr(plugin_params.plugin_permissions, "cached_probe_modules",
                            lambda _query: [str(bundle)])
        assert plugin_params.resolve_plugin_bundle("Displayed Name") == bundle.resolve()

    def test_missing_plugin_is_reported(self, monkeypatch):
        monkeypatch.setattr(plugin_params.vst3, "effective_search_paths", lambda: [])
        monkeypatch.setattr(plugin_params.plugin_permissions, "cached_probe_modules",
                            lambda _query: [])
        with pytest.raises(plugin_params.PluginParameterError, match="no VST3 bundle"):
            plugin_params.resolve_plugin_bundle("Does Not Exist")


# ---- probe primitives ----------------------------------------------------------------------

class TestProbePrimitives:
    def test_preset_container_round_trip(self, tmp_path):
        out = tmp_path / "round.vstpreset"
        vst3_probe._atomic_write(
            out, vst3_probe.build_vstpreset(SYNTHETIC_CID,
                                            {"Comp": b"component", "Cont": b"controller"}))
        parsed = vst3_probe.read_vstpreset(out)
        assert parsed["class_id"] == SYNTHETIC_CID
        assert parsed["chunks"] == {"Comp": b"component", "Cont": b"controller"}

    def test_preset_header_matches_the_sdk_layout(self, tmp_path):
        data = vst3_probe.build_vstpreset(SYNTHETIC_CID, {"Comp": b"abc"})
        assert data[:4] == b"VST3"
        assert struct.unpack_from("<i", data, 4)[0] == 1
        assert data[8:40].decode("ascii") == SYNTHETIC_CID
        list_offset = struct.unpack_from("<q", data, 40)[0]
        assert data[list_offset:list_offset + 4] == b"List"
        assert struct.unpack_from("<i", data, list_offset + 4)[0] == 1
        # One entry, little-endian offset/size, data directly after the header.
        chunk_id = data[list_offset + 8:list_offset + 12]
        offset, size = struct.unpack_from("<qq", data, list_offset + 12)
        assert chunk_id == b"Comp" and offset == 48 and size == 3
        assert data[offset:offset + size] == b"abc"

    def test_read_rejects_non_presets(self, tmp_path):
        bad = tmp_path / "bad.vstpreset"
        bad.write_bytes(b"NOPE" + b"\x00" * 60)
        with pytest.raises(ValueError, match="not a VST3 preset"):
            vst3_probe.read_vstpreset(bad)

    def test_read_rejects_truncated_chunk_tables(self, tmp_path):
        good = vst3_probe.build_vstpreset(SYNTHETIC_CID, {"Comp": b"abc"})
        bad = tmp_path / "truncated.vstpreset"
        bad.write_bytes(good[:-4])
        with pytest.raises(ValueError):
            vst3_probe.read_vstpreset(bad)

    def test_parameter_info_matches_the_sdk_struct(self):
        # On 64-bit hosts the SDK's Vst::ParameterInfo is 792 bytes with the
        # double at offset 776; ctypes must agree or the plugin writes past it.
        if struct.calcsize("P") != 8:
            pytest.skip("layout check is for 64-bit hosts")
        assert vst3_probe.ctypes.sizeof(vst3_probe._PParameterInfo) == 792
        assert vst3_probe.ctypes.sizeof(vst3_probe._PUnitInfo) == 268
        assert vst3_probe.ctypes.sizeof(vst3_probe._PProcessData) == 80

    def test_cid_bytes_round_trips_format(self):
        raw = bytes.fromhex(SYNTHETIC_CID)
        assert vst3_probe.cid_bytes(SYNTHETIC_CID) == raw
        assert vst3_probe.plat.format_cid(
            vst3_probe.cid_bytes(SYNTHETIC_CID)).upper() == SYNTHETIC_CID
        with pytest.raises(ValueError):
            vst3_probe.cid_bytes("nope")

    def test_text_buffers_are_utf16(self):
        buffer = vst3_probe._encode_text("Filter")
        assert vst3_probe._decode16(buffer) == "Filter"
        assert vst3_probe._decode16(vst3_probe._encode_text("テスト")) == "テスト"

    def test_controller_api_binds_edit_controller_slots(self):
        # A synthetic 18-entry vtable proves each binding reads the SDK's slot.
        calls = []
        callbacks = []
        entries = (vst3_probe.ctypes.c_void_p * 18)()
        for slot in range(18):
            callback = vst3_probe._FUNCTYPE(vst3_probe.ctypes.c_int32)(
                (lambda number: lambda *_args: calls.append(number) or 0)(slot))
            callbacks.append(callback)
            entries[slot] = vst3_probe.ctypes.cast(callback, vst3_probe.ctypes.c_void_p)
        obj = vst3_probe._PInterface(
            vst3_probe.ctypes.cast(entries, vst3_probe.ctypes.c_void_p))
        address = vst3_probe.ctypes.addressof(obj)
        api = vst3_probe._controller_api(address)
        assert api["count"](address) == 0
        api["string_by_value"](address, 1, 0.5, None)
        api["set_normalized"](address, 1, 0.5)
        assert calls == [8, 10, 15]


# ---- probe CLI -----------------------------------------------------------------------------

class TestProbeCli:
    def test_params_mode_prints_the_inspection_json(self, monkeypatch, capsys):
        captured = {}

        def fake(bundle, **options):
            captured.update({"bundle": bundle, **options})
            return {"module": str(bundle), "count": 1, "parameters": [{"id": 1}],
                    "text_queries": [], "value_queries": []}

        monkeypatch.setattr(vst3_probe, "inspect_bundle_parameters", fake)
        assert vst3_probe.main([
            "--params", "/x.vst3", "--preset", "/p.vstpreset",
            "--text", "1=800 Hz", "--value", "2=0.5",
        ]) == 0
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["count"] == 1
        assert captured["preset"] == "/p.vstpreset"
        assert captured["text_queries"] == [(1, "800 Hz")]
        assert captured["value_queries"] == [(2, 0.5)]

    def test_save_mode_forwards_overrides(self, monkeypatch, capsys):
        captured = {}

        def fake(bundle, out, **options):
            captured.update({"bundle": bundle, "out": out, **options})
            return {"path": out, "parameters_applied": 2}

        monkeypatch.setattr(vst3_probe, "save_bundle_preset", fake)
        assert vst3_probe.main([
            "--save-preset", "/x.vst3", "--out", "/tmp/x.vstpreset",
            "--set", "2=0.5", "--set-text", "1=800 Hz",
            "--set-fallback", "1=0.534",
        ]) == 0
        assert captured["overrides"] == [(2, 0.5)]
        assert captured["text_overrides"] == [(1, "800 Hz", 0.534)]

    def test_save_mode_needs_an_output_path(self, capsys):
        assert vst3_probe.main(["--save-preset", "/x.vst3"]) == 2
        assert "needs --out" in capsys.readouterr().out

    def test_unknown_option_reports_an_error(self, capsys):
        assert vst3_probe.main(["--bogus"]) == 2
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert "unknown option" in payload["error"]

    def test_legacy_class_mode_still_probes_bundles(self, monkeypatch, capsys):
        monkeypatch.setattr(
            vst3_probe, "probe_bundle",
            lambda bundle: {"module": str(bundle), "classes": []})
        assert vst3_probe.main(["/a.vst3", "/b.vst3"]) == 0
        lines = capsys.readouterr().out.strip().splitlines()
        assert [json.loads(line)["module"] for line in lines] == ["/a.vst3", "/b.vst3"]
