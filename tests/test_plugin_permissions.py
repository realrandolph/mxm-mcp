from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lmms_mcp import plugin_permissions, server, vst3, vst3_probe


class FakeContext:
    def __init__(self, decision="Allow once", capabilities=True):
        elicitation = (SimpleNamespace(form=object(), url=None) if capabilities else None)
        self.client_capabilities = SimpleNamespace(elicitation=elicitation)
        self.decision = decision
        self.messages = []

    async def elicit(self, message, schema):
        self.messages.append((message, schema))
        return SimpleNamespace(action="accept", data=SimpleNamespace(decision=self.decision))


def _synthetic_bundle(tmp_path, monkeypatch):
    bundle = tmp_path / "Fixture.vst3"
    binary = bundle / "Contents" / "x86_64-linux" / "Fixture.so"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"synthetic module identity")
    monkeypatch.setattr(vst3, "find_vst3_bundles", lambda: [bundle])
    monkeypatch.setattr(vst3_probe, "bundle_binary", lambda _bundle: binary)
    monkeypatch.setattr(vst3, "native_vst3_discovery_supported", lambda: True)
    probes = []

    def probe(path):
        probes.append(Path(path))
        return {"module": str(path), "classes": [{
            "cid": "A1B2C3D4E5F60718293A4B4C55667788",
            "name": "Fixture Synth", "category": "Audio Module Class",
            "sub_categories": "Instrument|Synth", "vendor": "Test",
            "version": "1", "class_flags": 0,
        }]}

    monkeypatch.setattr(vst3, "_probe_bundle", probe)
    return bundle, binary, probes


@pytest.fixture(autouse=True)
def clean_permission_state():
    plugin_permissions.clear_memory_state()
    yield
    plugin_permissions.clear_memory_state()


def test_elicitation_allow_once_executes_exact_binary_once(tmp_path, monkeypatch):
    bundle, _binary, probes = _synthetic_bundle(tmp_path, monkeypatch)
    ctx = FakeContext("Allow once")
    args = {"plugin_type": "vst3", "refresh": False}

    plugins, denied = asyncio.run(server._authorized_vst3_plugins(
        ctx, "list_native_plugins", args,
    ))
    assert plugins[0]["name"] == "Fixture Synth"
    assert denied == []
    assert len(ctx.messages) == 1
    assert str(bundle) in ctx.messages[0][0]
    assert "SHA-256:" in ctx.messages[0][0]
    assert probes == [bundle]

    asyncio.run(server._authorized_vst3_plugins(ctx, "list_native_plugins", args))
    assert len(ctx.messages) == 1
    assert probes == [bundle]  # cached metadata no longer executes plugin code


def test_elicitation_permanent_allow_and_deny_are_binary_scoped(tmp_path, monkeypatch):
    bundle, _binary, probes = _synthetic_bundle(tmp_path, monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    ctx = FakeContext("Always allow this binary")
    args = {"plugin_type": "vst3"}

    asyncio.run(server._authorized_vst3_plugins(ctx, "list_native_plugins", args))
    assert probes == [bundle]
    assert plugin_permissions._permission_file().is_file()

    plugin_permissions.clear_memory_state()
    no_prompt = FakeContext("deny")
    no_prompt.elicit = lambda *_args: pytest.fail("permanent grant should skip prompting")
    asyncio.run(server._authorized_vst3_plugins(no_prompt, "list_native_plugins", args))
    assert no_prompt.messages == []
    assert probes == [bundle, bundle]

    # Replacing the file changes its fingerprint and requires a fresh decision.
    plugin_permissions.clear_memory_state()
    (bundle / "Contents" / "x86_64-linux" / "Fixture.so").write_bytes(b"replacement")
    fresh = FakeContext("Deny")
    plugins, denied = asyncio.run(server._authorized_vst3_plugins(
        fresh, "list_native_plugins", args,
    ))
    assert plugins == []
    assert denied and "denied" in denied[0]["reason"]
    assert len(fresh.messages) == 1
    assert probes == [bundle, bundle]


def test_permanent_deny_overrides_cached_probe_metadata(tmp_path, monkeypatch):
    bundle, binary, probes = _synthetic_bundle(tmp_path, monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    ctx = FakeContext("Allow once")
    asyncio.run(server._authorized_vst3_plugins(ctx, "list_native_plugins", {}))
    identity = plugin_permissions.identify_plugin(bundle, binary)
    plugin_permissions.remember_decision(identity, "deny")

    no_prompt = FakeContext(capabilities=False)
    plugins, denied = asyncio.run(server._authorized_vst3_plugins(
        no_prompt, "list_native_plugins", {},
    ))
    assert plugins == []
    assert denied and "denied" in denied[0]["reason"]
    assert probes == [bundle]


def test_fallback_token_replays_frozen_action_once(tmp_path, monkeypatch):
    bundle, _binary, probes = _synthetic_bundle(tmp_path, monkeypatch)
    ctx = FakeContext(capabilities=False)
    args = {"plugin_type": "vst3", "query": "frozen value"}
    with pytest.raises(plugin_permissions.PendingAccessError) as raised:
        asyncio.run(server._authorized_vst3_plugins(
            ctx, "list_native_plugins", args,
        ))
    pending = raised.value.response()
    assert pending["permission_required"] is True
    assert pending["expires_in_seconds"] > 0
    assert probes == []

    class FakeMcpServer:
        async def call_tool(self, action, arguments, inner_ctx, convert_result=False):
            assert action == "list_native_plugins"
            assert arguments == args
            result, denied = await server._authorized_vst3_plugins(
                inner_ctx, action, arguments,
            )
            return json.dumps({"plugins": result, "denied": denied})

    ctx.mcp_server = FakeMcpServer()
    result = json.loads(asyncio.run(server.resolve_native_plugin_access(
        pending["token"], "allow_once", ctx,
    )))
    assert result["plugins"][0]["name"] == "Fixture Synth"
    assert probes == [bundle]
    replay = asyncio.run(server.resolve_native_plugin_access(
        pending["token"], "allow_once", ctx,
    ))
    assert "invalid, expired, or already used" in replay
    assert probes == [bundle]


def test_fallback_denial_is_saved_and_does_not_probe(tmp_path, monkeypatch):
    bundle, _binary, probes = _synthetic_bundle(tmp_path, monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    ctx = FakeContext(capabilities=False)
    args = {"plugin_type": "vst3"}
    with pytest.raises(plugin_permissions.PendingAccessError) as raised:
        asyncio.run(server._authorized_vst3_plugins(ctx, "list_native_plugins", args))
    token = raised.value.token

    class FakeMcpServer:
        async def call_tool(self, action, arguments, inner_ctx, convert_result=False):
            result, denied = await server._authorized_vst3_plugins(inner_ctx, action, arguments)
            return json.dumps({"plugins": result, "denied": denied})

    ctx.mcp_server = FakeMcpServer()
    result = json.loads(asyncio.run(server.resolve_native_plugin_access(token, "deny", ctx)))
    assert result["plugins"] == []
    assert result["denied"][0]["module"] == str(bundle)
    assert probes == []


def test_expired_fallback_token_is_discarded(monkeypatch):
    identity = plugin_permissions.PluginIdentity("/module.vst3", "/module.so", "a" * 64)
    now = [10.0]
    monkeypatch.setattr(plugin_permissions.time, "monotonic", lambda: now[0])
    token = plugin_permissions.create_pending("list_native_plugins", {"x": 1}, identity)
    now[0] += 301
    assert plugin_permissions.take_pending(token) is None
