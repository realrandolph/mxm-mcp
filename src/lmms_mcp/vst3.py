"""Discovery and selection of native VST3 plugins for MXM.

MXM (this project's LMMS fork) hosts VST3 natively through its
``vst3instrument`` plugin. This module finds the VST3 bundles MXM would find
and exposes the identity MXM needs to host them:

* ``module`` - absolute path of the ``.vst3`` bundle.
* ``cid``    - 32 upper-case hex class id (``Vst3Manager::Descriptor::cid``).

Both must match a descriptor discovered by MXM for the project to load, so
they are reported verbatim from the module's own factory through
:mod:`lmms_mcp.vst3_probe`.

This is deliberately separate from the Carla path. Carla is a third-party
host that loads VST3/CLAP/LV2 plugins through its own bridges; the tools built
on this module use MXM's built-in VST3 host and never touch Carla.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from . import lmms_app

#! Discovery must match MXM's Vst3Manager::discover(). When this environment
#! variable is set, MXM ignores the standard locations below and uses only
#! VST3_PATH; the same restriction is honoured here.
PATH_ONLY_ENV = "MXM_VST3_PATH_ONLY"

#! Timeout for probing a single bundle in its own subprocess.
_PROBE_TIMEOUT_SECONDS = 30

_CID_RE = re.compile(r"^[0-9A-Fa-f]{32}$")


def is_valid_cid(cid: str) -> bool:
    """Whether *cid* is a well-formed VST3 class id (32 hex characters)."""
    return bool(_CID_RE.match(cid.strip()))


def normalize_cid(cid: str) -> str:
    """Return the canonical (upper-case) form of a VST3 class id."""
    return cid.strip().upper()


def _app_vst3_dir() -> Path | None:
    """The application-level ``vst3`` directory MXM also scans."""
    exe = lmms_app.find_mxm_exe() or lmms_app.find_lmms_exe()
    if exe is None:
        return None
    candidate = exe.parent / "vst3"
    return candidate if candidate.is_dir() else None


def standard_vst3_dirs() -> list[Path]:
    """The standard paths MXM scans for VST3 bundles (in MXM's order)."""
    directories: list[Path] = []
    home = os.environ.get("HOME")
    if home:
        directories.append(Path(home) / ".vst3")
    for base in (Path("/usr"), Path("/usr/local")):
        directories.append(base / "lib64" / "vst3")
        directories.append(base / "lib" / "vst3")
    app = _app_vst3_dir()
    if app is not None:
        directories.append(app)
    return directories


def _add_bundles_from_path(path: Path, result: list[Path], seen: set[str]) -> None:
    """Append ``.vst3`` bundles found at/under *path* (mirrors MXM)."""
    if not path.exists():
        return
    if path.is_dir() and path.suffix.lower() == ".vst3":
        candidates = [path]
    elif path.is_dir():
        candidates = sorted(p for p in path.rglob("*.vst3") if p.is_dir())
    elif path.is_file() and path.suffix.lower() == ".vst3":
        candidates = [path]
    else:
        return
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            result.append(candidate)


def find_vst3_bundles(extra_paths: list[str] | None = None) -> list[Path]:
    """Return the ``.vst3`` bundles MXM's native host can discover.

    Honours ``MXM_VST3_PATH_ONLY`` and ``VST3_PATH`` exactly like MXM, and
    accepts *extra_paths* (bundle files or directories) for callers that want
    to scan a specific location.
    """
    bundles: list[Path] = []
    seen: set[str] = set()

    if not os.environ.get(PATH_ONLY_ENV):
        for directory in standard_vst3_dirs():
            if directory.is_dir():
                for entry in sorted(directory.iterdir()):
                    if entry.suffix.lower() == ".vst3" and entry.is_dir():
                        key = str(entry)
                        if key not in seen:
                            seen.add(key)
                            bundles.append(entry)

    raw_paths = []
    env_path = os.environ.get("VST3_PATH", "")
    if env_path:
        raw_paths.extend(env_path.split(os.pathsep))
    if extra_paths:
        raw_paths.extend(extra_paths)
    for raw in raw_paths:
        raw = raw.strip()
        if raw:
            _add_bundles_from_path(Path(raw), bundles, seen)

    return bundles


def _probe_runner() -> list[str]:
    """Command used to introspect one bundle in an isolated process."""
    return [sys.executable, "-m", "lmms_mcp.vst3_probe"]


def _probe_bundle(bundle: Path) -> dict:
    """Run the isolated prober for a single bundle and parse its JSON line."""
    try:
        proc = subprocess.run(
            [*_probe_runner(), str(bundle)],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"module": str(bundle), "error": f"{type(exc).__name__}: {exc}"}

    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
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

    Returns one descriptor per class with the keys ``name``, ``vendor``,
    ``module``, ``cid``, ``is_instrument``, ``sub_categories``, ``version`` and
    ``class_flags``. Only classes whose category is ``Audio Module Class`` are
    returned, matching MXM's ``Vst3Manager``. *probe* is injectable for tests.
    """
    if bundles is None:
        bundles = find_vst3_bundles()

    plugins: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for bundle in bundles:
        result = probe(bundle)
        module = result.get("module", str(bundle))
        if result.get("error") or not result.get("classes"):
            continue
        for description in result["classes"]:
            if description.get("category") != "Audio Module Class":
                continue
            cid = normalize_cid(description.get("cid", ""))
            if not is_valid_cid(cid):
                continue
            key = (module, cid)
            if key in seen:
                continue
            seen.add(key)
            sub_categories = description.get("sub_categories", "")
            plugins.append({
                "name": description.get("name", ""),
                "vendor": description.get("vendor", ""),
                "module": module,
                "cid": cid,
                "is_instrument": sub_categories.startswith("Instrument"),
                "sub_categories": sub_categories,
                "version": description.get("version", ""),
                "class_flags": description.get("class_flags", 0),
            })

    plugins.sort(key=lambda item: (item["name"].lower(), item["module"]))
    return plugins


def list_vst3_instruments(include_effects: bool = False) -> list[dict]:
    """Discovered VST3 plugins, instruments only unless *include_effects*."""
    plugins = discover_vst3_plugins()
    if include_effects:
        return plugins
    return [plugin for plugin in plugins if plugin["is_instrument"]]


def _find_exact(plugins: list[dict], module: str, cid: str) -> dict | None:
    normalized_module = os.path.abspath(module)
    normalized_cid = normalize_cid(cid)
    for plugin in plugins:
        if plugin["cid"] != normalized_cid:
            continue
        if os.path.abspath(plugin["module"]) == normalized_module:
            return plugin
    return None


def resolve_vst3_instrument(
    plugins: list[dict],
    plugin_name: str = "",
    module: str = "",
    cid: str = "",
) -> tuple[dict | None, list[dict]]:
    """Resolve a user request to a single discovered plugin descriptor.

    Returns ``(descriptor, candidates)``. On success *descriptor* is the
    chosen plugin and *candidates* is empty. On failure *descriptor* is None
    and *candidates* lists the closest matches (for a helpful error).
    """
    if module and cid:
        return _find_exact(plugins, module, cid), []
    if not plugin_name.strip():
        return None, []

    query = plugin_name.strip().lower()
    exact = [
        plugin for plugin in plugins
        if plugin["name"].lower() == query
    ]
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        return None, exact

    matches = [plugin for plugin in plugins if query in plugin["name"].lower()]
    if len(matches) == 1:
        return matches[0], []
    return None, matches
