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
import signal
import subprocess
import sys
import threading
from pathlib import Path

from . import discovery_cache
from . import lmms_app
from . import vst3_platform as plat
from .path_safety import resolved_path_within

#! MXM enables path-only mode on mere presence (std::getenv), so test for the
#! key, not its truthiness.
PATH_ONLY_ENV = "MXM_VST3_PATH_ONLY"

_PROBE_TIMEOUT_SECONDS = 30
_PROBE_OUTPUT_LIMIT = 4 * 1024 * 1024
_MAX_DISCOVERY_ENTRIES = 100_000
_MAX_DISCOVERY_BUNDLES = 4_096
_MAX_SEARCH_ROOTS = 4_096
_AUDIO_MODULE = "Audio Module Class"
_CID_RE = re.compile(r"^[0-9A-Fa-f]{32}$")


def native_vst3_discovery_supported() -> bool:
    """Whether this MCP can discover and introspect VST3 plugins here."""
    return plat.discovery_supported()


def path_only_enabled() -> bool:
    return PATH_ONLY_ENV in os.environ


def is_valid_cid(cid: str) -> bool:
    return bool(_CID_RE.match(cid.strip()))


def normalize_cid(cid: str) -> str:
    return cid.strip().upper()


def _app_vst3_dir() -> Path | None:
    """Application ``vst3`` dir, resolved like MXM's app-level scan."""
    return plat.app_dir_for_exe(lmms_app.find_mxm_binary())


def standard_vst3_dirs() -> list[Path]:
    """Standard ``.vst3`` locations, in MXM's order (per platform)."""
    return plat.standard_dirs(_app_vst3_dir())


def _vst3_path_entries(raw: str) -> list[str]:
    return [part for part in raw.split(os.pathsep) if part]


def effective_search_paths() -> list[str]:
    """Directories actually scanned (honours path-only), for reporting."""
    paths = [str(d) for d in standard_vst3_dirs()] if not path_only_enabled() else []
    return (paths + _vst3_path_entries(os.environ.get("VST3_PATH", "")))[:_MAX_SEARCH_ROOTS]


def _collect(directory, result, seen, dirs_only=False, visited=None, allowed_root=None,
             scan_state=None):
    """Recursively collect ``.vst3`` entries (case-sensitive, like MXM).

    ``dirs_only`` mirrors MXM's ``QDir::Dirs`` scan for ``VST3_PATH``; the
    standard locations use MXM's extension-based ``findFilesWithExt`` instead.
    A ``.vst3`` entry is never descended into.
    """
    visited = set() if visited is None else visited
    directory = Path(directory)
    allowed_root = Path(directory).resolve() if allowed_root is None else allowed_root
    scan_state = {"entries": 0} if scan_state is None else scan_state
    try:
        real_path = str(Path(directory).resolve(strict=True))
        if real_path in visited:
            return
        visited.add(real_path)
        entries = sorted(directory.iterdir())
    except (OSError, RuntimeError):
        return
    for entry in entries:
        scan_state["entries"] += 1
        if (scan_state["entries"] > _MAX_DISCOVERY_ENTRIES
                or len(result) >= _MAX_DISCOVERY_BUNDLES):
            return
        try:
            is_link = entry.is_symlink()
            if is_link:
                resolved = resolved_path_within(entry, allowed_root)
                if resolved is None:
                    continue
                real_parent = entry.parent.resolve()
                if resolved == real_parent or resolved in real_parent.parents:
                    continue
            else:
                resolved = entry
            is_dir = resolved.is_dir()
            is_file = resolved.is_file()
        except (OSError, RuntimeError):
            continue
        if entry.suffix == ".vst3" and (is_dir or (is_file and not dirs_only)):
            key = str(resolved)
            if key not in seen:
                seen.add(key)
                result.append(resolved)
        elif is_dir and not is_link:
            _collect(resolved, result, seen, dirs_only, visited, allowed_root, scan_state)


def _add_bundles_from_path(path, result, seen, scan_state=None):
    """Collect from one ``VST3_PATH``/extra entry (verbatim bundle, else dir)."""
    path = Path(os.path.abspath(path))
    if not path.exists():
        return
    if path.suffix == ".vst3":
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return
        key = str(resolved)
        if key not in seen and len(result) < _MAX_DISCOVERY_BUNDLES:
            seen.add(key)
            result.append(resolved)
    elif path.is_dir():
        try:
            root = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return
        _collect(root, result, seen, dirs_only=True, allowed_root=root,
                 scan_state=scan_state)


