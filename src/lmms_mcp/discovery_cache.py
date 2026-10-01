"""Small filesystem-aware cache shared by native plugin discovery indexes."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable, TypeVar

from .path_safety import resolved_path_within

T = TypeVar("T")
_lock = threading.RLock()
_entries: dict[str, tuple[tuple[tuple[object, ...], ...], object]] = {}
_MAX_FINGERPRINT_ENTRIES = 100_000


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
        if not refresh and cached is not None and cached[0] == snapshot:
            return cached[1]  # type: ignore[return-value]
    value = discover()
    with _lock:
        _entries[key] = (snapshot, value)
    return value


def clear_discovery_cache(key: str | None = None) -> None:
    """Invalidate one index or every native discovery index."""
    with _lock:
        if key is None:
            _entries.clear()
        else:
            _entries.pop(key, None)
