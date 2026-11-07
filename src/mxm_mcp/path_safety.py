"""Small helpers for keeping recursive discovery inside configured roots."""

from __future__ import annotations

from pathlib import Path


def resolved_path_within(path: str | Path, root: str | Path) -> Path | None:
    """Resolve *path* only when it stays beneath the resolved configured root."""
    try:
        resolved_root = Path(root).resolve(strict=True)
        resolved_path = Path(path).resolve(strict=True)
        resolved_path.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved_path
