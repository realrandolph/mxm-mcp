"""Standalone VST3 module introspection for MXM's native VST3 host."""

from __future__ import annotations

import ctypes
import json
import math
import os
import plistlib
import re
import struct
import sys
import tempfile
from pathlib import Path

from . import vst3_platform as plat
from .path_safety import resolved_path_within

_MAX_CLASSES, _MAX_PARAMETERS, _MAX_UNITS, _STRING128 = 4_096, 100_000, 4_096, 128
_MAX_PRESET_BYTES, _MAX_STATE_BYTES, _MAX_PRESET_CHUNKS = 64 * 1024 * 1024, 64 * 1024 * 1024, 256
_IID_FACTORY2, _IID_FACTORY3 = plat.iid_bytes("factory2"), plat.iid_bytes("factory3")
_vp, _i32, _i64, _u32, _u16, _f64 = ctypes.c_void_p, ctypes.c_int32, ctypes.c_int64, ctypes.c_uint32, ctypes.c_uint16, ctypes.c_double
_P = ctypes.POINTER

def _iid_words(l1: int, l2: int, l3: int, l4: int) -> bytes:
    return (struct.pack("<I", l1) + struct.pack("<HH", l2 >> 16, l2 & 0xFFFF) + struct.pack(">II", l3, l4)
            if plat.com_compatible() else struct.pack(">IIII", l1, l2, l3, l4))

_IID_UNKNOWN = _iid_words(0x00000000, 0x00000000, 0xC0000000, 0x00000046)
_IID_COMPONENT = _iid_words(0xE831FF31, 0xF2D54301, 0x928EBBEE, 0x25697802)
_IID_EDIT_CONTROLLER = _iid_words(0xDCD7BBE3, 0x7742448D, 0xA874AACC, 0x979C759E)
_IID_UNIT_INFO = _iid_words(0x3D4BD6B5, 0x913A4FD2, 0xA886E768, 0xA5EB92C1)
_IID_BSTREAM = _iid_words(0xC3BF6EA2, 0x30994752, 0x9B6BF990, 0x1EE33E9B)
_IID_CONNECTION_POINT = _iid_words(0x70A4156F, 0x6E6E4026, 0x989148BF, 0xAA60D8D1)
_IID_AUDIO_PROCESSOR = _iid_words(0x42043F99, 0xB7DA453C, 0xA569E79D, 0x9AAEC33D)
_IID_HOST_APPLICATION = _iid_words(0x58E595CC, 0xDB2D4969, 0x8B6AAF8C, 0x36A664E5)
_PARAM_CAN_AUTOMATE, _PARAM_READONLY, _PARAM_LIST = 1 << 0, 1 << 1, 1 << 3
_PARAM_HIDDEN, _PARAM_PROGRAM_CHANGE, _PARAM_BYPASS = 1 << 4, 1 << 15, 1 << 16
_NO_INTERFACE, _RESULT_OK, _SEEK_SET, _SEEK_CUR, _SEEK_END = ctypes.c_int32(0x80004002).value if plat.WINDOWS else -1, 0, 0, 1, 2
_CDLL, _FUNCTYPE = ctypes.CDLL, (ctypes.WINFUNCTYPE if plat.WINDOWS else ctypes.CFUNCTYPE)

if plat.WINDOWS:
    try:
        ctypes.windll.ole32.CoInitializeEx(None, 2)
    except Exception:
        pass

class _PFactoryInfo(ctypes.Structure):
    _fields_ = [("vendor", ctypes.c_char * 64), ("url", ctypes.c_char * 256), ("email", ctypes.c_char * 128), ("flags", _i32)]

class _PClassInfo2(ctypes.Structure):
    _fields_ = [("cid", ctypes.c_ubyte * 16), ("cardinality", _i32), ("category", ctypes.c_char * 32), ("name", ctypes.c_char * 64),
                ("class_flags", _u32), ("sub_categories", ctypes.c_char * 128), ("vendor", ctypes.c_char * 64),
                ("version", ctypes.c_char * 64), ("sdk_version", ctypes.c_char * 64)]

class _PClassInfoW(ctypes.Structure):
    _fields_ = [("cid", ctypes.c_ubyte * 16), ("cardinality", _i32), ("category", ctypes.c_char * 32), ("name", _u16 * 64),
                ("class_flags", _u32), ("sub_categories", ctypes.c_char * 128), ("vendor", _u16 * 64),
                ("version", _u16 * 64), ("sdk_version", _u16 * 64)]

class _PParameterInfo(ctypes.Structure):
    _fields_ = [("id", _u32), ("title", _u16 * _STRING128), ("short_title", _u16 * _STRING128), ("units", _u16 * _STRING128),
                ("step_count", _i32), ("default_normalized", _f64), ("unit_id", _i32), ("flags", _i32)]

class _PUnitInfo(ctypes.Structure):
    _fields_ = [("id", _i32), ("parent_id", _i32), ("name", _u16 * _STRING128), ("program_list_id", _i32)]

class _PInterface(ctypes.Structure):
    _fields_ = [("vtable", _vp)]

class _PProcessSetup(ctypes.Structure):
    _fields_ = [("process_mode", _i32), ("symbolic_sample_size", _i32), ("max_samples_per_block", _i32), ("sample_rate", _f64)]

class _PBusInfo(ctypes.Structure):
    _fields_ = [("media_type", _i32), ("direction", _i32), ("channel_count", _i32), ("name", _u16 * _STRING128), ("bus_type", _i32), ("flags", _u32)]

class _PAudioBusBuffers(ctypes.Structure):
    _fields_ = [("num_channels", _i32), ("silence_flags", ctypes.c_uint64), ("channel_buffers", _P(_P(ctypes.c_float)))]

class _PProcessContext(ctypes.Structure):
    _fields_ = [("state", _u32), ("sample_rate", _f64), ("project_time_samples", _i64), ("system_time", _i64),
                ("continuous_time_samples", _i64), ("project_time_music", _f64), ("bar_position_music", _f64),
                ("cycle_start_music", _f64), ("cycle_end_music", _f64), ("tempo", _f64), ("time_sig_numerator", _i32),
                ("time_sig_denominator", _i32), ("chord_key_note", ctypes.c_uint8), ("chord_root_note", ctypes.c_uint8),
                ("chord_mask", ctypes.c_int16), ("smpte_offset_subframes", _i32), ("frame_rate_fps", _u32),
                ("frame_rate_flags", _u32), ("samples_to_next_clock", _i32)]

