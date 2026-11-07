"""Vendor patch-directory catalog and ``<file>#<program>`` bank addressing.

Third-party plugins keep their patches outside the VST3 preset convention, in
vendor trees that differ per platform. This module maps the installed patch
trees for Surge XT, Vital, Dexed, Decent Sampler, u-he, Cherry Audio and Native
Instruments content, and expands bank files into individually addressable
program entries.

A catalog entry either names a whole file (``preset.vstpreset``) or one program
inside a bank (``cartridge.syx#14``, ``bank.fxb#7``, ``presets.p8bank#3``). The
selector accepts a zero-based index or a case-insensitive program name.
"""

from __future__ import annotations

import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from . import preset_formats
from .path_safety import resolved_path_within

__all__ = [
    "CatalogEntry",
    "vendor_preset_dirs",
    "decent_sampler_libraries",
    "cataloged_entries",
    "preset_belongs_to",
    "split_bank_reference",
    "bank_programs",
    "resolve_bank_program",
    "catalog_roots_by_plugin",
]

_MAX_CATALOG_FILES = 20_000
_MAX_CATALOG_BYTES = 128 * 1024 * 1024
_MAX_WALK_DEPTH = 8
_MAX_BANK_BYTES = 64 * 1024 * 1024
_MACOS_LEFTOVERS = re.compile(r"^\._")


def squash(value: str) -> str:
    """Normalize ``"Serum 2"``/``"serum2"`` and ``"OB-Xd"``/``"obxd"``."""
    return "".join(char for char in value.casefold() if char.isalnum())


def _plugin_key(plugin: dict) -> str:
    name = str(plugin.get("name", "")).strip()
    key = squash(name)
    for suffix in ("vst3", "vst", "clap", "au"):
        if key.endswith(suffix) and len(key) > len(suffix):
            key = key[:-len(suffix)]
            break
    return key


