"""Tests for VST3 parameter introspection and natural-text preset export (Issue #20)."""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path

import pytest

from mxm_mcp import plugin_params, vst3_probe

SYNTHETIC_CID = "A1B2C3D4E5F60718293A4B4C00000000"
BUNDLE = Path("/fixtures/Synthetic.vst3")


def _p(id, name, short, mod, units, mn, mx, cur, norm, step=0, stepped=False, is_list=False, unit_id=0, disp=""):
    return {
        "id": id, "name": name, "short_name": short, "module": mod, "units": units,
        "min": mn, "max": mx, "default": cur, "current": cur, "normalized": norm,
        "default_normalized": norm, "step_count": step, "stepped": stepped, "readonly": False,
        "hidden": False, "can_automate": True, "is_list": is_list, "is_program_change": False,
        "is_bypass": False, "unit_id": unit_id, "display": disp or f"{cur} {units}".strip(),
    }


def synthetic_parameters() -> list[dict]:
    return [
        _p(1, "Filter Cutoff", "Cutoff", "Filter", "Hz", 20.0, 20000.0, 800.0, 0.534, unit_id=1, disp="800 Hz"),
        _p(2, "Resonance", "Res", "Filter", "", 0.0, 1.0, 0.2, 0.2, unit_id=1, disp="0.200"),
        _p(3, "Attack", "A", "Amp Envelope", "ms", 0.0, 5000.0, 10.0, 0.002, unit_id=2, disp="10 ms"),
        _p(4, "Mode", "Mode", "", "", 0.0, 3.0, 0.0, 0.0, step=3, stepped=True, is_list=True, disp="Lowpass"),
        _p(5, "Output Gain", "Gain", "Output", "dB", -24.0, 24.0, 0.0, 0.5, unit_id=3, disp="0.0 dB"),
    ]


class FakeProbe:
    def __init__(self, parameters=None, text_values=None, verification=None):
        self.parameters = [dict(i) for i in (parameters or synthetic_parameters())]
        self.text_values, self.verification = text_values or {}, verification
        self.calls, self.state_files, self.saved = [], [], {}

    def _params(self, preset=None):
        params = [dict(i) for i in self.parameters]
        vals = self.verification if (preset and self.verification is not None) else self.saved
        for i in params:
            if int(i["id"]) in vals:
                i["normalized"] = i["current"] = vals[int(i["id"])]
        return params

    def __call__(self, arguments):
        args = list(arguments)
        self.calls.append(args)
        if "--state-file" in args:
            self.state_files.append(Path(args[args.index("--state-file") + 1]).read_bytes())
        if not args:
            return {"error": "no arguments"}
        if args[0] == "--params":
            p = args[args.index("--preset") + 1] if "--preset" in args else None
            return {"module": args[1], "class": {"cid": SYNTHETIC_CID, "name": "Synthetic Synth"},
                    "parameters": self._params(p), "count": len(self.parameters), "warnings": []}
        if args[0] == "--save-preset":
            out = Path(args[args.index("--out") + 1])
            applied, res = {}, []
            for idx, a in enumerate(args):
                if a == "--set":
                    k, v = args[idx + 1].partition("=")[::2]
                    applied[int(k)] = float(v)
            texts = [args[i + 1] for i, a in enumerate(args) if a == "--set-text"]
            fbs = {int(args[i + 1].partition("=")[0]): float(args[i + 1].partition("=")[2]) for i, a in enumerate(args) if a == "--set-fallback"}
            for entry in texts:
                k, txt = entry.partition("=")[::2]
                pid = int(k)
                acc = self.text_values.get(pid, {}).get(txt)
                if acc is not None:
                    applied[pid] = float(acc)
                    res.append({"id": pid, "text": txt, "source": "text"})
                elif pid in fbs:
                    applied[pid] = fbs[pid]
                    res.append({"id": pid, "text": txt, "source": "fallback"})
            self.saved = dict(applied)
            vst3_probe._atomic_write(out, vst3_probe.build_vstpreset(SYNTHETIC_CID, {"Comp": b"synthetic-state"}))
            return {
                "module": args[1], "path": str(out), "bytes": out.stat().st_size,
                "class": {"cid": SYNTHETIC_CID, "name": "Synthetic Synth"},
                "parameters_applied": len(applied),
                "applied": [{"id": k, "normalized": v} for k, v in sorted(applied.items())],
                "parameter_results": res, "warnings": [],
            }
        return {"error": f"unexpected probe mode {args[0]!r}"}