class _PProcessData(ctypes.Structure):
    _fields_ = [("process_mode", _i32), ("symbolic_sample_size", _i32), ("num_samples", _i32), ("num_inputs", _i32),
                ("num_outputs", _i32), ("inputs", _P(_PAudioBusBuffers)), ("outputs", _P(_PAudioBusBuffers)),
                ("input_parameter_changes", _vp), ("output_parameter_changes", _vp), ("input_events", _vp),
                ("output_events", _vp), ("process_context", _P(_PProcessContext))]

def _fn(restype, *argtypes):
    """A COM method type: every VST3 vtable slot takes ``this`` first."""
    return _FUNCTYPE(restype, _vp, *argtypes)

def _decode(val: bytes) -> str: return val.split(b"\x00", 1)[0].decode("utf-8", "replace")

def _decode16(val) -> str: return bytes(val).decode("utf-16-le", "replace").split("\x00", 1)[0]

def _address(val) -> int: return int(val.value or 0) if isinstance(val, ctypes.c_void_p) else int(val)

def _error(path, exc) -> dict: return {"module": str(path), "error": f"{type(exc).__name__}: {exc}"}

def _bind(handle, slot: int, restype, argtypes):
    vtable = ctypes.cast(_vp(_address(handle)), _P(_vp))[0]
    return _FUNCTYPE(restype, *argtypes)(ctypes.cast(_vp(vtable), _P(_vp))[slot])

def _query_interface(handle, iid: bytes):
    query, res, buf = _bind(handle, 0, _i32, (_vp, _vp, _P(_vp))), _vp(), (ctypes.c_ubyte * len(iid)).from_buffer_copy(iid)
    return res.value or None if query(_address(handle), ctypes.byref(buf), ctypes.byref(res)) == 0 else None

def _release(handle) -> None: _bind(handle, 2, _u32, (_vp,))(_address(handle))

def _initialize(handle, context=None) -> int: return _bind(handle, 3, _i32, (_vp, _vp))(_address(handle), context)

def _terminate(handle) -> None: _bind(handle, 4, _i32, (_vp,))(_address(handle))

def _create_instance(factory, class_id: bytes, interface_id: bytes):
    if len(class_id) != 16 or len(interface_id) != 16:
        return None
    create, res = _bind(factory, 6, _i32, (_vp, ctypes.c_char_p, ctypes.c_char_p, _P(_vp))), _vp()
    return res.value or None if create(_address(factory), class_id, interface_id, ctypes.byref(res)) == 0 else None

def cid_bytes(cid: str) -> bytes:
    txt = "".join(str(cid).split())
    if len(txt) != 32:
        raise ValueError("a VST3 class id must be 32 hex characters")
    raw = bytes.fromhex(txt)
    return (struct.pack("<I", int(txt[0:8], 16)) + struct.pack("<HH", int(txt[8:12], 16), int(txt[12:16], 16)) + raw[8:16]
            if plat.com_compatible() else raw)

def _encode_text(text: str):
    raw = str(text).encode("utf-16-le")[:2 * (_STRING128 - 1)]
    raw, buf = raw[:len(raw) - (len(raw) % 2)], (ctypes.c_uint16 * _STRING128)()
    ctypes.memmove(buf, raw + b"\x00\x00", len(raw) + 2)
    return buf

def format_display(text: str, units: str) -> str:
    t, u = (text or "").strip(), (units or "").strip()
    return t if not t or not u or u.casefold() in t.casefold() else f"{t} {u}"

def _init_com(instance, callbacks: list) -> None:
    instance._callbacks = callbacks
    instance._vtable = (_vp * len(callbacks))(*(ctypes.cast(c, _vp) for c in callbacks))
    instance._object = _PInterface(ctypes.cast(instance._vtable, _vp))
    instance.pointer = _vp(ctypes.addressof(instance._object))

def _com_entries(obj, supported: tuple[bytes, ...]) -> list:
    """queryInterface/addRef/release, identical on every COM object."""
    return [_fn(_i32, _vp, _P(_vp))(lambda _t, iid, tgt: _handle_qi(obj._object, iid, tgt, supported)),
            _fn(_u32)(lambda _this: 1), _fn(_u32)(lambda _this: 0)]

def _handle_qi(obj, iid, target, supported: tuple[bytes, ...]) -> int:
    if not target:
        return _NO_INTERFACE
    req = ctypes.string_at(iid, 16) if iid else b""
    if req in supported or req == _IID_UNKNOWN:
        target[0] = _vp(ctypes.addressof(obj))
        return _RESULT_OK
    target[0] = None
    return _NO_INTERFACE

def _set_none(tgt): tgt[0] = None

class _MemoryStream:
    _iid = _IID_BSTREAM

    def __init__(self, data: bytes = b"") -> None:
        self._data, self._position = bytearray(data), 0
        _init_com(self, _com_entries(self, (self._iid,)) + [
            _fn(_i32, _vp, _i32, _P(_i32))(self._read), _fn(_i32, _vp, _i32, _P(_i32))(self._write),
            _fn(_i32, _i64, _i32, _P(_i64))(self._seek), _fn(_i32, _P(_i64))(self._tell)])

    def _query_interface(self, _this, iid, target):
        return _handle_qi(self._object, iid, target, (self._iid,))

    def _read(self, _this, buffer, num_bytes, num_read):
        chunk = bytes(self._data[self._position:self._position + max(0, num_bytes)])
        if chunk and buffer: ctypes.memmove(buffer, chunk, len(chunk))
        self._position += len(chunk)
        if num_read: num_read[0] = len(chunk)
        return _RESULT_OK

    def _write(self, _this, buffer, num_bytes, num_written):
        if num_bytes < 0: return 1
        chunk = ctypes.string_at(buffer, num_bytes) if buffer else b""
        end = self._position + len(chunk)
        if end > len(self._data): self._data.extend(b"\x00" * (end - len(self._data)))
        self._data[self._position:end], self._position = chunk, end
        if num_written: num_written[0] = len(chunk)
        return _RESULT_OK

    def _seek(self, _this, position, mode, result):
        tgt = position if mode == _SEEK_SET else (self._position + position if mode == _SEEK_CUR else len(self._data) + position)
        self._position = max(0, min(int(tgt), len(self._data)))
        if result: result[0] = self._position
        return _RESULT_OK

    def _tell(self, _this, position):
        if position: position[0] = self._position
        return _RESULT_OK

    @property
    def data(self) -> bytes: return bytes(self._data)

