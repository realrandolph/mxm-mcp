"""Discovery and selection of native VST3 plugins for MXM.

MXM (this project's LMMS fork) hosts VST3 natively through its
``vst3instrument`` plugin. This module finds the VST3 bundles MXM would find
and exposes the identity MXM needs to host them:

* ``module`` - path of the ``.vst3`` bundle (absolute for standard and
  ``VST3_PATH`` directory scans, matching MXM).
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

#! Discovery must match MXM's Vst3Manager::discover(). MXM checks the
#! *presence* of this variable (std::getenv), so an empty value still enables
#! path-only mode; test for presence, not truthiness.
PATH_ONLY_ENV = "MXM_VST3_PATH_ONLY"

#! Timeout for probing a single bundle in its own subprocess.
_PROBE_TIMEOUT_SECONDS = 30

_CID_RE = re.compile(r"^[0-9A-Fa-f]{32}$")


def native_vst3_discovery_supported() -> bool:
    """Whether this MCP can discover native VST3 plugins on this platform.

    MXM builds its native VST3 host for all platforms it targets, but the
    MCP's discovery/probe only understands the Linux bundle layout and
    ``UID::toString()`` byte order today. On other platforms discovery returns
    nothing; authors can still pass an explicit ``module_path`` + ``cid`` with
    ``allow_unverified``.
    """
    return sys.platform.startswith("linux")


def path_only_enabled() -> bool:
    """True when only ``VST3_PATH`` should be scanned (matches ``std::getenv``)."""
    return PATH_ONLY_ENV in os.environ


def is_valid_cid(cid: str) -> bool:
    """Whether *cid* is a well-formed VST3 class id (32 hex characters)."""
    return bool(_CID_RE.match(cid.strip()))


def normalize_cid(cid: str) -> str:
    """Return the canonical (upper-case) form of a VST3 class id."""
    return cid.strip().upper()


def _app_vst3_dir() -> Path | None:
    """The application-level ``vst3`` directory MXM also scans.

    MXM derives this from the resolved executable (``/proc/<pid>/exe``), so
    follow symlinks rather than using the literal launcher path.
    """
    exe = lmms_app.find_mxm_exe() or lmms_app.find_lmms_exe()
    if exe is None:
        return None
    candidate = Path(os.path.realpath(exe)).parent / "vst3"
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


def effective_search_paths() -> list[str]:
    """The directories actually scanned, for reporting.

    Honours ``MXM_VST3_PATH_ONLY`` (standard locations omitted) and appends the
    ``VST3_PATH`` entries, so the reported paths describe the real scan.
    """
    paths: list[str] = []
    if not path_only_enabled():
        paths.extend(str(directory) for directory in standard_vst3_dirs())
    env_path = os.environ.get("VST3_PATH", "")
    if env_path:
        paths.extend(part for part in env_path.split(os.pathsep) if part)
    return paths


def _append_bundle(path: Path, result: list[Path], seen: set[str]) -> None:
    key = str(path)
    if key not in seen:
        seen.add(key)
        result.append(path)


def _collect_bundles(directory: Path, result: list[Path], seen: set[str]) -> None:
    """Recursively collect ``.vst3`` entries under *directory*.

    Mirrors MXM's ``findFilesWithExt`` (used for the standard locations):
    descend into ordinary directories but treat a ``.vst3`` entry as a bundle
    and do not descend into it. Named after MXM, which matches by extension
    here, so ``.vst3`` files are included as well as bundles. The match is
    case-sensitive, like MXM's ``extension()``/``endsWith``.
    """
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.suffix == ".vst3":
            _append_bundle(entry, result, seen)
        elif entry.is_dir():
            _collect_bundles(entry, result, seen)


def _collect_bundle_dirs(directory: Path, result: list[Path], seen: set[str]) -> None:
    """Recursively collect ``.vst3`` *directories* under *directory*.

    Mirrors MXM's ``discoverPathOrDirectory`` (used for ``VST3_PATH``), which
    lists directories only (``QDir::Dirs``) and therefore never picks up a
    ``.vst3`` file.
    """
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        if not entry.is_dir():
            continue
        if entry.suffix == ".vst3":
            _append_bundle(entry, result, seen)
        else:
            _collect_bundle_dirs(entry, result, seen)


def _add_bundles_from_path(path: Path, result: list[Path], seen: set[str]) -> None:
    """Append ``.vst3`` bundles found at/under *path* (mirrors MXM).

    A path that is itself a ``.vst3`` entry is used verbatim; a directory is
    scanned with absolute paths (MXM uses ``QDir::absoluteFilePath``) and only
    descends into directories, keeping the recorded ``module`` string stable
    regardless of the process cwd.
    """
    if not path.exists():
        return
    if path.suffix == ".vst3":
        _append_bundle(path, result, seen)
        return
    if path.is_dir():
        _collect_bundle_dirs(Path(os.path.abspath(path)), result, seen)


def find_vst3_bundles(extra_paths: list[str] | None = None) -> list[Path]:
    """Return the ``.vst3`` bundles MXM's native host can discover.

    Honours ``MXM_VST3_PATH_ONLY`` and ``VST3_PATH`` exactly like MXM
    (recursive, absolute for directory scans), and accepts *extra_paths*
    (bundle files or directories) for callers that want to scan a specific
    location.
    """
    bundles: list[Path] = []
    seen: set[str] = set()

    if not path_only_enabled():
        for directory in standard_vst3_dirs():
            if directory.is_dir():
                _collect_bundles(directory, bundles, seen)

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
    if not native_vst3_discovery_supported():
        return []
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
    instruments_only: bool = False,
) -> tuple[dict | None, list[dict]]:
    """Resolve a user request to a single discovered plugin descriptor.

    When *instruments_only* is set, effect classes are ignored during name
    matching (the explicit ``module`` + ``cid`` path is unaffected).
    Returns ``(descriptor, candidates)``. On success *descriptor* is the
    chosen plugin and *candidates* is empty. On failure *descriptor* is None
    and *candidates* lists the closest matches (for a helpful error).
    """
    if module and cid:
        return _find_exact(plugins, module, cid), []
    if not plugin_name.strip():
        return None, []

    pool = (
        [plugin for plugin in plugins if plugin.get("is_instrument")]
        if instruments_only
        else plugins
    )
    query = plugin_name.strip().lower()
    exact = [
        plugin for plugin in pool
        if plugin["name"].lower() == query
    ]
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        return None, exact

    matches = [plugin for plugin in pool if query in plugin["name"].lower()]
    if len(matches) == 1:
        return matches[0], []
    return None, matches