@pytest.fixture
def fake_probe(monkeypatch):
    probe = FakeProbe(text_values={1: {"800 Hz": 0.534, "1200 Hz": 0.61}})
    monkeypatch.setattr(plugin_params, "resolve_plugin_bundle", lambda _plugin: BUNDLE)
    return probe


class TestGetPluginParameters:
    def test_enumerates_core_fields(self, fake_probe):
        result = plugin_params.get_plugin_parameters("Synthetic", probe=fake_probe)
        assert result["plugin"] == "Synthetic" and result["count"] == 5
        cutoff = result["parameters"][0]
        assert cutoff["id"] == 1 and cutoff["name"] == "Filter Cutoff"
        assert cutoff["min"] == 20.0 and cutoff["max"] == 20000.0
        assert cutoff["display"] == "800 Hz" and "can_automate" not in cutoff

    def test_details_keeps_flags_and_short_names(self, fake_probe):
        result = plugin_params.get_plugin_parameters("Synthetic", details=True, probe=fake_probe)
        mode = result["parameters"][3]
        assert mode["short_name"] == "Mode" and mode["stepped"] is True
        assert mode["is_list"] is True and mode["can_automate"] is True

    @pytest.mark.parametrize("query,expected", [
        ("cutoff", [1]), ("Filter", [1, 2]), ("Filter/Cutoff", [1]),
        ("#2", [2]), ("amp envelope", [3]), ("filter cutoff", [1]), ("nothing-here", []),
    ])
    def test_search_matches_name_id_and_module(self, fake_probe, query, expected):
        result = plugin_params.get_plugin_parameters("Synthetic", search=query, probe=fake_probe)
        assert [i["id"] for i in result["parameters"]] == expected
        assert result["count"] == len(expected)

    def test_search_requires_every_token(self, fake_probe):
        result = plugin_params.get_plugin_parameters("Synthetic", search="filter resonance", probe=fake_probe)
        assert [i["id"] for i in result["parameters"]] == [2]

    def test_preset_is_loaded_before_inspection(self, fake_probe, tmp_path):
        preset = tmp_path / "patch.vstpreset"
        preset.write_bytes(vst3_probe.build_vstpreset(SYNTHETIC_CID, {"Comp": b"x"}))
        result = plugin_params.get_plugin_parameters("Synthetic", preset=str(preset), probe=fake_probe)
        assert ["--preset", str(preset)] == fake_probe.calls[0][2:4]
        assert result["preset"] == str(preset)

    def test_missing_preset_is_reported(self, fake_probe, tmp_path):
        with pytest.raises(plugin_params.PluginParameterError, match="preset not found"):
            plugin_params.get_plugin_parameters("Synthetic", preset=str(tmp_path / "nope.vstpreset"), probe=fake_probe)

    def test_probe_error_is_raised(self, monkeypatch):
        monkeypatch.setattr(plugin_params, "resolve_plugin_bundle", lambda _p: BUNDLE)
        with pytest.raises(plugin_params.PluginParameterError, match="boom"):
            plugin_params.get_plugin_parameters("Synthetic", probe=lambda _arguments: {"error": "boom"})