class _ComponentHandler:
    def __init__(self) -> None:
        self.edits: list[tuple[int, float]] = []
        _init_com(self, _com_entries(self, ()) + [
            _fn(_i32, _u32)(lambda _this, _pid: _RESULT_OK),
            _fn(_i32, _u32, _f64)(lambda _t, p, v: (self.edits.append((int(p), float(v))), _RESULT_OK)[1]),
            _fn(_i32, _u32)(lambda _this, _pid: _RESULT_OK), _fn(_i32, _i32)(lambda _this, _fl: _RESULT_OK)])

class _HostApplication:
    _iid = _IID_HOST_APPLICATION

    def __init__(self, name: str = "MXM-MCP") -> None:
        self.name = name
        _init_com(self, _com_entries(self, (self._iid,)) + [
            _fn(_i32, _P(_u16))(self._get_name),
            _fn(_i32, _vp, _vp, _P(_vp))(lambda _t, _c, _i, tgt: (_set_none(tgt), _NO_INTERFACE)[1] if tgt else _NO_INTERFACE)])

    def _get_name(self, _this, buffer):
        if buffer:
            text = _encode_text(self.name)
            ctypes.memmove(buffer, ctypes.addressof(text), 2 * _STRING128)
        return _RESULT_OK

class _ParamValueQueue:
    def __init__(self, param_id: int, value: float, sample_offset: int = 0) -> None:
        self.param_id, self.points = int(param_id), [(int(sample_offset), float(value))]
        _init_com(self, _com_entries(self, ()) + [
            _fn(_u32)(lambda _this: self.param_id), _fn(_i32)(lambda _this: len(self.points)),
            _fn(_i32, _i32, _P(_i32), _P(_f64))(self._get_point), _fn(_i32, _i32, _f64, _P(_i32))(self._add_point)])

    def _get_point(self, _this, index, sample_offset, value):
        if not 0 <= index < len(self.points): return 1
        off, pt = self.points[index]
        if sample_offset: sample_offset[0] = off
        if value: value[0] = pt
        return _RESULT_OK

    def _add_point(self, _this, sample_offset, value, index):
        pos = len(self.points)
        self.points.append((int(sample_offset), float(value)))
        if index: index[0] = pos
        return _RESULT_OK

class _ParameterChanges:
    def __init__(self, values: dict[int, float]) -> None:
        self.queues = [_ParamValueQueue(k, v) for k, v in dict(values).items()]
        _init_com(self, _com_entries(self, ()) + [
            _fn(_i32)(lambda _this: len(self.queues)),
            _fn(_vp, _i32)(lambda _t, idx: self.queues[idx].pointer.value if 0 <= idx < len(self.queues) else None),
            _fn(_vp, _P(_u32), _P(_i32))(self._add_queue)])

    def _add_queue(self, _this, param_id, index):
        q = _ParamValueQueue(param_id[0], 0.0)
        self.queues.append(q)
        if index: index[0] = len(self.queues) - 1
        return q.pointer.value

def bundle_binary(bundle: str | Path) -> Path | None:
    path = Path(bundle)
    if path.is_file(): return path.resolve(strict=True)
    if not path.is_dir(): return None
    if plat.MACOS:
        info_path = path / "Contents/Info.plist"
        try:
            if info_path.stat().st_size <= 1024 * 1024:
                with info_path.open("rb") as s:
                    info = plistlib.load(s)
                exe = info.get("CFBundleExecutable") if isinstance(info, dict) else None
                if isinstance(exe, str) and exe and Path(exe).name == exe:
                    bin_p = resolved_path_within(path / "Contents/MacOS" / exe, path / "Contents/MacOS")
                    return bin_p if bin_p and bin_p.is_file() else None
        except Exception:
            return None
    return next((p.resolve(strict=True) for p in plat.bundle_binaries(path) if p.is_file()), None)

def describe_factory(factory) -> list[dict]:
    count = _bind(factory, 4, _i32, (_vp,))(_address(factory))
    if count < 0 or count > _MAX_CLASSES:
        raise ValueError(f"plugin factory class count {count} exceeds the {_MAX_CLASSES} limit")
    f3, f2 = _query_interface(factory, _IID_FACTORY3), _query_interface(factory, _IID_FACTORY2)
    sources = ((f3, _bind(f3, 8, _i32, (_vp, _i32, _vp)) if f3 else None, _PClassInfoW, _decode16),
               (f2, _bind(f2, 7, _i32, (_vp, _i32, _vp)) if f2 else None, _PClassInfo2, _decode),
               (factory, _bind(factory, 5, _i32, (_vp, _i32, _vp)), _PClassInfo2, _decode))
    classes: list[dict] = []
    for idx in range(max(0, count)):
        for host, get, kind, dec in sources:
            info = kind()
            if get is not None and get(host, idx, ctypes.byref(info)) == 0:
                break
        else:
            continue
        classes.append({
            "cid": plat.format_cid(bytes(info.cid)), "name": dec(info.name),
            "category": _decode(info.category), "sub_categories": _decode(info.sub_categories),
            "vendor": dec(info.vendor), "version": dec(info.version), "class_flags": int(info.class_flags)})
    for iface in (f3, f2):
        if iface: _release(iface)
    return classes

