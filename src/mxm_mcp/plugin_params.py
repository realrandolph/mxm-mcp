"""VST3 parameter introspection and natural-text preset export.

This module powers the ``get_plugin_params`` and ``save_plugin_preset`` MCP
capabilities. It resolves a plugin name to the VST3 bundle MXM hosts, asks
:mod:`mxm_mcp.vst3_probe` (a separate process, so a faulty plugin cannot take
the server down) for the bundle's ``IEditController`` parameters, and writes
``.vstpreset`` files from the plugin's own component state plus overrides.

Values follow the VST3 model: the probe reports plain ``min``/``max``/
``default``/``current`` values for display, and every value that is applied is
*normalized* (0.0 to 1.0). Natural text such as ``"800 Hz"``, ``"-6 dB"`` or
``"1/16"`` is converted by the plugin itself through
``IEditController::getParamValueByString``; a small local parser covers plain
numbers and physical units when a plugin does not implement that call.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from . import plugin_permissions
from . import vst3

#! Parameter JSON for large synths is a few megabytes; the probe is bounded
#! the same way discovery is so a runaway plugin cannot fill memory.
_PROBE_OUTPUT_LIMIT = 32 * 1024 * 1024
_PROBE_TIMEOUT = 120.0
_MAX_RESOLUTION_ENTRIES = 4_096
_MAX_PARAMETER_TEXT = 2_048

_PARAMETER_FIELDS = (
    "id", "name", "module", "min", "max", "default", "current", "normalized",
    "display", "units", "stepped", "readonly", "hidden",
)
_PARAMETER_DETAIL_FIELDS = (
    "short_name", "default_normalized", "step_count", "unit_id",
    "can_automate", "is_list", "is_program_change", "is_bypass",
)

_NUMBER = re.compile(r"^\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*([A-Za-z%°µμ]*)\s*$")
_FREQUENCY_UNITS = {"hz", "khz", "mhz"}
_SECOND_UNITS = {"s", "sec", "secs", "second", "seconds"}
_SEMITONE_UNITS = {"st", "semitones", "semitone", "halfsteps", "halfstep"}


class PluginParameterError(RuntimeError):
    """A parameter or preset request could not be completed."""


# ---- plugin resolution --------------------------------------------------------------------

def _fold(text: str) -> str:
    return "".join(char for char in str(text).casefold() if char.isalnum())


def _bundle_entries(root: Path) -> list[Path]:
    """``.vst3`` entries to consider for one configured search root."""
    if root.suffix.lower() == ".vst3":
        return [root] if root.exists() else []
    try:
        if not root.is_dir():
            return []
        entries = []
        for index, entry in enumerate(root.iterdir()):
            if index >= _MAX_RESOLUTION_ENTRIES:
                break
            if entry.suffix.lower() == ".vst3":
                entries.append(entry)
        return entries
    except (OSError, RuntimeError):
        return []


def resolve_plugin_bundle(plugin: str) -> Path:
    """The ``.vst3`` bundle for a plugin path or bundle/plugin name.

    A path is used directly. Otherwise configured VST3 roots are checked for an
    exact bundle-name match, then a unique partial match, then names that
    already-probed descriptors reported. Nothing from a bundle is executed here.
    """
    query = str(plugin or "").strip()
    if not query:
        raise PluginParameterError("plugin is required")
    direct = Path(query).expanduser()
    if direct.suffix.lower() == ".vst3" and direct.exists():
        return direct.resolve()
    normalized = _fold(direct.stem if direct.suffix.lower() == ".vst3" else direct.name)
    exact: dict[str, Path] = {}
    partial: dict[str, Path] = {}
    if normalized:
        for raw_root in vst3.effective_search_paths():
            for entry in _bundle_entries(Path(raw_root).expanduser()):
                key = _fold(entry.stem)
                if key == normalized:
                    exact.setdefault(str(entry), entry)
                elif normalized in key:
                    partial.setdefault(str(entry), entry)
    if not exact:
        for module in plugin_permissions.cached_probe_modules(query):
            candidate = Path(module)
            if _fold(candidate.stem) == normalized:
                exact.setdefault(str(candidate), candidate)
            else:
                partial.setdefault(str(candidate), candidate)
    matches = exact or partial
    if len(matches) == 1:
        return Path(next(iter(matches.values()))).resolve()
    if not matches:
        raise PluginParameterError(
            f"no VST3 bundle matches {query!r}; pass the exact .vst3 bundle path")
    names = sorted({Path(path).stem for path in matches})
    raise PluginParameterError(
        f"{query!r} matches several VST3 bundles ({', '.join(names[:12])}); "
        "pass the exact .vst3 bundle path")


# ---- probe invocation ---------------------------------------------------------------------

def run_probe(arguments: list[str], *, timeout: float | None = None) -> dict:
    """Run :mod:`mxm_mcp.vst3_probe` in its own process and return its JSON."""
    command = [sys.executable, "-m", "mxm_mcp.vst3_probe", *arguments]
    try:
        stdout, stderr, status, timed_out, exceeded = vst3._run_capped_probe(
            command, _PROBE_OUTPUT_LIMIT, _PROBE_TIMEOUT if timeout is None else timeout)
    except OSError as exc:
        raise PluginParameterError(f"could not start the VST3 probe: {exc}") from exc
    if timed_out:
        raise PluginParameterError("the VST3 probe timed out")
    if exceeded:
        raise PluginParameterError("the VST3 probe produced too much output")
    stdout_text = stdout.decode("utf-8", "replace")
    for line in reversed(stdout_text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    detail = (stderr.decode("utf-8", "replace") or stdout_text).strip()
    raise PluginParameterError(detail or f"the VST3 probe exited with status {status}")


def decode_state(base_state) -> bytes:
    """The raw component state bytes for a base64 project ``state`` value."""
    if isinstance(base_state, (bytes, bytearray)):
        return bytes(base_state)
    text = str(base_state or "").strip()
    if text.startswith("base64:"):
        text = text[len("base64:"):].strip()
    if not text:
        return b""
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PluginParameterError("base_state is not valid base64") from exc


@contextmanager
def _state_file(base_state):
    """A temporary file holding the decoded base state, or ``None``."""
    data = decode_state(base_state)
    if not data:
        yield None
        return
    handle, path = tempfile.mkstemp(prefix="mxm-plugin-state-", suffix=".bin")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        yield path
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _invoke(arguments: list[str], probe) -> dict:
    runner = probe or run_probe
    payload = runner(list(arguments))
    if not isinstance(payload, dict):
        raise PluginParameterError("the VST3 probe returned no result")
    return payload


def _probe_error(payload: dict, context: str) -> PluginParameterError:
    return PluginParameterError(f"{context}: {payload.get('error')}")


# ---- parameter records --------------------------------------------------------------------

def full_parameter_name(param: dict) -> str:
    """``"Module/Name"`` for a parameter, or just its name at the root unit."""
    module = str(param.get("module") or "").strip("/ ")
    name = str(param.get("name") or "")
    return f"{module}/{name}" if module else name


def project_parameter(param: dict, details: bool = False) -> dict:
    """Trim a probe parameter record to the fields a tool call should expose."""
    record = {key: param[key] for key in _PARAMETER_FIELDS if key in param}
    if "id" in record:
        try:
            record["id"] = int(record["id"])
        except (TypeError, ValueError):
            pass
    if details:
        record.update({key: param[key] for key in _PARAMETER_DETAIL_FIELDS if key in param})
    return record


def parameter_matches(param: dict, search: str) -> bool:
    """Whether *param* matches every whitespace-separated token in *search*.

    A token matches the parameter name, the ``Module/Name`` path, or - when it
    is ``#id`` - the parameter id itself.
    """
    tokens = [token for token in str(search or "").split() if token]
    if not tokens:
        return True
    name = str(param.get("name") or "").casefold()
    module = str(param.get("module") or "").casefold()
    path = full_parameter_name(param).casefold()
    for token in tokens:
        if token.startswith("#"):
            wanted = token[1:]
            try:
                if int(wanted, 0) != int(param.get("id", -1)):
                    return False
            except (TypeError, ValueError):
                return False
            continue
        lowered = token.casefold()
        if "/" in lowered:
            module_token, _, name_token = lowered.partition("/")
            if module_token in module and name_token in name:
                continue
        if lowered not in name and lowered not in path:
            return False
    return True


def find_parameter(parameters: list[dict], key: str) -> dict | None:
    """The one parameter a name, ``Module/Name`` or ``#id`` key selects."""
    text = str(key or "").strip()
    if not text:
        return None
    if text.startswith("#"):
        stem = text[1:].strip()
        if stem:
            try:
                wanted = int(stem, 0)
            except ValueError:
                wanted = None
            if wanted is not None:
                return next((item for item in parameters
                             if int(item.get("id", -1)) == wanted), None)
    matches = [item for item in parameters if parameter_matches(item, text)]
    if not matches and text.isdigit():
        return next((item for item in parameters
                     if int(item.get("id", -1)) == int(text)), None)
    if len(matches) == 1:
        return matches[0]
    exact = [item for item in matches
             if str(item.get("name") or "").casefold() == text.casefold()
             or full_parameter_name(item).casefold() == text.casefold()]
    if len(exact) == 1:
        return exact[0]
    if not matches:
        return None
    raise PluginParameterError(
        f"{key!r} matches {len(matches)} parameters; use Module/Name or #id")


