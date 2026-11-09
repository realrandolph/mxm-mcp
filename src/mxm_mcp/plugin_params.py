"""VST3 parameter introspection, natural-text parsing, and preset export."""

import base64, binascii, json, math, os, re, sys, tempfile
from contextlib import contextmanager
from pathlib import Path

from . import plugin_permissions, vst3

__all__ = ["PluginParameterError", "resolve_plugin_bundle", "run_probe", "decode_state", "full_parameter_name",
           "project_parameter", "parameter_matches", "find_parameter", "parse_natural_value",
           "plain_to_normalized", "text_to_normalized", "get_plugin_parameters", "save_plugin_preset_file"]

_PROBE_TIMEOUT, _PROBE_OUTPUT_LIMIT = 15.0, 4 * 1024 * 1024
_MAX_PARAMETER_TEXT, _MAX_RESOLUTION_ENTRIES = 256, 512
_PARAMETER_FIELDS = "id name module min max default current normalized display units stepped readonly hidden".split()
_PARAMETER_DETAIL_FIELDS = "short_name default_normalized step_count unit_id can_automate is_list is_program_change is_bypass".split()
_NUMBER = re.compile(r"^\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*([A-Za-z%°µμ]*)\s*$")
_FREQUENCY_UNITS = {"hz", "khz", "mhz"}
_SECOND_UNITS = {"s", "sec", "secs", "second", "seconds"}
_SEMITONE_UNITS = {"st", "semitones", "semitone", "halfsteps", "halfstep"}


class PluginParameterError(RuntimeError): pass


def _fold(text: str) -> str:
    return "".join(c for c in str(text).casefold() if c.isalnum())


def _bundle_entries(root: Path) -> list[Path]:
    if root.suffix.lower() == ".vst3": return [root] if root.exists() else []
    try: return [e for e in list(root.iterdir())[:_MAX_RESOLUTION_ENTRIES] if e.suffix.lower() == ".vst3"] if root.is_dir() else []
    except (OSError, RuntimeError): return []


def resolve_plugin_bundle(plugin: str) -> Path:
    query = str(plugin or "").strip()
    if not query: raise PluginParameterError("plugin is required")
    direct = Path(query).expanduser()
    if direct.suffix.lower() == ".vst3" and direct.exists(): return direct.resolve()
    norm = _fold(direct.stem if direct.suffix.lower() == ".vst3" else direct.name)
    exact, partial = {}, {}
    if norm:
        for e in (e for r in vst3.effective_search_paths() for e in _bundle_entries(Path(r).expanduser())):
            k = _fold(e.stem)
            if k == norm: exact.setdefault(str(e), e)
            elif norm in k: partial.setdefault(str(e), e)
    if not exact:
        for cand in map(Path, plugin_permissions.cached_probe_modules(query)):
            (exact if _fold(cand.stem) == norm else partial).setdefault(str(cand), cand)
    matches = exact or partial
    if len(matches) == 1: return Path(next(iter(matches.values()))).resolve()
    if not matches: raise PluginParameterError(f"no VST3 bundle matches {query!r}; pass the exact .vst3 bundle path")
    names = sorted({Path(p).stem for p in matches})
    raise PluginParameterError(f"{query!r} matches several VST3 bundles ({', '.join(names[:12])}); pass the exact .vst3 bundle path")


def run_probe(arguments: list[str], *, timeout: float | None = None) -> dict:
    cmd = [sys.executable, "-m", "mxm_mcp.vst3_probe", *arguments]
    try:
        stdout, stderr, status, timed_out, exceeded = vst3._run_capped_probe(cmd, _PROBE_OUTPUT_LIMIT, _PROBE_TIMEOUT if timeout is None else timeout)
    except OSError as exc: raise PluginParameterError(f"could not start the VST3 probe: {exc}") from exc
    if timed_out: raise PluginParameterError("the VST3 probe timed out")
    if exceeded: raise PluginParameterError("the VST3 probe produced too much output")
    txt = stdout.decode("utf-8", "replace")
    for line in reversed(txt.splitlines()):
        try: p = json.loads(line.strip())
        except json.JSONDecodeError: continue
        if isinstance(p, dict): return p
    detail = (stderr.decode("utf-8", "replace") or txt).strip()
    raise PluginParameterError(detail or f"the VST3 probe exited with status {status}")


def decode_state(base_state) -> bytes:
    if isinstance(base_state, (bytes, bytearray)): return bytes(base_state)
    text = str(base_state or "").strip()
    if text.startswith("base64:"): text = text[7:].strip()
    if not text: return b""
    try: return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc: raise PluginParameterError("base_state is not valid base64") from exc


@contextmanager
def _state_file(base_state):
    data = decode_state(base_state)
    if not data: yield None; return
    fd, path = tempfile.mkstemp(prefix="mxm-plugin-state-", suffix=".bin")
    try:
        with os.fdopen(fd, "wb") as s: s.write(data)
        yield path
    finally:
        try: os.unlink(path)
        except FileNotFoundError: pass