def _load_macos(bundle: Path):
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

    def proto(name, restype, argtypes):
        fn = getattr(cf, name)
        fn.restype, fn.argtypes = restype, argtypes
        return fn

    raw = os.fsencode(str(bundle))
    url = proto("CFURLCreateFromFileSystemRepresentation", _vp, [_vp, ctypes.c_char_p, ctypes.c_long, ctypes.c_bool])(None, raw, len(raw), True)
    ref = proto("CFBundleCreate", _vp, [_vp, _vp])(None, url)
    if not ref or not proto("CFBundleLoadExecutable", ctypes.c_bool, [_vp])(ref):
        raise OSError("could not load the VST3 bundle")
    cf_str = proto("CFStringCreateWithCString", _vp, [_vp, ctypes.c_char_p, _u32])
    fn_ptr = proto("CFBundleGetFunctionPointerForName", _vp, [_vp, _vp])
    return ref, lambda name: fn_ptr(ref, cf_str(None, name.encode(), 0x08000100)) or None

def _open_factory(bundle: Path, binary: Path):
    entry_name, _ = plat.module_entry_names()
    if plat.MACOS:
        ref, get = _load_macos(bundle)
        entry = get(entry_name)
        if not entry or not _FUNCTYPE(ctypes.c_bool, _vp)(entry)(ref):
            raise OSError(f"{entry_name} failed")
        factory = get("GetPluginFactory")
        if not factory:
            raise OSError("bundle does not export GetPluginFactory")
        return _vp(_FUNCTYPE(_vp)(factory)())
    mod = _CDLL(str(binary))
    entry = getattr(mod, entry_name, None)
    if entry_name == "ModuleEntry":
        if entry is None: raise OSError("library does not export ModuleEntry")
        entry.restype, entry.argtypes = ctypes.c_bool, [_vp]
        if not entry(_vp(mod._handle)): raise OSError("ModuleEntry failed")
    elif entry is not None:
        entry.restype = ctypes.c_bool
        if not entry(): raise OSError("InitDll failed")
    mod.GetPluginFactory.restype = _vp
    f = _vp(mod.GetPluginFactory())
    if not f.value: raise OSError("GetPluginFactory returned null")
    return f

def _factory_vendor(factory) -> str:
    get_info, info = _bind(factory, 3, _i32, (_vp, _P(_PFactoryInfo))), _PFactoryInfo()
    return _decode(info.vendor) if get_info(_address(factory), ctypes.byref(info)) == 0 else ""

def _open_bundle(bundle):
    path = Path(bundle)
    binary = bundle_binary(path)
    if binary is None:
        return path, None, {"module": str(path), "error": "no loadable VST3 binary in bundle"}
    try:
        return path, _open_factory(path, binary), None
    except Exception as exc:
        return path, None, _error(path, exc)

def probe_bundle(bundle: str | Path) -> dict:
    path, factory, err = _open_bundle(bundle)
    if err: return err
    try:
        classes, vendor = describe_factory(factory), _factory_vendor(factory)
        for d in classes:
            d["vendor"] = d["vendor"] or vendor
        return {"module": str(path), "classes": classes}
    except Exception as exc:
        return _error(path, exc)
    finally:
        _release(factory)

# ---- parameters ----

def _finite(val, fallback: float = 0.0) -> float:
    try:
        n = float(val)
        return n if math.isfinite(n) else fallback
    except (TypeError, ValueError):
        return fallback

def _select_class(classes: list[dict], cid: str = "") -> dict | None:
    wanted = str(cid or "").strip().upper()
    if wanted:
        return next((i for i in classes if str(i.get("cid", "")).upper() == wanted), None)
    found = next((i for cat in ("Audio Module Class", "Component Controller Class") for i in classes
                  if i.get("category") == cat), None)
    return found if found is not None else (classes[0] if classes else None)

_CONTROLLER_SLOTS = {
    "set_component_state": (5, _i32, (_vp,)), "set_state": (6, _i32, (_vp,)), "get_state": (7, _i32, (_vp,)),
    "count": (8, _i32, ()), "info": (9, _i32, (_i32, _P(_PParameterInfo))),
    "string_by_value": (10, _i32, (_u32, _f64, _P(_u16))), "value_by_string": (11, _i32, (_u32, _P(_u16), _P(_f64))),
    "to_plain": (12, _f64, (_u32, _f64)), "to_normalized": (13, _f64, (_u32, _f64)),
    "normalized": (14, _f64, (_u32,)), "set_normalized": (15, _i32, (_u32, _f64)),
}

def _controller_api(controller) -> dict:
    return {name: _bind(controller, slot, restype, (_vp, *args))
            for name, (slot, restype, args) in _CONTROLLER_SLOTS.items()}

def _unit_paths(controller) -> dict[int, str]:
    u_info = _query_interface(controller, _IID_UNIT_INFO)
    if not u_info: return {}
    try:
        cnt = int(_bind(u_info, 3, _i32, (_vp,))(_address(u_info)))
        if cnt <= 0 or cnt > _MAX_UNITS: return {}
        get_i = _bind(u_info, 4, _i32, (_vp, _i32, _P(_PUnitInfo)))
        entries: dict[int, tuple[str, int]] = {}
        for idx in range(cnt):
            info = _PUnitInfo()
            if get_i(_address(u_info), idx, ctypes.byref(info)) == _RESULT_OK:
                entries[int(info.id)] = (_decode16(info.name), int(info.parent_id))
        paths: dict[int, str] = {}
        for uid in entries:
            parts, cur = [], uid
            for _ in range(len(entries) + 1):
                name, parent = entries.get(cur, ("", -1))
                if name and cur != 0: parts.append(name)
                if parent in (-1, cur) or parent not in entries: break
                cur = parent
            paths[uid] = "/".join(reversed(parts))
        return paths
    finally: _release(u_info)

def _string_for_value(api: dict, handle, param_id: int, value: float) -> str:
    buf = (ctypes.c_uint16 * _STRING128)()
    return _decode16(buf) if api["string_by_value"](handle, param_id, float(value), buf) == _RESULT_OK else ""

def _value_from_text(api: dict, handle, param_id: int, text: str) -> float | None:
    buf, res = _encode_text(text), _f64(0.0)
    return _finite(res.value, None) if api["value_by_string"](handle, param_id, buf, ctypes.byref(res)) == _RESULT_OK else None

