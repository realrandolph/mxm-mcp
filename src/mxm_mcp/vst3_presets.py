"""Filesystem-only VST3 preset indexing and standard preset-state extraction."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from . import discovery_cache
from . import preset_catalog
from . import preset_formats
from . import vst3
from .path_safety import resolved_path_within

_VST3_EXTENSIONS = {".vstpreset"}
_UHE_EXTENSIONS = {".h2p", ".uhe-preset"}
_CATALOG_EXTENSIONS = preset_formats.SUPPORTED_EXTENSIONS - _VST3_EXTENSIONS - _UHE_EXTENSIONS
_SUPPORTED_EXTENSIONS = _VST3_EXTENSIONS | _UHE_EXTENSIONS | _CATALOG_EXTENSIONS
_BANK_EXTENSIONS = preset_formats.BANK_EXTENSIONS
_ALWAYS_LOADABLE_FORMATS = {
    "nksf", "serum", "dx7", "juce-valuetree", "juce-xml",
    "decentsampler", "soundbox", "juce-string", "fxp", "cherry",
}
_BASE_STATE_FORMATS = {"serumfx", "synplant", "echobode", "permut8"}
_CLASS_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_INVALID_PRESET_PATH_CHARS = re.compile(r'[\\/*?:"<>|\x00]')
_MAX_CHUNKS = 256
_MAX_METADATA_BYTES = 1024 * 1024
_MAX_COMPONENT_BYTES = 32 * 1024 * 1024
_MAX_PRESET_FILES = 4_096
_MAX_PRESET_TOTAL_BYTES = 128 * 1024 * 1024
_MAX_PRESET_SCAN_ENTRIES = 100_000
_MAX_PRESET_ROOTS = 4_096


def _path_entries(name: str) -> list[Path]:
    return [Path(part).expanduser() for part in os.environ.get(name, "").split(os.pathsep)
            if part]


def standard_preset_dirs() -> list[Path]:
    """VST3 preset roots from Steinberg's cross-platform location convention."""
    home = Path.home()
    if sys.platform == "darwin":
        roots = [home / "Library/Audio/Presets", Path("/Library/Audio/Presets"),
                 Path("/Network/Library/Audio/Presets")]
    elif sys.platform.startswith("win"):
        documents = Path(os.environ.get("USERPROFILE", str(home))) / "Documents"
        roaming = Path(os.environ.get("APPDATA", str(home / "AppData/Roaming")))
        roots = [documents / "VST3 Presets", roaming / "VST3 Presets",
                 Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "VST3 Presets"]
    else:
        roots = [home / ".vst3/presets", Path("/usr/share/vst3/presets"),
                 Path("/usr/local/share/vst3/presets")]
    return roots


def _preset_path_component(value: str) -> str:
    """Sanitize vendor/product names as required by the VST3 preset layout."""
    component = _INVALID_PRESET_PATH_CHARS.sub("_", value.strip())
    return "_" if component in {"", ".", ".."} else component


