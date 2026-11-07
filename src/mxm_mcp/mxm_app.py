"""Detect the installed MXM application and its available plugins.

MXM is the DAW this MCP targets. The server manipulates LMMS-format
project files directly without launching MXM. However, knowing which
plugins the installed MXM actually ships prevents generating projects
with missing-plugin warnings, and the MXM binary is used for version
inspection and headless rendering.

Builds of the LMMS lineage differ in plugin availability, e.g.:
- SlicerT, Xpressive: 1.3+ only
- Compressor, Dispersion, FrequencyShifter, SlewDistortion effects: 1.3+ only
"""

import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

_MXM_CANDIDATE_BINARIES = [
    Path.home() / ".local" / "bin" / "mxm",
    Path("/usr/local/bin/mxm"),
    Path("/usr/bin/mxm"),
    Path("C:/Program Files/MXM/mxm.exe"),
    Path("C:/Program Files (x86)/MXM/mxm.exe"),
    Path.home() / "AppData/Local/Programs/MXM/mxm.exe",
    Path("/Applications/MXM.app/Contents/MacOS/mxm"),
    Path.home() / "Applications/MXM.app/Contents/MacOS/mxm",
]


def find_mxm_binary() -> Path | None:
    """Locate the installed MXM binary (the LMMS fork with native VST3).

    The configured ``MXM_EXECUTABLE`` wins, then the normal platform-specific
    MXM installation locations, then ``mxm`` on ``PATH``.
    """
    configured = os.environ.get("MXM_EXECUTABLE")
    if configured:
        candidate = Path(configured)
        if candidate.is_file():
            return candidate
    for candidate in _MXM_CANDIDATE_BINARIES:
        if candidate.is_file():
            return candidate
    found = shutil.which("mxm")
    return Path(found) if found else None


def get_plugins_dir() -> Path | None:
    """Return the plugins directory of the installed MXM application."""
    configured = os.environ.get("MXM_PLUGIN_DIR")
    if configured and Path(configured).is_dir():
        return Path(configured)
    binary = find_mxm_binary()
    if binary is None:
        return None
    exe = Path(os.path.realpath(binary))
    prefix = exe.parent.parent
    candidates = [
        exe.parent / "plugins",
        prefix / "lib" / "mxm",
        prefix / "lib64" / "mxm",
        prefix / "Resources" / "plugins",
    ]
    if os.name != "nt":
        candidates.extend((Path(p) for p in (
            "/usr/lib/x86_64-linux-gnu/mxm",
            "/usr/lib/mxm",
            "/usr/local/lib/mxm",
        )))
    return next((p for p in candidates if p.is_dir()), None)


def get_installed_plugins() -> set[str]:
    """Set of plugin library names shipped with the installed MXM.

    Names are lowercase LMMS plugin basenames (e.g. "tripleoscillator",
    "reverbsc", "carlarack"). Returns an empty set if MXM is not found.
    """
    plugins_dir = get_plugins_dir()
    if plugins_dir is None:
        return set()
    names = set()
    for f in plugins_dir.iterdir():
        if f.suffix.lower() not in {".dll", ".so", ".dylib"}:
            continue
        stem = f.stem.lower()
        if stem.startswith("lib"):
            stem = stem[3:]
        names.add(stem)
    return names


def get_mxm_version() -> str | None:
    """Version string of the installed MXM, or None.

    Keep prerelease and build components because MXM writes the complete
    application version into a project's creator metadata.
    """
    binary = find_mxm_binary()
    if binary is None:
        return None
    try:
        out = subprocess.run(
            [str(binary), "--version"],
            capture_output=True, text=True, timeout=10,
        )
        match = re.search(
            r"\bMXM\s+"
            r"(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)",
            out.stdout + out.stderr,
        )
        return match.group(1) if match else None
    except (OSError, subprocess.TimeoutExpired):
        return None


# Known aliases across versions (XML name -> possible DLL names).
# Some instruments are statically linked into the MXM binary and have no
# plugin library; those are listed in STATIC_PLUGINS.
STATIC_PLUGINS = {
    # Official Windows builds link FreeBoy into the main binary
    "freeboy",
}

PLUGIN_ALIASES = {
    "nes": {"nes", "papu", "nescaline"},
    "papu": {"nes", "papu", "nescaline"},
    "malletsstk": {"malletsstk", "stk"},
    "audiofileprocessor": {"audiofileprocessor"},
    "sf2player": {"sf2player", "fluidsynth"},
    "opulenz": {"opl2", "opulenz"},  # named OPL2 in LMMS 1.2.x
}


@lru_cache(maxsize=1)
def _mxm_build_options_cached() -> tuple[tuple[str, bool], ...]:
    binary = find_mxm_binary()
    if binary is None:
        return ()
    try:
        proc = subprocess.run(
            [str(binary), "--version"], capture_output=True, text=True, timeout=10,
        )
        out = proc.stdout + proc.stderr
    except (OSError, subprocess.TimeoutExpired):
        return ()
    options = {
        name.lower(): value.upper() in {"TRUE", "ON", "1"}
        for name, value in re.findall(
            r"(?:MXM_|WANT_)([A-Z0-9_]+)=?'?([A-Z0-9]+)'?", out
        )
    }
    return tuple(sorted(options.items()))