_VALUE_TEXT = re.compile(r"^\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*([A-Za-z%°µμ]+)\s*$")
_SECONDS = {"s", "sec", "secs", "second", "seconds"}
_SEMITONES = {"st", "semitone", "semitones"}

def normalize_value_text(text: str, units: str = "", minimum: float = 0.0, maximum: float = 1.0) -> str:
    m = _VALUE_TEXT.match(str(text or ""))
    if not m: return str(text)
    num, suf, b = float(m.group(1)), m.group(2).casefold().replace("µ", "u").replace("μ", "u"), str(units or "").casefold()
    if suf == "khz" or (suf == "k" and b == "hz"): return f"{num * 1000:g} Hz"
    if suf == "mhz": return f"{num * 1000000:g} Hz"
    if suf == "hz" and b == "khz": return f"{num / 1000:g} kHz"
    if suf in {"ms", "msec"} and b in _SECONDS: return f"{num / 1000:g} s"
    if suf == "%" and b not in {"%", "percent"}: return f"{(num / 100) if maximum <= 1.0 else num:g}"
    if suf in {"ct", "cents"} and b in _SEMITONES: return f"{num / 100:g} st"
    return str(text)

def _parameter_records(controller) -> list[dict]:
    api, handle = _controller_api(controller), _address(controller)
    count = int(api["count"](handle))
    if count < 0 or count > _MAX_PARAMETERS:
        raise ValueError(f"the plugin reports {count} parameters, outside the supported 0..{_MAX_PARAMETERS}")
    modules, records, seen = _unit_paths(controller), [], set()
    for idx in range(count):
        info = _PParameterInfo()
        if api["info"](handle, idx, ctypes.byref(info)) != _RESULT_OK: continue
        pid = int(info.id)
        if pid in seen: continue
        seen.add(pid)
        units, norm = _decode16(info.units), _finite(api["normalized"](handle, pid))
        p_min, p_max = _finite(api["to_plain"](handle, pid, 0.0)), _finite(api["to_plain"](handle, pid, 1.0), 1.0)
        if not p_max > p_min: p_min, p_max = 0.0, 1.0
        def_norm, fl = _finite(info.default_normalized, norm), int(info.flags)
        records.append({
            "id": pid, "name": _decode16(info.title) or f"Parameter {pid}",
            "short_name": _decode16(info.short_title), "module": modules.get(int(info.unit_id), ""),
            "units": units, "min": p_min, "max": p_max,
            "default": _finite(api["to_plain"](handle, pid, def_norm), def_norm),
            "current": _finite(api["to_plain"](handle, pid, norm), norm),
            "normalized": norm, "default_normalized": def_norm,
            "step_count": int(info.step_count), "stepped": int(info.step_count) > 0,
            "readonly": bool(fl & _PARAM_READONLY), "hidden": bool(fl & _PARAM_HIDDEN),
            "can_automate": bool(fl & _PARAM_CAN_AUTOMATE), "is_list": bool(fl & _PARAM_LIST),
            "is_program_change": bool(fl & _PARAM_PROGRAM_CHANGE), "is_bypass": bool(fl & _PARAM_BYPASS),
            "unit_id": int(info.unit_id), "display": format_display(_string_for_value(api, handle, pid, norm), units)})
    return records

class _PluginSession:
    def __init__(self, component, controller, description: dict, host: _HostApplication | None = None) -> None:
        self.component, self.controller, self.description, self.host = component, controller, description, host
        self.class_cid, self.class_name = str(description.get("cid", "")).upper(), str(description.get("name", ""))
        self.handler, self._api = (_ComponentHandler() if controller else None), (_controller_api(controller) if controller else None)

    def set_component_state(self, data: bytes) -> int:
        return int(_bind(self.component, 12, _i32, (_vp, _vp))(_address(self.component), _MemoryStream(data).pointer)) if self.component else 1

    def get_component_state(self) -> bytes:
        if not self.component: return b""
        s = _MemoryStream()
        return s.data if _bind(self.component, 13, _i32, (_vp, _vp))(_address(self.component), s.pointer) == _RESULT_OK else b""

    def set_controller_component_state(self, data: bytes) -> int:
        return int(self._api["set_component_state"](_address(self.controller), _MemoryStream(data).pointer))

    def set_controller_state(self, data: bytes) -> int:
        return int(self._api["set_state"](_address(self.controller), _MemoryStream(data).pointer))

    def get_controller_state(self) -> bytes:
        s = _MemoryStream()
        return s.data if self._api["get_state"](_address(self.controller), s.pointer) == _RESULT_OK else b""

    def parameters(self) -> list[dict]: return _parameter_records(self.controller)

    def close(self) -> None:
        for h in (self.controller, self.component):
            if h:
                try: _terminate(h)
                except Exception: pass
                _release(h)
        self.controller = self.component = None

def _open_session(factory, classes: list[dict], cid: str = "") -> _PluginSession:
    desc = _select_class(classes, cid)
    if desc is None: raise ValueError(f"no plugin class matches the requested class id {cid!r}")
    raw_cid, comp, ctl = cid_bytes(desc["cid"]), None, None
    comp = _create_instance(factory, raw_cid, _IID_COMPONENT)
    if comp is not None:
        ctl_cid = (ctypes.c_ubyte * 16)()
        get_id = _bind(comp, 5, _i32, (_vp, _P(ctypes.c_ubyte * 16)))
        if get_id(_address(comp), ctypes.byref(ctl_cid)) == _RESULT_OK:
            ctl = _create_instance(factory, bytes(ctl_cid), _IID_EDIT_CONTROLLER)
    if ctl is None: ctl = _create_instance(factory, raw_cid, _IID_EDIT_CONTROLLER)
    session = _PluginSession(comp, ctl, desc, _HostApplication())
    if comp: _initialize(comp, session.host.pointer)
    if ctl:
        _initialize(ctl, session.host.pointer)
        _bind(ctl, 16, _i32, (_vp, _vp))(_address(ctl), session.handler.pointer)
        if comp: _connect_component_controller(comp, ctl)
    return session