# ---- natural text -------------------------------------------------------------------------
#
# The plugin's own getParamValueByString is authoritative, because ranges,
# curves and discrete lists are plugin-defined. This parser is the fallback
# for controllers that do not implement it and covers plain numbers with the
# physical units those plugins usually show ("800 Hz", "-6 dB", "50%", "250 ms").

def _unit_scale(unit: str, parameter_units: str) -> float:
    key = unit.casefold().replace("µ", "u").replace("μ", "u")
    base = parameter_units.casefold()
    if key == "khz":
        return 1_000.0
    if key == "mhz":
        return 1_000_000.0
    if key == "hz" and base.startswith("k"):
        return 0.001
    if key in {"ms", "msec", "milliseconds"} and base in _SECOND_UNITS:
        return 0.001
    if key == "%" and base not in {"%", "percent"}:
        return 0.01
    if key in {"ct", "cent", "cents"} and base in _SEMITONE_UNITS:
        return 0.01
    return 1.0


def parse_natural_value(text: str, param: dict) -> float | None:
    """A plain parameter value for display text, or ``None`` if not numeric."""
    text = str(text or "")[:_MAX_PARAMETER_TEXT]
    match = _NUMBER.match(text)
    if not match:
        return None
    number = float(match.group(1))
    if not math.isfinite(number):
        return None
    plain = number * _unit_scale(match.group(2) or "", str(param.get("units") or ""))
    low, high = param.get("min"), param.get("max")
    try:
        low, high = float(low), float(high)
    except (TypeError, ValueError):
        return plain
    if math.isfinite(low) and math.isfinite(high) and high > low:
        plain = min(high, max(low, plain))
    return plain