def preset_search_paths(
    plugins: list[dict] | None = None,
    *,
    plugin_scoped: bool = False,
) -> list[Path]:
    """Return global roots or likely preset roots for only the given plugins."""
    plugins = plugins or []
    if plugin_scoped:
        # A filtered query should not recursively walk global preset/plugin
        # roots. Search common product/vendor subdirectories and paths adjacent
        # to each selected bundle instead. Explicit preset roots remain honored
        # as configured: they are user-selected search locations.
        explicit_roots = _path_entries("VST3_PRESET_PATH") + _path_entries("UHE_PRESET_PATH")
        bases = standard_preset_dirs() + explicit_roots
        paths: list[Path] = list(explicit_roots)
        for plugin in plugins:
            paths.extend(preset_catalog.vendor_preset_dirs(plugin))
            bundle = Path(plugin.get("module", ""))
            name = str(plugin.get("name", "")).strip()
            vendor = str(plugin.get("vendor", "")).strip()
            if not bundle or not name:
                continue
            product_names = list(dict.fromkeys((
                _preset_path_component(name),
                _preset_path_component(bundle.stem),
            )))
            company = _preset_path_component(vendor) if vendor else ""
            paths.extend([
                bundle / "Contents/Resources/Presets",
                bundle / "Presets",
                bundle.parent / f"{name}.data/Presets",
                bundle.parent / f"{name}.data/UserPresets",
                bundle.parent / name / "Presets",
                bundle.parent / name / "UserPresets",
                Path.home() / ".u-he" / name / "Presets",
                Path.home() / ".u-he" / name / "UserPresets",
                Path.home() / "Documents/u-he" / f"{name}.data/Presets",
                Path.home() / "Documents/u-he" / f"{name}.data/UserPresets",
                Path.home() / ".local/share/u-he" / name / "Presets",
                Path("/usr/share/u-he") / name / "Presets",
                Path("/usr/local/share/u-he") / name / "Presets",
            ])
            # Inspect bundle binary link if it resolves to an external data tree (e.g. ~/.u-he/Zebralette3)
            try:
                for candidate in bundle.glob("Contents/*/*"):
                    if candidate.is_symlink():
                        target_dir = candidate.resolve().parent
                        paths.extend([
                            target_dir / "Presets",
                            target_dir / "UserPresets",
                        ])
            except (OSError, RuntimeError):
                pass
            for base in bases:
                for product in product_names:
                    if company:
                        paths.append(base / company / product)
                    # Some vendors omit their company level; this also covers
                    # user-selected roots whose layout is already product-only.
                    paths.append(base / product)
    else:
        paths = standard_preset_dirs()
        paths += _path_entries("VST3_PRESET_PATH")
        paths += _path_entries("UHE_PRESET_PATH")
        # VST3_PATH is also a useful preset root: many vendors keep Presets or
        # <Plugin>.data banks beside their installed bundles.
        paths += _path_entries("VST3_PATH")
        paths += vst3.standard_vst3_dirs()
        for plugin in plugins:
            paths.extend(preset_catalog.vendor_preset_dirs(plugin))
        for plugin in plugins:
            bundle = Path(plugin.get("module", ""))
            name = str(plugin.get("name", "")).strip()
            if not bundle:
                continue
            paths.extend([
                bundle / "Contents/Resources/Presets",
                bundle / "Presets",
                bundle.parent / f"{name}.data/Presets",
                bundle.parent / f"{name}.data/UserPresets",
                bundle.parent / name / "Presets",
                bundle.parent / name / "UserPresets",
                Path.home() / ".u-he" / name / "Presets",
                Path.home() / ".u-he" / name / "UserPresets",
                Path.home() / "Documents/u-he" / f"{name}.data/Presets",
                Path.home() / "Documents/u-he" / f"{name}.data/UserPresets",
            ])
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        if len(unique) >= _MAX_PRESET_ROOTS:
            break
        normalized = str(Path(path).expanduser().absolute())
        if normalized not in seen:
            seen.add(normalized)
            unique.append(Path(normalized))
    return unique


def _collect_files(roots: list[Path]) -> list[Path]:
    found: dict[str, Path] = {}
    visited: set[str] = set()
    scanned = 0
    total_bytes = 0

    def add_file(path: Path) -> None:
        nonlocal total_bytes
        if len(found) >= _MAX_PRESET_FILES:
            return
        key = str(path)
        if key in found:
            return
        try:
            size = path.stat().st_size
        except (OSError, RuntimeError):
            return
        if total_bytes + size > _MAX_PRESET_TOTAL_BYTES:
            return
        total_bytes += size
        found[key] = path

    def walk(directory: Path, allowed_root: Path) -> None:
        nonlocal scanned
        try:
            resolved_dir = resolved_path_within(directory, allowed_root)
            if resolved_dir is None:
                return
            real = str(resolved_dir)
            if real in visited:
                return
            visited.add(real)
            entries = sorted(resolved_dir.iterdir(), key=lambda path: path.name.lower())
        except (OSError, RuntimeError):
            return
        for entry in entries:
            scanned += 1
            if scanned > _MAX_PRESET_SCAN_ENTRIES or len(found) >= _MAX_PRESET_FILES:
                return
            try:
                if entry.is_symlink():
                    resolved = resolved_path_within(entry, allowed_root)
                    if resolved is None:
                        continue
                else:
                    resolved = entry
                if resolved.is_file() and entry.suffix.lower() in _SUPPORTED_EXTENSIONS:
                    add_file(resolved)
                elif resolved.is_dir() and (
                        not entry.is_symlink() or entry.suffix.lower() == ".vst3"):
                    walk(resolved, allowed_root)
            except (OSError, RuntimeError):
                continue

    for root in roots[:_MAX_PRESET_ROOTS]:
        try:
            resolved_root = root.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved_root.is_dir():
            walk(resolved_root, resolved_root)
        elif resolved_root.is_file() and root.suffix.lower() in _SUPPORTED_EXTENSIONS:
            add_file(resolved_root)
        if scanned > _MAX_PRESET_SCAN_ENTRIES or len(found) >= _MAX_PRESET_FILES:
            break
    return sorted(found.values(), key=lambda path: str(path).casefold())