def _connect_component_controller(comp, ctl) -> None:
    p1, p2 = _query_interface(comp, _IID_CONNECTION_POINT), _query_interface(ctl, _IID_CONNECTION_POINT)
    if p1 and p2:
        try:
            _bind(p1, 3, _i32, (_vp, _vp))(_address(p1), p2)
            _bind(p2, 3, _i32, (_vp, _vp))(_address(p2), p1)
        finally:
            _release(p2); _release(p1)
    else:
        for p in (p2, p1):
            if p: _release(p)

def _allocate_buses(component, get_bus_info, direction: int, count: int, block: int, activate) -> tuple[ctypes.Array, list]:
    buses, keepalive = (_PAudioBusBuffers * max(1, count))(), []
    for idx in range(count):
        info = _PBusInfo()
        chans = int(info.channel_count) if get_bus_info(_address(component), 0, direction, idx, ctypes.byref(info)) == _RESULT_OK and info.channel_count > 0 else 2
        activate(_address(component), 0, direction, idx, 1)
        ptrs = (_P(ctypes.c_float) * chans)()
        for ch in range(chans):
            buf = (ctypes.c_float * block)()
            ptrs[ch] = ctypes.cast(buf, _P(ctypes.c_float))
            keepalive.append(buf)
        keepalive.append(ptrs)
        buses[idx].num_channels, buses[idx].silence_flags = chans, 0
        buses[idx].channel_buffers = ctypes.cast(ptrs, _P(_P(ctypes.c_float)))
    return buses, keepalive

def _run_processing(session: _PluginSession, values: dict[int, float], *, sample_rate: float = 48000.0, block: int = 256, blocks: int = 2) -> list[str]:
    comp = session.component
    if not comp: return ["the plugin has no component to run"]
    proc = _query_interface(comp, _IID_AUDIO_PROCESSOR)
    if not proc: return ["the plugin exposes no audio processor; its state keeps the controller values"]
    warnings: list[str] = []
    try:
        setup = _PProcessSetup(0, 0, int(block), float(sample_rate))
        if _bind(proc, 7, _i32, (_vp, _P(_PProcessSetup)))(_address(proc), ctypes.byref(setup)) != _RESULT_OK:
            return ["the plugin refused the processing setup"]
        get_bc = _bind(comp, 7, _i32, (_vp, _i32, _i32))
        get_bi = _bind(comp, 8, _i32, (_vp, _i32, _i32, _i32, _P(_PBusInfo)))
        act = _bind(comp, 10, _i32, (_vp, _i32, _i32, _i32, ctypes.c_uint8))
        set_act, set_proc = _bind(comp, 11, _i32, (_vp, ctypes.c_uint8)), _bind(proc, 8, _i32, (_vp, ctypes.c_uint8))
        process = _bind(proc, 9, _i32, (_vp, _P(_PProcessData)))
        out_count, in_count = max(0, int(get_bc(_address(comp), 0, 1))), max(0, int(get_bc(_address(comp), 0, 0)))
        if out_count < 1: return ["the plugin has no audio output to run"]
        outputs, _ = _allocate_buses(comp, get_bi, 1, out_count, block, act)
        inputs = _allocate_buses(comp, get_bi, 0, in_count, block, act)[0] if in_count else None
        if get_bc(_address(comp), 1, 0) > 0: act(_address(comp), 1, 0, 0, 1)
        ctx = _PProcessContext(sample_rate=float(sample_rate), tempo=120.0, time_sig_numerator=4,
                               time_sig_denominator=4, state=(1 << 10) | (1 << 13))
        changes, out_changes = (_ParameterChanges(values) if values else None), _ParameterChanges({})
        data = _PProcessData(num_samples=int(block), num_inputs=in_count, num_outputs=out_count,
                             inputs=ctypes.cast(inputs, _P(_PAudioBusBuffers)) if inputs else None,
                             outputs=ctypes.cast(outputs, _P(_PAudioBusBuffers)), process_context=ctypes.pointer(ctx),
                             input_parameter_changes=changes.pointer if changes else None,
                             output_parameter_changes=out_changes.pointer)
        set_act(_address(comp), 1)
        if set_proc(_address(proc), 1) != _RESULT_OK:
            warnings.append("the plugin refused to start processing")
            set_act(_address(comp), 0)
            return warnings
        try:
            for idx in range(max(1, blocks)):
                if idx: data.input_parameter_changes = None
                if process(_address(proc), ctypes.byref(data)) != _RESULT_OK:
                    warnings.append("the plugin failed while committing parameter values")
                    break
        finally:
            set_proc(_address(proc), 0)
            set_act(_address(comp), 0)
        return warnings
    finally:
        _release(proc)

def _clamp01(val) -> float:
    n = _finite(val, None) if val is not None else None
    if n is None: raise ValueError("a normalized parameter value must be a finite number")
    return min(1.0, max(0.0, n))

def _load_base_state(session: _PluginSession, *, preset: str = "", state: bytes | None = None) -> list[str]:
    warnings, comp_state, ctl_state = [], b"", b""
    if preset:
        parsed = read_vstpreset(preset)
        comp_state, ctl_state = parsed["chunks"].get("Comp", b""), parsed["chunks"].get("Cont", b"")
        if not comp_state and not ctl_state:
            raise ValueError(f"{preset} contains no component or controller state")
        if parsed["class_id"] and session.class_cid and parsed["class_id"] != session.class_cid:
            warnings.append(f"the preset was saved for class {parsed['class_id']}, not {session.class_cid}")
    elif state:
        if len(state) > _MAX_STATE_BYTES: raise ValueError("the component state exceeds the size limit")
        comp_state = bytes(state)
    if comp_state:
        ok = True
        if session.component:
            ok = session.set_component_state(comp_state) == _RESULT_OK
            if not ok: warnings.append("the component rejected the base state")
        if session.set_controller_component_state(comp_state) != _RESULT_OK and not ok:
            warnings.append("the controller also rejected the component state")
    if ctl_state and session.set_controller_state(ctl_state) != _RESULT_OK:
        warnings.append("the controller rejected its own state")
    return warnings