def _invoke(arguments: list[str], probe) -> dict:
    p = (probe or run_probe)(list(arguments))
    if not isinstance(p, dict): raise PluginParameterError("the VST3 probe returned no result")
    return p


def full_parameter_name(param: dict) -> str:
    m, n = str(param.get("module") or "").strip("/ "), str(param.get("name") or "")
    return f"{m}/{n}" if m else n


def project_parameter(param: dict, details: bool = False) -> dict:
    rec = {k: param[k] for k in (_PARAMETER_FIELDS + _PARAMETER_DETAIL_FIELDS if details else _PARAMETER_FIELDS) if k in param}
    if "id" in rec:
        try: rec["id"] = int(rec["id"])
        except (TypeError, ValueError): pass
    return rec


def parameter_matches(param: dict, search: str) -> bool:
    n, m = str(param.get("name") or "").casefold(), str(param.get("module") or "").casefold()
    full = full_parameter_name(param).casefold()
    for tok in str(search or "").split():
        if tok.startswith("#"):
            try:
                if int(tok[1:], 0) != int(param.get("id", -1)): return False
            except (TypeError, ValueError): return False
            continue
        low = tok.casefold()
        if "/" in low and all(a in b for a, b in zip(low.split("/", 1), (m, n))): continue
        if low not in n and low not in full: return False
    return True


def find_parameter(parameters: list[dict], key: str) -> dict | None:
    text = str(key or "").strip()
    if not text: return None
    if text.startswith("#") and (stem := text[1:].strip()):
        try: return next((i for i in parameters if int(i.get("id", -1)) == int(stem, 0)), None)
        except ValueError: pass
    matches = [i for i in parameters if parameter_matches(i, text)]
    if not matches and text.isdigit(): return next((i for i in parameters if int(i.get("id", -1)) == int(text)), None)
    if len(matches) == 1: return matches[0]
    exact = [i for i in matches if text.casefold() in (str(i.get("name") or "").casefold(), full_parameter_name(i).casefold())]
    if len(exact) == 1: return exact[0]
    if not matches: return None
    raise PluginParameterError(f"{key!r} matches {len(matches)} parameters; use Module/Name or #id")


def _unit_scale(unit: str, parameter_units: str) -> float:
    k, b = unit.casefold().replace("µ", "u").replace("μ", "u"), parameter_units.casefold()
    if k == "khz": return 1000.0
    if k == "mhz": return 1000000.0
    if k == "hz" and b.startswith("k"): return 0.001
    if k in {"ms", "msec", "milliseconds"} and b in _SECOND_UNITS: return 0.001
    if k == "%" and b not in {"%", "percent"}: return 0.01
    if k in {"ct", "cent", "cents"} and b in _SEMITONE_UNITS: return 0.01
    return 1.0


def parse_natural_value(text: str, param: dict) -> float | None:
    m = _NUMBER.match(str(text or "")[:_MAX_PARAMETER_TEXT])
    if not m: return None
    if not math.isfinite(num := float(m.group(1))): return None
    plain = num * _unit_scale(m.group(2) or "", str(param.get("units") or ""))
    try:
        low, high = float(param.get("min")), float(param.get("max"))
        if math.isfinite(low) and math.isfinite(high) and high > low: plain = min(high, max(low, plain))
    except (TypeError, ValueError): pass
    return plain


def plain_to_normalized(plain: float, param: dict) -> float:
    low, high = float(param.get("min", 0.0) or 0.0), float(param.get("max", 1.0) or 1.0)
    if not high > low: return 0.0
    if str(param.get("units") or "").strip().casefold() in _FREQUENCY_UNITS and low > 0 and plain > 0:
        return min(1.0, max(0.0, math.log(plain / low) / math.log(high / low)))
    return min(1.0, max(0.0, (plain - low) / (high - low)))


def text_to_normalized(text: str, param: dict) -> float | None:
    plain = parse_natural_value(text, param)
    return plain_to_normalized(plain, param) if plain is not None else None


def get_plugin_parameters(plugin: str, preset: str = "", search: str = "", details: bool = False, *, cid: str = "", probe=None) -> dict:
    bundle, p_path = resolve_plugin_bundle(plugin), Path(preset).expanduser() if preset else None
    if p_path and not p_path.exists(): raise PluginParameterError(f"preset not found: {p_path}")
    args = ["--params", str(bundle)] + (["--preset", str(p_path)] if p_path else []) + (["--cid", cid] if cid else [])
    payload = _invoke(args, probe)
    if payload.get("error"): raise PluginParameterError(f"could not inspect {plugin!r}: {payload.get('error')}")
    params = [project_parameter(i, details) for i in payload.get("parameters") or [] if isinstance(i, dict)]
    filtered = [i for i in params if parameter_matches(i, search)]
    res = {"plugin": plugin, "module": str(bundle), "count": len(filtered), "parameters": filtered}
    if payload.get("class"): res["class"] = payload["class"]
    if p_path: res["preset"] = str(p_path)
    if payload.get("warnings"): res["warnings"] = list(payload["warnings"])
    return res


