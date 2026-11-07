"""Standalone VST3 module introspection for MXM's native VST3 host.

Dependency-free (stdlib + :mod:`ctypes`) and run in a separate process by
:mod:`mxm_mcp.vst3`: loading third-party binaries in-process is unsafe, so a
faulty module only kills its own subprocess.

Mirrors the SDK loaders MXM vendors (module_linux/win32/mac) plus
``Vst3Manager``: load the bundle, get its ``IPluginFactory``, enumerate the
classes (preferring the UTF-16 ``IPluginFactory3`` info so non-ASCII names
survive), and report each class id exactly as ``UID::toString()`` formats it -
the value MXM matches against the project's ``<key>`` on load.

The probe also instantiates a class's ``IEditController`` (creating its
``IComponent`` when the class has one) to enumerate parameters with plain and
normalized ranges, unit paths, flags and display text, and to convert natural
text such as ``"800 Hz"`` in both directions. Optional preset/state loading
runs first so the reported values, and any preset the probe writes, reflect
that starting patch.

Run as ``python -m mxm_mcp.vst3_probe <bundle> [...]``; prints one JSON object
per bundle: ``{"module": ..., "classes": [...]}`` or ``{"module": ..., "error": ...}``.

Parameter inspection and preset export:

``python -m mxm_mcp.vst3_probe --params <bundle> [--preset <path>] [--state-file <path>]
    [--cid <32 hex>] [--text ID=TEXT] [--value ID=NUMBER]``

``python -m mxm_mcp.vst3_probe --save-preset <bundle> --out <path> [--preset <path>]
    [--state-file <path>] [--cid <32 hex>] [--set ID=NORMALIZED]
    [--set-text ID=TEXT [--set-fallback ID=NORMALIZED]]``
"""

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

_MAX_CLASSES = 4_096
_MAX_PARAMETERS = 100_000
_MAX_UNITS = 4_096
_MAX_PRESET_BYTES = 64 * 1024 * 1024
_MAX_STATE_BYTES = 64 * 1024 * 1024
_MAX_PRESET_CHUNKS = 256
_STRING128 = 128

_IID_FACTORY2 = plat.iid_bytes("factory2")
_IID_FACTORY3 = plat.iid_bytes("factory3")

#! Interface ids from the SDK's ``DECLARE_CLASS_IID``, as the four 32-bit words
#! given there (see ``_iid_words``).
def _iid_words(l1: int, l2: int, l3: int, l4: int) -> bytes:
    """Raw TUID bytes for a ``DECLARE_CLASS_IID`` interface id."""
    if plat.com_compatible():
        return (struct.pack("<I", l1) + struct.pack("<HH", l2 >> 16, l2 & 0xFFFF)
                + struct.pack(">II", l3, l4))
    return struct.pack(">IIII", l1, l2, l3, l4)


_IID_UNKNOWN = _iid_words(0x00000000, 0x00000000, 0xC0000000, 0x00000046)
_IID_COMPONENT = _iid_words(0xE831FF31, 0xF2D54301, 0x928EBBEE, 0x25697802)
_IID_EDIT_CONTROLLER = _iid_words(0xDCD7BBE3, 0x7742448D, 0xA874AACC, 0x979C759E)
_IID_UNIT_INFO = _iid_words(0x3D4BD6B5, 0x913A4FD2, 0xA886E768, 0xA5EB92C1)
_IID_BSTREAM = _iid_words(0xC3BF6EA2, 0x30994752, 0x9B6BF990, 0x1EE33E9B)
_IID_CONNECTION_POINT = _iid_words(0x70A4156F, 0x6E6E4026, 0x989148BF, 0xAA60D8D1)
_IID_AUDIO_PROCESSOR = _iid_words(0x42043F99, 0xB7DA453C, 0xA569E79D, 0x9AAEC33D)
_IID_HOST_APPLICATION = _iid_words(0x58E595CC, 0xDB2D4969, 0x8B6AAF8C, 0x36A664E5)

#! ParameterInfo::ParameterFlags
_PARAM_CAN_AUTOMATE = 1 << 0
_PARAM_READONLY = 1 << 1
_PARAM_LIST = 1 << 3
_PARAM_HIDDEN = 1 << 4
_PARAM_PROGRAM_CHANGE = 1 << 15
_PARAM_BYPASS = 1 << 16

_NO_INTERFACE = ctypes.c_int32(0x80004002).value if plat.WINDOWS else -1
_RESULT_OK = 0
_SEEK_SET = 0
_SEEK_CUR = 1
_SEEK_END = 2

#! The exported module entry points are ordinary C functions resolved from the
#! DLL (InitDll/ExitDll are cdecl even on Windows; GetPluginFactory is declared
#! PLUGIN_API, but that only affects __stdcall on 32-bit and it takes no
#! arguments). So load the module with the normal CDLL loader -- WinDLL would
#! impose stdcall on every lookup. Only the COM/vtable calls need the platform
#! function type below, since those do take arguments.
_CDLL = ctypes.CDLL
_FUNCTYPE = ctypes.WINFUNCTYPE if plat.WINDOWS else ctypes.CFUNCTYPE

if plat.WINDOWS:
    try:  # plugins may use COM while initializing; MXM does the same.
        ctypes.windll.ole32.CoInitializeEx(None, 2)
    except Exception:  # pragma: no cover - best effort
        pass

#! FUnknown/IPluginFactory vtable slots: queryInterface=0, release=2,
#! getFactoryInfo=3, countClasses=4, getClassInfo=5, getClassInfo2=7,
#! getClassInfoUnicode=8.


class _PFactoryInfo(ctypes.Structure):
    _fields_ = [("vendor", ctypes.c_char * 64), ("url", ctypes.c_char * 256),
                ("email", ctypes.c_char * 128), ("flags", ctypes.c_int32)]


class _PClassInfo2(ctypes.Structure):
    # cid is c_ubyte (not c_char) so ctypes returns the raw 16 bytes; c_char
    # arrays become NUL-terminated bytes and truncate ids ending in zero bytes.
    # The leading fields also match v1 PClassInfo, so its getClassInfo writes
    # safely into this larger buffer.
    _fields_ = [
        ("cid", ctypes.c_ubyte * 16), ("cardinality", ctypes.c_int32),
        ("category", ctypes.c_char * 32), ("name", ctypes.c_char * 64),
        ("class_flags", ctypes.c_uint32), ("sub_categories", ctypes.c_char * 128),
        ("vendor", ctypes.c_char * 64), ("version", ctypes.c_char * 64),
        ("sdk_version", ctypes.c_char * 64),
    ]


class _PClassInfoW(ctypes.Structure):
    # UTF-16 variant; cid/category/sub_categories stay char8.
    _fields_ = [
        ("cid", ctypes.c_ubyte * 16), ("cardinality", ctypes.c_int32),
        ("category", ctypes.c_char * 32), ("name", ctypes.c_uint16 * 64),
        ("class_flags", ctypes.c_uint32), ("sub_categories", ctypes.c_char * 128),
        ("vendor", ctypes.c_uint16 * 64), ("version", ctypes.c_uint16 * 64),
        ("sdk_version", ctypes.c_uint16 * 64),
    ]


class _PParameterInfo(ctypes.Structure):
    """Vst::ParameterInfo, filled by ``IEditController::getParameterInfo``."""
    _fields_ = [
        ("id", ctypes.c_uint32),
        ("title", ctypes.c_uint16 * _STRING128),
        ("short_title", ctypes.c_uint16 * _STRING128),
        ("units", ctypes.c_uint16 * _STRING128),
        ("step_count", ctypes.c_int32),
        ("default_normalized", ctypes.c_double),
        ("unit_id", ctypes.c_int32),
        ("flags", ctypes.c_int32),
    ]


class _PUnitInfo(ctypes.Structure):
    """Vst::UnitInfo, filled by ``IUnitInfo::getUnitInfo``."""
    _fields_ = [
        ("id", ctypes.c_int32),
        ("parent_id", ctypes.c_int32),
        ("name", ctypes.c_uint16 * _STRING128),
        ("program_list_id", ctypes.c_int32),
    ]


class _PInterface(ctypes.Structure):
    """An object whose first word is a vtable pointer (any COM-style object)."""
    _fields_ = [("vtable", ctypes.c_void_p)]


class _PProcessSetup(ctypes.Structure):
    _fields_ = [
        ("process_mode", ctypes.c_int32),
        ("symbolic_sample_size", ctypes.c_int32),
        ("max_samples_per_block", ctypes.c_int32),
        ("sample_rate", ctypes.c_double),
    ]