def _is_frequency(param: dict) -> bool:
    return str(param.get("units") or "").strip().casefold() in _FREQUENCY_UNITS


def plain_to_normalized(plain: float, param: dict) -> float:
    """Normalize a plain value using the range, log-style for frequencies."""
    low = float(param.get("min", 0.0) or 0.0)
    high = float(param.get("max", 1.0) or 1.0)
    if not high > low:
        return 0.0
    if _is_frequency(param) and low > 0 and plain > 0:
        return min(1.0, max(0.0, math.log(plain / low) / math.log(high / low)))
    return min(1.0, max(0.0, (plain - low) / (high - low)))


def text_to_normalized(text: str, param: dict) -> float | None:
    """A normalized value for text, or ``None`` when it is not numeric."""
    plain = parse_natural_value(text, param)
    if plain is None:
        return None
    return plain_to_normalized(plain, param)


# ---- capabilities -------------------------------------------------------------------------

def get_plugin_parameters(plugin: str, preset: str = "", search: str = "",
                          details: bool = False, *, cid: str = "",
                          probe=None) -> dict:
    """Enumerate a VST3 plugin's parameters, optionally filtered by *search*.

    *preset* loads a ``.vstpreset`` file first so the reported ``current``
    values and display text reflect that patch. A search token matches the
    name, ``Module/Name`` or ``#id``. *details* keeps the extra flags and short
    titles out of the default response.
    """
    bundle = resolve_plugin_bundle(plugin)
    preset_path = Path(preset).expanduser() if preset else None
    arguments = ["--params", str(bundle)]
    if preset_path is not None:
        if not preset_path.exists():
            raise PluginParameterError(f"preset not found: {preset_path}")
        arguments += ["--preset", str(preset_path)]
    if cid:
        arguments += ["--cid", cid]
    payload = _invoke(arguments, probe)
    if payload.get("error"):
        raise _probe_error(payload, f"could not inspect {plugin!r}")
    parameters = [project_parameter(item, details)
                  for item in payload.get("parameters") or [] if isinstance(item, dict)]
    filtered = [item for item in parameters if parameter_matches(item, search)]
    result = {
        "plugin": plugin,
        "module": str(bundle),
        "count": len(filtered),
        "parameters": filtered,
    }
    if payload.get("class"):
        result["class"] = payload["class"]
    if preset_path is not None:
        result["preset"] = str(preset_path)
    warnings = list(payload.get("warnings") or [])
    if warnings:
        result["warnings"] = warnings
    return result


