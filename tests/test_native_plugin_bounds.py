"""Tests for bounded, paginated list_native_plugins responses (Issue #9)."""

from __future__ import annotations

import asyncio
import json
import pytest

from lmms_mcp import server, lv2 as lv2_mod, vst3 as vst3_mod


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
        "effective_search_paths",
        lambda: [],
    )
    return plugins


def test_default_list_is_compact_and_paginated(synthetic_inventory):
    """Default call must paginate to 100 records and omit heavy port metadata."""
    raw = asyncio.run(server.list_native_plugins(plugin_type="lv2"))
    result = json.loads(raw)

    assert result["total"] == 250
    assert result["count"] == 100
    assert result["offset"] == 0
    assert result["limit"] == 100
    assert result["next_offset"] == 100
    assert len(result["plugins"]) == 100

    # Verify compact record contents
    first = result["plugins"][0]
    assert first["plugin_type"] == "LV2"
    assert "name" in first
    assert "uri" in first
    assert "bundle" in first
    assert "is_instrument" in first
    assert "classes" in first

    # Ports, source, and required features must NOT be present in compact mode
    assert "ports" not in first
    assert "source" not in first
    assert "required_features" not in first


def test_pagination_across_pages(synthetic_inventory):
    """Paginating across all records preserves sequence and produces correct next_offset."""
    all_seen_uris: list[str] = []
    offset = 0
    limit = 60

    while True:
        raw = asyncio.run(
            server.list_native_plugins(
                plugin_type="lv2", offset=offset, limit=limit
            )
        )
        page = json.loads(raw)
        assert page["total"] == 250
        assert page["offset"] == offset
        assert page["limit"] == limit
        page_uris = [p["uri"] for p in page["plugins"]]
        assert len(page_uris) == page["count"]
        all_seen_uris.extend(page_uris)

        if page["next_offset"] is None:
            break
        assert page["next_offset"] == offset + page["count"]
        offset = page["next_offset"]

    assert len(all_seen_uris) == 250
    assert len(set(all_seen_uris)) == 250


def test_pagination_past_end(synthetic_inventory):
    """Requesting an offset beyond total returns an empty list with next_offset=None."""
    raw = asyncio.run(
        server.list_native_plugins(plugin_type="lv2", offset=500, limit=100)
    )
    result = json.loads(raw)

    assert result["total"] == 250
    assert result["count"] == 0
    assert result["offset"] == 500
    assert result["next_offset"] is None
    assert result["plugins"] == []


def test_include_effects_filtering_does_not_change_discovery(synthetic_inventory):
    """include_effects=False filters post-discovery and updates total accurately."""
    raw = asyncio.run(
        server.list_native_plugins(plugin_type="lv2", include_effects=False)
    )
    result = json.loads(raw)

    assert result["total"] == 100
    assert result["count"] == 100
    assert result["next_offset"] is None
    assert all(p["is_instrument"] is True for p in result["plugins"])


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


def test_details_mode_returns_ports(synthetic_inventory):
    """Explicit details=True includes full ports in list_native_plugins."""
    raw = asyncio.run(
        server.list_native_plugins(
            plugin_type="lv2", offset=0, limit=5, details=True
        )
    )
    result = json.loads(raw)

    assert result["count"] == 5
    first = result["plugins"][0]
    assert "ports" in first
    assert len(first["ports"]) == 100
    assert "required_features" in first


def test_payload_size_regression_prevention(synthetic_inventory):
    """Ensure compact response size stays orders of magnitude smaller than full ports."""
    compact_raw = asyncio.run(
        server.list_native_plugins(plugin_type="lv2", offset=0, limit=100)
    )
    # 100 compact plugins should easily fit in under 35 KB
    assert len(compact_raw.encode("utf-8")) < 35_000

    # By contrast, 100 full plugins with 100 ports each serialized directly would exceed 1 MB
    full_sample = json.dumps(synthetic_inventory[:100], indent=2)
    assert len(full_sample.encode("utf-8")) > 1_000_000


def test_payload_ceiling_enforces_truncation(synthetic_inventory, monkeypatch):
    """When serialized response exceeds the byte limit, it truncates safely with metadata."""
    # Constrain the ceiling to ~4 KB so only a few plugins fit
    monkeypatch.setattr(server, "_MAX_NATIVE_PLUGINS_PAYLOAD_BYTES", 4000)

    raw = asyncio.run(
        server.list_native_plugins(plugin_type="lv2", offset=0, limit=100)
    )
    result = json.loads(raw)

    assert result["truncated"] is True
    assert "Response size ceiling reached" in result["truncation_reason"]
    assert 0 < result["count"] < 100
    assert result["next_offset"] == result["count"]
    # Verify records that were included are complete and valid
    for item in result["plugins"]:
        assert "name" in item
        assert "uri" in item


def test_mcp_transport_call_tool(synthetic_inventory):
    """Invoke list_native_plugins through the MCP server call_tool interface."""
    mcp_result = asyncio.run(
        server.mcp.call_tool(
            "list_native_plugins",
            {"plugin_type": "lv2", "offset": 10, "limit": 20},
        )
    )
    if hasattr(mcp_result, "content") and mcp_result.content:
        content = mcp_result.content[0].text
    elif isinstance(mcp_result, list) and mcp_result:
        content = getattr(mcp_result[0], "text", str(mcp_result[0]))
    else:
        content = str(mcp_result)
    parsed = json.loads(content)

    assert parsed["total"] == 250
    assert parsed["count"] == 20
    assert parsed["offset"] == 10
    assert parsed["limit"] == 20
    assert parsed["next_offset"] == 30
    assert len(parsed["plugins"]) == 20


def test_vst3_compact_record_representation(monkeypatch):
    """Verify VST3 plugins are properly formatted in compact listing."""
    vst3_records = [_make_synthetic_vst3_plugin(i) for i in range(10)]

    async def fake_authorized(*args, **kwargs):
        return (list(vst3_records), [])

    monkeypatch.setattr(
        server,
        "_authorized_vst3_plugins",
        fake_authorized,
    )
    monkeypatch.setattr(
        vst3_mod,
        "effective_search_paths",
        lambda: ["/usr/lib/vst3"],
    )

    raw = asyncio.run(server.list_native_plugins(plugin_type="vst3", limit=5))
    result = json.loads(raw)

    assert result["total"] == 10
    assert result["count"] == 5
    assert result["offset"] == 0
    assert result["next_offset"] == 5
    first = result["plugins"][0]
    assert first["plugin_type"] == "VST3"
    assert first["name"] == "Synthetic VST3 0000"
    assert first["module"] == "/usr/lib/vst3/synthetic_0000.vst3"
    assert first["cid"] == f"CID{0:028d}"
    assert first["is_instrument"] is True
    assert first["sub_categories"] == "Instrument|Synth"


def test_invalid_arguments_handled_cleanly():
    """Invalid plugin_type or pagination parameters produce clear JSON errors."""
    bad_type = json.loads(
        asyncio.run(server.list_native_plugins(plugin_type="invalid"))
    )
    assert "error" in bad_type
    assert "plugin_type must be 'all', 'vst3', or 'lv2'" in bad_type["error"]

    bad_offset = json.loads(
        asyncio.run(server.list_native_plugins(offset="not-an-int"))
    )
    assert "error" in bad_offset
    assert "Invalid pagination parameters" in bad_offset["error"]
