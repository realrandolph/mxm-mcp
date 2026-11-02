"""Small filesystem-aware cache shared by native plugin discovery indexes.

Entries live in a bounded LRU in memory and are mirrored to a per-user cache
file, so a server restart can reuse discovery results without re-probing
plugins. A fingerprint of the discovered trees travels with every value; a
mismatch invalidates the entry even when it was loaded from disk.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Callable, TypeVar

from .path_safety import resolved_path_within

T = TypeVar("T")
_lock = threading.RLock()
_entries: OrderedDict[str, tuple[tuple[tuple[object, ...], ...], object]] = OrderedDict()
_MAX_FINGERPRINT_ENTRIES = 100_000
_MAX_CACHE_ENTRIES = 32
_CACHE_FORMAT_VERSION = 1
_CACHE_FILENAME = "discovery-cache.json"
_MAX_DISK_CACHE_BYTES = 32 * 1024 * 1024


def cache_directory() -> Path:
    """Return the per-user directory that holds the discovery cache file.

    Windows uses ``%LOCALAPPDATA%``; POSIX uses ``$XDG_CACHE_HOME``. Both fall
    back to ``~/.cache`` when the platform variable is unset.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", "").strip()
        if base:
            return Path(base) / "lmms-mcp"
    else:
        base = os.environ.get("XDG_CACHE_HOME", "").strip()
        if base:
            return Path(base) / "lmms-mcp"
    return Path.home() / ".cache" / "lmms-mcp"


def cache_file_path() -> Path:
    """Return the exact JSON file that mirrors the in-memory cache."""
    return cache_directory() / _CACHE_FILENAME


def _stat_signature(path: Path) -> tuple[object, ...] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(path), stat.st_dev, stat.st_ino, stat.st_mode, stat.st_size,
            stat.st_mtime_ns, stat.st_ctime_ns)


def filesystem_fingerprint(paths: list[str | Path]) -> tuple[tuple[object, ...], ...]:
    """Return an identity snapshot of directory trees without parsing files.

    Directory metadata detects additions and removals; file metadata detects
    edits and replacements. Configured roots establish their own resolved
    boundary. Within one, only in-root .vst3/.lv2 directory links are followed;
    external links and cycles are recorded/skipped, never traversed.
    """
    result: list[tuple[object, ...]] = []
    visited: set[str] = set()

    def append(value: tuple[object, ...]) -> bool:
        result.append(value)
        return len(result) < _MAX_FINGERPRINT_ENTRIES

    def visit(path: Path, allowed_root: Path, follow_root_link: bool = False) -> None:
        normalized = Path(os.path.abspath(path))
        try:
            is_link = normalized.is_symlink()
            if is_link:
                if not append((str(normalized), "symlink", os.readlink(normalized))):
                    return
                resolved = resolved_path_within(normalized, allowed_root)
                if resolved is None:
                    return
                normalized = resolved
            elif follow_root_link:
                normalized = normalized.resolve(strict=True)
            signature = _stat_signature(normalized)
        except (OSError, RuntimeError):
            signature = None
        if signature is None:
            append((str(normalized), "missing"))
            return
        if not append(signature):
            return
        try:
            if not normalized.is_dir():
                return
            real = str(normalized.resolve())
            if real in visited:
                return
            visited.add(real)
            with os.scandir(normalized) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
            for entry in entries:
                if len(result) >= _MAX_FINGERPRINT_ENTRIES:
                    return
                child = Path(entry.path)
                try:
                    if entry.is_symlink():
                        if not append((str(child), "symlink", os.readlink(child))):
                            return
                        target = resolved_path_within(child, allowed_root)
                        if target is None:
                            continue
                        target_signature = _stat_signature(target)
                        if target_signature is not None:
                            if not append(("symlink-target", *target_signature)):
                                return
                        if child.suffix.lower() in {".vst3", ".lv2"} and target.is_dir():
                            visit(target, allowed_root)
                        continue
                    child_stat = entry.stat(follow_symlinks=False)
                    if not append((str(child), child_stat.st_dev, child_stat.st_ino,
                                   child_stat.st_mode, child_stat.st_size,
                                   child_stat.st_mtime_ns, child_stat.st_ctime_ns)):
                        return
                    if entry.is_dir(follow_symlinks=False):
                        # The directory identity above is enough; visit adds its
                        # children and a duplicate signature is harmless.
                        visit(child, allowed_root)
                except (OSError, RuntimeError):
                    append((str(child), "unreadable"))
        except (OSError, RuntimeError):
            append((str(normalized), "unreadable"))

    for raw_path in sorted({str(Path(p).expanduser()) for p in paths}):
        if len(result) >= _MAX_FINGERPRINT_ENTRIES:
            break
        configured = Path(os.path.abspath(raw_path))
        try:
            allowed_root = configured.resolve(strict=True)
        except (OSError, RuntimeError):
            append((str(configured), "missing"))
            continue
        visit(configured, allowed_root, follow_root_link=True)
    return tuple(result)


