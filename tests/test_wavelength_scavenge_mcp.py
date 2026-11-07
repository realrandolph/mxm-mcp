"""Tests for Wavelength-designed MCP tools in MXM-MCP (Issue #20)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from mxm_mcp import audition, plugin_params, quarantine, server


def _run_tool(awaitable):
    return asyncio.run(awaitable)


def test_list_instruments_builtins_and_quarantine(monkeypatch, tmp_path):
    # Set isolated quarantine store
    monkeypatch.setattr(quarantine, "quarantine_file_path", lambda: tmp_path / "quarantine.json")

    # 1. Default list includes builtins
    raw = _run_tool(server.list_instruments(search="triple"))
    result = json.loads(raw)
    assert result["count"] >= 1
    names = [inst["name"] for inst in result["instruments"]]
    assert "builtin:tripleoscillator" in names

    # 2. Kind filtering
    effects_raw = _run_tool(server.list_instruments(kind="effect"))
    effects_result = json.loads(effects_raw)
    assert all(e["kind"] == "effect" for e in effects_result["instruments"])

    # 3. Quarantine filtering
    quarantine.quarantine_plugin("tripleoscillator", "test crash")
    after_raw = _run_tool(server.list_instruments())
    after_result = json.loads(after_raw)
    after_names = [inst["name"] for inst in after_result["instruments"]]
    assert "builtin:tripleoscillator" not in after_names
    quarantined_names = [q["name"] for q in after_result["quarantined_plugins"]]
    assert "tripleoscillator" in quarantined_names or "builtin:tripleoscillator" in quarantined_names


def test_list_presets_tool_calls_resolver(monkeypatch):
    async def mock_list_plugin_presets(plugin, query="", offset=0, limit=200, refresh=False, ctx=None):
        return json.dumps({
            "plugin": plugin,
            "count": 2,
            "total": 2,
            "presets": [{"name": "Preset A"}, {"name": "Preset B"}],
        })

    monkeypatch.setattr(server, "list_plugin_presets", mock_list_plugin_presets)

    raw = _run_tool(server.list_presets(plugin="Dexed", search="Preset"))
    result = json.loads(raw)
    assert result["count"] == 2
    assert result["presets"][0]["name"] == "Preset A"


def test_plugin_params_tool(monkeypatch):
    def mock_get_params(plugin, preset="", search="", details=False):
        return {
            "plugin": plugin,
            "count": 1,
            "parameters": [
                {"id": 10, "name": "Cutoff", "min": 20.0, "max": 20000.0, "display": "1.2 kHz", "units": "Hz"}
            ],
        }

    monkeypatch.setattr(server.plugin_params_mod, "get_plugin_parameters", mock_get_params)

    raw = _run_tool(server.plugin_params(plugin="Surge XT", search="Cutoff"))
    result = json.loads(raw)
    assert result["count"] == 1
    assert result["parameters"][0]["name"] == "Cutoff"
    assert result["parameters"][0]["display"] == "1.2 kHz"


def test_save_preset_tool(monkeypatch, tmp_path):
    dest = tmp_path / "presets" / "lead.vstpreset"

    def mock_save_file(plugin, out_path, base_preset="", base_state="", params=None):
        dest_path = Path(out_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(b"VST3 PRESET DATA")
        return {
            "path": str(dest_path),
            "bytes": len(b"VST3 PRESET DATA"),
            "plugin": plugin,
            "parameters_applied": 2,
        }

    monkeypatch.setattr(server.plugin_params_mod, "save_plugin_preset_file", mock_save_file)

    raw = _run_tool(server.save_preset(
        plugin="Surge XT",
        out=str(dest),
        params={"Cutoff": "1.2 kHz", "Resonance": 0.5},
    ))
    result = json.loads(raw)
    assert result["bytes"] == len(b"VST3 PRESET DATA")
    assert result["parameters_applied"] == 2
    assert dest.is_file()


def test_patch_card_tool(monkeypatch):
    def mock_audition(plugin, preset="", state="", output_dir=None, keep_audio=False):
        return {
            "plugin": plugin,
            "preset": preset,
            "attack_time_ms": 12.5,
            "tail_duration_ms": 1200.0,
            "pitch_drift_semitones": 0.01,
            "band_balance": {"sub_db": -18.0, "brightness_db": -12.0},
        }

    monkeypatch.setattr(server.audition_mod, "audition_plugin", mock_audition)

    raw = _run_tool(server.patch_card(patches=[
        {"plugin": "Surge XT", "preset": "Warm Pad"},
        {"plugin": "Dexed", "preset": "E.PIANO 1"},
    ]))
    result = json.loads(raw)
    assert result["count"] == 2
    assert result["patches"][0]["attack_time_ms"] == 12.5
    assert result["patches"][1]["plugin"] == "Dexed"


def test_wavelength_tools_mcp_dispatch():
    # Verify tools are registered in MCPServer tool registry
    tools = _run_tool(server.mcp.list_tools())
    tool_names = {t.name for t in tools}
    assert "list_instruments" in tool_names
    assert "list_presets" in tool_names
    assert "plugin_params" in tool_names
    assert "save_preset" in tool_names
    assert "patch_card" in tool_names
