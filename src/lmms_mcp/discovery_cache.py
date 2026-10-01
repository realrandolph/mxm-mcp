"""Small filesystem-aware cache shared by native plugin discovery indexes."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")
_lock = threading.RLock()
_entries: dict[str, tuple[tuple[tuple[object, ...], ...], object]] = {}


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
    edits and replacements. Symlinked plugin bundles are followed once, while
    unrelated links encountered inside a tree are recorded but not traversed.
    """
    result: list[tuple[object, ...]] = []
    visited: set[str] = set()

    def visit(path: Path, follow_root_link: bool = False) -> None:
        normalized = Path(os.path.abspath(path))
        try:
            is_link = normalized.is_symlink()
            if is_link and follow_root_link:
                normalized = normalized.resolve(strict=True)
            signature = _stat_signature(normalized)
        except OSError:
            signature = None
        if signature is None:
            result.append((str(normalized), "missing"))
            return
        result.append(signature)
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
                child = Path(entry.path)
                try:
                    if entry.is_symlink():
                        result.append((str(child), "symlink", os.readlink(child)))
                        target = child.resolve(strict=True)
                        target_signature = _stat_signature(target)
                        if target_signature is not None:
                            result.append(("symlink-target", *target_signature))
                        if child.suffix.lower() in {".vst3", ".lv2"} and target.is_dir():
                            visit(target)
                        continue
                    child_stat = entry.stat(follow_symlinks=False)
                    result.append((str(child), child_stat.st_dev, child_stat.st_ino,
                                   child_stat.st_mode, child_stat.st_size,
                                   child_stat.st_mtime_ns, child_stat.st_ctime_ns))
                    if entry.is_dir(follow_symlinks=False):
                        # The directory identity above is enough; visit adds its
                        # children and a duplicate signature is harmless.
                        visit(child)
                except OSError:
                    result.append((str(child), "unreadable"))
        except OSError:
            result.append((str(normalized), "unreadable"))

    for raw_path in sorted({str(Path(p).expanduser()) for p in paths}):
        visit(Path(raw_path), follow_root_link=True)
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
