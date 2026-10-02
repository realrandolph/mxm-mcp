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
from . import vst3
from .path_safety import resolved_path_within

_VST3_EXTENSIONS = {".vstpreset"}
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
        explicit_roots = _path_entries("VST3_PRESET_PATH")
        bases = standard_preset_dirs() + explicit_roots
        paths: list[Path] = list(explicit_roots)
        for plugin in plugins:
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
            ])
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
        # VST3_PATH is also a useful preset root: many vendors keep Presets or
        # <Plugin>.data banks beside their installed bundles.
        paths += _path_entries("VST3_PATH")
        paths += vst3.standard_vst3_dirs()
        for plugin in plugins:
            bundle = Path(plugin.get("module", ""))
            if not bundle:
                continue
            paths.extend([
                bundle / "Contents/Resources/Presets",
                bundle / "Presets",
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
                if resolved.is_file() and entry.suffix.lower() in _VST3_EXTENSIONS:
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
        elif resolved_root.is_file() and root.suffix.lower() in _VST3_EXTENSIONS:
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


def _associate_preset(preset: dict, plugins: list[dict], path: Path) -> dict:
    cid = str(preset.get("cid", "")).upper()
    plugin = next((item for item in plugins if item.get("cid", "").upper() == cid), None)
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
        for path in _collect_files(paths):
            parsed = parse_vstpreset(path)
            record = _associate_preset(parsed, plugins, path)
            presets.append(record)
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
    """Read at most one selected component chunk, after rechecking its root and CID."""
    source = preset.get("source")
    if not source or not preset.get("loadable") or preset.get("requires_controller_state"):
        return None
    safe_path = next((resolved for root in search_paths
                      if (resolved := resolved_path_within(source, root)) is not None), None)
    if safe_path is None or safe_path.suffix.lower() != ".vstpreset":
        return None
    # This is deliberately the only path that asks the parser to retain state:
    # one selected file, capped at _MAX_COMPONENT_BYTES (32 MiB).
    parsed = parse_vstpreset(safe_path, include_state=True)
    if (not parsed.get("loadable")
            or parsed.get("cid", "").upper() != str(preset.get("plugin_cid", "")).upper()
            or parsed.get("requires_controller_state")
            or parsed.get("unsupported_state_chunks")):
        return None
    return parsed.get("state")


def preset_matches_plugin(preset: dict, *, module: str = "", cid: str = "") -> bool:
    """Whether a preset belongs to a project plugin identity."""
    return ((not cid or str(preset.get("plugin_cid", "")).upper() == cid.upper())
            and (not module or os.path.normcase(os.path.abspath(
                str(preset.get("plugin_module", "")))) ==
                os.path.normcase(os.path.abspath(module))))
