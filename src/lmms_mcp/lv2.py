"""Headless LV2 plugin and preset discovery from installed RDF bundles."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import DC, RDF, RDFS

from . import discovery_cache

LV2 = "http://lv2plug.in/ns/lv2core#"
PS = "http://lv2plug.in/ns/ext/presets#"
DOAP = "http://usefulinc.com/ns/doap#"
FOAF = "http://xmlns.com/foaf/0.1/"
STATE = "http://lv2plug.in/ns/ext/state#"


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
    return result


def _find_bundles(roots: list[Path]) -> list[Path]:
    result: dict[str, Path] = {}
    visited: set[str] = set()

    def walk(directory: Path) -> None:
        try:
            resolved = str(directory.resolve())
            if resolved in visited:
                return
            visited.add(resolved)
            children = sorted(directory.iterdir(), key=lambda child: child.name.casefold())
        except OSError:
            return
        for child in children:
            try:
                if child.suffix.lower() == ".lv2" and child.is_dir():
                    resolved_bundle = child.resolve()
                    result[str(resolved_bundle)] = resolved_bundle
                elif child.is_dir() and not child.is_symlink():
                    walk(child)
            except OSError:
                continue

    for root in roots:
        if root.suffix.lower() == ".lv2" and root.is_dir():
            resolved = root.resolve()
            result[str(resolved)] = resolved
        elif root.is_dir():
            walk(root)
    return sorted(result.values(), key=lambda path: str(path).casefold())


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


def _load_bundle(bundle: Path) -> tuple[Graph, dict[str, str]]:
    graph = Graph()
    source_by_subject: dict[str, str] = {}
    files: list[Path] = []
    try:
        files = sorted((path for path in bundle.rglob("*")
                        if path.is_file() and path.suffix.lower() in {".ttl", ".rdf", ".n3"}),
                       key=lambda path: str(path).casefold())
    except OSError:
        pass
    manifest = bundle / "manifest.ttl"
    if manifest.is_file() and manifest not in files:
        files.insert(0, manifest)
    for source in files:
        try:
            parsed = Graph()
            rdf_format = {".ttl": "turtle", ".n3": "n3", ".rdf": "xml"}.get(
                source.suffix.lower())
            parsed.parse(source.as_posix(), format=rdf_format)
            for subject in set(parsed.subjects()):
                source_by_subject.setdefault(str(subject), str(source.resolve()))
            for triple in parsed:
                graph.add(triple)
        except Exception:
            # A broken optional metadata file should not hide valid plugins in
            # the same bundle. The bundle remains discoverable from other RDF.
            continue
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


def _bundle_index(bundle: Path) -> tuple[list[dict], list[dict]]:
    graph, source_by_subject = _load_bundle(bundle)
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
        for bundle in _find_bundles(paths):
            bundle_plugins, bundle_presets = _bundle_index(bundle)
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