def _origin(path: Path) -> str:
    parts = [part.casefold() for part in path.parts]
    text = str(path).casefold()
    if any(part in {"factory", "factory presets", "factory-presets"} for part in parts):
        return "factory"
    if ("/usr/" in text or "/system/" in text or "program files" in text):
        return "factory"
    if (str(Path.home()).casefold() in text or "user" in parts or "/users/" in text):
        return "user"
    return "unknown"


def parse_vstpreset(path: str | Path, *, include_state: bool = False) -> dict:
    """Read preset metadata; component state is loaded only on explicit request."""
    preset_path = Path(path)
    info: dict = {"format": "vstpreset", "loadable": False}
    try:
        size = preset_path.stat().st_size
        with preset_path.open("rb") as stream:
            header = stream.read(48)
            if len(header) != 48 or header[:4] != b"VST3":
                return {**info, "error": "invalid VST3 preset header"}
            raw_cid = header[8:40].decode("ascii", "ignore").strip("\x00 ")
            cid = raw_cid.upper() if _CLASS_ID_RE.fullmatch(raw_cid) else header[8:24].hex().upper()
            chunk_offset = int.from_bytes(header[40:48], "big")
            if chunk_offset < 48 or chunk_offset + 4 > size:
                return {**info, "error": "invalid VST3 preset chunk table offset",
                        "cid": cid}
            stream.seek(chunk_offset)
            count_raw = stream.read(4)
            count = int.from_bytes(count_raw, "big") if len(count_raw) == 4 else 0
            if count > _MAX_CHUNKS or chunk_offset + 4 + count * 20 > size:
                return {**info, "error": "invalid VST3 preset chunk table", "cid": cid}
            chunks: dict[str, tuple[int, int]] = {}
            invalid_chunks: set[str] = set()
            for _ in range(count):
                record = stream.read(20)
                chunk_id = record[:4].decode("ascii", "replace")
                offset = int.from_bytes(record[4:12], "big")
                length = int.from_bytes(record[12:20], "big")
                if offset < 48 or offset > size or length > size - offset \
                        or offset + length > chunk_offset:
                    invalid_chunks.add(chunk_id)
                    continue
                chunks[chunk_id] = (offset, length)

            component = chunks.get("Comp")
            controller = chunks.get("Cont")
            unsupported_state_chunks = sorted({
                chunk_id for chunk_id, (_, length) in chunks.items()
                if chunk_id not in {"Comp", "Meta"} and length
            } | {chunk_id for chunk_id in invalid_chunks if chunk_id != "Meta"})
            metadata: dict[str, str] = {}
            meta = chunks.get("Meta")
            if meta and meta[1] <= _MAX_METADATA_BYTES:
                stream.seek(meta[0])
                raw_meta = stream.read(meta[1])
                try:
                    if b"<!doctype" in raw_meta.lower() or b"<!entity" in raw_meta.lower():
                        raise ET.ParseError("DTD/entity declarations are not accepted")
                    root = ET.fromstring(raw_meta)
                    for node in root.iter():
                        key = node.get("name") or node.tag.rsplit("}", 1)[-1]
                        value = node.get("value") or (node.text or "").strip()
                        if key and value:
                            metadata[key.strip().casefold()] = value
                except ET.ParseError:
                    pass

            state = None
            if include_state and component and component[1] <= _MAX_COMPONENT_BYTES:
                # The component chunk is the opaque state expected by the host;
                # the .vstpreset container itself is not copied into the MMP.
                stream.seek(component[0])
                state = stream.read(component[1])
                if len(state) != component[1]:
                    state = None
    except OSError as exc:
        return {**info, "error": f"could not read preset: {exc}"}

    name = next((metadata[key] for key in ("name", "presetname", "programname")
                 if metadata.get(key)), preset_path.stem)
    category = next((metadata[key] for key in ("category", "subcategory", "bank")
                     if metadata.get(key)), None)
    result = {
        **info,
        "name": name,
        "cid": cid,
        "metadata": metadata,
        "category": category or (preset_path.parent.name or None),
        "state_chunks": sorted(chunk_id for chunk_id, (_, length) in chunks.items()
                                if chunk_id != "Meta" and length),
        "requires_controller_state": bool(controller and controller[1]),
        "unsupported_state_chunks": unsupported_state_chunks,
        "loadable": bool(component and component[1] <= _MAX_COMPONENT_BYTES
                          and not unsupported_state_chunks),
    }
    if state:
        result["state"] = base64.b64encode(state).decode("ascii")
    return result