class _PBusInfo(ctypes.Structure):
    _fields_ = [
        ("media_type", ctypes.c_int32),
        ("direction", ctypes.c_int32),
        ("channel_count", ctypes.c_int32),
        ("name", ctypes.c_uint16 * _STRING128),
        ("bus_type", ctypes.c_int32),
        ("flags", ctypes.c_uint32),
    ]


class _PAudioBusBuffers(ctypes.Structure):
    _fields_ = [
        ("num_channels", ctypes.c_int32),
        ("silence_flags", ctypes.c_uint64),
        ("channel_buffers", ctypes.POINTER(ctypes.POINTER(ctypes.c_float))),
    ]


class _PProcessContext(ctypes.Structure):
    _fields_ = [
        ("state", ctypes.c_uint32),
        ("sample_rate", ctypes.c_double),
        ("project_time_samples", ctypes.c_int64),
        ("system_time", ctypes.c_int64),
        ("continuous_time_samples", ctypes.c_int64),
        ("project_time_music", ctypes.c_double),
        ("bar_position_music", ctypes.c_double),
        ("cycle_start_music", ctypes.c_double),
        ("cycle_end_music", ctypes.c_double),
        ("tempo", ctypes.c_double),
        ("time_sig_numerator", ctypes.c_int32),
        ("time_sig_denominator", ctypes.c_int32),
        ("chord_key_note", ctypes.c_uint8),
        ("chord_root_note", ctypes.c_uint8),
        ("chord_mask", ctypes.c_int16),
        ("smpte_offset_subframes", ctypes.c_int32),
        ("frame_rate_fps", ctypes.c_uint32),
        ("frame_rate_flags", ctypes.c_uint32),
        ("samples_to_next_clock", ctypes.c_int32),
    ]


class _PProcessData(ctypes.Structure):
    _fields_ = [
        ("process_mode", ctypes.c_int32),
        ("symbolic_sample_size", ctypes.c_int32),
        ("num_samples", ctypes.c_int32),
        ("num_inputs", ctypes.c_int32),
        ("num_outputs", ctypes.c_int32),
        ("inputs", ctypes.POINTER(_PAudioBusBuffers)),
        ("outputs", ctypes.POINTER(_PAudioBusBuffers)),
        ("input_parameter_changes", ctypes.c_void_p),
        ("output_parameter_changes", ctypes.c_void_p),
        ("input_events", ctypes.c_void_p),
        ("output_events", ctypes.c_void_p),
        ("process_context", ctypes.POINTER(_PProcessContext)),
    ]



def _decode(value: bytes) -> str:
    return value.split(b"\x00", 1)[0].decode("utf-8", "replace")


def _decode16(value) -> str:
    return bytes(value).decode("utf-16-le", "replace").split("\x00", 1)[0]


def _address(value) -> int:
    return int(value.value or 0) if isinstance(value, ctypes.c_void_p) else int(value)


def _bind(handle, slot: int, restype, argtypes):
    """Return a callable for vtable *slot* of *handle*."""
    vtable = ctypes.cast(ctypes.c_void_p(_address(handle)), ctypes.POINTER(ctypes.c_void_p))[0]
    address = ctypes.cast(ctypes.c_void_p(vtable), ctypes.POINTER(ctypes.c_void_p))[slot]
    return _FUNCTYPE(restype, *argtypes)(address)


def _query_interface(handle, iid: bytes):
    """FUnknown::queryInterface; return the interface address or None."""
    query = _bind(handle, 0, ctypes.c_int32,
                  (ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)))
    iid_buffer = (ctypes.c_ubyte * len(iid)).from_buffer_copy(iid)
    result = ctypes.c_void_p()
    if query(_address(handle), ctypes.byref(iid_buffer), ctypes.byref(result)) == 0:
        return result.value or None
    return None


def _release(handle) -> None:
    _bind(handle, 2, ctypes.c_uint32, (ctypes.c_void_p,))(_address(handle))


def _initialize(handle, context=None) -> int:
    """IPluginBase::initialize (vtable slot 3), optionally with a host context."""
    return _bind(handle, 3, ctypes.c_int32,
                 (ctypes.c_void_p, ctypes.c_void_p))(
                     _address(handle), context if context is not None else None)


def _terminate(handle) -> None:
    """IPluginBase::terminate (vtable slot 4)."""
    _bind(handle, 4, ctypes.c_int32, (ctypes.c_void_p,))(_address(handle))


def _create_instance(factory, class_id: bytes, interface_id: bytes):
    """``IPluginFactory::createInstance``; the raw interface pointer or None."""
    if len(class_id) != 16 or len(interface_id) != 16:
        return None
    create = _bind(factory, 6, ctypes.c_int32,
                   (ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p,
                    ctypes.POINTER(ctypes.c_void_p)))
    result = ctypes.c_void_p()
    if create(_address(factory), class_id, interface_id, ctypes.byref(result)) == 0:
        return result.value or None
    return None


def cid_bytes(cid: str) -> bytes:
    """The 16 raw class id bytes for a ``format_cid`` style 32-hex string."""
    text = "".join(str(cid).split())
    if len(text) != 32:
        raise ValueError("a VST3 class id must be 32 hex characters")
    raw = bytes.fromhex(text)
    if plat.com_compatible():
        first = int(text[0:8], 16)
        second = int(text[8:12], 16)
        third = int(text[12:16], 16)
        return struct.pack("<I", first) + struct.pack("<HH", second, third) + raw[8:16]
    return raw


def _encode_text(text: str):
    """A NUL-terminated UTF-16LE ``String128`` buffer for a plugin call."""
    raw = str(text).encode("utf-16-le")[:2 * (_STRING128 - 1)]
    raw = raw[:len(raw) - (len(raw) % 2)]
    buffer = (ctypes.c_uint16 * _STRING128)()
    ctypes.memmove(buffer, raw + b"\x00\x00", len(raw) + 2)
    return buffer


def format_display(text: str, units: str) -> str:
    """A parameter's display text with its unit appended when the plugin omits it."""
    text = (text or "").strip()
    units = (units or "").strip()
    if not text:
        return ""
    if not units or units.casefold() in text.casefold():
        return text
    return f"{text} {units}"


class _MemoryStream:
    """A minimal ``IBStream`` implemented in Python for state in and out.

    The plugin only sees a COM-style object whose first word is a vtable of
    Python callbacks. Each instance keeps its callbacks and vtable alive for as
    long as the plugin may hold the pointer.
    """

    _iid = _IID_BSTREAM

    def __init__(self, data: bytes = b"") -> None:
        self._data = bytearray(data)
        self._position = 0
        callbacks = [
            _FUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p,
                      ctypes.POINTER(ctypes.c_void_p))(self._query_interface),
            _FUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)(self._add_ref),
            _FUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)(self._release),
            _FUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32,
                      ctypes.POINTER(ctypes.c_int32))(self._read),
            _FUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32,
                      ctypes.POINTER(ctypes.c_int32))(self._write),
            _FUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_int64, ctypes.c_int32,
                      ctypes.POINTER(ctypes.c_int64))(self._seek),
            _FUNCTYPE(ctypes.c_int32, ctypes.c_void_p,
                      ctypes.POINTER(ctypes.c_int64))(self._tell),
        ]
        self._callbacks = callbacks
        self._vtable = (ctypes.c_void_p * len(callbacks))(
            *(ctypes.cast(callback, ctypes.c_void_p) for callback in callbacks))
        self._object = _PInterface(ctypes.cast(self._vtable, ctypes.c_void_p))
        self.pointer = ctypes.c_void_p(ctypes.addressof(self._object))

    # -- IBStream ---------------------------------------------------------
    def _query_interface(self, _this, iid, target):
        if not target:
            return _NO_INTERFACE
        requested = ctypes.string_at(iid, 16) if iid else b""
        if requested in (self._iid, _IID_UNKNOWN):
            target[0] = ctypes.c_void_p(ctypes.addressof(self._object))
            return _RESULT_OK
        target[0] = None
        return _NO_INTERFACE

    def _add_ref(self, _this):
        return 1

    def _release(self, _this):
        return 0

    def _read(self, _this, buffer, num_bytes, num_read):
        chunk = bytes(self._data[self._position:self._position + max(0, num_bytes)])
        if chunk and buffer:
            ctypes.memmove(buffer, chunk, len(chunk))
        self._position += len(chunk)
        if num_read:
            num_read[0] = len(chunk)
        # Like the SDK's ReadOnlyBStream: reaching the end is not an error,
        # it is reported through numBytesRead so plugins can parse the tail.
        return _RESULT_OK

    def _write(self, _this, buffer, num_bytes, num_written):
        if num_bytes < 0:
            return 1
        chunk = ctypes.string_at(buffer, num_bytes) if buffer else b""
        end = self._position + len(chunk)
        if end > len(self._data):
            self._data.extend(b"\x00" * (end - len(self._data)))
        self._data[self._position:end] = chunk
        self._position = end
        if num_written:
            num_written[0] = len(chunk)
        return _RESULT_OK

    def _seek(self, _this, position, mode, result):
        if mode == _SEEK_SET:
            target = position
        elif mode == _SEEK_CUR:
            target = self._position + position
        else:
            target = len(self._data) + position
        self._position = max(0, min(int(target), len(self._data)))
        if result:
            result[0] = self._position
        return _RESULT_OK

    def _tell(self, _this, position):
        if position:
            position[0] = self._position
        return _RESULT_OK

    @property
    def data(self) -> bytes:
        return bytes(self._data)