def _with_session(bundle, action, *, preset: str = "", state: bytes | None = None, cid: str = ""):
    path, factory, err = _open_bundle(bundle)
    if err: return err
    session = None
    try:
        classes = describe_factory(factory)
        if not classes: return {"module": str(path), "error": "the bundle exports no plugin classes"}
        session = _open_session(factory, classes, cid)
        if session.controller is None:
            return {"module": str(path), "error": "the plugin class exposes no IEditController"}
        warnings, res = _load_base_state(session, preset=preset, state=state), action(path, session)
        if isinstance(res, dict) and warnings:
            res["warnings"] = warnings + list(res.get("warnings") or [])
        return res
    except Exception as exc:
        return _error(path, exc)
    finally:
        if session: session.close()
        _release(factory)

def inspect_bundle_parameters(bundle, *, preset: str = "", state: bytes | None = None, cid: str = "", text_queries=(), value_queries=()) -> dict:
    if state is not None and len(state) > _MAX_STATE_BYTES:
        return {"module": str(Path(bundle)), "error": "the component state is too large"}

    def action(path, session):
        params = session.parameters()
        recs = {i["id"]: i for i in params}
        api, handle = _controller_api(session.controller), _address(session.controller)
        t_res, v_res = [], []
        for pid, txt in text_queries:
            pid, rec = int(pid), recs.get(int(pid))
            units, lo, hi = (rec["units"], rec["min"], rec["max"]) if rec else ("", 0.0, 1.0)
            conv = normalize_value_text(txt, units, lo, hi)
            norm = _value_from_text(api, handle, pid, conv)
            if norm is None:
                t_res.append({"id": pid, "text": str(txt), "error": "the plugin could not parse this text"})
                continue
            t_res.append({"id": pid, "name": rec["name"] if rec else "", "text": str(txt), "parsed_text": conv,
                          "normalized": norm, "plain": _finite(api["to_plain"](handle, pid, norm), norm),
                          "display": format_display(_string_for_value(api, handle, pid, norm), units)})
        for pid, val in value_queries:
            pid, rec = int(pid), recs.get(int(pid))
            units, norm = (rec["units"] if rec else ""), _clamp01(val)
            v_res.append({"id": pid, "name": rec["name"] if rec else "", "normalized": norm,
                          "plain": _finite(api["to_plain"](handle, pid, norm), norm),
                          "text": format_display(_string_for_value(api, handle, pid, norm), units), "units": units})
        return {"module": str(path), "class": {"cid": session.class_cid, "name": session.class_name},
                "count": len(params), "parameters": params, "text_queries": t_res, "value_queries": v_res}

    return _with_session(bundle, action, preset=preset, state=state, cid=cid)

def text_to_value(bundle, param_id: int, text: str, *, preset: str = "", state: bytes | None = None, cid: str = "") -> dict:
    res = inspect_bundle_parameters(bundle, preset=preset, state=state, cid=cid, text_queries=[(param_id, text)])
    return res if res.get("error") else {"module": res["module"], "class": res.get("class"), **(res.get("text_queries") or [{}])[0]}

def value_to_text(bundle, param_id: int, value: float, *, preset: str = "", state: bytes | None = None, cid: str = "") -> dict:
    res = inspect_bundle_parameters(bundle, preset=preset, state=state, cid=cid, value_queries=[(param_id, value)])
    return res if res.get("error") else {"module": res["module"], "class": res.get("class"), **(res.get("value_queries") or [{}])[0]}

def save_bundle_preset(bundle, out_path, *, preset: str = "", state: bytes | None = None, cid: str = "", overrides=(), text_overrides=()) -> dict:
    dest = Path(out_path)
    if state is not None and len(state) > _MAX_STATE_BYTES:
        return {"module": str(Path(bundle)), "error": "the component state is too large"}

    def action(path, session):
        api, handle = _controller_api(session.controller), _address(session.controller)
        recs = {i["id"]: i for i in session.parameters()}
        warnings, applied, expected, t_res = [], 0, {}, []
        for pid, val in overrides:
            pid, norm = int(pid), _clamp01(val)
            if pid not in recs or api["set_normalized"](handle, pid, norm) != _RESULT_OK:
                warnings.append(f"parameter #{pid} rejected the value" if pid in recs else f"parameter #{pid} is not exposed by this plugin")
                continue
            expected[pid], applied = _finite(api["normalized"](handle, pid), norm), applied + 1
        for pid, txt, fb in text_overrides:
            pid = int(pid)
            if pid not in recs:
                warnings.append(f"parameter #{pid} is not exposed by this plugin")
                continue
            rec = recs[pid]
            conv = normalize_value_text(txt, rec.get("units", ""), rec.get("min", 0.0), rec.get("max", 1.0))
            norm, src = _value_from_text(api, handle, pid, conv), "text"
            if norm is None:
                if fb is None:
                    warnings.append(f"{rec['name']!r} cannot parse {str(txt)!r}")
                    continue
                norm, src = _clamp01(fb), "fallback"
            if api["set_normalized"](handle, pid, norm) != _RESULT_OK:
                warnings.append(f"parameter {rec['name']!r} rejected the value")
                continue
            expected[pid], applied = _finite(api["normalized"](handle, pid), norm), applied + 1
            t_res.append({"id": pid, "text": str(txt), "parsed_text": conv, "source": src})
        if expected: warnings.extend(_run_processing(session, dict(expected)))
        comp_s = session.get_component_state() if session.component else b""
        ctl_s = session.get_controller_state()
        if not comp_s and not ctl_s: raise ValueError("the plugin returned no state to save")
        chunks = {"Comp": comp_s} if comp_s else {}
        if ctl_s: chunks["Cont"] = ctl_s
        data = build_vstpreset(session.class_cid, chunks)
        _atomic_write(dest, data)
        return {"module": str(path), "path": str(dest), "bytes": len(data),
                "class": {"cid": session.class_cid, "name": session.class_name},
                "parameters_applied": applied,
                "applied": [{"id": k, "normalized": v} for k, v in sorted(expected.items())],
                "parameter_results": t_res, "warnings": warnings}

    return _with_session(bundle, action, preset=preset, state=state, cid=cid)

# ---- .vstpreset ----