def _home(environ: dict[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    return Path(environ.get("HOME") or environ.get("USERPROFILE") or str(Path.home()))


def _platform_name(platform: str | None) -> str:
    value = sys.platform if platform is None else platform
    if value == "win32" or value.startswith("win"):
        return "win32"
    if value == "darwin":
        return "darwin"
    return "linux"


def _program_data(environ: dict[str, str]) -> Path:
    return Path(environ.get("PROGRAMDATA") or "C:/ProgramData")


def _app_data(environ: dict[str, str], home: Path) -> Path:
    return Path(environ.get("APPDATA") or str(home / "AppData/Roaming"))


def _xdg_data(environ: dict[str, str], home: Path) -> Path:
    return Path(environ.get("XDG_DATA_HOME") or str(home / ".local/share"))


def _existing_or(first: Path, second: Path) -> Path:
    return first if first.exists() else second


_UHE_PRODUCTS = {
    "ace", "bazille", "colourcopy", "diva", "filterscape", "hive", "hive2",
    "presswerk", "repro1", "repro5", "satin", "twangstrom", "uhbik",
    "zebra", "zebra2", "zebralette", "zebralette2", "zebralette3",
}


def _is_uhe(plugin: dict, key: str) -> bool:
    vendor = squash(str(plugin.get("vendor", "")))
    return "uhe" in vendor or key in _UHE_PRODUCTS or key.startswith("zebralette")


def _surge_dirs(key: str, platform: str, environ: dict[str, str],
                home: Path) -> list[Path]:
    if platform == "darwin":
        root = Path("/Library/Application Support/Surge XT")
    elif platform == "win32":
        root = _program_data(environ) / "Surge XT"
    else:
        root = _existing_or(Path("/usr/share/surge-xt"),
                            Path("/usr/local/share/surge-xt"))
    if "surge" not in key:
        return []
    if "effect" in key or key.endswith("fx"):
        return [
            root / "fx_presets",
            home / "Documents/Surge XT/FX Presets",
        ]
    return [
        root / "patches_factory",
        root / "patches_3rdparty",
        home / "Documents/Surge XT/Patches",
    ]


def _vital_dirs(key: str, home: Path) -> list[Path]:
    if key != "vital":
        return []
    return [home / "Music/Vital", home / "Documents/Vital"]


def _dexed_dirs(key: str, platform: str, environ: dict[str, str],
                home: Path) -> list[Path]:
    if key != "dexed":
        return []
    if platform == "darwin":
        base = home / "Library/Application Support/DigitalSuburban/Dexed"
    elif platform == "win32":
        base = _app_data(environ, home) / "DigitalSuburban/Dexed"
    else:
        base = _xdg_data(environ, home) / "DigitalSuburban/Dexed"
    return [base / "Cartridges", home / "Documents/Dexed/Cartridges"]


def _decent_sampler_settings(platform: str, environ: dict[str, str],
                             home: Path) -> list[Path]:
    if platform == "darwin":
        return [home / "Music/Audio Music Apps/Decidedly/DecentSampler/DecentSampler.xml"]
    if platform == "win32":
        return [_app_data(environ, home) / "Decidedly/DecentSampler/DecentSampler.xml"]
    return [home / ".config/Decidedly/DecentSampler/DecentSampler.xml"]


def _decent_sampler_dirs(key: str, platform: str, environ: dict[str, str],
                         home: Path) -> list[Path]:
    if key != "decentsampler":
        return []
    dirs = [home / "Documents/Decent Sampler",
            home / "Library/Application Support/DecentSampler"]
    dirs.extend(decent_sampler_libraries(
        platform=platform, environ=environ, home=home))
    return dirs


def decent_sampler_libraries(
    *,
    platform: str | None = None,
    environ: dict[str, str] | None = None,
    home: Path | None = None,
) -> list[Path]:
    """Read Decent Sampler's ``sampleLibraryDirectory`` settings value."""
    environ = os.environ if environ is None else environ
    home = _home(environ) if home is None else Path(home)
    platform_name = _platform_name(platform)
    libraries: list[Path] = []
    for settings in _decent_sampler_settings(platform_name, environ, home):
        try:
            raw = settings.read_bytes()
        except OSError:
            continue
        if b"<!doctype" in raw.lower() or b"<!entity" in raw.lower():
            continue
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            continue
        for node in root.iter():
            if node.get("name") != "sampleLibraryDirectory":
                continue
            directory = (node.get("val") or node.text or "").strip()
            if directory:
                libraries.append(Path(directory))
    return libraries


def _uhe_dirs(plugin: dict, key: str, platform: str, environ: dict[str, str],
              home: Path) -> list[Path]:
    if not _is_uhe(plugin, key):
        return []
    name = str(plugin.get("name", "")).strip() or "u-he"
    alternate = name
    if key == "hive2":
        alternate = "Hive"
    dirs = [
        home / ".u-he" / name / "Presets",
        home / ".u-he" / name / "UserPresets",
        home / "Documents/u-he" / f"{name}.data" / "Presets",
        home / "Documents/u-he" / f"{name}.data" / "UserPresets",
        Path("/usr/share/u-he") / name / "Presets",
        Path("/usr/local/share/u-he") / name / "Presets",
        Path("/usr/share/u-he") / alternate / "Presets",
        Path("/usr/local/share/u-he") / alternate / "Presets",
    ]
    if platform == "darwin":
        dirs.extend([
            Path("/Library/Application Support/u-he") / name / "Presets",
            home / "Library/Application Support/u-he" / name / "Presets",
            home / "Library/Application Support/u-he" / name / "UserPresets",
        ])
    elif platform == "win32":
        dirs.extend([
            _program_data(environ) / "u-he" / name / "Presets",
            _app_data(environ, home) / "u-he" / name / "Presets",
        ])
    return dirs


def _cherry_dirs(plugin: dict, key: str, platform: str,
                 environ: dict[str, str], home: Path) -> list[Path]:
    vendor = squash(str(plugin.get("vendor", "")))
    if "cherry" not in vendor and not key.startswith(("dco", "mg1", "sem", "voltage")):
        return []
    name = str(plugin.get("name", "")).strip()
    if platform == "darwin":
        bases = [Path("/Library/Application Support/CherryAudio"),
                 home / "Library/Application Support/CherryAudio"]
    elif platform == "win32":
        bases = [_program_data(environ) / "CherryAudio",
                 _app_data(environ, home) / "CherryAudio"]
    else:
        bases = [home / ".config/CherryAudio", home / ".local/share/CherryAudio"]
    return [base / name for base in bases] + [home / "Documents/Cherry Audio" / name]


def _native_instruments_dirs(plugin: dict, platform: str,
                              environ: dict[str, str], home: Path) -> list[Path]:
    vendor = squash(str(plugin.get("vendor", "")))
    if "nativeinstruments" not in vendor and "ni" != vendor:
        return []
    dirs = [home / "Documents/Native Instruments",
            home / "Documents/Native Instruments/User Content"]
    if platform == "darwin":
        dirs.append(Path("/Users/Shared"))
    elif platform == "win32":
        public = Path(environ.get("PUBLIC") or "C:/Users/Public")
        dirs.append(public / "Documents/Native Instruments")
    else:
        dirs.append(home / ".local/share/Native Instruments")
    return dirs


def vendor_preset_dirs(
    plugin: dict,
    *,
    platform: str | None = None,
    environ: dict[str, str] | None = None,
    home: Path | None = None,
) -> list[Path]:
    """Return the installed patch trees that belong to *plugin*.

    Paths are candidates: entries that do not exist are harmless and are
    skipped by discovery. The function is pure with respect to the injected
    platform/environment so it can be tested on any host.
    """
    environ = os.environ if environ is None else environ
    home = _home(environ) if home is None else Path(home)
    platform_name = _platform_name(platform)
    key = _plugin_key(plugin)
    dirs: list[Path] = []
    dirs.extend(_surge_dirs(key, platform_name, environ, home))
    dirs.extend(_vital_dirs(key, home))
    dirs.extend(_dexed_dirs(key, platform_name, environ, home))
    dirs.extend(_decent_sampler_dirs(key, platform_name, environ, home))
    dirs.extend(_uhe_dirs(plugin, key, platform_name, environ, home))
    dirs.extend(_cherry_dirs(plugin, key, platform_name, environ, home))
    dirs.extend(_native_instruments_dirs(plugin, platform_name, environ, home))
    unique: list[Path] = []
    seen: set[str] = set()
    for directory in dirs:
        normalized = os.path.normpath(os.path.abspath(str(directory)))
        if normalized not in seen:
            seen.add(normalized)
            unique.append(Path(normalized))
    return unique


def catalog_roots_by_plugin(
    plugins: Iterable[dict],
    *,
    platform: str | None = None,
    environ: dict[str, str] | None = None,
    home: Path | None = None,
) -> list[tuple[Path, dict]]:
    """Map each vendor patch root to the plugin record that owns it."""
    roots: list[tuple[Path, dict]] = []
    seen: set[tuple[str, str]] = set()
    for plugin in plugins:
        for directory in vendor_preset_dirs(
                plugin, platform=platform, environ=environ, home=home):
            identity = (str(directory), str(plugin.get("module", "")))
            if identity in seen:
                continue
            seen.add(identity)
            roots.append((directory, plugin))
    return roots


@dataclass(frozen=True)
class CatalogEntry:
    """One addressable preset: a whole file or one program in a bank."""

    path: Path
    format: str
    plugin_name: str
    name: str
    category: str = ""
    bank: str = ""
    selector: int | None = None
    program_name: str = ""

    @property
    def source(self) -> str:
        if self.selector is None:
            return str(self.path)
        return f"{self.path}#{self.selector}"


def _extension(path: Path) -> str:
    return path.suffix.lower()


def preset_belongs_to(plugin: dict, path: str | Path, format_name: str) -> bool:
    """Whether a preset file format can be loaded by *plugin*."""
    key = _plugin_key(plugin)
    if format_name in {"serum"}:
        return "serum" in key and "fx" not in key
    if format_name == "serumfx":
        return key in {"serum2fx", "serumfx"}
    if format_name == "dx7":
        return key == "dexed"
    if format_name == "juce-valuetree":
        return "odin" in key
    if format_name == "juce-xml":
        return key == "vaporizer2"
    if format_name == "decentsampler":
        return key == "decentsampler"
    if format_name == "soundbox":
        return key == "soundbox"
    if format_name == "cherry":
        suffix = _extension(Path(path))
        if suffix == ".dco106preset":
            return key in {"dco106", "cherryaudiodco106"}
        if suffix == ".mg1preset":
            return "mg1" in key
        if suffix == ".sempreset":
            return key.startswith("sem")
        if suffix == ".voltagepreset":
            return "voltagemodular" in key
        return "cherry" in squash(str(plugin.get("vendor", "")))
    if format_name == "synplant":
        return key == "synplant"
    if format_name == "echobode":
        return key == "echobode"
    if format_name == "permut8":
        return key == "permut8"
    if format_name in {"fxp", "nksf"}:
        return True
    if format_name == "h2p":
        target = _h2p_target(Path(path))
        if not target:
            return True
        return squash(target) == key
    return True


def _h2p_target(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            head = stream.read(4096)
    except OSError:
        return ""
    match = re.search(rb"^#AM=([^\r\n]+)", head, re.MULTILINE)
    return match.group(1).decode("utf-8", "ignore").strip() if match else ""


def split_bank_reference(reference: str | Path) -> tuple[str, str | None]:
    """Split ``cartridge.syx#14`` into its file path and program selector."""
    return preset_formats.split_bank_reference(reference)


def bank_programs(path: str | Path, data: bytes | None = None) -> list[preset_formats.BankProgram]:
    """Programs addressable inside a bank file."""
    return preset_formats.bank_programs(path, data)


def resolve_bank_program(
    programs: list[preset_formats.BankProgram], selector: str | int,
) -> preset_formats.BankProgram:
    """Resolve a zero-based index or a case-insensitive program name."""
    return preset_formats.resolve_bank_program(programs, selector)


def _walk(root: Path, *, max_depth: int = _MAX_WALK_DEPTH) -> Iterable[Path]:
    """Yield regular files under *root*, following only in-root links."""
    try:
        resolved_root = root.resolve(strict=True)
    except (OSError, RuntimeError):
        return
    if not resolved_root.is_dir():
        if resolved_root.is_file():
            yield resolved_root
        return
    stack: list[tuple[Path, int]] = [(resolved_root, 0)]
    visited: set[str] = set()
    while stack:
        directory, depth = stack.pop()
        try:
            real = str(directory.resolve())
            if real in visited:
                continue
            visited.add(real)
            entries = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
        except (OSError, RuntimeError):
            continue
        for entry in entries:
            if _MACOS_LEFTOVERS.match(entry.name):
                continue
            try:
                resolved = resolved_path_within(entry, resolved_root)
                if resolved is None:
                    continue
                if resolved.is_dir():
                    if depth < max_depth:
                        stack.append((resolved, depth + 1))
                elif resolved.is_file():
                    yield resolved
            except (OSError, RuntimeError):
                continue


def _read_head(path: Path, limit: int = 4096) -> bytes:
    try:
        with path.open("rb") as stream:
            return stream.read(limit)
    except OSError:
        return b""


def cataloged_entries(
    plugin: dict,
    *,
    dirs: Iterable[str | Path] | None = None,
    platform: str | None = None,
    environ: dict[str, str] | None = None,
    home: Path | None = None,
    max_files: int = _MAX_CATALOG_FILES,
    max_bytes: int = _MAX_CATALOG_BYTES,
) -> list[CatalogEntry]:
    """Enumerate a plugin's vendor patch files and bank programs."""
    roots = (list(dirs) if dirs is not None else
             vendor_preset_dirs(plugin, platform=platform, environ=environ, home=home))
    plugin_name = str(plugin.get("name", "")).strip()
    entries: list[CatalogEntry] = []
    seen: set[str] = set()
    total_bytes = 0
    for root in roots:
        for path in _walk(Path(root)):
            if len(entries) >= max_files:
                return entries
            extension = _extension(path)
            if extension not in preset_formats.SUPPORTED_EXTENSIONS:
                continue
            format_name = preset_formats.detect_format(path, _read_head(path))
            if not preset_belongs_to(plugin, path, format_name):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if total_bytes + size > max_bytes:
                return entries
            total_bytes += size
            identity = str(path.resolve())
            if identity in seen:
                continue
            seen.add(identity)
            category = path.parent.name
            if extension in preset_formats.BANK_EXTENSIONS:
                try:
                    if size > _MAX_BANK_BYTES:
                        continue
                    programs = bank_programs(path)
                except (OSError, preset_formats.PresetFormatError):
                    programs = []
                if programs:
                    for program in programs:
                        entries.append(CatalogEntry(
                            path=path,
                            format=format_name,
                            plugin_name=plugin_name,
                            name=program.name or f"{path.stem} #{program.index}",
                            category=path.stem,
                            bank=path.stem,
                            selector=program.index,
                            program_name=program.name,
                        ))
                    continue
            entries.append(CatalogEntry(
                path=path,
                format=format_name,
                plugin_name=plugin_name,
                name=path.stem,
                category=category,
                bank=path.parent.name,
            ))
    return entries
