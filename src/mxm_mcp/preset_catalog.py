"""Vendor patch-directory catalog and ``<file>#<program>`` bank addressing."""

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
    "CatalogEntry", "vendor_preset_dirs", "decent_sampler_libraries", "cataloged_entries",
    "preset_belongs_to", "split_bank_reference", "bank_programs", "resolve_bank_program",
    "catalog_roots_by_plugin",
]

_MAX_CATALOG_FILES = 20_000
_MAX_CATALOG_BYTES = 128 * 1024 * 1024
_MAX_WALK_DEPTH = 8
_MAX_BANK_BYTES = 64 * 1024 * 1024
_MACOS_LEFTOVERS = re.compile(r"^\._")

_UHE_PRODUCTS = {
    "ace", "bazille", "colourcopy", "diva", "filterscape", "hive", "hive2",
    "presswerk", "repro1", "repro5", "satin", "twangstrom", "uhbik",
    "zebra", "zebra2", "zebralette", "zebralette2", "zebralette3",
}


def squash(value: str) -> str:
    return "".join(char for char in value.casefold() if char.isalnum())


def _plugin_key(plugin: dict) -> str:
    key = squash(str(plugin.get("name", "")).strip())
    for s in ("vst3", "vst", "clap", "au"):
        if key.endswith(s) and len(key) > len(s):
            return key[:-len(s)]
    return key


def _home(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env.get("HOME") or env.get("USERPROFILE") or str(Path.home()))


def _platform_name(platform: str | None) -> str:
    val = sys.platform if platform is None else platform
    return "win32" if "win" in val else ("darwin" if val == "darwin" else "linux")


def decent_sampler_libraries(
    *, platform: str | None = None, environ: dict[str, str] | None = None, home: Path | None = None,
) -> list[Path]:
    env = os.environ if environ is None else environ
    h = _home(env) if home is None else Path(home)
    plat = _platform_name(platform)
    cfgs = [h / "Music/Audio Music Apps/Decidedly/DecentSampler/DecentSampler.xml"] if plat == "darwin" else (
        [Path(env.get("APPDATA") or str(h / "AppData/Roaming")) / "Decidedly/DecentSampler/DecentSampler.xml"]
        if plat == "win32" else [h / ".config/Decidedly/DecentSampler/DecentSampler.xml"]
    )
    libs: list[Path] = []
    for cfg in cfgs:
        try:
            raw = cfg.read_bytes()
            if b"<!doctype" in raw.lower() or b"<!entity" in raw.lower():
                continue
            root = ET.fromstring(raw)
            for node in root.iter():
                if node.get("name") == "sampleLibraryDirectory":
                    dir_val = (node.get("val") or node.text or "").strip()
                    if dir_val:
                        libs.append(Path(dir_val))
        except (OSError, ET.ParseError):
            pass
    return libs