def get_mxm_build_options() -> dict[str, bool]:
    """MXM ``--version`` build options (cached; returns a fresh dict)."""
    return dict(_mxm_build_options_cached())


def clear_mxm_build_options_cache() -> None:
    _mxm_build_options_cached.cache_clear()


def mxm_supports_carla() -> bool:
    """Whether the installed MXM exposes its Carla instrument plugins."""
    installed = get_installed_plugins()
    if {"carlarack", "carlapatchbay"} <= installed:
        return True
    options = get_mxm_build_options()
    return any(options.get(name, False) for name in (
        "carla", "have_carla", "have_weakcarla",
    ))


def mxm_supports_native_vst3() -> bool:
    """Whether the installed MXM was built with native VST3 hosting.

    ``MXM_HAVE_VST3`` is the CMake define for the compiled host; ``WANT_VST3``
    only records that it was requested, so it must not count (MXM does not
    build the host on every platform).
    """
    return bool(get_mxm_build_options().get("have_vst3"))


def find_linux_plugins(directory: str | Path, recursive: bool = True) -> list[dict]:
    """Discover Linux VST3, CLAP, LV2, and LADSPA plugin files/bundles."""
    root = Path(directory)
    if not root.is_dir():
        raise ValueError(f"Directory not found: {directory}")
    results = []
    entries = root.rglob("*") if recursive else root.glob("*")
    seen = set()
    for path in sorted(entries):
        if any(parent.suffix.lower() in {".vst3", ".lv2"} for parent in path.parents):
            continue
        kind = None
        if path.is_dir() and path.suffix.lower() == ".vst3":
            kind = "vst3"
        elif path.is_file() and path.suffix.lower() == ".clap":
            kind = "clap"
        elif path.is_dir() and path.suffix.lower() == ".lv2":
            kind = "lv2"
        elif path.is_file() and path.suffix.lower() == ".so":
            kind = "ladspa"
        if kind and path not in seen:
            seen.add(path)
            item = {"name": path.stem, "path": str(path), "type": kind}
            if kind == "lv2":
                ttl_files = list(path.glob("*.ttl"))
                for ttl in ttl_files:
                    try:
                        text = ttl.read_text(errors="ignore")
                    except OSError:
                        continue
                    match = re.search(r"<([^>]+)>\s+a\s+lv2:Plugin", text)
                    if match:
                        item["plugin_id"] = match.group(1)
                        break
            results.append(item)
    return results


def check_plugin_available(plugin_name: str) -> tuple[bool, str]:
    """Check whether a plugin exists in the installed MXM.

    Returns (available, reason). If no MXM installation (or no MXM plugins
    directory) is detected, everything is considered available (cannot
    verify).
    """
    name = plugin_name.lower()
    if name in {"carlarack", "carlapatchbay"} and mxm_supports_carla():
        return True, "installed"
    if name in STATIC_PLUGINS:
        return True, "built-in"
    installed = get_installed_plugins()
    if not installed:
        return True, "MXM plugins not found - cannot verify"
    if name in installed:
        return True, "installed"
    if any(alias in installed for alias in PLUGIN_ALIASES.get(name, {name})):
        return True, "installed (alias)"
    return False, (
        f"'{plugin_name}' is not included in your installed MXM "
        f"(found {len(installed)} plugins). It may require a newer "
        f"build."
    )


def classify_installed_plugins(
    known_instruments: set[str],
    known_effects: set[str],
) -> dict[str, list[str]]:
    """Classify all installed plugin libraries by comparing with known names.

    Returns dict with keys "instruments", "effects", "unknown".
    Unknown libraries are custom/newer plugins the user added - they can be
    used but their type (instrument vs effect) is not known from the
    filename alone.
    """
    installed = get_installed_plugins()
    result: dict[str, list[str]] = {"instruments": [], "effects": [], "unknown": []}
    for name in sorted(installed):
        if name.startswith("lib"):
            continue  # support libraries, not plugins
        if name in known_instruments or any(
            name in aliases for aliases in PLUGIN_ALIASES.values()
        ) and name in known_instruments:
            result["instruments"].append(name)
        elif name in known_effects:
            result["effects"].append(name)
        else:
            result["unknown"].append(name)
    return result


def find_vst_plugins(directory: str | Path, recursive: bool = True) -> list[dict]:
    """Scan a directory for VST plugin DLLs.

    Returns list of dicts with name and path. Note: any .dll could be a
    VST effect or instrument - MXM decides on load.
    """
    root = Path(directory)
    if not root.is_dir():
        raise ValueError(f"Directory not found: {directory}")
    it = root.rglob("*.dll") if recursive else root.glob("*.dll")
    skip = {"mxm.exe", "remotevstplugin.exe", "32bitvsthelper.exe"}
    results = []
    for dll in sorted(it):
        if dll.stem.lower() in skip:
            continue
        results.append({
            "name": dll.stem,
            "path": str(dll),
            "size_kb": round(dll.stat().st_size / 1024),
        })
    return results