def parse_h2p(path: str | Path, *, include_state: bool = False) -> dict:
    """Read u-he preset metadata and optionally base64-encode its raw state stream."""
    preset_path = Path(path)
    info: dict = {"format": "h2p", "loadable": False}
    try:
        size = preset_path.stat().st_size
        if size > _MAX_COMPONENT_BYTES:
            return {**info, "error": "preset exceeds maximum component size"}
        raw_bytes = preset_path.read_bytes()
        text = raw_bytes[:16384].decode("utf-8", "ignore")
        metadata: dict[str, str] = {}
        meta_match = re.search(r"/\*@Meta(.*?)\*/", text, re.DOTALL)
        if meta_match:
            for m in re.finditer(r"([A-Za-z0-9_-]+):\s*'([^']*)'", meta_match.group(1)):
                metadata[m.group(1).casefold()] = m.group(2)
        target_match = re.search(r"^#AM=([^\r\n]+)", text, re.MULTILINE)
        target_plugin = target_match.group(1).strip() if target_match else None

        name = preset_path.stem
        category = metadata.get("categories") or preset_path.parent.name or None
        if category and ":" in category:
            category = category.split(",", 1)[0].strip()
        result = {
            **info,
            "name": name,
            "target_plugin": target_plugin,
            "metadata": metadata,
            "category": category,
            "loadable": True,
            "state_chunks": ["h2p"],
            "requires_controller_state": False,
            "unsupported_state_chunks": [],
        }
        if include_state:
            result["state"] = base64.b64encode(raw_bytes).decode("ascii")
        return result
    except OSError as exc:
        return {**info, "error": f"could not read preset: {exc}"}


def _catalog_roots(plugins: list[dict]) -> list[tuple[str, dict]]:
    """Resolved vendor preset roots paired with the plugin that owns them."""
    roots: list[tuple[str, dict]] = []
    grouped: dict[Path, dict] = {}
    for directory, plugin in preset_catalog.catalog_roots_by_plugin(plugins):
        grouped.setdefault(Path(os.path.normpath(os.path.abspath(str(directory)))), plugin)
    for directory, plugin in grouped.items():
        try:
            roots.append((str(directory.resolve(strict=True)), plugin))
        except (OSError, RuntimeError):
            pass
    return roots


def _owning_plugin(path: Path, roots: list[tuple[str, dict]], plugins: list[dict] | None = None) -> dict | None:
    try:
        resolved = str(path.resolve(strict=True))
    except (OSError, RuntimeError):
        resolved = str(path)
    owner, best = None, -1
    for root, plugin in roots:
        try:
            Path(resolved).relative_to(root)
        except ValueError:
            continue
        if len(root) > best:
            best, owner = len(root), plugin
    if owner is None and plugins:
        fmt = preset_formats.detect_format(path)
        return next((p for p in plugins if preset_catalog.preset_belongs_to(p, path, fmt)), None)
    return owner


def _nks_owner(parsed: preset_formats.DecodedPreset, plugins: list[dict]) -> dict | None:
    """Associate an NKS file by VST3 uid, VST2 magic, or bank name."""
    uid, magic, bank = parsed.uid.upper(), parsed.magic.upper(), preset_catalog.squash(parsed.bank)
    for p in plugins:
        cid = str(p.get("cid", "")).upper()
        if (uid and cid == uid) or (magic and magic in cid) or (bank and bank == preset_catalog.squash(str(p.get("name", "")))):
            return p
    return None