def vendor_preset_dirs(
    plugin: dict, *, platform: str | None = None, environ: dict[str, str] | None = None, home: Path | None = None,
) -> list[Path]:
    env = os.environ if environ is None else environ
    h = _home(env) if home is None else Path(home)
    plat = _platform_name(platform)
    key = _plugin_key(plugin)
    vendor = squash(str(plugin.get("vendor", "")))
    name = str(plugin.get("name", "")).strip()
    pdata = Path(env.get("PROGRAMDATA") or "C:/ProgramData")
    appdata = Path(env.get("APPDATA") or str(h / "AppData/Roaming"))
    xdg_data = Path(env.get("XDG_DATA_HOME") or str(h / ".local/share"))

    dirs: list[Path] = []
    if "surge" in key:
        roots = [Path("/Library/Application Support/Surge XT")] if plat == "darwin" else (
            [pdata / "Surge XT"] if plat == "win32" else [Path("/usr/share/surge-xt"), Path("/usr/local/share/surge-xt")]
        )
        if "effect" in key or key.endswith("fx"):
            dirs.extend([r / "fx_presets" for r in roots] + [h / "Documents/Surge XT/FX Presets"])
        else:
            for r in roots:
                dirs.extend([r / "patches_factory", r / "patches_3rdparty"])
            dirs.append(h / "Documents/Surge XT/Patches")
    if key == "vital":
        dirs.extend([h / "Music/Vital", h / "Documents/Vital"])
    if key == "dexed":
        base = (h / "Library/Application Support/DigitalSuburban/Dexed") if plat == "darwin" else (
            (appdata / "DigitalSuburban/Dexed") if plat == "win32" else (xdg_data / "DigitalSuburban/Dexed")
        )
        dirs.extend([base / "Cartridges", h / "Documents/Dexed/Cartridges"])
    if key == "decentsampler":
        dirs.extend([h / "Documents/Decent Sampler", h / "Library/Application Support/DecentSampler"] +
                    decent_sampler_libraries(platform=plat, environ=env, home=h))
    if "uhe" in vendor or key in _UHE_PRODUCTS or key.startswith("zebralette"):
        un = name or "u-he"
        alt = "Hive" if key == "hive2" else un
        dirs.extend([
            h / ".u-he" / un / "Presets", h / ".u-he" / un / "UserPresets",
            h / "Documents/u-he" / f"{un}.data" / "Presets", h / "Documents/u-he" / f"{un}.data" / "UserPresets",
            Path("/usr/share/u-he") / un / "Presets", Path("/usr/local/share/u-he") / un / "Presets",
            Path("/usr/share/u-he") / alt / "Presets", Path("/usr/local/share/u-he") / alt / "Presets",
        ])
        if plat == "darwin":
            dirs.extend([
                Path("/Library/Application Support/u-he") / un / "Presets",
                h / "Library/Application Support/u-he" / un / "Presets",
                h / "Library/Application Support/u-he" / un / "UserPresets",
            ])
        elif plat == "win32":
            dirs.extend([pdata / "u-he" / un / "Presets", appdata / "u-he" / un / "Presets"])
    if "cherry" in vendor or key.startswith(("dco", "mg1", "sem", "voltage")):
        bases = [Path("/Library/Application Support/CherryAudio"), h / "Library/Application Support/CherryAudio"] if plat == "darwin" else (
            [pdata / "CherryAudio", appdata / "CherryAudio"] if plat == "win32" else [h / ".config/CherryAudio", xdg_data / "CherryAudio"]
        )
        dirs.extend([b / name for b in bases] + [h / "Documents/Cherry Audio" / name])
    if "nativeinstruments" in vendor or vendor == "ni":
        dirs.extend([h / "Documents/Native Instruments", h / "Documents/Native Instruments/User Content"])
        if plat == "darwin":
            dirs.append(Path("/Users/Shared"))
        elif plat == "win32":
            dirs.append(Path(env.get("PUBLIC") or "C:/Users/Public") / "Documents/Native Instruments")
        else:
            dirs.append(xdg_data / "Native Instruments")

    unique: list[Path] = []
    seen: set[str] = set()
    for d in dirs:
        norm = os.path.normpath(os.path.abspath(str(d)))
        if norm not in seen:
            seen.add(norm)
            unique.append(Path(norm))
    return unique


def catalog_roots_by_plugin(
    plugins: Iterable[dict], *, platform: str | None = None, environ: dict[str, str] | None = None, home: Path | None = None,
) -> list[tuple[Path, dict]]:
    roots: list[tuple[Path, dict]] = []
    seen: set[tuple[str, str]] = set()
    for p in plugins:
        for d in vendor_preset_dirs(p, platform=platform, environ=environ, home=home):
            ident = (str(d), str(p.get("module", "")))
            if ident not in seen:
                seen.add(ident)
                roots.append((d, p))
    return roots


@dataclass(frozen=True)
class CatalogEntry:
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
        return str(self.path) if self.selector is None else f"{self.path}#{self.selector}"