class _ComponentHandler:
    """A silent ``IComponentHandler`` so controllers never see a null host."""

    def __init__(self) -> None:
        self.edits: list[tuple[int, float]] = []
        callbacks = [
            _FUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p,
                      ctypes.POINTER(ctypes.c_void_p))(self._query_interface),
            _FUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)(self._add_ref),
            _FUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)(self._release),
            _FUNCTYPE(ctypes.c_int32, ctypes.c_void_p,
                      ctypes.c_uint32)(self._begin_edit),
            _FUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint32,
                      ctypes.c_double)(self._perform_edit),
            _FUNCTYPE(ctypes.c_int32, ctypes.c_void_p,
                      ctypes.c_uint32)(self._end_edit),
            _FUNCTYPE(ctypes.c_int32, ctypes.c_void_p,
                      ctypes.c_int32)(self._restart_component),
        ]
        self._callbacks = callbacks
        self._vtable = (ctypes.c_void_p * len(callbacks))(
            *(ctypes.cast(callback, ctypes.c_void_p) for callback in callbacks))
        self._object = _PInterface(ctypes.cast(self._vtable, ctypes.c_void_p))
        self.pointer = ctypes.c_void_p(ctypes.addressof(self._object))

    def _query_interface(self, _this, iid, target):
        if not target:
            return _NO_INTERFACE
        requested = ctypes.string_at(iid, 16) if iid else b""
        if requested == _IID_UNKNOWN:
            target[0] = ctypes.c_void_p(ctypes.addressof(self._object))
            return _RESULT_OK
        target[0] = None
        return _NO_INTERFACE

    @staticmethod
    def _add_ref(_this):
        return 1

    @staticmethod
    def _release(_this):
        return 0

    @staticmethod
    def _begin_edit(_this, _param_id):
        return _RESULT_OK

    def _perform_edit(self, _this, param_id, value):
        self.edits.append((int(param_id), float(value)))
        return _RESULT_OK

    @staticmethod
    def _end_edit(_this, _param_id):
        return _RESULT_OK

    @staticmethod
    def _restart_component(_this, _flags):
        return _RESULT_OK


class _HostApplication:
    """A minimal ``IHostApplication`` so plugins that require one initialize.

    u-he instruments dereference the context passed to ``initialize``; a null
    pointer crashes them. Headless probing needs only the host name.
    """

    def __init__(self, name: str = "MXM-MCP") -> None:
        self.name = name
        callbacks = [
            _FUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p,
                      ctypes.POINTER(ctypes.c_void_p))(self._query_interface),
            _FUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)(lambda _this: 1),
            _FUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)(lambda _this: 0),
            _FUNCTYPE(ctypes.c_int32, ctypes.c_void_p,
                      ctypes.POINTER(ctypes.c_uint16))(self._get_name),
            _FUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                      ctypes.POINTER(ctypes.c_void_p))(self._create_instance),
        ]
        self._callbacks = callbacks
        self._vtable = (ctypes.c_void_p * len(callbacks))(
            *(ctypes.cast(callback, ctypes.c_void_p) for callback in callbacks))
        self._object = _PInterface(ctypes.cast(self._vtable, ctypes.c_void_p))
        self.pointer = ctypes.c_void_p(ctypes.addressof(self._object))

    def _query_interface(self, _this, iid, target):
        if not target:
            return _NO_INTERFACE
        requested = ctypes.string_at(iid, 16) if iid else b""
        if requested in (self._iid, _IID_UNKNOWN):
            target[0] = ctypes.c_void_p(ctypes.addressof(self._object))
            return _RESULT_OK
        target[0] = None
        return _NO_INTERFACE

    _iid = _IID_HOST_APPLICATION

    def _get_name(self, _this, buffer):
        if buffer:
            raw = self.name.encode("utf-16-le")[:2 * (_STRING128 - 1)]
            raw = raw[:len(raw) - (len(raw) % 2)]
            ctypes.memmove(buffer, raw + b"\x00\x00", len(raw) + 2)
        return _RESULT_OK

    @staticmethod
    def _create_instance(_this, _cid, _iid, target):
        if target:
            target[0] = None
        return _NO_INTERFACE


class _ParamValueQueue:
    """A minimal ``IParamValueQueue`` holding one value for the first block."""

    def __init__(self, param_id: int, value: float, sample_offset: int = 0) -> None:
        self.param_id = int(param_id)
        self.points: list[tuple[int, float]] = [(int(sample_offset), float(value))]
        callbacks = [
            _FUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p,
                      ctypes.POINTER(ctypes.c_void_p))(self._query_interface),
            _FUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)(lambda _this: 1),
            _FUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)(lambda _this: 0),
            _FUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)(self._get_id),
            _FUNCTYPE(ctypes.c_int32, ctypes.c_void_p)(self._get_count),
            _FUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_int32,
                      ctypes.POINTER(ctypes.c_int32),
                      ctypes.POINTER(ctypes.c_double))(self._get_point),
            _FUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_int32, ctypes.c_double,
                      ctypes.POINTER(ctypes.c_int32))(self._add_point),
        ]
        self._callbacks = callbacks
        self._vtable = (ctypes.c_void_p * len(callbacks))(
            *(ctypes.cast(callback, ctypes.c_void_p) for callback in callbacks))
        self._object = _PInterface(ctypes.cast(self._vtable, ctypes.c_void_p))
        self.pointer = ctypes.c_void_p(ctypes.addressof(self._object))

    def _query_interface(self, _this, iid, target):
        if not target:
            return _NO_INTERFACE
        if iid and ctypes.string_at(iid, 16) == _IID_UNKNOWN:
            target[0] = ctypes.c_void_p(ctypes.addressof(self._object))
            return _RESULT_OK
        target[0] = None
        return _NO_INTERFACE

    def _get_id(self, _this):
        return self.param_id

    def _get_count(self, _this):
        return len(self.points)

    def _get_point(self, _this, index, sample_offset, value):
        if index < 0 or index >= len(self.points):
            return 1
        offset, point = self.points[index]
        if sample_offset:
            sample_offset[0] = offset
        if value:
            value[0] = point
        return _RESULT_OK

    def _add_point(self, _this, sample_offset, value, index):
        position = len(self.points)
        self.points.append((int(sample_offset), float(value)))
        if index:
            index[0] = position
        return _RESULT_OK


class _ParameterChanges:
    """A minimal ``IParameterChanges`` for a block's incoming values."""

    def __init__(self, values: dict[int, float]) -> None:
        self.queues = [_ParamValueQueue(param_id, value)
                       for param_id, value in dict(values).items()]
        callbacks = [
            _FUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p,
                      ctypes.POINTER(ctypes.c_void_p))(self._query_interface),
            _FUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)(lambda _this: 1),
            _FUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)(lambda _this: 0),
            _FUNCTYPE(ctypes.c_int32, ctypes.c_void_p)(self._get_count),
            _FUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32)(self._get_queue),
            _FUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
                      ctypes.POINTER(ctypes.c_int32))(self._add_queue),
        ]
        self._callbacks = callbacks
        self._vtable = (ctypes.c_void_p * len(callbacks))(
            *(ctypes.cast(callback, ctypes.c_void_p) for callback in callbacks))
        self._object = _PInterface(ctypes.cast(self._vtable, ctypes.c_void_p))
        self.pointer = ctypes.c_void_p(ctypes.addressof(self._object))

    def _query_interface(self, _this, iid, target):
        if not target:
            return _NO_INTERFACE
        if iid and ctypes.string_at(iid, 16) == _IID_UNKNOWN:
            target[0] = ctypes.c_void_p(ctypes.addressof(self._object))
            return _RESULT_OK
        target[0] = None
        return _NO_INTERFACE

    def _get_count(self, _this):
        return len(self.queues)

    def _get_queue(self, _this, index):
        if index < 0 or index >= len(self.queues):
            return None
        return self.queues[index].pointer.value

    def _add_queue(self, _this, param_id, index):
        queue = _ParamValueQueue(param_id[0], 0.0)
        self.queues.append(queue)
        if index:
            index[0] = len(self.queues) - 1
        return queue.pointer.value