class TestNaturalText:
    @pytest.mark.parametrize("text,plain", [
        ("800 Hz", 800.0), ("1.2 kHz", 1200.0), ("-6 dB", -6.0),
        ("50%", 0.5), ("250 ms", 250.0), ("0.65", 0.65),
    ])
    def test_parse_natural_value(self, text, plain):
        cutoff, resonance, attack, _, gain = synthetic_parameters()
        param = attack if text.endswith("ms") else (gain if text.endswith("dB") else (resonance if "%" in text or text == "0.65" else cutoff))
        assert plugin_params.parse_natural_value(text, param) == pytest.approx(plain)

    def test_ratio_text_is_left_to_the_plugin(self):
        mode = synthetic_parameters()[3]
        assert plugin_params.parse_natural_value("1/16", mode) is None
        assert plugin_params.text_to_normalized("1/16", mode) is None

    def test_frequency_conversion_is_logarithmic(self):
        cutoff = synthetic_parameters()[0]
        expected = math.log(800 / 20) / math.log(20000 / 20)
        assert plugin_params.text_to_normalized("800 Hz", cutoff) == pytest.approx(expected)

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
        ("800", "Hz", "800 Hz"), ("800 Hz", "Hz", "800 Hz"),
        ("1.2", "kHz", "1.2 kHz"), ("", "Hz", ""), ("0.5", "", "0.5"),
    ])
    def test_display_appends_units_once(self, text, units, expected):
        assert vst3_probe.format_display(text, units) == expected


class TestSavePluginPreset:
    def test_applies_text_and_numeric_overrides(self, fake_probe, tmp_path):
        out = tmp_path / "patch.vstpreset"
        result = plugin_params.save_plugin_preset_file(
            "Synthetic", str(out),
            params={"Filter Cutoff": "800 Hz", "#2": 0.65, "Amp Envelope/Attack": "250 ms"},
            probe=fake_probe,
        )
        assert result["parameters_applied"] == 3 and result["verified"] is True
        args = next(c for c in fake_probe.calls if c[0] == "--save-preset")
        assert "--set" in args and "2=0.65" in args
        assert "--set-text" in args and "1=800 Hz" in args
        assert "3=0.05" in args
        preset = vst3_probe.read_vstpreset(out)
        assert preset["class_id"] == SYNTHETIC_CID
        assert preset["chunks"]["Comp"] == b"synthetic-state"

    def test_numeric_values_must_be_normalized(self, fake_probe, tmp_path):
        with pytest.raises(plugin_params.PluginParameterError, match="normalized 0..1"):
            plugin_params.save_plugin_preset_file("Synthetic", str(tmp_path / "x.vstpreset"), params={"Resonance": 2.5}, probe=fake_probe)

    def test_unknown_parameter_is_rejected(self, fake_probe, tmp_path):
        with pytest.raises(plugin_params.PluginParameterError, match="no parameter matches"):
            plugin_params.save_plugin_preset_file("Synthetic", str(tmp_path / "x.vstpreset"), params={"Nope": 0.5}, probe=fake_probe)

    def test_ambiguous_parameter_is_rejected(self, fake_probe, tmp_path):
        with pytest.raises(plugin_params.PluginParameterError, match="matches 2 parameters"):
            plugin_params.save_plugin_preset_file("Synthetic", str(tmp_path / "x.vstpreset"), params={"Filter": 0.5}, probe=fake_probe)

    def test_extension_and_parent_directories_are_created(self, fake_probe, tmp_path):
        out = tmp_path / "nested" / "dir" / "patch"
        result = plugin_params.save_plugin_preset_file("Synthetic", str(out), params={"Resonance": 0.4}, probe=fake_probe)
        assert result["path"] == str(out.with_suffix(".vstpreset"))
        assert out.with_suffix(".vstpreset").is_file()

    def test_base_preset_and_state_are_forwarded(self, fake_probe, tmp_path):
        base, state = tmp_path / "base.vstpreset", tmp_path / "state.bin"
        base.write_bytes(vst3_probe.build_vstpreset(SYNTHETIC_CID, {"Comp": b"base"}))
        state.write_bytes(b"raw-state")
        plugin_params.save_plugin_preset_file(
            "Synthetic", str(tmp_path / "out.vstpreset"),
            base_preset=str(base), base_state="cmF3LXN0YXRl", params={"Resonance": 0.3}, probe=fake_probe)
        for call in fake_probe.calls[:2]:
            assert call[call.index("--preset"):call.index("--preset") + 2] == ["--preset", str(base)]
        assert fake_probe.state_files[:2] == [b"raw-state", b"raw-state"]

    def test_invalid_base64_state_is_rejected(self, fake_probe, tmp_path):
        with pytest.raises(plugin_params.PluginParameterError, match="base64"):
            plugin_params.save_plugin_preset_file("Synthetic", str(tmp_path / "x.vstpreset"), base_state="not base64!", params={"Resonance": 0.3}, probe=fake_probe)

    def test_verification_failure_is_reported(self, monkeypatch, tmp_path):
        monkeypatch.setattr(plugin_params, "resolve_plugin_bundle", lambda _plugin: BUNDLE)
        probe = FakeProbe(text_values={}, verification={1: 0.1})
        result = plugin_params.save_plugin_preset_file("Synthetic", str(tmp_path / "out.vstpreset"), params={"Filter Cutoff": 0.6}, probe=probe)
        assert result["verified"] is False and result["unverified_parameters"] == [1]

    def test_fallback_conversion_is_flagged(self, fake_probe, tmp_path):
        result = plugin_params.save_plugin_preset_file("Synthetic", str(tmp_path / "out.vstpreset"), params={"Amp Envelope/Attack": "250 ms"}, probe=fake_probe)
        assert result["verified"] is True
        assert any("local text parser" in w for w in result["warnings"])

    def test_probe_error_is_raised(self, monkeypatch, tmp_path):
        monkeypatch.setattr(plugin_params, "resolve_plugin_bundle", lambda _plugin: BUNDLE)
        probe = lambda args: ({"error": "save failed"} if "--save-preset" in args else {"module": str(BUNDLE), "parameters": synthetic_parameters(), "count": 4})
        with pytest.raises(plugin_params.PluginParameterError, match="save failed"):
            plugin_params.save_plugin_preset_file("Synthetic", str(tmp_path / "out.vstpreset"), params={"Resonance": 0.5}, probe=probe)