def _snapshot_to_json(
    snapshot: tuple[tuple[object, ...], ...],
) -> list[list[object]]:
    return [list(row) for row in snapshot]


def _snapshot_from_json(raw: object) -> tuple[tuple[object, ...], ...]:
    """Rebuild a fingerprint tuple tree from decoded JSON, or reject it."""
    if not isinstance(raw, list):
        raise TypeError("fingerprint must be a list")
    if len(raw) > _MAX_FINGERPRINT_ENTRIES:
        raise ValueError("fingerprint is too large")
    snapshot: list[tuple[object, ...]] = []
    for row in raw:
        if not isinstance(row, list) or not row:
            raise TypeError("fingerprint rows must be non-empty lists")
        fields: list[object] = []
        for field in row:
            if isinstance(field, bool) or not isinstance(field, (str, int, float)):
                raise TypeError("unsupported fingerprint field")
            fields.append(field)
        snapshot.append(tuple(fields))
    return tuple(snapshot)


def _remove_cache_file(path: Path | None = None) -> None:
    if path is None:
        try:
            path = cache_file_path()
        except (OSError, RuntimeError):
            return
    try:
        path.unlink()
    except OSError:
        pass


def _read_disk_entries() -> OrderedDict[str, tuple[tuple[tuple[object, ...], ...], object]]:
    """Decode the cache file, returning an empty cache for any bad file."""
    try:
        path = cache_file_path()
        if path.stat().st_size > _MAX_DISK_CACHE_BYTES:
            return OrderedDict()
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, RuntimeError, ValueError, TypeError, RecursionError):
        return OrderedDict()
    if not isinstance(payload, dict):
        return OrderedDict()
    if payload.get("version") != _CACHE_FORMAT_VERSION:
        return OrderedDict()
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        return OrderedDict()
    entries: OrderedDict[str, tuple[tuple[tuple[object, ...], ...], object]] = OrderedDict()
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        key = raw.get("key")
        if not isinstance(key, str) or not key:
            continue
        try:
            snapshot = _snapshot_from_json(raw.get("fingerprint"))
        except (TypeError, ValueError):
            continue
        entries.pop(key, None)
        entries[key] = (snapshot, raw.get("value"))
        while len(entries) > _MAX_CACHE_ENTRIES:
            entries.popitem(last=False)
    return entries


def _write_disk_entries(
    entries: OrderedDict[str, tuple[tuple[tuple[object, ...], ...], object]],
) -> None:
    """Atomically replace the cache file; unsupported values stay memory-only."""
    try:
        path = cache_file_path()
    except (OSError, RuntimeError):
        return
    if not entries:
        _remove_cache_file(path)
        return
    payload = {
        "version": _CACHE_FORMAT_VERSION,
        "entries": [
            {
                "key": key,
                "fingerprint": _snapshot_to_json(snapshot),
                "value": value,
            }
            for key, (snapshot, value) in entries.items()
        ],
    }
    try:
        text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return
    if len(text) > _MAX_DISK_CACHE_BYTES:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    except OSError:
        return
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except (OSError, TypeError, ValueError):
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _load_disk_entry(key: str) -> tuple[tuple[tuple[object, ...], ...], object] | None:
    return _read_disk_entries().get(key)


def cached_discovery(
    key: str,
    paths: list[str | Path],
    discover: Callable[[], T],
    *,
    refresh: bool = False,
) -> T:
    """Return a cached index when the discovered filesystem is unchanged."""
    snapshot = filesystem_fingerprint(paths)
    with _lock:
        cached = _entries.get(key)
        if cached is None and not refresh:
            cached = _load_disk_entry(key)
            if cached is not None:
                _entries[key] = cached
                _entries.move_to_end(key)
                while len(_entries) > _MAX_CACHE_ENTRIES:
                    _entries.popitem(last=False)
        if not refresh and cached is not None and cached[0] == snapshot:
            _entries.move_to_end(key)
            return cached[1]  # type: ignore[return-value]
    value = discover()
    with _lock:
        disk_entries = _read_disk_entries()
        disk_entries.pop(key, None)
        disk_entries[key] = (snapshot, value)
        while len(disk_entries) > _MAX_CACHE_ENTRIES:
            disk_entries.popitem(last=False)
        _write_disk_entries(disk_entries)
        _entries[key] = (snapshot, value)
        _entries.move_to_end(key)
        while len(_entries) > _MAX_CACHE_ENTRIES:
            _entries.popitem(last=False)
    return value


def clear_discovery_cache(key: str | None = None) -> None:
    """Invalidate one index or every native discovery index, memory and disk."""
    with _lock:
        if key is None:
            _entries.clear()
            _remove_cache_file()
            return
        _entries.pop(key, None)
        disk_entries = _read_disk_entries()
        if disk_entries.pop(key, None) is not None:
            _write_disk_entries(disk_entries)