def bundle_binary(bundle: str | Path) -> Path | None:
    """The loadable VST3 binary for *bundle* (a package, or a bare module)."""
    path = Path(bundle)
    if path.is_file():
        return path.resolve(strict=True)
    if not path.is_dir():
        return None
    if plat.MACOS:
        contents = path / "Contents"
        info_path = contents / "Info.plist"
        try:
            if info_path.stat().st_size > 1024 * 1024:
                return None
            with info_path.open("rb") as stream:
                info = plistlib.load(stream)
        except (OSError, plistlib.InvalidFileException, ValueError, RecursionError):
            return None
        executable = info.get("CFBundleExecutable") if isinstance(info, dict) else None
        if not isinstance(executable, str) or not executable or Path(executable).name != executable:
            return None
        binary = resolved_path_within(contents / "MacOS" / executable, contents / "MacOS")
        return binary if binary is not None and binary.is_file() else None
    return next((p.resolve(strict=True) for p in plat.bundle_binaries(path) if p.is_file()), None)


def describe_factory(factory) -> list[dict]:
    """Enumerate the classes exported by a VST3 ``IPluginFactory``.

    Prefers ``IPluginFactory3::getClassInfoUnicode`` (UTF-16) and falls back
    to ``IPluginFactory2`` then v1, like the SDK's ``ClassInfo``.
    """
    count = _bind(factory, 4, ctypes.c_int32, (ctypes.c_void_p,))(_address(factory))
    if count < 0 or count > _MAX_CLASSES:
        raise ValueError(f"plugin factory class count {count} exceeds the {_MAX_CLASSES} limit")
    factory3 = _query_interface(factory, _IID_FACTORY3)
    factory2 = _query_interface(factory, _IID_FACTORY2)
    get3 = (_bind(factory3, 8, ctypes.c_int32, (ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p))
            if factory3 else None)
    get2 = (_bind(factory2, 7, ctypes.c_int32, (ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p))
            if factory2 else None)
    get1 = _bind(factory, 5, ctypes.c_int32, (ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p))

    classes: list[dict] = []
    for index in range(max(0, count)):
        info, wide = _PClassInfoW(), True
        ok = get3 is not None and get3(factory3, index, ctypes.byref(info)) == 0
        if not ok:
            info, wide = _PClassInfo2(), False
            ok = get2 is not None and get2(factory2, index, ctypes.byref(info)) == 0
            if not ok:
                info = _PClassInfo2()
                ok = get1(factory, index, ctypes.byref(info)) == 0
        if not ok:
            continue
        decode = _decode16 if wide else _decode
        classes.append({
            "cid": plat.format_cid(bytes(info.cid)),
            "name": decode(info.name),
            "category": _decode(info.category),
            "sub_categories": _decode(info.sub_categories),
            "vendor": decode(info.vendor),
            "version": decode(info.version),
            "class_flags": int(info.class_flags),
        })
    for interface in (factory3, factory2):
        if interface:
            _release(interface)
    return classes


def _load_macos(bundle: Path):
    """Load a macOS VST3 bundle through CoreFoundation, like module_mac.mm."""
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

    def proto(name, restype, argtypes):
        fn = getattr(cf, name)
        fn.restype, fn.argtypes = restype, argtypes
        return fn

    raw = os.fsencode(str(bundle))
    url = proto("CFURLCreateFromFileSystemRepresentation", ctypes.c_void_p,
                [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_bool])(
                    None, raw, len(raw), True)
    ref = proto("CFBundleCreate", ctypes.c_void_p,
                [ctypes.c_void_p, ctypes.c_void_p])(None, url)
    if not ref or not proto("CFBundleLoadExecutable", ctypes.c_bool,
                            [ctypes.c_void_p])(ref):
        raise OSError("could not load the VST3 bundle")
    cf_str = proto("CFStringCreateWithCString", ctypes.c_void_p,
                   [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32])
    fn_ptr = proto("CFBundleGetFunctionPointerForName", ctypes.c_void_p,
                   [ctypes.c_void_p, ctypes.c_void_p])

    def get(name: str):
        symbol = cf_str(None, name.encode(), 0x08000100)  # kCFStringEncodingUTF8
        return fn_ptr(ref, symbol) or None

    return ref, get


def _open_factory(bundle: Path, binary: Path):
    """Load a VST3 binary and return its raw ``IPluginFactory`` pointer."""
    entry_name, _ = plat.module_entry_names()
    if plat.MACOS:
        ref, get = _load_macos(bundle)
        entry = get(entry_name)
        if not entry:
            raise OSError(f"bundle does not export {entry_name}")
        if not _FUNCTYPE(ctypes.c_bool, ctypes.c_void_p)(entry)(ref):
            raise OSError(f"{entry_name} failed")
        factory = get("GetPluginFactory")
        if not factory:
            raise OSError("bundle does not export GetPluginFactory")
        return ctypes.c_void_p(_FUNCTYPE(ctypes.c_void_p)(factory)())

    module = _CDLL(str(binary))
    entry = getattr(module, entry_name, None)
    if entry_name == "ModuleEntry":  # mandatory on Linux
        if entry is None:
            raise OSError("library does not export ModuleEntry")
        entry.restype = ctypes.c_bool
        entry.argtypes = [ctypes.c_void_p]
        if not entry(ctypes.c_void_p(module._handle)):
            raise OSError("ModuleEntry failed")
    elif entry is not None:  # InitDll is optional on Windows
        entry.restype = ctypes.c_bool
        if not entry():
            raise OSError("InitDll failed")
    module.GetPluginFactory.restype = ctypes.c_void_p
    factory = ctypes.c_void_p(module.GetPluginFactory())
    if not factory.value:
        raise OSError("GetPluginFactory returned null")
    return factory


def probe_bundle(bundle: str | Path) -> dict:
    """Load a ``.vst3`` bundle and return a JSON-serialisable description."""
    path = Path(bundle)
    binary = bundle_binary(path)
    if binary is None:
        return {"module": str(path), "error": "no loadable VST3 binary in bundle"}
    try:
        factory = _open_factory(path, binary)
    except Exception as exc:
        return {"module": str(path), "error": f"{type(exc).__name__}: {exc}"}
    try:
        classes = describe_factory(factory)
        vendor = _factory_vendor(factory)
        for description in classes:
            description["vendor"] = description["vendor"] or vendor
        return {"module": str(path), "classes": classes}
    except Exception as exc:  # pragma: no cover - defensive against odd modules
        return {"module": str(path), "error": f"{type(exc).__name__}: {exc}"}
    finally:
        _release(factory)


def _factory_vendor(factory) -> str:
    get_info = _bind(factory, 3, ctypes.c_int32,
                     (ctypes.c_void_p, ctypes.POINTER(_PFactoryInfo)))
    info = _PFactoryInfo()
    if get_info(_address(factory), ctypes.byref(info)) == 0:
        return _decode(info.vendor)
    return ""


# ---- parameters ---------------------------------------------------------------------------

def _finite(value, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if math.isfinite(number) else fallback


def _select_class(classes: list[dict], cid: str = "") -> dict | None:
    wanted = str(cid or "").strip().upper()
    if wanted:
        return next((item for item in classes
                     if str(item.get("cid", "")).upper() == wanted), None)
    for category in ("Audio Module Class", "Component Controller Class"):
        for item in classes:
            if item.get("category") == category:
                return item
    return classes[0] if classes else None


def _controller_api(controller) -> dict:
    """Bound ``IEditController`` methods (vtable slots from the VST3 SDK)."""
    return {
        "set_component_state": _bind(
            controller, 5, ctypes.c_int32, (ctypes.c_void_p, ctypes.c_void_p)),
        "set_state": _bind(
            controller, 6, ctypes.c_int32, (ctypes.c_void_p, ctypes.c_void_p)),
        "get_state": _bind(
            controller, 7, ctypes.c_int32, (ctypes.c_void_p, ctypes.c_void_p)),
        "count": _bind(controller, 8, ctypes.c_int32, (ctypes.c_void_p,)),
        "info": _bind(
            controller, 9, ctypes.c_int32,
            (ctypes.c_void_p, ctypes.c_int32, ctypes.POINTER(_PParameterInfo))),
        "string_by_value": _bind(
            controller, 10, ctypes.c_int32,
            (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_double,
             ctypes.POINTER(ctypes.c_uint16))),
        "value_by_string": _bind(
            controller, 11, ctypes.c_int32,
            (ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint16),
             ctypes.POINTER(ctypes.c_double))),
        "to_plain": _bind(
            controller, 12, ctypes.c_double,
            (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_double)),
        "to_normalized": _bind(
            controller, 13, ctypes.c_double,
            (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_double)),
        "normalized": _bind(
            controller, 14, ctypes.c_double, (ctypes.c_void_p, ctypes.c_uint32)),
        "set_normalized": _bind(
            controller, 15, ctypes.c_int32,
            (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_double)),
    }