class TestResolvePluginBundle:
    def test_direct_path_wins(self, tmp_path):
        bundle = tmp_path / "Direct.vst3"; bundle.mkdir()
        assert plugin_params.resolve_plugin_bundle(str(bundle)) == bundle.resolve()

    def test_exact_name_in_configured_root(self, tmp_path, monkeypatch):
        (tmp_path / "Surge XT.vst3").mkdir(); (tmp_path / "Surge XT Effects.vst3").mkdir()
        monkeypatch.setattr(plugin_params.vst3, "effective_search_paths", lambda: [str(tmp_path)])
        assert plugin_params.resolve_plugin_bundle("Surge XT") == (tmp_path / "Surge XT.vst3").resolve()

    def test_ambiguous_partial_name_is_rejected(self, tmp_path, monkeypatch):
        (tmp_path / "A Synth.vst3").mkdir(); (tmp_path / "B Synth.vst3").mkdir()
        monkeypatch.setattr(plugin_params.vst3, "effective_search_paths", lambda: [str(tmp_path)])
        with pytest.raises(plugin_params.PluginParameterError, match="several VST3 bundles"):
            plugin_params.resolve_plugin_bundle("Synth")

    def test_cached_descriptor_name_is_used(self, tmp_path, monkeypatch):
        bundle = tmp_path / "Product.vst3"; bundle.mkdir()
        monkeypatch.setattr(plugin_params.vst3, "effective_search_paths", lambda: [])
        monkeypatch.setattr(plugin_params.plugin_permissions, "cached_probe_modules", lambda _query: [str(bundle)])
        assert plugin_params.resolve_plugin_bundle("Displayed Name") == bundle.resolve()

    def test_missing_plugin_is_reported(self, monkeypatch):
        monkeypatch.setattr(plugin_params.vst3, "effective_search_paths", lambda: [])
        monkeypatch.setattr(plugin_params.plugin_permissions, "cached_probe_modules", lambda _query: [])
        with pytest.raises(plugin_params.PluginParameterError, match="no VST3 bundle"):
            plugin_params.resolve_plugin_bundle("Does Not Exist")