def find_vst3_bundles(extra_paths: list[str] | None = None) -> list[Path]:
    """The ``.vst3`` bundles MXM's native host can discover.

    Honours ``MXM_VST3_PATH_ONLY`` and ``VST3_PATH`` like MXM; *extra_paths*
    (bundles or directories) are scanned too.
    """
    bundles: list[Path] = []
    seen: set[str] = set()
    scan_state = {"entries": 0}
    if not path_only_enabled():
        for directory in standard_vst3_dirs()[:_MAX_SEARCH_ROOTS]:
            if len(bundles) >= _MAX_DISCOVERY_BUNDLES:
                break
            if directory.is_dir():
                try:
                    root = directory.resolve(strict=True)
                except (OSError, RuntimeError):
                    continue
                _collect(root, bundles, seen, allowed_root=root, scan_state=scan_state)
    raw = _vst3_path_entries(os.environ.get("VST3_PATH", ""))
    extra = list(extra_paths or [])
    for entry in [str(part).strip() for part in (raw + extra)[:_MAX_SEARCH_ROOTS]]:
        if len(bundles) >= _MAX_DISCOVERY_BUNDLES:
            break
        if entry:
            _add_bundles_from_path(Path(entry), bundles, seen, scan_state)
    return bundles


def _terminate_probe_process(process: subprocess.Popen) -> None:
    """Stop the probe and, on POSIX, its subprocess group."""
    try:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass


def _run_capped_probe(command: list[str], output_limit: int, timeout: float):
    """Run a probe while bounding captured stdout and stderr independently."""
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name != "nt",
        creationflags=creationflags,
    )
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    totals = {"stdout": 0, "stderr": 0}
    exceeded = threading.Event()
    discard_capture = threading.Event()

    def drain(name: str, pipe) -> None:
        target = captured[name]
        try:
            while True:
                chunk = pipe.read(64 * 1024)
                if not chunk:
                    return
                if totals[name] + len(chunk) > output_limit:
                    if not exceeded.is_set():
                        exceeded.set()
                        _terminate_probe_process(process)
                    return
                totals[name] = min(output_limit + 1, totals[name] + len(chunk))
                if exceeded.is_set():
                    return
                if not discard_capture.is_set():
                    target.extend(chunk)
        except (OSError, ValueError):
            return
        finally:
            pipe.close()

    readers = [
        threading.Thread(target=drain, args=(name, pipe), daemon=True)
        for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr))
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_probe_process(process)
        process.wait()

    # Normally process exit closes both pipes. A hostile plugin could leave a
    # descendant holding one open; do not let that pin this MCP request forever.
    for reader in readers:
        reader.join(timeout=1.0)
    if any(reader.is_alive() for reader in readers):
        # A child may have inherited a pipe after the probe exited. Stop
        # retaining output and close our read ends rather than leaking memory.
        discard_capture.set()
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
    for reader in readers:
        reader.join(timeout=0.1)
    return (bytes(captured["stdout"]), bytes(captured["stderr"]), process.returncode,
            timed_out, exceeded.is_set())


def _probe_bundle(bundle: Path) -> dict:
    """Introspect one user-approved bundle in a crash-isolated process."""
    try:
        stdout, stderr, returncode, timed_out, output_exceeded = _run_capped_probe(
            [sys.executable, "-m", "lmms_mcp.vst3_probe", str(bundle)],
            _PROBE_OUTPUT_LIMIT, _PROBE_TIMEOUT_SECONDS,
        )
    except OSError as exc:
        return {"module": str(bundle), "error": f"{type(exc).__name__}: {exc}"}
    if timed_out:
        return {"module": str(bundle), "error": "VST3 probe timed out"}
    if output_exceeded:
        return {"module": str(bundle), "error": "VST3 probe output limit exceeded"}
    stdout_text = stdout.decode("utf-8", "replace")
    stderr_text = stderr.decode("utf-8", "replace")
    for line in stdout_text.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    message = (stderr_text or stdout_text).strip()
    return {"module": str(bundle), "error": message or f"probe exited {returncode}"}


def discover_vst3_plugins(
    bundles: list[Path] | None = None,
    probe=None,
    refresh: bool = False,
) -> list[dict]:
    """Discover the VST3 audio module classes MXM can host.

    Returns one descriptor per class (``name``, ``vendor``, ``module``,
    ``cid``, ``is_instrument``, ``sub_categories``, ``version``,
    ``class_flags``), keeping only ``Audio Module Class`` entries like
    ``Vst3Manager``. *probe* is injectable for tests.
    """
    if not native_vst3_discovery_supported():
        return []
    probe = probe or _probe_bundle
    if bundles is None and probe is _probe_bundle:
        paths = effective_search_paths()
        return discovery_cache.cached_discovery(
            "vst3.plugins", paths,
            lambda: _discover_vst3_plugins(find_vst3_bundles(), probe),
            refresh=refresh,
        )
    if bundles is None:
        bundles = find_vst3_bundles()
    return _discover_vst3_plugins(bundles, probe)


def _discover_vst3_plugins(bundles: list[Path], probe) -> list[dict]:
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
