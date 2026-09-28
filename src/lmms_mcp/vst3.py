"""Discovery and selection of native VST3 plugins for MXM.

MXM hosts VST3 natively through its built-in ``vst3instrument`` plugin. This
module finds the ``.vst3`` bundles MXM would find and reports the identity MXM
matches on load: the bundle ``module`` path and its 32-hex ``cid``
(``Vst3Manager::Descriptor::cid``), read from the module's own factory via
:mod:`lmms_mcp.vst3_probe`. Deliberately separate from the Carla path.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from . import lmms_app

#! MXM enables path-only mode on mere presence (std::getenv), so test for the
#! key, not its truthiness.
PATH_ONLY_ENV = "MXM_VST3_PATH_ONLY"

_PROBE_TIMEOUT_SECONDS = 30
_AUDIO_MODULE = "Audio Module Class"
_CID_RE = re.compile(r"^[0-9A-Fa-f]{32}$")


def native_vst3_discovery_supported() -> bool:
    """Whether this MCP can discover VST3 plugins here (Linux-only today)."""
    return sys.platform.startswith("linux")


def path_only_enabled() -> bool:
    return PATH_ONLY_ENV in os.environ


def is_valid_cid(cid: str) -> bool:
    return bool(_CID_RE.match(cid.strip()))


def normalize_cid(cid: str) -> str:
    return cid.strip().upper()


def _app_vst3_dir() -> Path | None:
    """Application ``vst3`` dir, resolved like MXM's ``/proc/<pid>/exe``."""
    exe = lmms_app.find_mxm_exe() or lmms_app.find_lmms_exe()
    if exe is None:
        return None
    candidate = Path(os.path.realpath(exe)).parent / "vst3"
    return candidate if candidate.is_dir() else None


def standard_vst3_dirs() -> list[Path]:
    """Standard ``.vst3`` locations, in MXM's order."""
    dirs = [Path(home) / ".vst3" for home in (os.environ.get("HOME"),) if home]
    for base in (Path("/usr"), Path("/usr/local")):
        dirs += [base / "lib64" / "vst3", base / "lib" / "vst3"]
    app = _app_vst3_dir()
    return dirs + [app] if app else dirs


def _vst3_path_entries(raw: str) -> list[str]:
    return [part for part in raw.split(os.pathsep) if part]


def effective_search_paths() -> list[str]:
    """Directories actually scanned (honours path-only), for reporting."""
    paths = [str(d) for d in standard_vst3_dirs()] if not path_only_enabled() else []
    return paths + _vst3_path_entries(os.environ.get("VST3_PATH", ""))


def _collect(directory, result, seen, dirs_only=False):
    """Recursively collect ``.vst3`` entries (case-sensitive, like MXM).

    ``dirs_only`` mirrors MXM's ``QDir::Dirs`` scan for ``VST3_PATH``; the
    standard locations use MXM's extension-based ``findFilesWithExt`` instead.
    A ``.vst3`` entry is never descended into.
    """
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.suffix == ".vst3" and (entry.is_dir() or not dirs_only):
            key = str(entry)
            if key not in seen:
                seen.add(key)
                result.append(entry)
        elif entry.is_dir():
            _collect(entry, result, seen, dirs_only)


def _add_bundles_from_path(path, result, seen):
    """Collect from one ``VST3_PATH``/extra entry (verbatim bundle, else dir)."""
    if not path.exists():
        return
    if path.suffix == ".vst3":
        if str(path) not in seen:
            seen.add(str(path))
            result.append(path)
    elif path.is_dir():
        _collect(Path(os.path.abspath(path)), result, seen, dirs_only=True)


def find_vst3_bundles(extra_paths: list[str] | None = None) -> list[Path]:
    """The ``.vst3`` bundles MXM's native host can discover.

    Honours ``MXM_VST3_PATH_ONLY`` and ``VST3_PATH`` like MXM; *extra_paths*
    (bundles or directories) are scanned too.
    """
    bundles: list[Path] = []
    seen: set[str] = set()
    if not path_only_enabled():
        for directory in standard_vst3_dirs():
            if directory.is_dir():
                _collect(directory, bundles, seen)
    raw = _vst3_path_entries(os.environ.get("VST3_PATH", ""))
    for entry in [str(part).strip() for part in (raw + list(extra_paths or []))]:
        if entry:
            _add_bundles_from_path(Path(entry), bundles, seen)
    return bundles


def _probe_bundle(bundle: Path) -> dict:
    """Introspect one bundle in an isolated subprocess and parse its JSON."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "lmms_mcp.vst3_probe", str(bundle)],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"module": str(bundle), "error": f"{type(exc).__name__}: {exc}"}
    for line in proc.stdout.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    message = (proc.stderr or proc.stdout).strip()
    return {"module": str(bundle), "error": message or f"probe exited {proc.returncode}"}


def discover_vst3_plugins(
    bundles: list[Path] | None = None,
    probe=_probe_bundle,
) -> list[dict]:
    """Discover the VST3 audio module classes MXM can host.

    Returns one descriptor per class (``name``, ``vendor``, ``module``,
    ``cid``, ``is_instrument``, ``sub_categories``, ``version``,
    ``class_flags``), keeping only ``Audio Module Class`` entries like
    ``Vst3Manager``. *probe* is injectable for tests.
    """
    if not native_vst3_discovery_supported():
        return []
    if bundles is None:
        bundles = find_vst3_bundles()
    plugins: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for bundle in bundles:
        result = probe(bundle)
        module = result.get("module", str(bundle))
        for desc in result.get("classes") or []:
            cid = normalize_cid(desc.get("cid", ""))
            if (desc.get("category") != _AUDIO_MODULE or not is_valid_cid(cid)
                    or (module, cid) in seen):
                continue
            seen.add((module, cid))
            sub = desc.get("sub_categories", "")
            plugins.append({
                "name": desc.get("name", ""),
                "vendor": desc.get("vendor", ""),
                "module": module,
                "cid": cid,
                "is_instrument": sub.startswith("Instrument"),
                "sub_categories": sub,
                "version": desc.get("version", ""),
                "class_flags": desc.get("class_flags", 0),
            })
    plugins.sort(key=lambda p: (p["name"].lower(), p["module"]))
    return plugins


def resolve_vst3_instrument(
    plugins: list[dict],
    plugin_name: str = "",
    module: str = "",
    cid: str = "",
    instruments_only: bool = False,
) -> tuple[dict | None, list[dict]]:
    """Resolve a request to one descriptor as ``(descriptor, candidates)``.

    An explicit ``module`` + ``cid`` matches exactly. Otherwise *plugin_name*
    is matched exactly, then as a substring; *instruments_only* ignores effect
    classes. On success candidates is empty; on ambiguity/failure descriptor
    is None and candidates holds the closest matches.
    """
    if module and cid:
        cid, module = normalize_cid(cid), os.path.abspath(module)
        return next(
            (p for p in plugins
             if p["cid"] == cid and os.path.abspath(p["module"]) == module),
            None,
        ), []
    query = plugin_name.strip().lower()
    if not query:
        return None, []
    pool = [p for p in plugins if p.get("is_instrument")] if instruments_only else plugins
    exact = [p for p in pool if p["name"].lower() == query]
    if len(exact) == 1:
        return exact[0], []
    if exact:
        return None, exact
    matches = [p for p in pool if query in p["name"].lower()]
    return (matches[0], []) if len(matches) == 1 else (None, matches)