def _unit_paths(controller) -> dict[int, str]:
    """Unit id -> ``"Module/Submodule"`` path (the root unit is not a module)."""
    unit_info = _query_interface(controller, _IID_UNIT_INFO)
    if not unit_info:
        return {}
    try:
        get_count = _bind(unit_info, 3, ctypes.c_int32, (ctypes.c_void_p,))
        get_info = _bind(unit_info, 4, ctypes.c_int32,
                         (ctypes.c_void_p, ctypes.c_int32, ctypes.POINTER(_PUnitInfo)))
        count = int(get_count(_address(unit_info)))
        if count <= 0 or count > _MAX_UNITS:
            return {}
        entries: dict[int, tuple[str, int]] = {}
        for index in range(count):
            info = _PUnitInfo()
            if get_info(_address(unit_info), index, ctypes.byref(info)) != _RESULT_OK:
                continue
            entries[int(info.id)] = (_decode16(info.name), int(info.parent_id))
        paths: dict[int, str] = {}
        for unit_id in entries:
            parts: list[str] = []
            current = unit_id
            for _ in range(len(entries) + 1):
                name, parent = entries.get(current, ("", -1))
                if name and current != 0:
                    parts.append(name)
                if parent == -1 or parent == current or parent not in entries:
                    break
                current = parent
            paths[unit_id] = "/".join(reversed(parts))
        return paths
    finally:
        _release(unit_info)


def _string_for_value(api: dict, handle, param_id: int, value: float) -> str:
    buffer = (ctypes.c_uint16 * _STRING128)()
    if api["string_by_value"](handle, param_id, float(value), buffer) != _RESULT_OK:
        return ""
    return _decode16(buffer)


def _value_from_text(api: dict, handle, param_id: int, text: str) -> float | None:
    buffer = _encode_text(text)
    result = ctypes.c_double(0.0)
    if api["value_by_string"](handle, param_id, buffer, ctypes.byref(result)) != _RESULT_OK:
        return None
    return _finite(result.value, None)


_VALUE_TEXT = re.compile(r"^\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*([A-Za-z%°µμ]+)\s*$")
_SECONDS = {"s", "sec", "secs", "second", "seconds"}
_SEMITONES = {"st", "semitone", "semitones"}


def normalize_value_text(text: str, units: str = "", minimum: float = 0.0,
                         maximum: float = 1.0) -> str:
    """Rewrite a unit-prefixed number into the parameter's own unit.

    Controllers vary in what they accept: Surge XT parses ``"800 Hz"`` but
    silently returns zero for ``"1.2 kHz"``. Rewriting the value into the
    parameter's base unit (``"1200 Hz"``) before asking the plugin keeps
    natural text working without guessing the plugin's own curve.
    """
    match = _VALUE_TEXT.match(str(text or ""))
    if not match:
        return str(text)
    number = float(match.group(1))
    suffix = match.group(2).casefold().replace("µ", "u").replace("μ", "u")
    base = str(units or "").casefold()
    if suffix == "khz":
        return f"{number * 1_000:g} Hz"
    if suffix == "mhz":
        return f"{number * 1_000_000:g} Hz"
    if suffix == "k" and base == "hz":
        return f"{number * 1_000:g} Hz"
    if suffix == "hz" and base == "khz":
        return f"{number / 1_000:g} kHz"
    if suffix in {"ms", "msec"} and base in _SECONDS:
        return f"{number / 1_000:g} s"
    if suffix == "%" and base not in {"%", "percent"}:
        return f"{(number / 100) if maximum <= 1.0 else number:g}"
    if suffix in {"ct", "cents"} and base in _SEMITONES:
        return f"{number / 100:g} st"
    return str(text)



def _parameter_records(controller) -> list[dict]:
    """Every parameter the controller exports, in its own order."""
    api = _controller_api(controller)
    handle = _address(controller)
    count = int(api["count"](handle))
    if count < 0 or count > _MAX_PARAMETERS:
        raise ValueError(
            f"the plugin reports {count} parameters, outside the supported 0..{_MAX_PARAMETERS}")
    modules = _unit_paths(controller)
    records: list[dict] = []
    seen: set[int] = set()
    for index in range(count):
        info = _PParameterInfo()
        if api["info"](handle, index, ctypes.byref(info)) != _RESULT_OK:
            continue
        param_id = int(info.id)
        if param_id in seen:
            continue
        seen.add(param_id)
        title = _decode16(info.title) or f"Parameter {param_id}"
        units = _decode16(info.units)
        normalized = _finite(api["normalized"](handle, param_id))
        plain_min = _finite(api["to_plain"](handle, param_id, 0.0))
        plain_max = _finite(api["to_plain"](handle, param_id, 1.0), 1.0)
        if not plain_max > plain_min:
            # A controller that does not map plain values still exposes the
            # normalized range, which is the only honest fallback.
            plain_min, plain_max = 0.0, 1.0
        default_normalized = _finite(info.default_normalized, normalized)
        flags = int(info.flags)
        records.append({
            "id": param_id,
            "name": title,
            "short_name": _decode16(info.short_title),
            "module": modules.get(int(info.unit_id), ""),
            "units": units,
            "min": plain_min,
            "max": plain_max,
            "default": _finite(api["to_plain"](handle, param_id, default_normalized),
                               default_normalized),
            "current": _finite(api["to_plain"](handle, param_id, normalized), normalized),
            "normalized": normalized,
            "default_normalized": default_normalized,
            "step_count": int(info.step_count),
            "stepped": int(info.step_count) > 0,
            "readonly": bool(flags & _PARAM_READONLY),
            "hidden": bool(flags & _PARAM_HIDDEN),
            "can_automate": bool(flags & _PARAM_CAN_AUTOMATE),
            "is_list": bool(flags & _PARAM_LIST),
            "is_program_change": bool(flags & _PARAM_PROGRAM_CHANGE),
            "is_bypass": bool(flags & _PARAM_BYPASS),
            "unit_id": int(info.unit_id),
            "display": format_display(_string_for_value(api, handle, param_id, normalized), units),
        })
    return records


class _PluginSession:
    """One created component/controller pair, with the calls the probe needs."""

    def __init__(self, component, controller, description: dict,
                 host: _HostApplication | None = None) -> None:
        self.component = component
        self.controller = controller
        self.description = description
        self.host = host
        self.class_cid = str(description.get("cid", "")).upper()
        self.class_name = str(description.get("name", ""))
        self.handler = _ComponentHandler() if controller else None
        self._api = _controller_api(controller) if controller else None

    def set_component_state(self, data: bytes) -> int:
        if self.component is None:
            return 1
        stream = _MemoryStream(data)
        set_state = _bind(self.component, 12, ctypes.c_int32,
                          (ctypes.c_void_p, ctypes.c_void_p))
        return int(set_state(_address(self.component), stream.pointer))

    def get_component_state(self) -> bytes:
        if self.component is None:
            return b""
        stream = _MemoryStream()
        get_state = _bind(self.component, 13, ctypes.c_int32,
                          (ctypes.c_void_p, ctypes.c_void_p))
        if get_state(_address(self.component), stream.pointer) != _RESULT_OK:
            return b""
        return stream.data

    def set_controller_component_state(self, data: bytes) -> int:
        stream = _MemoryStream(data)
        return int(self._api["set_component_state"](_address(self.controller), stream.pointer))

    def set_controller_state(self, data: bytes) -> int:
        stream = _MemoryStream(data)
        return int(self._api["set_state"](_address(self.controller), stream.pointer))

    def get_controller_state(self) -> bytes:
        stream = _MemoryStream()
        if self._api["get_state"](_address(self.controller), stream.pointer) != _RESULT_OK:
            return b""
        return stream.data

    def parameters(self) -> list[dict]:
        return _parameter_records(self.controller)

    def close(self) -> None:
        for handle in (self.controller, self.component):
            if handle:
                try:
                    _terminate(handle)
                except Exception:  # pragma: no cover - a dying plugin is not fatal here
                    pass
                _release(handle)
        self.controller = self.component = None