class TestProbePrimitives:
    def test_preset_container_round_trip(self, tmp_path):
        out = tmp_path / "round.vstpreset"
        vst3_probe._atomic_write(out, vst3_probe.build_vstpreset(SYNTHETIC_CID, {"Comp": b"component", "Cont": b"controller"}))
        parsed = vst3_probe.read_vstpreset(out)
        assert parsed["class_id"] == SYNTHETIC_CID and parsed["chunks"] == {"Comp": b"component", "Cont": b"controller"}

    def test_preset_header_matches_the_sdk_layout(self, tmp_path):
        data = vst3_probe.build_vstpreset(SYNTHETIC_CID, {"Comp": b"abc"})
        assert data[:4] == b"VST3" and struct.unpack_from("<i", data, 4)[0] == 1 and data[8:40].decode("ascii") == SYNTHETIC_CID
        list_off = struct.unpack_from("<q", data, 40)[0]
        assert data[list_off:list_off + 4] == b"List" and struct.unpack_from("<i", data, list_off + 4)[0] == 1
        chunk_id = data[list_off + 8:list_off + 12]
        offset, size = struct.unpack_from("<qq", data, list_off + 12)
        assert chunk_id == b"Comp" and offset == 48 and size == 3 and data[offset:offset + size] == b"abc"

    def test_read_rejects_non_presets(self, tmp_path):
        bad = tmp_path / "bad.vstpreset"; bad.write_bytes(b"NOPE" + b"\x00" * 60)
        with pytest.raises(ValueError, match="not a VST3 preset"): vst3_probe.read_vstpreset(bad)

    def test_read_rejects_truncated_chunk_tables(self, tmp_path):
        bad = tmp_path / "truncated.vstpreset"; bad.write_bytes(vst3_probe.build_vstpreset(SYNTHETIC_CID, {"Comp": b"abc"})[:-4])
        with pytest.raises(ValueError): vst3_probe.read_vstpreset(bad)

    def test_parameter_info_matches_the_sdk_struct(self):
        if struct.calcsize("P") != 8: pytest.skip("layout check is for 64-bit hosts")
        assert vst3_probe.ctypes.sizeof(vst3_probe._PParameterInfo) == 792
        assert vst3_probe.ctypes.sizeof(vst3_probe._PUnitInfo) == 268
        assert vst3_probe.ctypes.sizeof(vst3_probe._PProcessData) == 80

    def test_cid_bytes_round_trips_format(self):
        assert vst3_probe.cid_bytes(SYNTHETIC_CID) == bytes.fromhex(SYNTHETIC_CID)
        assert vst3_probe.plat.format_cid(vst3_probe.cid_bytes(SYNTHETIC_CID)).upper() == SYNTHETIC_CID
        with pytest.raises(ValueError): vst3_probe.cid_bytes("nope")

    def test_text_buffers_are_utf16(self):
        assert vst3_probe._decode16(vst3_probe._encode_text("Filter")) == "Filter"
        assert vst3_probe._decode16(vst3_probe._encode_text("テスト")) == "テスト"

    def test_controller_api_binds_edit_controller_slots(self):
        calls, callbacks, entries = [], [], (vst3_probe.ctypes.c_void_p * 18)()
        for slot in range(18):
            cb = vst3_probe._FUNCTYPE(vst3_probe.ctypes.c_int32)((lambda n: lambda *_args: calls.append(n) or 0)(slot))
            callbacks.append(cb); entries[slot] = vst3_probe.ctypes.cast(cb, vst3_probe.ctypes.c_void_p)
        obj = vst3_probe._PInterface(vst3_probe.ctypes.cast(entries, vst3_probe.ctypes.c_void_p))
        addr = vst3_probe.ctypes.addressof(obj)
        api = vst3_probe._controller_api(addr)
        assert api["count"](addr) == 0
        api["string_by_value"](addr, 1, 0.5, None); api["set_normalized"](addr, 1, 0.5)
        assert calls == [8, 10, 15]


