"""Plugin quarantine records: plugins that crashed, hung, or were blocked."""

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
_IDENTITY_FIELDS = ("module", "path", "bundle", "bundle_path", "id", "cid", "uri", "name", "plugin", "identity")

REASON_SEGFAULT = "segfault during probe"
REASON_TIMEOUT = "timed out during probe"
REASON_CRASH = "crashed during probe"
REASON_NO_AUDIO = "render produced no audio during probe"
REASON_USER_BLOCKED = "user blocked"

_lock = threading.RLock()


def _config_root() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") if os.name == "nt" else Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def quarantine_directory() -> Path: return _config_root() / "mxm-mcp"
def quarantine_file_path() -> Path: return quarantine_directory() / _FILENAME
def legacy_quarantine_file_path() -> Path: return _config_root() / "lmms-mcp" / _FILENAME
def _timestamp() -> str: return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _normalize(value: str) -> str:
    text = os.path.expanduser(str(value).strip())
    return os.path.normcase(os.path.normpath(text)) if text else ""


def _record_key(module: str) -> str:
    return _normalize(module) or str(module).strip().casefold()


def _read_records() -> dict[str, dict]:
    path = quarantine_file_path()
    if not path.exists(): path = legacy_quarantine_file_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw = payload.get("plugins", {}) if isinstance(payload, dict) else {}
        return {
            k: {
                "module": v.get("module") or k if isinstance(v.get("module"), str) else k,
                "name": str(v.get("name") or ""), "reason": str(v.get("reason") or ""), "timestamp": str(v.get("timestamp") or ""),
            }
            for k, v in raw.items() if isinstance(k, str) and isinstance(v, dict)
        }
    except (OSError, ValueError, TypeError, RecursionError):
        return {}


def _write_records(records: dict[str, dict]) -> None:
    path = quarantine_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"version": _FORMAT_VERSION, "plugins": records}, indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".quarantine-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text); stream.flush(); os.fsync(stream.fileno())
        if os.name != "nt": os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        try: os.unlink(tmp)
        except FileNotFoundError: pass


def _matches(query: str, records: dict[str, dict]) -> bool:
    text = str(query).strip()
    if not text: return False
    norm, fold, stem = _normalize(text), text.casefold(), Path(text).stem.casefold()
    for r in records.values():
        mod, name = str(r.get("module", "")), str(r.get("name", ""))
        if (norm and norm == _normalize(mod)) or (fold and fold in {mod.casefold(), name.casefold()}) or (stem and mod and stem == Path(mod).stem.casefold()):
            return True
    return False


def quarantined_entry(module_or_identity: str) -> dict | None:
    with _lock: records = _read_records()
    for r in records.values():
        if _matches(module_or_identity, {_record_key(r["module"]): r}): return dict(r)
    return None


def is_quarantined(module_or_identity: str) -> bool:
    with _lock: return _matches(module_or_identity, _read_records())


def quarantine_plugin(module: str, reason: str, name: str = "") -> dict:
    mod_text = str(module).strip()
    if not mod_text: raise ValueError("A plugin module or identity is required")
    with _lock:
        records = _read_records()
        key = _record_key(mod_text)
        prev = records.get(key, {})
        disp = str(name).strip() or str(prev.get("name") or "").strip() or Path(mod_text).stem or mod_text
        entry = {"module": mod_text, "name": disp, "reason": str(reason).strip() or "quarantined", "timestamp": _timestamp()}
        records[key] = entry
        if len(records) > _MAX_RECORDS:
            records = dict(sorted(records.items(), key=lambda i: str(i[1].get("timestamp", "")))[-_MAX_RECORDS:])
        _write_records(records)
        return dict(entry)


def unquarantine_plugin(module: str) -> bool:
    with _lock:
        records = _read_records()
        key = _record_key(module)
        if key in records: del records[key]
        else:
            match = next((k for k in records if _matches(module, {k: records[k]})), None)
            if not match: return False
            del records[match]
        _write_records(records)
        return True


def list_quarantined() -> list[dict]:
    with _lock: records = list(_read_records().values())
    records.sort(key=lambda i: (str(i.get("timestamp", "")), str(i.get("module", ""))))
    return [dict(r) for r in records]


def filter_quarantined(plugins: list[dict]) -> list[dict]:
    with _lock: records = _read_records()
    if not records: return list(plugins)
    kept = []
    for p in plugins:
        if not isinstance(p, dict):
            if not _matches(str(p), records): kept.append(p)
            continue
        cands = [str(p[f]).strip() for f in _IDENTITY_FIELDS if isinstance(p.get(f), str) and str(p[f]).strip()]
        if not any(_matches(c, records) for c in cands): kept.append(p)
    return kept
