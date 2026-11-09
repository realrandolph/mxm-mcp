"""Tests for Wavelength-designed MCP tools in MXM-MCP (Issue #20)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mxm_mcp import quarantine, server


def _run(awaitable):
    return asyncio.run(awaitable)


def test_list_instruments_builtins_and_quarantine(monkeypatch, tmp_path):
    monkeypatch.setattr(quarantine, "quarantine_file_path", lambda: tmp_path / "quarantine.json")
    res = json.loads(_run(server.list_instruments(search="triple")))
    assert res["count"] >= 1 and "builtin:tripleoscillator" in [i["name"] for i in res["instruments"]]

    effects = json.loads(_run(server.list_instruments(kind="effect")))
    assert all(e["kind"] == "effect" for e in effects["instruments"])

    quarantine.quarantine_plugin("tripleoscillator", "test crash")
    after = json.loads(_run(server.list_instruments()))
    assert "builtin:tripleoscillator" not in [i["name"] for i in after["instruments"]]
    assert any("tripleoscillator" in q["name"] for q in after["quarantined_plugins"])


def test_list_presets_tool_calls_resolver(monkeypatch):
    monkeypatch.setattr(server, "list_plugin_presets", lambda **kw: asyncio.sleep(0, json.dumps({
        "plugin": kw.get("plugin"), "count": 2, "total": 2, "presets": [{"name": "Preset A"}, {"name": "Preset B"}],
    })))
    res = json.loads(_run(server.list_presets(plugin="Dexed", search="Preset")))
    assert res["count"] == 2 and res["presets"][0]["name"] == "Preset A"


def test_plugin_params_tool(monkeypatch):
    monkeypatch.setattr(server.plugin_params_mod, "get_plugin_parameters", lambda **kw: {
        "plugin": kw.get("plugin"), "count": 1, "parameters": [{"id": 10, "name": "Cutoff", "min": 20.0, "max": 20000.0, "display": "1.2 kHz", "units": "Hz"}],
    })
    res = json.loads(_run(server.plugin_params(plugin="Surge XT", search="Cutoff")))
    assert res["count"] == 1 and res["parameters"][0]["display"] == "1.2 kHz"


def test_save_preset_tool(monkeypatch, tmp_path):
    dest = tmp_path / "presets/lead.vstpreset"

    def mock_save(plugin, out_path, **kw):
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"VST3 PRESET DATA")
        return {"path": str(p), "bytes": len(b"VST3 PRESET DATA"), "plugin": plugin, "parameters_applied": 2}

    monkeypatch.setattr(server.plugin_params_mod, "save_plugin_preset_file", mock_save)
    res = json.loads(_run(server.save_preset(plugin="Surge XT", out=str(dest), params={"Cutoff": "1.2 kHz"})))
    assert res["parameters_applied"] == 2 and dest.is_file()


def test_patch_card_tool(monkeypatch):
    monkeypatch.setattr(server.audition_mod, "audition_plugin", lambda plugin, **kw: {
        "plugin": plugin, "preset": kw.get("preset", ""), "attack_time_ms": 12.5, "tail_duration_ms": 1200.0,
    })
    res = json.loads(_run(server.patch_card(patches=[{"plugin": "Surge XT", "preset": "Warm Pad"}, {"plugin": "Dexed"}])) )
    assert res["count"] == 2 and res["patches"][0]["attack_time_ms"] == 12.5


def test_wavelength_tools_mcp_dispatch():
    tools = _run(server.mcp.list_tools())
    names = {t.name for t in tools}
    assert {"list_instruments", "list_presets", "plugin_params", "save_preset", "patch_card"}.issubset(names)
