"""Per-plugin consent records and short-lived approval continuations."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import tempfile
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

_PENDING_TTL_SECONDS = 300
_MAX_PENDING = 128
_MAX_PENDING_ARGUMENT_BYTES = 8 * 1024 * 1024
_MAX_PROBE_CACHE = 4096
_MAX_HASHED_BINARY_BYTES = 2 * 1024 * 1024 * 1024
_lock = threading.RLock()
_pending: dict[str, "PendingPluginAccess"] = {}
_probe_results: OrderedDict[str, dict] = OrderedDict()
_identity_cache: dict[str, tuple[tuple[int, ...], "PluginIdentity"]] = {}
_decision_override: ContextVar[dict[str, str]] = ContextVar(
    "lmms_mcp_plugin_permission_override", default={}
)


@dataclass(frozen=True)
class PluginIdentity:
    module: str
    binary: str
    sha256: str

    @property
    def key(self) -> str:
        data = json.dumps(
            {"module": self.module, "binary": self.binary, "sha256": self.sha256},
            sort_keys=True, separators=(",", ":"),
        ).encode()
        return hashlib.sha256(data).hexdigest()

    def public(self) -> dict[str, str]:
        return {"module": self.module, "binary": self.binary, "sha256": self.sha256}


def identify_plugin(module: str | Path, binary: str | Path) -> PluginIdentity:
    """Fingerprint the exact VST3 binary that the probe will load."""
    module_path = Path(module).resolve(strict=True)
    binary_path = Path(binary).resolve(strict=True)
    info = binary_path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_HASHED_BINARY_BYTES:
        raise ValueError("VST3 module binary is not a regular file or exceeds the 2 GiB limit")
    signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    key = str(binary_path)
    with _lock:
        cached = _identity_cache.get(key)
        if cached is not None and cached[0] == signature:
            return cached[1]

    digest = hashlib.sha256()
    with binary_path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    identity = PluginIdentity(str(module_path), str(binary_path), digest.hexdigest())
    with _lock:
        _identity_cache[key] = (signature, identity)
        if len(_identity_cache) > _MAX_PROBE_CACHE:
            _identity_cache.pop(next(iter(_identity_cache)))
    return identity


@dataclass(frozen=True)
class PendingPluginAccess:
    action: str
    arguments: dict
    plugin: PluginIdentity
    expires_at: float


class PendingAccessError(Exception):
    def __init__(self, token: str, plugin: PluginIdentity):
        self.token = token
        self.plugin = plugin
        super().__init__("Explicit user approval is required before probing this VST3 plugin")

    def response(self) -> dict:
        return {
            "permission_required": True,
            "plugin": self.plugin.public(),
            "token": self.token,
            "expires_in_seconds": _PENDING_TTL_SECONDS,
            "instructions": (
                "No plugin code has been executed for this plugin. Ask the user whether to "
                "allow this exact plugin once, always, or deny it. Use the question tool if "
                "available, otherwise ask directly. Only after the user explicitly approves, "
                "call resolve_native_plugin_access with this token and decision='allow_once' "
                "or 'always_allow'. If denied, call it with decision='deny'."
            ),
        }


def _permission_file() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "lmms-mcp" / "native-plugin-permissions.json"


def _read_permissions() -> dict[str, dict]:
    path = _permission_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        plugins = data.get("plugins", {})
        return plugins if isinstance(plugins, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def stored_decision(identity: PluginIdentity) -> str | None:
    with _lock:
        record = _read_permissions().get(identity.key)
    if isinstance(record, dict) and record.get("decision") in {"always_allow", "deny"}:
        return record["decision"]
    return None


def remember_decision(identity: PluginIdentity, decision: str) -> None:
    if decision not in {"always_allow", "deny"}:
        raise ValueError("Only permanent allow or deny decisions can be stored")
    with _lock:
        records = _read_permissions()
        records[identity.key] = {"decision": decision, **identity.public()}
        # Avoid an unbounded local trust database. Oldest insertion order is pruned.
        records = dict(list(records.items())[-4096:])
        path = _permission_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="plugin-permissions-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "plugins": records}, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def create_pending(action: str, arguments: dict, identity: PluginIdentity) -> str:
    frozen = json.loads(json.dumps(arguments, sort_keys=True, ensure_ascii=False))
    encoded = json.dumps(frozen, sort_keys=True, ensure_ascii=False).encode("utf-8")
    if len(encoded) > _MAX_PENDING_ARGUMENT_BYTES:
        raise ValueError(
            "Cannot safely freeze this large tool request for fallback approval; "
            "use a client with MCP elicitation support"
        )
    now = time.monotonic()
    with _lock:
        for token, pending in list(_pending.items()):
            if pending.expires_at <= now:
                _pending.pop(token, None)
        while len(_pending) >= _MAX_PENDING:
            _pending.pop(next(iter(_pending)))
        token = secrets.token_urlsafe(32)
        _pending[token] = PendingPluginAccess(
            action=action,
            arguments=frozen,
            plugin=identity,
            expires_at=now + _PENDING_TTL_SECONDS,
        )
        return token


def take_pending(token: str) -> PendingPluginAccess | None:
    now = time.monotonic()
    with _lock:
        for expired, pending in list(_pending.items()):
            if pending.expires_at <= now:
                _pending.pop(expired, None)
        pending = _pending.pop(token, None)
    if pending is None or pending.expires_at <= now:
        return None
    return pending


def cached_probe(identity: PluginIdentity) -> dict | None:
    with _lock:
        result = _probe_results.get(identity.key)
        if result is not None:
            _probe_results.move_to_end(identity.key)
            return json.loads(json.dumps(result))
    return None


def cache_probe(identity: PluginIdentity, result: dict) -> None:
    if result.get("error") or not isinstance(result.get("classes"), list):
        return
    with _lock:
        _probe_results[identity.key] = json.loads(json.dumps(result))
        _probe_results.move_to_end(identity.key)
        while len(_probe_results) > _MAX_PROBE_CACHE:
            _probe_results.popitem(last=False)


def current_override(identity: PluginIdentity) -> str | None:
    return _decision_override.get().get(identity.key)


@contextmanager
def decision_override(identity: PluginIdentity, decision: str) -> Iterator[None]:
    current = dict(_decision_override.get())
    current[identity.key] = decision
    reset = _decision_override.set(current)
    try:
        yield
    finally:
        _decision_override.reset(reset)


def clear_memory_state() -> None:
    """Test helper; persistent permissions are deliberately left untouched."""
    with _lock:
        _pending.clear()
        _probe_results.clear()
        _identity_cache.clear()