def _catalog_loadable(format_name: str, path: Path) -> bool:
    return format_name in _ALWAYS_LOADABLE_FORMATS and not (format_name == "cherry" and path.suffix.lower() == ".voltagepreset")


def _catalog_preset_record(
    parsed: preset_formats.DecodedPreset, plugin: dict, path: Path, *, selector: int | None = None, program_name: str = "",
) -> dict:
    source = str(path) + (f"#{selector}" if selector is not None else "")
    meta = {k: v for k, v in [("vendor", parsed.vendor), ("bank", parsed.bank), ("category", parsed.category)] if v}
    return {
        "id": source, "name": program_name or parsed.program_name or parsed.name or path.stem,
        "bank": parsed.bank or path.stem, "category": parsed.category or path.parent.name or None,
        "author": parsed.vendor or None, "tags": [], "character": None, "origin": _origin(path),
        "source": source, "format": parsed.format, "plugin_uri": None,
        "plugin_name": plugin.get("name"), "plugin_is_instrument": bool(plugin.get("is_instrument")),
        "plugin_module": plugin.get("module"), "plugin_cid": plugin.get("cid"),
        "loadable": _catalog_loadable(parsed.format, path),
        "state_chunks": [parsed.format], "requires_controller_state": False,
        "unsupported_state_chunks": [], "metadata": meta,
        **({"program_index": selector} if selector is not None else {}),
    }


def _catalog_bank_records(path: Path, plugin: dict, format_name: str) -> list[dict]:
    try:
        programs = preset_catalog.bank_programs(path)
    except (OSError, preset_formats.PresetFormatError):
        return []
    return [
        _catalog_preset_record(
            preset_formats.DecodedPreset(format=format_name, bank=path.stem, category=path.stem),
            plugin, path, selector=p.index, program_name=p.name or f"{path.stem} #{p.index}",
        )
        for p in programs
    ]


def _associate_preset(preset: dict, plugins: list[dict], path: Path) -> dict:
    cid = str(preset.get("cid", "")).upper()
    plugin = next((item for item in plugins if item.get("cid", "").upper() == cid), None)
    if plugin is None and path.suffix.lower() in _UHE_EXTENSIONS:
        target_name = (preset.get("target_plugin") or "").casefold()
        path_str = str(path).casefold()
        for item in plugins:
            if not item.get("is_instrument"):
                continue
            item_name = str(item.get("name", "")).casefold()
            bundle_name = Path(item.get("module", "")).stem.casefold()
            if (target_name and (target_name == item_name or target_name == bundle_name)) or (
                item_name and item_name in path_str
            ) or (bundle_name and bundle_name in path_str):
                plugin = item
                break
    metadata = preset.get("metadata", {})
    name = preset.get("name") or path.stem
    tags = sorted({tag.strip() for key, value in metadata.items()
                   if key in {"tag", "tags", "style", "musicalstyle", "character", "features"}
                   for tag in re.split(r"[,;]", value) if tag.strip()})
    author = next((metadata[key] for key in ("author", "creator", "createdby")
                   if metadata.get(key)), None)
    return {
        "id": str(path.resolve()),
        "name": name,
        "bank": metadata.get("bank") or path.parent.name or None,
        "category": preset.get("category"),
        "author": author,
        "tags": tags,
        "character": metadata.get("character") or metadata.get("features"),
        "origin": _origin(path),
        "source": str(path.resolve()),
        "format": preset.get("format"),
        "plugin_uri": None,
        "plugin_name": plugin.get("name") if plugin else None,
        "plugin_is_instrument": bool(plugin and plugin.get("is_instrument")),
        "plugin_module": plugin.get("module") if plugin else None,
        "plugin_cid": plugin.get("cid") if plugin else cid or None,
        "loadable": bool(preset.get("loadable") and plugin and plugin.get("is_instrument")),
        "state_chunks": preset.get("state_chunks", []),
        "requires_controller_state": preset.get("requires_controller_state", False),
        "unsupported_state_chunks": preset.get("unsupported_state_chunks", []),
        "metadata": metadata,
        **({"state": preset["state"]} if preset.get("state") else {}),
    }