def _override_arguments(params: dict, parameters: list[dict]) -> tuple[list[str], dict[str, dict]]:
    """Probe arguments for ``params`` and what was requested per parameter."""
    arguments: list[str] = []
    requested: dict[str, dict] = {}
    for key, value in params.items():
        record = find_parameter(parameters, key)
        if record is None:
            raise PluginParameterError(f"no parameter matches {key!r}")
        param_id = int(record["id"])
        if isinstance(value, bool) or isinstance(value, (int, float)):
            normalized = float(value)
            if not math.isfinite(normalized) or not 0.0 <= normalized <= 1.0:
                raise PluginParameterError(
                    f"{key!r}: numeric values are normalized 0..1; "
                    f"pass text such as '1.2 kHz' for plain values")
            arguments += ["--set", f"{param_id}={normalized!r}"]
            requested[key] = {"id": param_id, "normalized": normalized}
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                raise PluginParameterError(f"{key!r}: text value is empty")
            fallback = text_to_normalized(text, record)
            arguments += ["--set-text", f"{param_id}={text}"]
            if fallback is not None:
                arguments += ["--set-fallback", f"{param_id}={fallback!r}"]
            requested[key] = {"id": param_id, "text": text, "fallback": fallback}
        else:
            raise PluginParameterError(
                f"{key!r}: expected a number or a string, got {type(value).__name__}")
    return arguments, requested


def save_plugin_preset_file(plugin: str, out_path: str, base_preset: str = "",
                            base_state: str = "", params: dict | None = None, *,
                            cid: str = "", probe=None) -> dict:
    """Write a ``.vstpreset`` for *plugin* with parameter overrides applied.

    *params* maps a parameter name, ``Module/Name`` or ``#id`` to either a
    normalized number (0..1) or natural text the plugin understands. String
    values are converted by the plugin; plain numbers with physical units are
    parsed locally when it cannot. The preset starts from *base_preset* (a
    ``.vstpreset`` file) or *base_state* (a base64 component state), and its
    component state is reloaded to check the overrides survived.
    """
    overrides = dict(params or {})
    bundle = resolve_plugin_bundle(plugin)
    destination = Path(out_path).expanduser()
    if destination.suffix.lower() != ".vstpreset":
        destination = destination.with_name(f"{destination.name}.vstpreset")
    base_preset_path = Path(base_preset).expanduser() if base_preset else None
    if base_preset_path is not None and not base_preset_path.exists():
        raise PluginParameterError(f"base preset not found: {base_preset_path}")
    with _state_file(base_state) as state_path:
        listing_arguments = ["--params", str(bundle)]
        save_arguments = ["--save-preset", str(bundle), "--out", str(destination)]
        for arguments in (listing_arguments, save_arguments):
            if base_preset_path is not None:
                arguments += ["--preset", str(base_preset_path)]
            if state_path is not None:
                arguments += ["--state-file", state_path]
            if cid:
                arguments += ["--cid", cid]
        listing = _invoke(listing_arguments, probe)
        if listing.get("error"):
            raise _probe_error(listing, f"could not inspect {plugin!r}")
        parameters = [item for item in listing.get("parameters") or []
                      if isinstance(item, dict)]
        if not parameters:
            raise PluginParameterError(f"{plugin!r} exposes no parameters to set")
        override_arguments, requested = _override_arguments(overrides, parameters)
        payload = _invoke(save_arguments + override_arguments, probe)
        if payload.get("error"):
            raise _probe_error(payload, f"could not save a preset for {plugin!r}")
        try:
            size = destination.stat().st_size
        except OSError as exc:
            raise PluginParameterError(
                f"the probe did not write {destination}: {exc}") from exc
    applied = int(payload.get("parameters_applied", 0))
    warnings = list(payload.get("warnings") or [])
    for item in payload.get("parameter_results") or []:
        if isinstance(item, dict) and item.get("source") == "fallback":
            warnings.append(
                f"parameter #{item.get('id')} used the local text parser for {item.get('text')!r}")
    if applied < len(requested):
        warnings.append(f"{len(requested) - applied} override(s) were not applied")
    applied_values = {
        int(item["id"]): float(item["normalized"])
        for item in payload.get("applied") or []
        if isinstance(item, dict) and "id" in item and "normalized" in item
    }
    unverified: list[int] = []
    if applied_values:
        check = _invoke(["--params", str(bundle), "--preset", str(destination)], probe)
        if check.get("error"):
            unverified = sorted(applied_values)
            warnings.append(
                f"could not reload the saved preset to verify it: {check.get('error')}")
        else:
            saved = {
                int(item["id"]): item.get("normalized")
                for item in check.get("parameters") or []
                if isinstance(item, dict) and "id" in item
            }
            unverified = [
                param_id for param_id, wanted in applied_values.items()
                if saved.get(param_id) is None
                or abs(float(saved[param_id]) - wanted) > 1e-3
            ]
            if unverified:
                warnings.append(
                    "the saved preset does not reload every override; this plugin may keep "
                    "parameter values outside its component state")
    result = {
        "path": str(destination),
        "bytes": size,
        "plugin": plugin,
        "parameters_applied": applied,
        "verified": not unverified,
    }
    if unverified:
        result["unverified_parameters"] = unverified
    if payload.get("class"):
        result["class"] = payload["class"]
    if warnings:
        result["warnings"] = warnings
    return result
