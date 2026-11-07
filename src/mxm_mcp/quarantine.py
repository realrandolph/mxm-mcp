"""Plugin quarantine records: plugins that crashed, hung, or were blocked.

A probe render runs plugin code inside MXM's child process. When MXM segfaults
or hangs, the offending plugin identity is remembered so later discovery
sweeps and auditions can skip it instead of crashing the same way again.

Records live in one per-user JSON file, modelled on the blocked-plugin list
Wavelength keeps for its render worker:

* POSIX: ``$XDG_CONFIG_HOME/mxm-mcp/quarantine.json``
  (``~/.config/mxm-mcp/quarantine.json`` by default)
* Windows: ``%APPDATA%/mxm-mcp/quarantine.json``

Releases before the MXM rename wrote to ``lmms-mcp``. That file is still read
while the ``mxm-mcp`` file does not exist yet, and the next write migrates its
records into the new location.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

_FORMAT_VERSION = 1
_FILENAME = "quarantine.json"
_MAX_RECORDS = 4096
_IDENTITY_FIELDS = (
    "module",
    "path",
    "bundle",
    "bundle_path",
    "id",
    "cid",
    "uri",
    "name",
    "plugin",
    "identity",
)

# Reasons used by the probe and by explicit user blocking. They are plain
# strings on disk; these constants keep the common cases consistent.
REASON_SEGFAULT = "segfault during probe"
REASON_TIMEOUT = "timed out during probe"
REASON_CRASH = "crashed during probe"
REASON_NO_AUDIO = "render produced no audio during probe"
REASON_USER_BLOCKED = "user blocked"

_lock = threading.RLock()


def _config_root() -> Path:
    """Return the per-user configuration root (not the ``*-mcp`` directory)."""
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def quarantine_directory() -> Path:
    """Directory that holds the current quarantine file."""
    return _config_root() / "mxm-mcp"


def quarantine_file_path() -> Path:
    """Return the ``mxm-mcp`` quarantine file path this release writes."""
    return quarantine_directory() / _FILENAME


def legacy_quarantine_file_path() -> Path:
    """Return the pre-rename ``lmms-mcp`` quarantine file path."""
    return _config_root() / "lmms-mcp" / _FILENAME


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _normalize(value: str) -> str:
    """Normalize a module path or identity for comparison."""
    text = os.path.expanduser(str(value).strip())
    if not text:
        return ""
    return os.path.normcase(os.path.normpath(text))


def _record_key(module: str) -> str:
    return _normalize(module) or str(module).strip().casefold()


def _read_records() -> dict[str, dict]:
    """Read the active file, falling back to the legacy location.

    When the ``mxm-mcp`` file exists it is the sole source of truth. Reading a
    merged view would resurrect entries that were removed after the migration.
    """
    path = quarantine_file_path()
    if not path.exists():
        path = legacy_quarantine_file_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, RecursionError):
        return {}
    if not isinstance(payload, dict):
        return {}
    raw = payload.get("plugins")
    if not isinstance(raw, dict):
        return {}
    records: dict[str, dict] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        module = value.get("module")
        if not isinstance(module, str) or not module.strip():
            module = key
        records[key] = {
            "module": module,
            "name": str(value.get("name") or ""),
            "reason": str(value.get("reason") or ""),
            "timestamp": str(value.get("timestamp") or ""),
        }
    return records


def _write_records(records: dict[str, dict]) -> None:
    """Atomically replace the ``mxm-mcp`` quarantine file."""
    path = quarantine_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": _FORMAT_VERSION, "plugins": records}
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    descriptor, temporary = tempfile.mkstemp(
        prefix=".quarantine-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
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


def _matches(query: str, records: dict[str, dict]) -> bool:
    text = str(query).strip()
    if not text:
        return False
    normalized = _normalize(text)
    folded = text.casefold()
    stem = Path(text).stem.casefold()
    for record in records.values():
        module = str(record.get("module", ""))
        name = str(record.get("name", ""))
        if normalized and normalized == _normalize(module):
            return True
        if folded and folded in {module.casefold(), name.casefold()}:
            return True
        if stem and module and stem == Path(module).stem.casefold():
            return True
    return False


def quarantined_entry(module_or_identity: str) -> dict | None:
    """Return the record matching *module_or_identity*, or ``None``."""
    with _lock:
        records = _read_records()
    for record in records.values():
        if _matches(module_or_identity, {_record_key(record["module"]): record}):
            return dict(record)
    return None


def is_quarantined(module_or_identity: str) -> bool:
    """Whether a module path, display name, or identity is quarantined."""
    with _lock:
        return _matches(module_or_identity, _read_records())


def quarantine_plugin(module: str, reason: str, name: str = "") -> dict:
    """Record *module* as quarantined and return the stored entry.

    Re-quarantining the same identity replaces the reason and timestamp while
    keeping a previously recorded display name when *name* is empty.
    """
    module_text = str(module).strip()
    if not module_text:
        raise ValueError("A plugin module or identity is required")
    reason_text = str(reason).strip() or "quarantined"
    with _lock:
        records = _read_records()
        details = records.get(_record_key(module_text), {})
        display_name = str(name).strip() or str(details.get("name") or "").strip()
        if not display_name:
            display_name = Path(module_text).stem or module_text
        entry = {
            "module": module_text,
            "name": display_name,
            "reason": reason_text,
            "timestamp": _timestamp(),
        }
        records[_record_key(module_text)] = entry
        if len(records) > _MAX_RECORDS:
            oldest = sorted(
                records.items(), key=lambda item: str(item[1].get("timestamp", ""))
            )
            records = dict(oldest[-_MAX_RECORDS:])
        _write_records(records)
        return dict(entry)


def unquarantine_plugin(module: str) -> bool:
    """Remove a quarantine record. Returns whether one was removed."""
    with _lock:
        records = _read_records()
        key = _record_key(module)
        if key in records:
            del records[key]
        else:
            matched = next(
                (existing for existing in records if _matches(module, {existing: records[existing]})),
                None,
            )
            if matched is None:
                return False
            del records[matched]
        _write_records(records)
        return True


def list_quarantined() -> list[dict]:
    """Return every quarantine record, oldest first."""
    with _lock:
        records = list(_read_records().values())
    records.sort(key=lambda item: (str(item.get("timestamp", "")), str(item.get("module", ""))))
    return [dict(record) for record in records]


def filter_quarantined(plugins: list[dict]) -> list[dict]:
    """Drop entries whose module, path, id, or name is quarantined.

    Discovery sweeps return heterogeneous plugin dictionaries (native VST3
    records use ``module``/``cid``, LV2 records use ``bundle``/``uri``,
    stock MXM plugins use ``name``). Every identity-bearing field is checked,
    so one helper can filter all of those sweeps.
    """
    with _lock:
        records = _read_records()
    if not records:
        return list(plugins)
    kept: list[dict] = []
    for plugin in plugins:
        if not isinstance(plugin, dict):
            if not _matches(str(plugin), records):
                kept.append(plugin)
            continue
        candidates = [
            str(plugin[field]).strip()
            for field in _IDENTITY_FIELDS
            if isinstance(plugin.get(field), str) and str(plugin[field]).strip()
        ]
        if any(_matches(candidate, records) for candidate in candidates):
            continue
        kept.append(plugin)
    return kept