def _open_session(factory, classes: list[dict], cid: str = "") -> _PluginSession:
    """Create the class's component and controller, like a host would."""
    description = _select_class(classes, cid)
    if description is None:
        raise ValueError(f"no plugin class matches the requested class id {cid!r}")
    raw_cid = cid_bytes(description["cid"])
    component = _create_instance(factory, raw_cid, _IID_COMPONENT)
    controller = None
    if component is not None:
        controller_cid = (ctypes.c_ubyte * 16)()
        get_id = _bind(component, 5, ctypes.c_int32,
                       (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ubyte * 16)))
        if get_id(_address(component), ctypes.byref(controller_cid)) == _RESULT_OK:
            controller = _create_instance(
                factory, bytes(controller_cid), _IID_EDIT_CONTROLLER)
    if controller is None:
        # Single-component classes implement both interfaces on one class id.
        controller = _create_instance(factory, raw_cid, _IID_EDIT_CONTROLLER)
    session = _PluginSession(component, controller, description, _HostApplication())
    if component is not None:
        _initialize(component, session.host.pointer)
    if controller is not None:
        _initialize(controller, session.host.pointer)
        set_handler = _bind(controller, 16, ctypes.c_int32,
                            (ctypes.c_void_p, ctypes.c_void_p))
        set_handler(_address(controller), session.handler.pointer)
        if component is not None:
            _connect_component_controller(component, controller)
    return session


def _connect_component_controller(component, controller) -> None:
    """Link component and controller through ``IConnectionPoint``, like a host."""
    component_point = _query_interface(component, _IID_CONNECTION_POINT)
    controller_point = _query_interface(controller, _IID_CONNECTION_POINT)
    if not component_point or not controller_point:
        for point in (controller_point, component_point):
            if point:
                _release(point)
        return
    try:
        connect = _bind(component_point, 3, ctypes.c_int32,
                        (ctypes.c_void_p, ctypes.c_void_p))
        back = _bind(controller_point, 3, ctypes.c_int32,
                     (ctypes.c_void_p, ctypes.c_void_p))
        connect(_address(component_point), controller_point)
        back(_address(controller_point), component_point)
    finally:
        _release(controller_point)
        _release(component_point)



def _allocate_buses(component, get_bus_info, direction: int, count: int, block: int,
                    activate) -> tuple[ctypes.Array, list]:
    """Zeroed float buffers for every channel of *count* buses, activated."""
    buses = (_PAudioBusBuffers * max(1, count))()
    keepalive: list = []
    for index in range(count):
        info = _PBusInfo()
        channels = 2
        if get_bus_info(_address(component), 0, direction, index,
                        ctypes.byref(info)) == _RESULT_OK and info.channel_count > 0:
            channels = int(info.channel_count)
        activate(_address(component), 0, direction, index, 1)
        pointers = (ctypes.POINTER(ctypes.c_float) * channels)()
        for channel in range(channels):
            buffer = (ctypes.c_float * block)()
            pointers[channel] = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_float))
            keepalive.append(buffer)
        keepalive.append(pointers)
        buses[index].num_channels = channels
        buses[index].silence_flags = 0
        buses[index].channel_buffers = ctypes.cast(
            pointers, ctypes.POINTER(ctypes.POINTER(ctypes.c_float)))
    return buses, keepalive


def _run_processing(session: _PluginSession, values: dict[int, float], *,
                    sample_rate: float = 48_000.0, block: int = 256,
                    blocks: int = 2) -> list[str]:
    """Commit normalized values by processing silent blocks, like a host.

    Some plugins (Surge XT among them) apply incoming parameter changes to the
    state they serialise only inside ``IAudioProcessor::process``. Without this
    step a controller-only change never reaches the component state that MXM
    loads, so the saved preset would silently keep the old patch.
    """
    component = session.component
    if component is None:
        return ["the plugin has no component to run"]
    processor = _query_interface(component, _IID_AUDIO_PROCESSOR)
    if not processor:
        return ["the plugin exposes no audio processor; its state keeps the controller values"]
    warnings: list[str] = []
    try:
        setup = _PProcessSetup(0, 0, int(block), float(sample_rate))
        setup_call = _bind(processor, 7, ctypes.c_int32,
                           (ctypes.c_void_p, ctypes.POINTER(_PProcessSetup)))
        if setup_call(_address(processor), ctypes.byref(setup)) != _RESULT_OK:
            return ["the plugin refused the processing setup"]
        get_bus_count = _bind(component, 7, ctypes.c_int32,
                              (ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32))
        get_bus_info = _bind(component, 8, ctypes.c_int32,
                             (ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32,
                              ctypes.c_int32, ctypes.POINTER(_PBusInfo)))
        activate = _bind(component, 10, ctypes.c_int32,
                         (ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32,
                          ctypes.c_int32, ctypes.c_uint8))
        set_active = _bind(component, 11, ctypes.c_int32,
                           (ctypes.c_void_p, ctypes.c_uint8))
        set_processing = _bind(processor, 8, ctypes.c_int32,
                               (ctypes.c_void_p, ctypes.c_uint8))
        process = _bind(processor, 9, ctypes.c_int32,
                        (ctypes.c_void_p, ctypes.POINTER(_PProcessData)))
        out_count = max(0, int(get_bus_count(_address(component), 0, 1)))
        in_count = max(0, int(get_bus_count(_address(component), 0, 0)))
        if out_count < 1:
            return ["the plugin has no audio output to run"]
        outputs, out_keepalive = _allocate_buses(
            component, get_bus_info, 1, out_count, block, activate)
        inputs = None
        in_keepalive: list = []
        if in_count:
            inputs, in_keepalive = _allocate_buses(
                component, get_bus_info, 0, in_count, block, activate)
        if get_bus_count(_address(component), 1, 0) > 0:
            activate(_address(component), 1, 0, 0, 1)  # event input bus 0

        data = _PProcessData()
        data.process_mode = 0
        data.symbolic_sample_size = 0
        data.num_samples = int(block)
        data.num_inputs = in_count
        data.num_outputs = out_count
        data.inputs = (ctypes.cast(inputs, ctypes.POINTER(_PAudioBusBuffers))
                       if inputs is not None else None)
        data.outputs = ctypes.cast(outputs, ctypes.POINTER(_PAudioBusBuffers))
        context = _PProcessContext()
        context.sample_rate = float(sample_rate)
        context.tempo = 120.0
        context.time_sig_numerator = 4
        context.time_sig_denominator = 4
        context.state = (1 << 10) | (1 << 13)  # kTempoValid | kTimeSigValid
        data.process_context = ctypes.pointer(context)
        changes = _ParameterChanges(values) if values else None
        data.input_parameter_changes = changes.pointer if changes is not None else None
        # Some plugins (DPF's hosts) assert when the outgoing side is null.
        output_changes = _ParameterChanges({})
        data.output_parameter_changes = output_changes.pointer

        set_active(_address(component), 1)
        if set_processing(_address(processor), 1) != _RESULT_OK:
            warnings.append("the plugin refused to start processing")
            set_active(_address(component), 0)
            return warnings
        try:
            for index in range(max(1, blocks)):
                if index:
                    data.input_parameter_changes = None
                if process(_address(processor), ctypes.byref(data)) != _RESULT_OK:
                    warnings.append("the plugin failed while committing parameter values")
                    break
        finally:
            set_processing(_address(processor), 0)
            set_active(_address(component), 0)
        return warnings
    finally:
        _release(processor)


def _clamp01(value) -> float:
    number = _finite(value, None) if value is not None else None
    if number is None:
        raise ValueError("a normalized parameter value must be a finite number")
    return min(1.0, max(0.0, number))


