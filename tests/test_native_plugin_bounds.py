"""Tests for bounded, paginated list_instruments responses."""

from __future__ import annotations

import asyncio
import json
import pytest

from mxm_mcp import server, lv2 as lv2_mod, vst3 as vst3_mod


def _make_synthetic_lv2_plugin(
    index: int,
    is_instrument: bool = False,
    num_ports: int = 100,
) -> dict:
    uri = f"http://example.org/lv2/plugin_{index:04d}"
    ports = [
        {
            "index": str(p),
            "symbol": f"control_{p}",
            "name": f"Control Port {p}",
            "direction": "input" if p % 2 == 0 else "output",
            "type": "control" if p > 2 else "audio",
            "default": "0.5",
            "minimum": "0.0",
            "maximum": "1.0",
        }
        for p in range(num_ports)
    ]
    classes = ["InstrumentPlugin"] if is_instrument else ["FilterPlugin"]
    return {
        "uri": uri,
        "name": f"Synthetic {'Instrument' if is_instrument else 'Effect'} {index:04d}",
        "vendor": "Test Vendor",
        "bundle": f"/usr/lib/lv2/test_{index:04d}.lv2",
        "source": f"/usr/lib/lv2/test_{index:04d}.lv2/manifest.ttl",
        "plugin_type": "LV2",
        "is_instrument": is_instrument,
        "classes": classes,
        "version": "1.2.3",
        "required_features": ["http://lv2plug.in/ns/ext/urid#map"],
        "ports": ports,
    }


def _make_synthetic_vst3_plugin(
    index: int,
    is_instrument: bool = True,
) -> dict:
    return {
        "name": f"Synthetic VST3 {index:04d}",
        "vendor": "VST3 Vendor",
        "module": f"/usr/lib/vst3/synthetic_{index:04d}.vst3",
        "cid": f"CID{index:028d}",
        "is_instrument": is_instrument,
        "sub_categories": "Instrument|Synth" if is_instrument else "Fx",
        "version": "2.0.0",
        "class_flags": 0,
    }


@pytest.fixture
def synthetic_inventory(monkeypatch):
    """Provide an inventory of 250 LV2 plugins (100 instruments, 150 effects), each with 100 ports."""
    plugins = [
        _make_synthetic_lv2_plugin(i, is_instrument=(i < 100), num_ports=100)
        for i in range(250)
    ]
    monkeypatch.setattr(
        lv2_mod,
        "discover_lv2",
        lambda refresh=False: {
            "plugins": list(plugins),
            "presets": [],
            "search_paths": ["/usr/lib/lv2"],
        },
    )
    monkeypatch.setattr(
        server,
        "_authorized_vst3_plugins",
        lambda *args, **kwargs: ([], []),
    )
    monkeypatch.setattr(
        vst3_mod,
        "find_vst3_bundles",
        lambda: [],
    )
    monkeypatch.setattr(
        vst3_mod,
        "effective_search_paths",
        lambda: [],
    )
    return plugins


def test_default_list_is_compact_and_paginated(synthetic_inventory):
    """Default call must paginate to 100 records and omit heavy port metadata."""
    raw = asyncio.run(server.list_instruments(kind="all", limit=100))
    result = json.loads(raw)

    # 250 synthetic LV2 + 8 built-ins = 258
    assert result["total"] == 258
    assert result["count"] == 100
    assert result["offset"] == 0
    assert result["limit"] == 100
    assert result["next_offset"] == 100
    assert len(result["instruments"]) == 100

    # Verify compact record contents
    first = result["instruments"][0]
    assert "name" in first
    assert "format" in first
    assert "kind" in first

    # Ports, source, and required features must NOT be present in compact mode
    assert "ports" not in first
    assert "source" not in first
    assert "required_features" not in first


def test_pagination_across_pages(synthetic_inventory):
    """Paginating across all records preserves sequence and produces correct next_offset."""
    all_seen_names: list[str] = []
    offset = 0
    limit = 60

    while True:
        raw = asyncio.run(
            server.list_instruments(
                kind="all", offset=offset, limit=limit
            )
        )
        page = json.loads(raw)
        assert page["total"] == 258
        assert page["offset"] == offset
        assert page["limit"] == limit
        page_names = [p["name"] for p in page["instruments"]]
        assert len(page_names) == page["count"]
        all_seen_names.extend(page_names)

        if page["next_offset"] is None:
            break
        assert page["next_offset"] == offset + page["count"]
        offset = page["next_offset"]

    assert len(all_seen_names) == 258
    assert len(set(all_seen_names)) == 258


def test_pagination_past_end(synthetic_inventory):
    """Requesting an offset beyond total returns an empty list with next_offset=None."""
    raw = asyncio.run(
        server.list_instruments(kind="all", offset=500, limit=100)
    )
    result = json.loads(raw)

    assert result["total"] == 258
    assert result["count"] == 0
    assert result["offset"] == 500
    assert result["next_offset"] is None
    assert result["instruments"] == []


def test_kind_effects_filtering(synthetic_inventory):
    """kind='effect' filters accurately."""
    raw = asyncio.run(
        server.list_instruments(kind="effect", limit=0)
    )
    result = json.loads(raw)

    assert result["total"] == 150
    assert result["count"] == 150
    assert result["next_offset"] is None
    assert all(p["kind"] == "effect" for p in result["instruments"])


def test_inspect_native_plugin_retains_full_ports(synthetic_inventory):
    """inspect_native_plugin still returns the complete plugin record with all ports."""
    target_uri = synthetic_inventory[0]["uri"]
    raw = asyncio.run(
        server.inspect_native_plugin(plugin_type="lv2", plugin=target_uri)
    )
    record = json.loads(raw)

    assert record["uri"] == target_uri
    assert "ports" in record
    assert len(record["ports"]) == 100
    assert record["ports"][0]["symbol"] == "control_0"
    assert record["required_features"] == ["http://lv2plug.in/ns/ext/urid#map"]
    assert "source" in record


def test_mcp_transport_call_tool(synthetic_inventory):
    """Invoke list_instruments through the MCP server call_tool interface."""
    mcp_result = asyncio.run(
        server.mcp.call_tool(
            "list_instruments",
            {"kind": "all", "offset": 10, "limit": 20},
        )
    )
    if hasattr(mcp_result, "content") and mcp_result.content:
        content = mcp_result.content[0].text
    elif isinstance(mcp_result, list) and mcp_result:
        content = getattr(mcp_result[0], "text", str(mcp_result[0]))
    else:
        content = str(mcp_result)
    parsed = json.loads(content)

    assert parsed["total"] == 258
    assert parsed["count"] == 20
    assert parsed["offset"] == 10
    assert parsed["limit"] == 20
    assert parsed["next_offset"] == 30
    assert len(parsed["instruments"]) == 20


def test_invalid_arguments_handled_cleanly():
    """Invalid kind or pagination parameters produce clear JSON errors."""
    bad_type = json.loads(
        asyncio.run(server.list_instruments(kind="invalid"))
    )
    assert "error" in bad_type
    assert "kind must be 'all', 'instrument', or 'effect'" in bad_type["error"]
