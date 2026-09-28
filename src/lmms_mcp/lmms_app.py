"""Detect the installed LMMS application and its available plugins.

The MCP server manipulates project files directly without launching
LMMS. However, knowing which plugins the installed LMMS actually ships
prevents generating projects with missing-plugin warnings.

LMMS 1.2.x and 1.3.x differ in plugin availability, e.g.:
- SlicerT, Xpressive: 1.3+ only
- Compressor, Dispersion, FrequencyShifter, SlewDistortion effects: 1.3+ only
"""

import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

_CANDIDATE_EXES = [
    Path(os.environ.get("LMMS_EXECUTABLE", "")) if os.environ.get("LMMS_EXECUTABLE") else None,
    Path("C:/Program Files/LMMS/lmms.exe"),
    Path("C:/Program Files (x86)/LMMS/lmms.exe"),
    Path.home() / "AppData/Local/Programs/LMMS/lmms.exe",
    Path.home() / ".local/bin/lmms",
]


def find_lmms_exe() -> Path | None:
    """Locate the installed LMMS executable."""
    for candidate in _CANDIDATE_EXES:
        if candidate is not None and candidate.is_file():
            return candidate
    return None


_MXM_CANDIDATE_EXES = [
    Path(os.environ.get("MXM_EXECUTABLE", "")) if os.environ.get("MXM_EXECUTABLE") else None,
    Path("/usr/local/bin/mxm"),
    Path("/usr/bin/mxm"),
    Path.home() / ".local" / "bin" / "mxm",
]


def find_mxm_exe() -> Path | None:
    """Locate the installed MXM executable (the LMMS fork with native VST3)."""
    for candidate in _MXM_CANDIDATE_EXES:
        if candidate is not None and candidate.is_file():
            return candidate
    return None


def get_plugins_dir() -> Path | None:
    """Return the plugins directory of the installed LMMS."""
    configured = os.environ.get("LMMS_PLUGIN_DIR")
    if configured and Path(configured).is_dir():
        return Path(configured)
    exe = find_lmms_exe()
    if exe is None:
        return None
    candidates = [exe.parent / "plugins"]
    if os.name != "nt":
        candidates.extend((Path(p) for p in (
            "/usr/lib/x86_64-linux-gnu/lmms",
            "/usr/lib/lmms",
            "/usr/local/lib/lmms",
        )))
    return next((p for p in candidates if p.is_dir()), None)


def get_installed_plugins() -> set[str]:
    """Set of plugin library names shipped with the installed LMMS.

    Names are lowercase LMMS plugin basenames (e.g. "tripleoscillator",
    "reverbsc", "carlarack"). Returns an empty set if LMMS is not found.
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


def get_lmms_version() -> str | None:
    """Version string of the installed LMMS, or None.

    Keep prerelease and build components because LMMS writes the complete
    application version into a project's creator metadata.
    """
    exe = find_lmms_exe()
    if exe is None:
        return None
    try:
        out = subprocess.run(
            [str(exe), "--version"],
            capture_output=True, text=True, timeout=10,
        )
        match = re.search(
            r"\bLMMS\s+"
            r"(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)",
            out.stdout + out.stderr,
        )
        return match.group(1) if match else None
    except (OSError, subprocess.TimeoutExpired):
        return None


# Known aliases across versions (XML name -> possible DLL names).
# Some instruments are statically linked into lmms.exe and have no DLL;
# those are listed in STATIC_PLUGINS.
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


def get_lmms_build_options() -> dict[str, bool]:
    """Return boolean LMMS build options reported by ``--version``."""
    exe = find_lmms_exe()
    if exe is None:
        return {}
    try:
        proc = subprocess.run(
            [str(exe), "--version"], capture_output=True, text=True, timeout=10,
        )
        out = proc.stdout + proc.stderr
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return {
        name.lower(): value.upper() in {"TRUE", "ON", "1"}
        for name, value in re.findall(
            r"(?:LMMS_|WANT_)([A-Z0-9_]+)=?'?([A-Z0-9]+)'?", out
        )
    }


def lmms_supports_carla() -> bool:
    """Whether LMMS exposes its Carla instrument plugins."""
    installed = get_installed_plugins()
    if {"carlarack", "carlapatchbay"} <= installed:
        return True
    options = get_lmms_build_options()
    return options.get("carla", False) or options.get("weakcarla", False)


@lru_cache(maxsize=1)
def _mxm_build_options_cached() -> tuple[tuple[str, bool], ...]:
    exe = find_mxm_exe()
    if exe is None:
        return ()
    try:
        proc = subprocess.run(
            [str(exe), "--version"], capture_output=True, text=True, timeout=10,
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
    """Return boolean build options reported by the MXM binary's ``--version``.

    The subprocess result is cached (the installed binary does not change
    during a session) but a fresh dict is returned so callers cannot mutate
    the cache.
    """
    return dict(_mxm_build_options_cached())


def clear_mxm_build_options_cache() -> None:
    """Drop the cached MXM ``--version`` result (used by tests)."""
    _mxm_build_options_cached.cache_clear()


def mxm_supports_native_vst3() -> bool:
    """Whether the installed MXM was built with native VST3 hosting."""
    options = get_mxm_build_options()
    return bool(options.get("have_vst3") or options.get("vst3"))


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
    """Check whether a plugin exists in the installed LMMS.

    Returns (available, reason). If no LMMS installation is detected,
    everything is considered available (cannot verify).
    """
    name = plugin_name.lower()
    if name in {"carlarack", "carlapatchbay"} and lmms_supports_carla():
        return True, "installed"
    if name in STATIC_PLUGINS:
        return True, "built-in"
    installed = get_installed_plugins()
    if not installed:
        return True, "LMMS installation not found - cannot verify"
    if name in installed:
        return True, "installed"
    if any(alias in installed for alias in PLUGIN_ALIASES.get(name, {name})):
        return True, "installed (alias)"
    return False, (
        f"'{plugin_name}' is not included in your installed LMMS "
        f"(found {len(installed)} plugins). It may require a newer "
        f"LMMS version."
    )


def classify_installed_plugins(
    known_instruments: set[str],
    known_effects: set[str],
) -> dict[str, list[str]]:
    """Classify all installed plugin DLLs by comparing with known names.

    Returns dict with keys "instruments", "effects", "unknown".
    Unknown DLLs are custom/newer plugins the user added - they can be
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
    VST effect or instrument - LMMS decides on load.
    """
    root = Path(directory)
    if not root.is_dir():
        raise ValueError(f"Directory not found: {directory}")
    it = root.rglob("*.dll") if recursive else root.glob("*.dll")
    skip = {"lmms.exe", "remotevstplugin.exe", "32bitvsthelper.exe"}
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