def _load_base_state(session: _PluginSession, *, preset: str = "",
                     state: bytes | None = None) -> list[str]:
    """Load a preset file or component state before inspecting parameters."""
    warnings: list[str] = []
    component_state = b""
    controller_state = b""
    if preset:
        parsed = read_vstpreset(preset)
        chunks = parsed["chunks"]
        component_state = chunks.get("Comp", b"")
        controller_state = chunks.get("Cont", b"")
        if not component_state and not controller_state:
            raise ValueError(f"{preset} contains no component or controller state")
        if parsed["class_id"] and session.class_cid and parsed["class_id"] != session.class_cid:
            warnings.append(
                f"the preset was saved for class {parsed['class_id']}, not {session.class_cid}")
        if not component_state:
            warnings.append("the preset has no component state; the component keeps its defaults")
    elif state:
        if len(state) > _MAX_STATE_BYTES:
            raise ValueError("the component state exceeds the size limit")
        component_state = bytes(state)
    if component_state:
        component_ok = True
        if session.component is not None:
            component_ok = session.set_component_state(component_state) == _RESULT_OK
            if not component_ok:
                warnings.append("the component rejected the base state")
        if (session.set_controller_component_state(component_state) != _RESULT_OK
                and not component_ok):
            # A controller may decline setComponentState while still receiving
            # the component's parameter changes through its connection point.
            warnings.append("the controller also rejected the component state")
    if controller_state and session.set_controller_state(controller_state) != _RESULT_OK:
        warnings.append("the controller rejected its own state")
    return warnings