def preset_belongs_to(plugin: dict, path: str | Path, format_name: str) -> bool:
    key = _plugin_key(plugin)
    if format_name == "serum": return "serum" in key and "fx" not in key
    if format_name == "serumfx": return key in {"serum2fx", "serumfx"}
    if format_name == "dx7": return key == "dexed"
    if format_name == "juce-valuetree": return "odin" in key
    if format_name == "juce-xml": return key == "vaporizer2"
    if format_name in {"decentsampler", "soundbox"}: return key == format_name
    if format_name == "cherry":
        suf = Path(path).suffix.lower()
        mapping = {".dco106preset": {"dco106", "cherryaudiodco106"}, ".mg1preset": {"mg1"}, ".voltagepreset": {"voltagemodular"}}
        if suf in mapping: return any(m in key for m in mapping[suf])
        if suf == ".sempreset": return key.startswith("sem")
        return "cherry" in squash(str(plugin.get("vendor", "")))
    if format_name in {"synplant", "echobode", "permut8"}: return key == format_name
    if format_name == "h2p":
        try:
            m = re.search(rb"^#AM=([^\r\n]+)", Path(path).read_bytes()[:4096], re.MULTILINE)
            if m: return squash(m.group(1).decode("utf-8", "ignore").strip()) == key
        except OSError: pass
    return True


split_bank_reference = preset_formats.split_bank_reference
bank_programs = preset_formats.bank_programs
resolve_bank_program = preset_formats.resolve_bank_program


def _walk(root: Path, *, max_depth: int = _MAX_WALK_DEPTH) -> Iterable[Path]:
    try: resolved_root = root.resolve(strict=True)
    except (OSError, RuntimeError): return
    if not resolved_root.is_dir():
        if resolved_root.is_file(): yield resolved_root
        return
    stack, visited = [(resolved_root, 0)], set()
    while stack:
        directory, depth = stack.pop()
        try:
            real = str(directory.resolve())
            if real in visited: continue
            visited.add(real)
            entries = sorted(directory.iterdir(), key=lambda i: i.name.casefold())
        except (OSError, RuntimeError): continue
        for entry in entries:
            if _MACOS_LEFTOVERS.match(entry.name): continue
            try:
                resolved = resolved_path_within(entry, resolved_root)
                if resolved is None: continue
                if resolved.is_dir() and depth < max_depth: stack.append((resolved, depth + 1))
                elif resolved.is_file(): yield resolved
            except (OSError, RuntimeError): pass


def cataloged_entries(
    plugin: dict, *, dirs: Iterable[str | Path] | None = None, platform: str | None = None,
    environ: dict[str, str] | None = None, home: Path | None = None,
    max_files: int = _MAX_CATALOG_FILES, max_bytes: int = _MAX_CATALOG_BYTES,
) -> list[CatalogEntry]:
    roots = list(dirs) if dirs is not None else vendor_preset_dirs(plugin, platform=platform, environ=environ, home=home)
    p_name, entries, seen, total_bytes = str(plugin.get("name", "")).strip(), [], set(), 0
    for root in roots:
        for path in _walk(Path(root)):
            if len(entries) >= max_files: return entries
            ext = path.suffix.lower()
            if ext not in preset_formats.SUPPORTED_EXTENSIONS: continue
            try:
                with path.open("rb") as s: head = s.read(4096)
                size = path.stat().st_size
            except OSError: continue
            fmt = preset_formats.detect_format(path, head)
            if not preset_belongs_to(plugin, path, fmt): continue
            if total_bytes + size > max_bytes: return entries
            total_bytes += size
            ident = str(path.resolve())
            if ident in seen: continue
            seen.add(ident)
            if ext in preset_formats.BANK_EXTENSIONS and size <= _MAX_BANK_BYTES:
                try:
                    for prog in bank_programs(path):
                        entries.append(CatalogEntry(
                            path=path, format=fmt, plugin_name=p_name,
                            name=prog.name or f"{path.stem} #{prog.index}",
                            category=path.stem, bank=path.stem,
                            selector=prog.index, program_name=prog.name,
                        ))
                    continue
                except (OSError, preset_formats.PresetFormatError): pass
            entries.append(CatalogEntry(
                path=path, format=fmt, plugin_name=p_name,
                name=path.stem, category=path.parent.name, bank=path.parent.name,
            ))
    return entries