def build_vstpreset(class_id: str, chunks: dict[str, bytes]) -> bytes:
    order = [n for n in ("Comp", "Cont", "Prog", "Info") if chunks.get(n)]
    payload = bytearray(b"VST3") + struct.pack("<i", 1) + str(class_id).strip().upper().encode("ascii", "replace")[:32].ljust(32, b"\x00") + b"\x00" * 8
    entries = []
    for n in order:
        d = chunks[n]; entries.append((n.encode("ascii")[:4].ljust(4, b"\x00"), len(payload), len(d))); payload += d
    list_off = len(payload)
    payload += b"List" + struct.pack("<i", len(entries))
    for n, off, sz in entries: payload += n + struct.pack("<qq", off, sz)
    payload[40:48] = struct.pack("<q", list_off)
    return bytes(payload)

def read_vstpreset(path) -> dict:
    pp = Path(path)
    try: data = pp.read_bytes()
    except OSError as exc: raise ValueError(f"cannot read {pp}: {exc}") from exc
    if len(data) < 48 or data[:4] != b"VST3": raise ValueError(f"{pp} is not a VST3 preset")
    cid = data[8:40].decode("ascii", "replace").strip("\x00 ").upper()
    list_off = struct.unpack_from("<q", data, 40)[0]
    if list_off < 48 or list_off + 8 > len(data) or data[list_off:list_off + 4] != b"List": raise ValueError(f"{pp} has an invalid chunk list")
    cnt, chunks = struct.unpack_from("<i", data, list_off + 4)[0], {}
    for idx in range(cnt):
        pos = list_off + 8 + idx * 20
        if pos + 20 > len(data): raise ValueError(f"{pp} has a truncated chunk table")
        cid_str = data[pos:pos + 4].decode("ascii", "replace")
        off, length = struct.unpack_from("<qq", data, pos + 4)
        if off < 48 or length < 0 or off + length > len(data): raise ValueError(f"{pp} has an invalid {cid_str!r} chunk")
        chunks[cid_str] = data[off:off + length]
    return {"class_id": cid, "chunks": chunks}

def _atomic_write(path: Path, data: bytes) -> None:
    p = Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}-", dir=p.parent)
    try:
        with os.fdopen(fd, "wb") as s: s.write(data); s.flush(); os.fsync(s.fileno())
        os.replace(tmp, p)
    finally:
        try: os.unlink(tmp)
        except FileNotFoundError: pass

_USAGE = ("usage: python -m mxm_mcp.vst3_probe [options]\n\n"
          "  <bundle> [...]                     print each bundle's classes (default)\n"
          "  --params <bundle>                  print parameter inspection JSON\n"
          "  --save-preset <bundle> --out PATH  write a .vstpreset from the current state\n")

_VALUE_OPTIONS = ("--params", "--save-preset", "--preset", "--state-file", "--cid", "--out")
_PAIR_OPTIONS = ("--text", "--value", "--set", "--set-text", "--set-fallback")

def _parse_arguments(argv: list[str]) -> dict:
    opt = {"mode": "classes", "bundle": "", "preset": "", "state": None, "cid": "", "out": "",
           "text": [], "value": [], "sets": [], "text_sets": [], "fallbacks": {}, "bundles": []}
    args = iter(argv)
    for arg in args:
        if arg in ("-h", "--help"): return {"mode": "help"}
        if not arg.startswith("-"):
            opt["bundles"].append(arg)
            continue
        if arg not in _VALUE_OPTIONS + _PAIR_OPTIONS: raise ValueError(f"unknown option {arg}")
        try: val = next(args)
        except StopIteration: raise ValueError(f"{arg} needs a value") from None
        if arg in ("--params", "--save-preset"): opt["mode"], opt["bundle"] = ("params" if arg == "--params" else "save"), val
        elif arg == "--state-file":
            try: data = Path(val).read_bytes()
            except OSError as exc: raise ValueError(f"cannot read {val}: {exc}") from exc
            if len(data) > _MAX_STATE_BYTES: raise ValueError("the state file exceeds the size limit")
            opt["state"] = data
        elif arg not in _PAIR_OPTIONS: opt[arg.lstrip("-")] = val
        else:
            k, sep, v = val.partition("=")
            if not sep or not k.strip(): raise ValueError(f"expected ID=VALUE, got {val!r}")
            pid = int(k.strip(), 0)
            if arg == "--text": opt["text"].append((pid, v.strip()))
            elif arg == "--value": opt["value"].append((pid, float(v)))
            elif arg == "--set": opt["sets"].append((pid, float(v)))
            elif arg == "--set-text": opt["text_sets"].append((pid, v.strip()))
            else: opt["fallbacks"][pid] = float(v)
    return opt

def _emit(payload: dict) -> None: sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n"); sys.stdout.flush()

def main(argv: list[str] | None = None) -> int:
    try: opt = _parse_arguments(list(argv if argv is not None else sys.argv[1:]))
    except ValueError as exc: _emit({"error": str(exc)}); return 2
    if opt.get("mode") == "help": sys.stdout.write(_USAGE); return 0
    if opt["mode"] == "classes":
        bundles = ([opt["bundle"]] if opt["bundle"] else []) + opt["bundles"]
        if not bundles: _emit({"error": "no bundle given; see --help"}); return 2
        for b in bundles: _emit(probe_bundle(b))
        return 0
    if not opt["bundle"]: _emit({"error": "no bundle given; see --help"}); return 2
    if opt["mode"] == "params":
        _emit(inspect_bundle_parameters(opt["bundle"], preset=opt["preset"], state=opt["state"], cid=opt["cid"],
                                       text_queries=opt["text"], value_queries=opt["value"]))
        return 0
    if not opt["out"]: _emit({"error": "--save-preset needs --out PATH"}); return 2
    text_ov = [(pid, txt, opt["fallbacks"].get(pid)) for pid, txt in opt["text_sets"]]
    res = save_bundle_preset(opt["bundle"], opt["out"], preset=opt["preset"], state=opt["state"], cid=opt["cid"],
                             overrides=opt["sets"], text_overrides=text_ov)
    _emit(res)
    return 1 if res.get("error") else 0

if __name__ == "__main__":
    raise SystemExit(main())