def _with_session(bundle, action, *, preset: str = "", state: bytes | None = None,
                  cid: str = ""):
    """Run *action* with a live component/controller pair, returning JSON data."""
    path = Path(bundle)
    binary = bundle_binary(path)
    if binary is None:
        return {"module": str(path), "error": "no loadable VST3 binary in bundle"}
    try:
        factory = _open_factory(path, binary)
    except Exception as exc:
        return {"module": str(path), "error": f"{type(exc).__name__}: {exc}"}
    session = None
    try:
        classes = describe_factory(factory)
        if not classes:
            return {"module": str(path), "error": "the bundle exports no plugin classes"}
        session = _open_session(factory, classes, cid)
        if session.controller is None:
            return {"module": str(path), "error": "the plugin class exposes no IEditController"}
        warnings = _load_base_state(session, preset=preset, state=state)
        result = action(path, session)
        if isinstance(result, dict) and warnings:
            existing = list(result.get("warnings") or [])
            result["warnings"] = warnings + existing
        return result
    except Exception as exc:
        return {"module": str(path), "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if session is not None:
            session.close()
        _release(factory)


def inspect_bundle_parameters(bundle, *, preset: str = "", state: bytes | None = None,
                              cid: str = "", text_queries=(), value_queries=()) -> dict:
    """Enumerate a bundle's parameters, optionally converting sample values.

    *text_queries* is ``[(param_id, text), ...]`` and *value_queries* is
    ``[(param_id, normalized_value), ...]``; each is answered with the plugin's
    own conversion so callers can round-trip natural text.
    """
    if state is not None and len(state) > _MAX_STATE_BYTES:
        return {"module": str(Path(bundle)), "error": "the component state is too large"}

    def action(path, session):
        parameters = session.parameters()
        records = {item["id"]: item for item in parameters}
        api = _controller_api(session.controller)
        handle = _address(session.controller)
        text_results = []
        for param_id, text in text_queries:
            param_id = int(param_id)
            record = records.get(param_id)
            converted = normalize_value_text(
                text,
                record["units"] if record else "",
                record["min"] if record else 0.0,
                record["max"] if record else 1.0,
            )
            normalized = _value_from_text(api, handle, param_id, converted)
            if normalized is None:
                text_results.append({"id": param_id, "text": str(text),
                                     "error": "the plugin could not parse this text"})
                continue
            plain = _finite(api["to_plain"](handle, param_id, normalized), normalized)
            text_results.append({
                "id": param_id,
                "name": record["name"] if record else "",
                "text": str(text),
                "parsed_text": converted,
                "normalized": normalized,
                "plain": plain,
                "display": format_display(
                    _string_for_value(api, handle, param_id, normalized),
                    record["units"] if record else ""),
            })
        value_results = []
        for param_id, value in value_queries:
            param_id = int(param_id)
            record = records.get(param_id)
            normalized = _clamp01(value)
            plain = _finite(api["to_plain"](handle, param_id, normalized), normalized)
            value_results.append({
                "id": param_id,
                "name": record["name"] if record else "",
                "normalized": normalized,
                "plain": plain,
                "text": format_display(
                    _string_for_value(api, handle, param_id, normalized),
                    record["units"] if record else ""),
                "units": record["units"] if record else "",
            })
        return {
            "module": str(path),
            "class": {"cid": session.class_cid, "name": session.class_name},
            "count": len(parameters),
            "parameters": parameters,
            "text_queries": text_results,
            "value_queries": value_results,
        }

    return _with_session(bundle, action, preset=preset, state=state, cid=cid)


def text_to_value(bundle, param_id: int, text: str, *, preset: str = "",
                  state: bytes | None = None, cid: str = "") -> dict:
    """The plugin's normalized value for natural text such as ``"800 Hz"``."""
    result = inspect_bundle_parameters(
        bundle, preset=preset, state=state, cid=cid, text_queries=[(param_id, text)])
    if result.get("error"):
        return result
    return {"module": result["module"], "class": result.get("class"),
            **(result.get("text_queries") or [{}])[0]}


def value_to_text(bundle, param_id: int, value: float, *, preset: str = "",
                  state: bytes | None = None, cid: str = "") -> dict:
    """The plugin's display text (with units) for a normalized value."""
    result = inspect_bundle_parameters(
        bundle, preset=preset, state=state, cid=cid, value_queries=[(param_id, value)])
    if result.get("error"):
        return result
    return {"module": result["module"], "class": result.get("class"),
            **(result.get("value_queries") or [{}])[0]}


def save_bundle_preset(bundle, out_path, *, preset: str = "", state: bytes | None = None,
                       cid: str = "", overrides=(), text_overrides=()) -> dict:
    """Write a ``.vstpreset`` from the plugin's current state plus overrides.

    *overrides* is ``[(param_id, normalized_value), ...]``; *text_overrides* is
    ``[(param_id, text, fallback_normalized_or_None), ...]``. A fallback is used
    only when the plugin itself cannot parse the text. The result reports the
    values that were applied (``applied``) so the caller can reload the saved
    file in a fresh session and verify them independently.
    """
    destination = Path(out_path)
    if state is not None and len(state) > _MAX_STATE_BYTES:
        return {"module": str(Path(bundle)), "error": "the component state is too large"}

    def action(path, session):
        api = _controller_api(session.controller)
        handle = _address(session.controller)
        records = {item["id"]: item for item in session.parameters()}
        warnings: list[str] = []
        applied = 0
        expected: dict[int, float] = {}
        text_results = []
        for param_id, value in overrides:
            param_id = int(param_id)
            normalized = _clamp01(value)
            if param_id not in records:
                warnings.append(f"parameter #{param_id} is not exposed by this plugin")
                continue
            if api["set_normalized"](handle, param_id, normalized) != _RESULT_OK:
                warnings.append(f"parameter {records[param_id]['name']!r} rejected the value")
                continue
            expected[param_id] = _finite(api["normalized"](handle, param_id), normalized)
            applied += 1
        for param_id, text, fallback in text_overrides:
            param_id = int(param_id)
            if param_id not in records:
                warnings.append(f"parameter #{param_id} is not exposed by this plugin")
                continue
            record = records[param_id]
            converted = normalize_value_text(
                text, record.get("units", ""), record.get("min", 0.0),
                record.get("max", 1.0))
            normalized = _value_from_text(api, handle, param_id, converted)
            source = "text"
            if normalized is None:
                if fallback is None:
                    warnings.append(
                        f"{records[param_id]['name']!r} cannot parse {str(text)!r}")
                    continue
                normalized = _clamp01(fallback)
                source = "fallback"
            if api["set_normalized"](handle, param_id, normalized) != _RESULT_OK:
                warnings.append(f"parameter {records[param_id]['name']!r} rejected the value")
                continue
            expected[param_id] = _finite(api["normalized"](handle, param_id), normalized)
            applied += 1
            text_results.append({"id": param_id, "text": str(text),
                                 "parsed_text": converted, "source": source})
        if expected:
            warnings.extend(_run_processing(session, dict(expected)))
        component_state = session.get_component_state() if session.component is not None else b""
        controller_state = session.get_controller_state()
        if not component_state and not controller_state:
            raise ValueError("the plugin returned no state to save")
        if not component_state:
            warnings.append(
                "the plugin has no component state; MXM's host loads the component chunk")
        chunks: dict[str, bytes] = {}
        if component_state:
            chunks["Comp"] = component_state
        if controller_state:
            chunks["Cont"] = controller_state
        data = build_vstpreset(session.class_cid, chunks)
        _atomic_write(destination, data)
        return {
            "module": str(path),
            "path": str(destination),
            "bytes": len(data),
            "class": {"cid": session.class_cid, "name": session.class_name},
            "parameters_applied": applied,
            "applied": [{"id": param_id, "normalized": value}
                        for param_id, value in sorted(expected.items())],
            "parameter_results": text_results,
            "warnings": warnings,
        }

    return _with_session(bundle, action, preset=preset, state=state, cid=cid)


# ---- .vstpreset container -----------------------------------------------------------------

def build_vstpreset(class_id: str, chunks: dict[str, bytes]) -> bytes:
    """A Steinberg ``.vstpreset`` stream (native little-endian, as the SDK writes it)."""
    order = [name for name in ("Comp", "Cont", "Prog", "Info") if chunks.get(name)]
    payload = bytearray(b"VST3")
    payload += struct.pack("<i", 1)
    encoded = str(class_id).strip().upper().encode("ascii", "replace")[:32]
    payload += encoded.ljust(32, b"\x00")
    payload += b"\x00" * 8  # chunk list offset, filled in below
    entries: list[tuple[bytes, int, int]] = []
    for name in order:
        data = chunks[name]
        entries.append((name.encode("ascii"), len(payload), len(data)))
        payload += data
    list_offset = len(payload)
    payload += b"List" + struct.pack("<i", len(entries))
    for name, offset, size in entries:
        payload += name.ljust(4, b"\x00")[:4] + struct.pack("<qq", offset, size)
    payload[40:48] = struct.pack("<q", list_offset)
    return bytes(payload)


def read_vstpreset(path) -> dict:
    """Read a ``.vstpreset`` file as ``{"class_id": ..., "chunks": {...}}``."""
    preset_path = Path(path)
    try:
        size = preset_path.stat().st_size
        if size > _MAX_PRESET_BYTES:
            raise ValueError(f"{preset_path} exceeds the preset size limit")
        data = preset_path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read {preset_path}: {exc}") from exc
    if len(data) < 48 or data[:4] != b"VST3":
        raise ValueError(f"{preset_path} is not a VST3 preset")
    class_id = data[8:40].decode("ascii", "replace").strip("\x00 ").upper()
    list_offset = struct.unpack_from("<q", data, 40)[0]
    if (list_offset < 48 or list_offset + 8 > len(data)
            or data[list_offset:list_offset + 4] != b"List"):
        raise ValueError(f"{preset_path} has an invalid chunk list")
    count = struct.unpack_from("<i", data, list_offset + 4)[0]
    if count < 0 or count > _MAX_PRESET_CHUNKS:
        raise ValueError(f"{preset_path} has an unsupported chunk count")
    chunks: dict[str, bytes] = {}
    for index in range(count):
        position = list_offset + 8 + index * 20
        if position + 20 > len(data):
            raise ValueError(f"{preset_path} has a truncated chunk table")
        chunk_id = data[position:position + 4].decode("ascii", "replace")
        offset, length = struct.unpack_from("<qq", data, position + 4)
        if offset < 48 or length < 0 or offset + length > len(data):
            raise ValueError(f"{preset_path} has an invalid {chunk_id!r} chunk")
        chunks[chunk_id] = data[offset:offset + length]
    return {"class_id": class_id, "chunks": chunks}


def _atomic_write(path: Path, data: bytes) -> None:
    """Write *data* to *path* through a temporary file in the same directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


_USAGE = """usage: python -m mxm_mcp.vst3_probe [options]

  <bundle> [...]                     print each bundle's classes (default)
  --params <bundle>                  print parameter inspection JSON
  --save-preset <bundle> --out PATH  write a .vstpreset from the current state

Inspection and export options:
  --preset PATH        load this .vstpreset before inspecting/saving
  --state-file PATH    load this raw component state before inspecting/saving
  --cid CID            inspect a specific class id instead of the first module
  --text ID=TEXT       (with --params) convert natural text to a value
  --value ID=NUMBER    (with --params) format a normalized value as text
  --set ID=NORMALIZED  (with --save-preset) set a normalized parameter value
  --set-text ID=TEXT   (with --save-preset) set a parameter from natural text
  --set-fallback ID=NUMBER
                       value used when the plugin cannot parse --set-text
"""


def _next_value(argv: list[str], index: int, flag: str) -> tuple[str, int]:
    if index + 1 >= len(argv):
        raise ValueError(f"{flag} needs a value")
    return argv[index + 1], index + 2


def _split_assignment(text: str) -> tuple[str, str]:
    key, separator, value = str(text).partition("=")
    if not separator or not key.strip():
        raise ValueError(f"expected ID=VALUE, got {text!r}")
    return key.strip(), value.strip()


def _parameter_id(key: str) -> int:
    try:
        return int(key, 0)
    except ValueError as exc:
        raise ValueError(f"parameter id {key!r} is not a number") from exc


def _parse_arguments(argv: list[str]) -> dict:
    options = {
        "mode": "classes", "bundle": "", "preset": "", "state": None, "cid": "",
        "out": "", "text": [], "value": [], "sets": [], "text_sets": [],
        "fallbacks": {}, "bundles": [],
    }
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument in ("-h", "--help"):
            options["mode"] = "help"
            return options
        if argument == "--params":
            options["mode"] = "params"
            options["bundle"], index = _next_value(argv, index, argument)
        elif argument == "--save-preset":
            options["mode"] = "save"
            options["bundle"], index = _next_value(argv, index, argument)
        elif argument == "--preset":
            options["preset"], index = _next_value(argv, index, argument)
        elif argument == "--state-file":
            source, index = _next_value(argv, index, argument)
            try:
                data = Path(source).read_bytes()
            except OSError as exc:
                raise ValueError(f"cannot read {source}: {exc}") from exc
            if len(data) > _MAX_STATE_BYTES:
                raise ValueError("the state file exceeds the size limit")
            options["state"] = data
        elif argument == "--cid":
            options["cid"], index = _next_value(argv, index, argument)
        elif argument == "--out":
            options["out"], index = _next_value(argv, index, argument)
        elif argument == "--text":
            pairs, index = _next_value(argv, index, argument)
            key, text = _split_assignment(pairs)
            options["text"].append((_parameter_id(key), text))
        elif argument == "--value":
            pairs, index = _next_value(argv, index, argument)
            key, value = _split_assignment(pairs)
            options["value"].append((_parameter_id(key), float(value)))
        elif argument == "--set":
            pairs, index = _next_value(argv, index, argument)
            key, value = _split_assignment(pairs)
            options["sets"].append((_parameter_id(key), float(value)))
        elif argument == "--set-text":
            pairs, index = _next_value(argv, index, argument)
            key, text = _split_assignment(pairs)
            options["text_sets"].append((_parameter_id(key), text))
        elif argument == "--set-fallback":
            pairs, index = _next_value(argv, index, argument)
            key, value = _split_assignment(pairs)
            options["fallbacks"][_parameter_id(key)] = float(value)
        elif argument.startswith("-"):
            raise ValueError(f"unknown option {argument}")
        else:
            options["bundles"].append(argument)
            index += 1
    return options


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    try:
        options = _parse_arguments(list(argv if argv is not None else sys.argv[1:]))
    except ValueError as exc:
        _emit({"error": str(exc)})
        return 2
    if options["mode"] == "help":
        sys.stdout.write(_USAGE)
        return 0
    if options["mode"] == "classes":
        bundles = ([options["bundle"]] if options["bundle"] else []) + options["bundles"]
        if not bundles:
            _emit({"error": "no bundle given; see --help"})
            return 2
        for bundle in bundles:
            _emit(probe_bundle(bundle))
        return 0
    if not options["bundle"]:
        _emit({"error": "no bundle given; see --help"})
        return 2
    if options["mode"] == "params":
        result = inspect_bundle_parameters(
            options["bundle"], preset=options["preset"], state=options["state"],
            cid=options["cid"], text_queries=options["text"],
            value_queries=options["value"],
        )
        _emit(result)
        return 0
    if not options["out"]:
        _emit({"error": "--save-preset needs --out PATH"})
        return 2
    fallbacks = options["fallbacks"]
    text_overrides = [(param_id, text, fallbacks.get(param_id))
                      for param_id, text in options["text_sets"]]
    result = save_bundle_preset(
        options["bundle"], options["out"], preset=options["preset"],
        state=options["state"], cid=options["cid"], overrides=options["sets"],
        text_overrides=text_overrides,
    )
    _emit(result)
    return 1 if result.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