class TestProbeConversions:
    def test_text_to_value_returns_the_plugin_conversion(self, monkeypatch):
        monkeypatch.setattr(vst3_probe, "inspect_bundle_parameters",
                            lambda bundle, **opts: {
                                "module": str(bundle), "class": {"cid": "AB"},
                                "text_queries": [{"id": 1, "text": "800 Hz", "normalized": 0.5, "plain": 800.0, "display": "800 Hz"}],
                                "value_queries": [],
                            })
        result = vst3_probe.text_to_value("/x.vst3", 1, "800 Hz")
        assert result["normalized"] == 0.5 and result["plain"] == 800.0 and result["class"] == {"cid": "AB"}

    def test_value_to_text_returns_the_display_string(self, monkeypatch):
        monkeypatch.setattr(vst3_probe, "inspect_bundle_parameters",
                            lambda bundle, **opts: {"module": str(bundle), "class": None, "text_queries": [],
                                                    "value_queries": [{"id": 1, "normalized": 0.5, "text": "800 Hz", "units": "Hz"}]})
        result = vst3_probe.value_to_text("/x.vst3", 1, 0.5)
        assert result["text"] == "800 Hz" and result["units"] == "Hz"

    def test_conversion_errors_are_passed_through(self, monkeypatch):
        monkeypatch.setattr(vst3_probe, "inspect_bundle_parameters", lambda bundle, **opts: {"module": str(bundle), "error": "boom"})
        assert vst3_probe.text_to_value("/x.vst3", 1, "800 Hz")["error"] == "boom"
        assert vst3_probe.value_to_text("/x.vst3", 1, 0.5)["error"] == "boom"


class TestProbeCli:
    def test_params_mode_prints_the_inspection_json(self, monkeypatch, capsys):
        monkeypatch.setattr(vst3_probe, "inspect_bundle_parameters",
                            lambda b, **opts: {"module": str(b), "count": 1, "parameters": [{"id": 1}], "text_queries": [], "value_queries": []})
        assert vst3_probe.main(["--params", "/x.vst3", "--preset", "/p.vstpreset", "--text", "1=800 Hz", "--value", "2=0.5"]) == 0
        assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["count"] == 1

    def test_save_mode_forwards_overrides(self, monkeypatch, capsys):
        captured = {}
        monkeypatch.setattr(vst3_probe, "save_bundle_preset", lambda b, out, **opts: captured.update({"bundle": b, "out": out, **opts}) or {"path": out, "parameters_applied": 2})
        assert vst3_probe.main(["--save-preset", "/x.vst3", "--out", "/tmp/x.vstpreset", "--set", "2=0.5", "--set-text", "1=800 Hz", "--set-fallback", "1=0.534"]) == 0
        assert captured["overrides"] == [(2, 0.5)] and captured["text_overrides"] == [(1, "800 Hz", 0.534)]

    def test_save_mode_needs_an_output_path(self, capsys):
        assert vst3_probe.main(["--save-preset", "/x.vst3"]) == 2
        assert "needs --out" in capsys.readouterr().out

    def test_unknown_option_reports_an_error(self, capsys):
        assert vst3_probe.main(["--bogus"]) == 2
        assert "unknown option" in json.loads(capsys.readouterr().out.strip().splitlines()[-1])["error"]

    def test_legacy_class_mode_still_probes_bundles(self, monkeypatch, capsys):
        monkeypatch.setattr(vst3_probe, "probe_bundle", lambda b: {"module": str(b), "classes": []})
        assert vst3_probe.main(["/a.vst3", "/b.vst3"]) == 0
        lines = capsys.readouterr().out.strip().splitlines()
        assert [json.loads(line)["module"] for line in lines] == ["/a.vst3", "/b.vst3"]