def _override_arguments(params: dict, parameters: list[dict]) -> tuple[list[str], dict[str, dict]]:
    args, req = [], {}
    for k, v in params.items():
        rec = find_parameter(parameters, k)
        if rec is None: raise PluginParameterError(f"no parameter matches {k!r}")
        pid = int(rec["id"])
        if isinstance(v, (int, float)):
            val = float(v)
            if not math.isfinite(val) or not 0.0 <= val <= 1.0:
                raise PluginParameterError(f"{k!r}: numeric values are normalized 0..1; pass text such as '1.2 kHz' for plain values")
            args += ["--set", f"{pid}={val!r}"]; req[k] = {"id": pid, "normalized": val}
        elif isinstance(v, str):
            txt = v.strip()
            if not txt: raise PluginParameterError(f"{k!r}: text value is empty")
            fb = text_to_normalized(txt, rec)
            args += ["--set-text", f"{pid}={txt}"]
            if fb is not None: args += ["--set-fallback", f"{pid}={fb!r}"]
            req[k] = {"id": pid, "text": txt, "fallback": fb}
        else:
            raise PluginParameterError(f"{k!r}: expected a number or a string, got {type(v).__name__}")
    return args, req


def save_plugin_preset_file(plugin: str, out_path: str, base_preset: str = "", base_state: str = "",
                            params: dict | None = None, *, cid: str = "", probe=None) -> dict:
    overrides, bundle = dict(params or {}), resolve_plugin_bundle(plugin)
    dest = Path(out_path).expanduser()
    if dest.suffix.lower() != ".vstpreset": dest = dest.with_name(f"{dest.name}.vstpreset")
    b_path = Path(base_preset).expanduser() if base_preset else None
    if b_path and not b_path.exists(): raise PluginParameterError(f"base preset not found: {b_path}")
    with _state_file(base_state) as state_path:
        list_args, save_args = ["--params", str(bundle)], ["--save-preset", str(bundle), "--out", str(dest)]
        for a in (list_args, save_args):
            if b_path: a += ["--preset", str(b_path)]
            if state_path: a += ["--state-file", state_path]
            if cid: a += ["--cid", cid]
        listing = _invoke(list_args, probe)
        if listing.get("error"): raise PluginParameterError(f"could not inspect {plugin!r}: {listing.get('error')}")
        parameters = [i for i in listing.get("parameters") or [] if isinstance(i, dict)]
        if not parameters: raise PluginParameterError(f"{plugin!r} exposes no parameters to set")
        ov_args, requested = _override_arguments(overrides, parameters)
        payload = _invoke(save_args + ov_args, probe)
        if payload.get("error"): raise PluginParameterError(f"could not save a preset for {plugin!r}: {payload.get('error')}")
        try: size = dest.stat().st_size
        except OSError as exc: raise PluginParameterError(f"the probe did not write {dest}: {exc}") from exc

    applied = int(payload.get("parameters_applied", 0))
    warnings = list(payload.get("warnings") or [])
    warnings += [f"parameter #{it.get('id')} used the local text parser for {it.get('text')!r}"
                 for it in payload.get("parameter_results") or [] if isinstance(it, dict) and it.get("source") == "fallback"]
    if applied < len(requested): warnings.append(f"{len(requested) - applied} override(s) were not applied")
    applied_vals = {int(i["id"]): float(i["normalized"]) for i in payload.get("applied") or [] if isinstance(i, dict) and "id" in i and "normalized" in i}
    unverified: list[int] = []
    if applied_vals:
        chk = _invoke(["--params", str(bundle), "--preset", str(dest)], probe)
        if chk.get("error"):
            unverified = sorted(applied_vals)
            warnings.append(f"could not reload the saved preset to verify it: {chk.get('error')}")
        else:
            saved = {int(i["id"]): i.get("normalized") for i in chk.get("parameters") or [] if isinstance(i, dict) and "id" in i}
            unverified = [pid for pid, want in applied_vals.items() if saved.get(pid) is None or abs(float(saved[pid]) - want) > 1e-3]
            if unverified: warnings.append("the saved preset does not reload every override; this plugin may keep parameter values outside its component state")
    res = {"path": str(dest), "bytes": size, "plugin": plugin, "parameters_applied": applied, "verified": not unverified}
    if unverified: res["unverified_parameters"] = unverified
    if payload.get("class"): res["class"] = payload["class"]
    if warnings: res["warnings"] = warnings
    return res