def discover_vst3_presets(
    plugins: list[dict] | None = None,
    *,
    refresh: bool = False,
    plugin_scoped: bool = False,
) -> dict:
    """Index VST3 presets globally or within roots associated with plugins."""
    plugins = plugins if plugins is not None else vst3.discover_vst3_plugins(refresh=refresh)
    paths = preset_search_paths(plugins, plugin_scoped=plugin_scoped)

    def build() -> dict:
        presets: list[dict] = []
        catalog_roots = _catalog_roots(plugins)
        for path in _collect_files(paths):
            suffix = path.suffix.lower()
            if suffix == ".vstpreset" or suffix in _UHE_EXTENSIONS:
                parsed = parse_vstpreset(path) if suffix == ".vstpreset" else parse_h2p(path)
                presets.append(_associate_preset(parsed, plugins, path))
                continue
            owner = _owning_plugin(path, catalog_roots, plugins)
            if suffix in _BANK_EXTENSIONS and owner is not None:
                presets.extend(_catalog_bank_records(
                    path, owner, preset_formats.detect_format(path)))
                continue
            if owner is None and suffix not in {".nksf", ".nksfx"}:
                continue
            try:
                parsed = preset_formats.describe_preset(path)
            except (OSError, preset_formats.PresetFormatError):
                continue
            target = owner
            if target is None and parsed.format == "nksf":
                target = _nks_owner(parsed, plugins)
            if target is None:
                continue
            presets.append(_catalog_preset_record(parsed, target, path))
        presets.sort(key=lambda item: (str(item.get("plugin_name") or "").casefold(),
                                       str(item.get("name") or "").casefold(),
                                       item["source"].casefold()))
        return {"presets": presets, "search_paths": [str(path) for path in paths]}

    key = "vst3.presets"
    if plugin_scoped:
        identity = "\n".join(sorted(
            f"{plugin.get('module', '')}\0{plugin.get('cid', '')}" for plugin in plugins
        ))
        key += ".plugin." + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return discovery_cache.cached_discovery(key, paths, build, refresh=refresh)


def load_vst3preset_state(preset: dict, search_paths: list[str | Path]) -> str | None:
    """Read and decode at most one selected component chunk or preset file.

    The source is rechecked against the indexed roots, so a preset record
    cannot be pointed at an arbitrary path after discovery.
    """
    source = preset.get("source")
    if not source or not preset.get("loadable") or preset.get("requires_controller_state"):
        return None
    source_path, selector = preset_catalog.split_bank_reference(source)
    safe_path = next((resolved for root in search_paths
                      if (resolved := resolved_path_within(source_path, root)) is not None), None)
    if safe_path is None:
        return None
    suffix = safe_path.suffix.lower()
    if suffix == ".vstpreset":
        parsed = parse_vstpreset(safe_path, include_state=True)
        if (not parsed.get("loadable")
                or parsed.get("cid", "").upper() != str(preset.get("plugin_cid", "")).upper()
                or parsed.get("requires_controller_state")
                or parsed.get("unsupported_state_chunks")):
            return None
        return parsed.get("state")
    if suffix in _UHE_EXTENSIONS:
        parsed = parse_h2p(safe_path, include_state=True)
        return parsed.get("state") if parsed.get("loadable") else None
    if suffix not in _CATALOG_EXTENSIONS:
        return None
    program = preset.get("program_index")
    if program is None and selector is not None:
        program = int(selector) if selector.lstrip("+-").isdigit() else selector
    if isinstance(program, int) and program < 0:
        program = None
    try:
        decoded = preset_formats.decode_preset(safe_path, format=str(preset.get("format") or ""), selector=program)
    except (OSError, preset_formats.PresetFormatError):
        return None
    state = decoded.state
    return base64.b64encode(state).decode("ascii") if state and len(state) <= _MAX_COMPONENT_BYTES else None


def preset_matches_plugin(preset: dict, *, module: str = "", cid: str = "") -> bool:
    """Whether a preset belongs to a project plugin identity."""
    return ((not cid or str(preset.get("plugin_cid", "")).upper() == cid.upper())
            and (not module or os.path.normcase(os.path.abspath(
                str(preset.get("plugin_module", "")))) ==
                os.path.normcase(os.path.abspath(module))))
