"""Filesystem-only VST3 preset indexing and standard preset-state extraction."""

from __future__ import annotations

import base64
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from . import discovery_cache
from . import vst3

_VST3_EXTENSIONS = {".vstpreset"}
_UHE_EXTENSIONS = {".h2p", ".uhe-preset"}
_CLASS_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_MAX_CHUNKS = 256
_MAX_METADATA_BYTES = 1024 * 1024
_MAX_COMPONENT_BYTES = 256 * 1024 * 1024


def _path_entries(name: str) -> list[Path]:
    return [Path(part).expanduser() for part in os.environ.get(name, "").split(os.pathsep)
            if part]


def standard_preset_dirs() -> list[Path]:
    """Common VST3 preset roots, plus the usual u-he factory/user bank roots."""
    home = Path.home()
    if sys.platform == "darwin":
        roots = [home / "Library/Audio/Presets", Path("/Library/Audio/Presets")]
    elif sys.platform.startswith("win"):
        documents = Path(os.environ.get("USERPROFILE", str(home))) / "Documents"
        roots = [documents / "VST3 Presets", documents / "VST3 Presets" / "Steinberg",
                 Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "VST3 Presets"]
    else:
        roots = [home / "Documents/VST3 Presets", home / ".local/share/vst3/presets",
                 Path("/usr/share/vst3/presets"), Path("/usr/local/share/vst3/presets"),
                 Path("/usr/share/vst3"), Path("/usr/local/share/vst3")]
    roots.extend([
        home / "Documents/u-he", home / ".u-he", home / ".local/share/u-he",
        Path("/usr/share/u-he"), Path("/usr/local/share/u-he"),
    ])
    return roots


def preset_search_paths(plugins: list[dict] | None = None) -> list[Path]:
    """Roots searched for standard presets and vendor-specific preset banks."""
    paths = standard_preset_dirs()
    paths += _path_entries("VST3_PRESET_PATH")
    paths += _path_entries("UHE_PRESET_PATH")
    # VST3_PATH is also a useful preset root: many vendors keep Presets or
    # <Plugin>.data banks beside their installed bundles.
    paths += _path_entries("VST3_PATH")
    paths += vst3.standard_vst3_dirs()
    for plugin in plugins or []:
        bundle = Path(plugin.get("module", ""))
        if not bundle:
            continue
        name = str(plugin.get("name", "")).strip()
        if not name:
            continue
        paths.extend([
            bundle / "Contents/Resources/Presets",
            bundle / "Presets",
            bundle.parent / f"{name}.data/Presets",
            bundle.parent / name / "Presets",
            Path.home() / "Documents/u-he" / f"{name}.data/Presets",
            Path.home() / ".u-he" / f"{name}.data/Presets",
        ])
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        normalized = str(Path(path).expanduser().absolute())
        if normalized not in seen:
            seen.add(normalized)
            unique.append(Path(normalized))
    return unique


def _collect_files(roots: list[Path]) -> list[Path]:
    found: dict[str, Path] = {}
    visited: set[str] = set()

    def walk(directory: Path) -> None:
        try:
            real = str(directory.resolve())
            if real in visited:
                return
            visited.add(real)
            entries = sorted(directory.iterdir(), key=lambda path: path.name.lower())
        except OSError:
            return
        for entry in entries:
            try:
                if entry.is_file() and entry.suffix.lower() in _VST3_EXTENSIONS | _UHE_EXTENSIONS:
                    found[str(entry.resolve())] = entry
                elif entry.is_dir() and (
                        not entry.is_symlink() or entry.suffix.lower() == ".vst3"):
                    walk(entry)
            except OSError:
                continue

    for root in roots:
        if root.is_dir():
            walk(root)
        elif root.is_file() and root.suffix.lower() in _VST3_EXTENSIONS | _UHE_EXTENSIONS:
            found[str(root.resolve())] = root
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


def parse_vstpreset(path: str | Path) -> dict:
    """Read the VST3 preset header, chunk table, component state and Meta XML."""
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
            if component and component[1] <= _MAX_COMPONENT_BYTES:
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
    }
    if state:
        result["state"] = base64.b64encode(state).decode("ascii")
        result["loadable"] = not unsupported_state_chunks
    return result


def _associate_preset(preset: dict, plugins: list[dict], path: Path) -> dict:
    cid = str(preset.get("cid", "")).upper()
    plugin = next((item for item in plugins if item.get("cid", "").upper() == cid), None)
    if plugin is None and path.suffix.lower() in _UHE_EXTENSIONS:
        haystack = str(path).casefold()
        matches = [item for item in plugins if item.get("is_instrument") and
                   str(item.get("name", "")).casefold() in haystack]
        if len(matches) == 1:
            plugin = matches[0]
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
) -> dict:
    """Index standard VST3 presets and filesystem-friendly u-he preset banks."""
    plugins = plugins if plugins is not None else vst3.discover_vst3_plugins(refresh=refresh)
    paths = preset_search_paths(plugins)

    def build() -> dict:
        presets: list[dict] = []
        for path in _collect_files(paths):
            if path.suffix.lower() == ".vstpreset":
                parsed = parse_vstpreset(path)
            else:
                # The h2p format is indexed for discovery, but is not a VST3
                # component-state stream and cannot safely be embedded as one.
                parsed = {"format": path.suffix.lower().lstrip("."),
                          "name": path.stem, "loadable": False, "metadata": {}}
            record = _associate_preset(parsed, plugins, path)
            presets.append(record)
        presets.sort(key=lambda item: (str(item.get("plugin_name") or "").casefold(),
                                       str(item.get("name") or "").casefold(),
                                       item["source"].casefold()))
        return {"presets": presets, "search_paths": [str(path) for path in paths]}

    return discovery_cache.cached_discovery(
        "vst3.presets", paths, build, refresh=refresh,
    )


def preset_matches_plugin(preset: dict, *, module: str = "", cid: str = "") -> bool:
    """Whether a preset belongs to a project plugin identity."""
    return ((not cid or str(preset.get("plugin_cid", "")).upper() == cid.upper())
            and (not module or os.path.normcase(os.path.abspath(
                str(preset.get("plugin_module", "")))) ==
                os.path.normcase(os.path.abspath(module))))
