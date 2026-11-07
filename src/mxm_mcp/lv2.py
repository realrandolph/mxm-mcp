"""Headless LV2 plugin and preset discovery from installed RDF bundles."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import DC, RDF, RDFS

from . import discovery_cache
from .path_safety import resolved_path_within

LV2 = "http://lv2plug.in/ns/lv2core#"
PS = "http://lv2plug.in/ns/ext/presets#"
DOAP = "http://usefulinc.com/ns/doap#"
FOAF = "http://xmlns.com/foaf/0.1/"
STATE = "http://lv2plug.in/ns/ext/state#"

_MAX_SCAN_ENTRIES = 100_000
_MAX_SEARCH_ROOTS = 4_096
_MAX_BUNDLES = 2_048
_MAX_RDF_FILES = 4_096
_MAX_RDF_FILE_BYTES = 4 * 1024 * 1024
_MAX_RDF_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_RDF_FILE_TRIPLES = 100_000
_MAX_RDF_TOTAL_TRIPLES = 500_000


def standard_lv2_dirs() -> list[Path]:
    """Standard per-user and system bundle roots for the current platform."""
    home = Path.home()
    if sys.platform == "darwin":
        return [home / "Library/Audio/Plug-Ins/LV2", Path("/Library/Audio/Plug-Ins/LV2")]
    if sys.platform.startswith("win"):
        appdata = Path(os.environ.get("APPDATA", str(home / "AppData/Roaming")))
        program_files = Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
        return [appdata / "LV2", program_files / "Common Files/LV2"]
    return [home / ".lv2", Path("/usr/lib/lv2"), Path("/usr/lib64/lv2"),
            Path("/usr/local/lib/lv2"), Path("/usr/local/lib64/lv2")]


def effective_search_paths() -> list[Path]:
    paths = standard_lv2_dirs()
    paths.extend(Path(part).expanduser()
                 for part in os.environ.get("LV2_PATH", "").split(os.pathsep) if part)
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        normalized = str(Path(os.path.abspath(path)))
        if normalized not in seen:
            seen.add(normalized)
            result.append(Path(normalized))
    return result[:_MAX_SEARCH_ROOTS]


def _find_bundles(roots: list[Path]) -> list[tuple[Path, Path]]:
    result: dict[str, tuple[Path, Path]] = {}
    visited: set[str] = set()
    entries_seen = 0

    def walk(directory: Path, allowed_root: Path) -> None:
        nonlocal entries_seen
        try:
            resolved = str(directory.resolve(strict=True))
            if resolved in visited:
                return
            visited.add(resolved)
            children = sorted(directory.iterdir(), key=lambda child: child.name.casefold())
        except (OSError, RuntimeError):
            return
        for child in children:
            entries_seen += 1
            if entries_seen > _MAX_SCAN_ENTRIES or len(result) >= _MAX_BUNDLES:
                return
            try:
                if child.is_symlink():
                    resolved_bundle = resolved_path_within(child, allowed_root)
                    if resolved_bundle is None or not resolved_bundle.is_dir():
                        continue
                    real_parent = child.parent.resolve()
                    if (resolved_bundle == real_parent
                            or resolved_bundle in real_parent.parents):
                        continue
                else:
                    resolved_bundle = child
                if child.suffix.lower() == ".lv2" and resolved_bundle.is_dir():
                    result[str(resolved_bundle)] = (resolved_bundle, allowed_root)
                elif resolved_bundle.is_dir() and not child.is_symlink():
                    walk(resolved_bundle, allowed_root)
            except (OSError, RuntimeError):
                continue

    for root in roots[:_MAX_SEARCH_ROOTS]:
        if len(result) >= _MAX_BUNDLES or entries_seen >= _MAX_SCAN_ENTRIES:
            break
        try:
            resolved_root = root.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if root.suffix.lower() == ".lv2" and resolved_root.is_dir():
            result[str(resolved_root)] = (resolved_root, resolved_root)
        elif resolved_root.is_dir():
            walk(resolved_root, resolved_root)
    return sorted(result.values(), key=lambda item: str(item[0]).casefold())


def _text(graph: Graph, subject, predicate) -> str | None:
    value = next(iter(graph.objects(subject, predicate)), None)
    return str(value) if value is not None else None


def _first_text(graph: Graph, subject, predicates: tuple[URIRef, ...]) -> str | None:
    for predicate in predicates:
        text = _text(graph, subject, predicate)
        if text:
            return text
    return None


def _namespaces() -> tuple[URIRef, ...]:
    return (
        URIRef(LV2 + "Plugin"), URIRef(LV2 + "InstrumentPlugin"),
        URIRef(LV2 + "GeneratorPlugin"), URIRef(LV2 + "SynthPlugin"),
        URIRef(LV2 + "AmplifierPlugin"), URIRef(LV2 + "DelayPlugin"),
        URIRef(LV2 + "DistortionPlugin"), URIRef(LV2 + "DynamicsPlugin"),
        URIRef(LV2 + "FilterPlugin"), URIRef(LV2 + "ModulatorPlugin"),
        URIRef(LV2 + "ReverbPlugin"), URIRef(LV2 + "UtilityPlugin"),
    )


class _RdfLimitExceeded(Exception):
    """Raised as soon as parsing would exceed a configured RDF triple bound."""


class _BoundedGraph(Graph):
    def __init__(self, max_triples: int):
        super().__init__()
        self.max_triples = max_triples

    def add(self, triple):
        if len(self) >= self.max_triples:
            raise _RdfLimitExceeded("RDF triple limit exceeded")
        return super().add(triple)


class _RdfBudget:
    def __init__(self):
        self.files = 0
        self.bytes = 0
        self.triples = 0
        self.entries = 0


def _rdf_files(bundle: Path, allowed_root: Path, budget: _RdfBudget) -> list[Path]:
    files: dict[str, Path] = {}
    visited: set[str] = set()
    def walk(directory: Path) -> None:
        try:
            real = str(directory.resolve(strict=True))
            if real in visited or resolved_path_within(directory, allowed_root) is None:
                return
            visited.add(real)
            entries = sorted(directory.iterdir(), key=lambda path: str(path).casefold())
        except (OSError, RuntimeError):
            return
        for path in entries:
            budget.entries += 1
            if (budget.entries > _MAX_SCAN_ENTRIES
                    or budget.files + len(files) >= _MAX_RDF_FILES):
                return
            try:
                if path.is_symlink():
                    resolved = resolved_path_within(path, allowed_root)
                    if resolved is None:
                        continue
                else:
                    resolved = path
                if resolved.is_dir():
                    # A symlinked bundle is allowed, but arbitrary links are
                    # not recursive traversal roots.
                    if not path.is_symlink():
                        walk(resolved)
                elif resolved.is_file() and resolved.suffix.lower() in {".ttl", ".rdf", ".n3"}:
                    files[str(resolved)] = resolved
            except OSError:
                continue

    walk(bundle)
    manifest = bundle / "manifest.ttl"
    resolved_manifest = resolved_path_within(manifest, allowed_root)
    ordered = sorted(files.values(), key=lambda path: str(path).casefold())
    if resolved_manifest is not None and resolved_manifest.is_file():
        ordered = [resolved_manifest] + [path for path in ordered
                                         if path != resolved_manifest]
    return ordered[:max(0, _MAX_RDF_FILES - budget.files)]


def _load_bundle(bundle: Path, allowed_root: Path,
                 budget: _RdfBudget) -> tuple[Graph, dict[str, str]]:
    graph = _BoundedGraph(_MAX_RDF_TOTAL_TRIPLES - budget.triples)
    source_by_subject: dict[str, str] = {}
    for source in _rdf_files(bundle, allowed_root, budget):
        if (budget.files >= _MAX_RDF_FILES or budget.bytes >= _MAX_RDF_TOTAL_BYTES
                or len(graph) >= graph.max_triples):
            break
        budget.files += 1
        try:
            size = source.stat().st_size
            remaining = _MAX_RDF_TOTAL_BYTES - budget.bytes
            limit = min(_MAX_RDF_FILE_BYTES, remaining)
            if size > limit:
                continue
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(source, flags)
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode):
                    continue
                raw = stream.read(limit + 1)
            budget.bytes += min(len(raw), remaining)
            if len(raw) != size or len(raw) > limit:
                continue
            # RDF/XML's external-entity features are unnecessary for LV2
            # metadata and are rejected before invoking the XML parser.
            lowered = raw.lower()
            if source.suffix.lower() == ".rdf" and (
                    b"<!doctype" in lowered or b"<!entity" in lowered):
                continue
            parsed = _BoundedGraph(_MAX_RDF_FILE_TRIPLES)
            rdf_format = {".ttl": "turtle", ".n3": "n3", ".rdf": "xml"}.get(
                source.suffix.lower())
            parsed.parse(data=raw, publicID=source.as_uri(), format=rdf_format)
            new_triples = [triple for triple in parsed if triple not in graph]
            if len(graph) + len(new_triples) > graph.max_triples:
                continue
            for subject in set(parsed.subjects()):
                source_by_subject.setdefault(str(subject), str(source))
            for triple in new_triples:
                graph.add(triple)
        except Exception:
            # A broken optional metadata file should not hide valid plugins in
            # the same bundle. The bundle remains discoverable from other RDF.
            continue
    budget.triples += len(graph)
    return graph, source_by_subject


def _origin(bundle: Path, source: str) -> str:
    path = str(Path(source)).casefold()
    parts = {part.casefold() for part in Path(source).parts}
    if parts & {"factory", "factory presets"}:
        return "factory"
    if "/usr/" in path or "/system/" in path or "program files" in path:
        return "factory"
    if str(Path.home()).casefold() in path or "/users/" in path:
        return "user"
    return "unknown"


def _bundle_index(bundle: Path, allowed_root: Path,
                  budget: _RdfBudget) -> tuple[list[dict], list[dict]]:
    graph, source_by_subject = _load_bundle(bundle, allowed_root, budget)
    plugin_types = _namespaces()
    candidates: set[URIRef] = set()
    for plugin_type in plugin_types:
        candidates.update(subject for subject in graph.subjects(RDF.type, plugin_type)
                          if isinstance(subject, URIRef))
    candidates.update(subject for subject in graph.subjects(URIRef(LV2 + "binary"), None)
                      if isinstance(subject, URIRef))
    plugins: list[dict] = []
    plugin_port_symbols: dict[str, set[str]] = {}
    plugin_instruments: dict[str, bool] = {}
    for uri in sorted(candidates, key=str.casefold):
        types = [str(value) for value in graph.objects(uri, RDF.type)]
        classes = sorted({value.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
                          for value in types if value.startswith(LV2) and
                          value.rsplit("#", 1)[-1] != "Plugin"})
        ports = []
        input_symbols: set[str] = set()
        for port in graph.objects(uri, URIRef(LV2 + "port")):
            symbol = _text(graph, port, URIRef(LV2 + "symbol"))
            if not symbol:
                continue
            port_types = {str(value) for value in graph.objects(port, RDF.type)}
            direction = "input" if LV2 + "InputPort" in port_types else (
                "output" if LV2 + "OutputPort" in port_types else "unknown")
            kind = "control" if LV2 + "ControlPort" in port_types else (
                "audio" if LV2 + "AudioPort" in port_types else "other")
            if direction == "input" and kind == "control":
                input_symbols.add(symbol)
            ports.append({
                "index": _text(graph, port, URIRef(LV2 + "index")),
                "symbol": symbol,
                "name": _text(graph, port, URIRef(LV2 + "name")) or symbol,
                "direction": direction,
                "type": kind,
                "default": _text(graph, port, URIRef(LV2 + "default")),
                "minimum": _text(graph, port, URIRef(LV2 + "minimum")),
                "maximum": _text(graph, port, URIRef(LV2 + "maximum")),
            })
        ports.sort(key=lambda port: (int(port["index"]) if str(port["index"] or "").isdigit()
                                     else 2**31, port["symbol"]))
        plugin_port_symbols[str(uri)] = input_symbols
        plugin_instruments[str(uri)] = any(item in classes for item in
                                           ("InstrumentPlugin", "GeneratorPlugin", "SynthPlugin"))
        maintainer = next(iter(graph.objects(uri, URIRef(DOAP + "maintainer"))), None)
        vendor = (_first_text(graph, maintainer,
                              (URIRef(FOAF + "name"), URIRef(DOAP + "name")))
                  if maintainer is not None else None)
        name = _first_text(graph, uri, (URIRef(DOAP + "name"), RDFS.label)) or str(uri)
        plugins.append({
            "uri": str(uri),
            "name": name,
            "vendor": vendor,
            "bundle": str(bundle.resolve()),
            "source": source_by_subject.get(str(uri), str((bundle / "manifest.ttl").resolve())),
            "plugin_type": "LV2",
            "is_instrument": plugin_instruments[str(uri)],
            "classes": classes,
            "version": _first_text(graph, uri, (URIRef(DOAP + "revision"),)),
            "required_features": sorted(str(value) for value in
                                         graph.objects(uri, URIRef(LV2 + "requiredFeature"))),
            "ports": ports,
        })

    presets: list[dict] = []
    preset_type = URIRef(PS + "Preset")
    applies_to = URIRef(LV2 + "appliesTo")
    for preset_uri in sorted({subject for subject in graph.subjects(RDF.type, preset_type)
                              if isinstance(subject, URIRef)}, key=str.casefold):
        plugin_uri = next(iter(graph.objects(preset_uri, applies_to)), None)
        if plugin_uri is None:
            continue
        plugin_uri_text = str(plugin_uri)
        name = _first_text(graph, preset_uri, (RDFS.label, URIRef(DC.title),
                                               URIRef(DOAP + "name")))
        if not name:
            name = str(preset_uri).rsplit("/", 1)[-1].rsplit("#", 1)[-1]
        bank_node = next(iter(graph.objects(preset_uri, URIRef(PS + "bank"))), None)
        bank = _first_text(graph, bank_node, (RDFS.label,)) if bank_node is not None else None
        creator = next(iter(graph.objects(preset_uri, URIRef(DC.creator))), None)
        if isinstance(creator, Literal):
            author = str(creator)
        elif creator is not None:
            author = (_text(graph, creator, URIRef(FOAF + "name"))
                      or _text(graph, creator, URIRef(DOAP + "name")))
        else:
            author = None
        tags = sorted({str(value) for value in graph.objects(preset_uri, URIRef(DC.subject))})
        values: dict[str, float] = {}
        port_states = set(graph.objects(preset_uri, URIRef(LV2 + "port")))
        opaque_state = any(
            str(predicate).startswith(STATE)
            for subject in {preset_uri, *port_states}
            for predicate in graph.predicates(subject, None)
        )
        invalid_port_states = 0
        for port_state in port_states:
            port_symbols = set(graph.objects(port_state, URIRef(LV2 + "symbol")))
            port_values = set(graph.objects(port_state, URIRef(PS + "value")))
            if len(port_symbols) != 1 or len(port_values) != 1:
                invalid_port_states += 1
                continue
            symbol = str(next(iter(port_symbols)))
            value = next(iter(port_values))
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                invalid_port_states += 1
                continue
            if number != number or abs(number) == float("inf"):
                invalid_port_states += 1
                continue
            if symbol in values:
                invalid_port_states += 1
                continue
            values[symbol] = number
        source = source_by_subject.get(str(preset_uri), str((bundle / "manifest.ttl").resolve()))
        symbols = plugin_port_symbols.get(plugin_uri_text, set())
        presets.append({
            "id": str(preset_uri),
            "name": name,
            "plugin_uri": plugin_uri_text,
            "bank": bank,
            "category": bank,
            "author": author,
            "tags": tags,
            "character": None,
            "origin": _origin(bundle, source),
            "source": source,
            "format": "lv2-rdf",
            "port_values": values,
            "port_state_count": len(port_states),
            "invalid_port_states": invalid_port_states,
            "requires_opaque_state": opaque_state,
            "loadable": (bool(values) and invalid_port_states == 0
                         and len(values) == len(port_states)
                         and set(values).issubset(symbols)
                         and plugin_instruments.get(plugin_uri_text, False)
                         and not opaque_state),
        })
    return plugins, presets


def discover_lv2(refresh: bool = False) -> dict:
    """Discover installed LV2 plugins and RDF-declared presets, entirely headless."""
    paths = effective_search_paths()

    def build() -> dict:
        plugins: list[dict] = []
        presets: list[dict] = []
        budget = _RdfBudget()
        for bundle, allowed_root in _find_bundles(paths):
            if budget.files >= _MAX_RDF_FILES or budget.bytes >= _MAX_RDF_TOTAL_BYTES \
                    or budget.triples >= _MAX_RDF_TOTAL_TRIPLES:
                break
            bundle_plugins, bundle_presets = _bundle_index(bundle, allowed_root, budget)
            plugins.extend(bundle_plugins)
            presets.extend(bundle_presets)
        plugins.sort(key=lambda item: (item["name"].casefold(), item["uri"].casefold()))
        plugins_by_uri: dict[str, list[dict]] = {}
        for plugin in plugins:
            plugins_by_uri.setdefault(plugin["uri"], []).append(plugin)
        for preset in presets:
            matching_plugins = plugins_by_uri.get(preset["plugin_uri"], [])
            input_symbols = [
                {port["symbol"] for port in plugin["ports"]
                 if port["direction"] == "input" and port["type"] == "control"}
                for plugin in matching_plugins
            ]
            supported_symbols = set.intersection(*input_symbols) if input_symbols else set()
            preset["loadable"] = (
                bool(matching_plugins)
                and all(plugin["is_instrument"] for plugin in matching_plugins)
                and bool(preset["port_values"])
                and preset["invalid_port_states"] == 0
                and len(preset["port_values"]) == preset["port_state_count"]
                and set(preset["port_values"]).issubset(supported_symbols)
                and not preset["requires_opaque_state"]
            )
        presets.sort(key=lambda item: (item["plugin_uri"].casefold(),
                                       item["name"].casefold(), item["id"].casefold()))
        return {"plugins": plugins, "presets": presets,
                "search_paths": [str(path) for path in paths]}

    return discovery_cache.cached_discovery("lv2.index", paths, build, refresh=refresh)


def resolve_plugin(plugins: list[dict], query: str) -> tuple[dict | None, list[dict]]:
    """Resolve an LV2 plugin by URI or unambiguous case-insensitive name."""
    query = query.strip()
    if not query:
        return None, []
    exact_uri = [plugin for plugin in plugins if plugin["uri"] == query]
    if len(exact_uri) == 1:
        return exact_uri[0], []
    lowered = query.casefold()
    exact = [plugin for plugin in plugins if plugin["name"].casefold() == lowered]
    if len(exact) == 1:
        return exact[0], []
    candidates = exact or [plugin for plugin in plugins
                           if lowered in plugin["name"].casefold()
                           or lowered in plugin["uri"].casefold()]
    return (candidates[0], []) if len(candidates) == 1 else (None, candidates)
