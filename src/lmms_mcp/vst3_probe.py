"""Standalone VST3 module introspection for MXM's native VST3 host.

This module is intentionally dependency-free (stdlib + :mod:`ctypes` only) and
is executed in a separate process by :mod:`lmms_mcp.vst3`. Loading third-party
plugin binaries into the MCP server process is unsafe: a faulty module can
crash the interpreter, so every probe runs isolated and a broken plug-in only
kills its own subprocess.

The introspection mirrors what MXM's ``Vst3Manager`` does when it discovers
native VST3 plugins ("plugins/Vst3Base/vst3base/Vst3Manager.cpp"):

* locate the shared object inside the ``.vst3`` bundle directory,
* call the mandatory ``ModuleEntry`` entry point,
* obtain the factory via ``GetPluginFactory``,
* enumerate the classes and keep the ones whose category is
  ``Audio Module Class`` (the same filter MXM applies),
* report the class id exactly as ``Vst3Manager`` stores it, i.e.
  ``classInfo.ID().toString()`` (32 upper-case hex characters).

The same identifiers must be written into the project's ``<key>`` block for
MXM to find the class again on load.

Usage (usually through :mod:`lmms_mcp.vst3`, not directly)::

    python -m lmms_mcp.vst3_probe /path/to/Plugin.vst3 [more.bundles ...]

One JSON object is printed per bundle, one per line:

    {"module": "...", "classes": [{"cid": "...", "name": "...", ...}]}
    {"module": "...", "error": "..."}
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
from pathlib import Path

#! VST3 class category of an audio processor (instrument or effect).
AUDIO_MODULE_CLASS = "Audio Module Class"

#! IPluginFactory2 / IPluginFactory3 interface ids (see
#! pluginterfaces/base/ipluginbase.h). Needed to reach getClassInfo2, which
#! carries the subcategories that identify instruments.
_IID_IPLUGIN_FACTORY2 = bytes.fromhex("0007B650F24B4C0BA464EDB9F00B2ABB")
_IID_IPLUGIN_FACTORY3 = bytes.fromhex("4555A2ABC1234E579B12291036878931")

# FUnknown / IPluginFactory vtable layout (pointers, in order).
_VT_QUERY_INTERFACE = 0
_VT_RELEASE = 2
_VT_GET_FACTORY_INFO = 3
_VT_COUNT_CLASSES = 4
_VT_GET_CLASS_INFO = 5
_VT_GET_CLASS_INFO2 = 7


class _PFactoryInfo(ctypes.Structure):
    _fields_ = [
        ("vendor", ctypes.c_char * 64),
        ("url", ctypes.c_char * 256),
        ("email", ctypes.c_char * 128),
        ("flags", ctypes.c_int32),
    ]


class _PClassInfo(ctypes.Structure):
    # cid is c_ubyte (not c_char) so ctypes returns the raw 16 bytes; c_char
    # arrays are converted to NUL-terminated Python bytes, which truncates
    # class ids that end in zero bytes (common - e.g. Dragonfly, Zam*).
    _fields_ = [
        ("cid", ctypes.c_ubyte * 16),
        ("cardinality", ctypes.c_int32),
        ("category", ctypes.c_char * 32),
        ("name", ctypes.c_char * 64),
    ]


class _PClassInfo2(ctypes.Structure):
    _fields_ = [
        ("cid", ctypes.c_ubyte * 16),
        ("cardinality", ctypes.c_int32),
        ("category", ctypes.c_char * 32),
        ("name", ctypes.c_char * 64),
        ("class_flags", ctypes.c_uint32),
        ("sub_categories", ctypes.c_char * 128),
        ("vendor", ctypes.c_char * 64),
        ("version", ctypes.c_char * 64),
        ("sdk_version", ctypes.c_char * 64),
    ]


def _decode(value: bytes) -> str:
    """Decode a fixed-size C string field, trimming the NUL padding."""
    return value.split(b"\x00", 1)[0].decode("utf-8", "replace")


def vst3_bundle_so_path(bundle: str | Path) -> Path | None:
    """Return the loadable ``.so`` inside a Linux ``.vst3`` bundle.

    Mirrors the layout VST3 uses on Linux:
    ``<Name>.vst3/Contents/<machine>-linux/<Name>.so``.
    """
    path = Path(bundle)
    if not path.is_dir():
        return None
    stem = path.name[:-5] if path.name.lower().endswith(".vst3") else path.name
    so_name = f"{stem}.so"
    machine = os.uname().machine
    candidates = [
        path / "Contents" / f"{machine}-linux" / so_name,
        path / "Contents" / "x86_64-linux" / so_name,
        path / "Contents" / "aarch64-linux" / so_name,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _address(value) -> int:
    """Normalise a factory/interface handle to a plain integer address."""
    if isinstance(value, ctypes.c_void_p):
        return int(value.value or 0)
    return int(value)


def _bind(factory, index: int, restype, argtypes):
    """Return a callable for vtable slot *index* of *factory*."""
    obj = ctypes.cast(ctypes.c_void_p(_address(factory)), ctypes.POINTER(ctypes.c_void_p))
    vtable_address = obj[0]
    vtable = ctypes.cast(ctypes.c_void_p(vtable_address), ctypes.POINTER(ctypes.c_void_p))
    address = vtable[index]
    return ctypes.CFUNCTYPE(restype, *argtypes)(address)


def _query_interface(factory, iid: bytes):
    """Call FUnknown::queryInterface; return the interface pointer or None."""
    query = _bind(
        factory,
        _VT_QUERY_INTERFACE,
        ctypes.c_int32,
        (ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
    )
    iid_buffer = (ctypes.c_char * len(iid)).from_buffer_copy(iid)
    result = ctypes.c_void_p()
    if query(_address(factory), ctypes.byref(iid_buffer), ctypes.byref(result)) == 0:
        return result.value or None
    return None


def _release(factory) -> None:
    release = _bind(factory, _VT_RELEASE, ctypes.c_uint32, (ctypes.c_void_p,))
    release(_address(factory))


def _factory_vendor(factory) -> str:
    get_info = _bind(
        factory,
        _VT_GET_FACTORY_INFO,
        ctypes.c_int32,
        (ctypes.c_void_p, ctypes.POINTER(_PFactoryInfo)),
    )
    info = _PFactoryInfo()
    if get_info(_address(factory), ctypes.byref(info)) == 0:
        return _decode(info.vendor)
    return ""


def describe_factory(factory) -> list[dict]:
    """Enumerate the classes exported by a VST3 ``IPluginFactory``."""
    count_classes = _bind(factory, _VT_COUNT_CLASSES, ctypes.c_int32, (ctypes.c_void_p,))
    count = count_classes(_address(factory))

    # Prefer IPluginFactory2, which carries subcategories (needed to tell an
    # instrument from an effect). Fall back to plain PClassInfo.
    factory2 = _query_interface(factory, _IID_IPLUGIN_FACTORY2)
    # IPluginFactory3 exposes the same information as Unicode; the ASCII
    # IPluginFactory2 path is sufficient for discovery, so it is only released
    # here (queryInterface bumps its reference count).
    factory3 = _query_interface(factory, _IID_IPLUGIN_FACTORY3)
    if factory3:
        _release(factory3)

    if factory2:
        get_class_info = _bind(
            factory2,
            _VT_GET_CLASS_INFO2,
            ctypes.c_int32,
            (ctypes.c_void_p, ctypes.c_int32, ctypes.POINTER(_PClassInfo2)),
        )
    else:
        get_class_info = _bind(
            factory,
            _VT_GET_CLASS_INFO,
            ctypes.c_int32,
            (ctypes.c_void_p, ctypes.c_int32, ctypes.POINTER(_PClassInfo)),
        )

    classes: list[dict] = []
    for index in range(max(0, count)):
        if factory2:
            info = _PClassInfo2()
            if get_class_info(factory2, index, ctypes.byref(info)) != 0:
                continue
            description = {
                "cid": bytes(info.cid).hex().upper(),
                "name": _decode(info.name),
                "category": _decode(info.category),
                "sub_categories": _decode(info.sub_categories),
                "vendor": _decode(info.vendor),
                "version": _decode(info.version),
                "class_flags": int(info.class_flags),
            }
        else:
            info = _PClassInfo()  # type: ignore[assignment]
            if get_class_info(_address(factory), index, ctypes.byref(info)) != 0:
                continue
            description = {
                "cid": bytes(info.cid).hex().upper(),
                "name": _decode(info.name),
                "category": _decode(info.category),
                "sub_categories": "",
                "vendor": "",
                "version": "",
                "class_flags": 0,
            }
        classes.append(description)

    if factory2:
        _release(factory2)
    return classes


def probe_bundle(bundle: str | Path) -> dict:
    """Load a ``.vst3`` bundle and return a JSON-serialisable description."""
    path = Path(bundle)
    so_path = vst3_bundle_so_path(path)
    if so_path is None:
        return {"module": str(path), "error": "no loadable .so found in bundle"}

    try:
        module = ctypes.CDLL(str(so_path), mode=ctypes.RTLD_LOCAL)
    except OSError as exc:
        return {"module": str(path), "error": f"dlopen failed: {exc}"}

    try:
        module_entry = module.ModuleEntry
        module_entry.restype = ctypes.c_bool
        module_entry.argtypes = [ctypes.c_void_p]
        if not module_entry(ctypes.c_void_p(module._handle)):
            return {"module": str(path), "error": "ModuleEntry failed"}

        module.GetPluginFactory.restype = ctypes.c_void_p
        factory = module.GetPluginFactory()
        if not factory:
            return {"module": str(path), "error": "GetPluginFactory returned null"}

        factory = ctypes.c_void_p(factory)
        try:
            classes = describe_factory(factory)
            vendor = _factory_vendor(factory)
            for description in classes:
                if not description["vendor"]:
                    description["vendor"] = vendor
        finally:
            _release(factory)

        return {"module": str(path), "classes": classes}
    except AttributeError as exc:
        return {"module": str(path), "error": f"missing entry point: {exc}"}
    except Exception as exc:  # pragma: no cover - defensive against odd modules
        return {"module": str(path), "error": f"{type(exc).__name__}: {exc}"}


def main(argv: list[str] | None = None) -> int:
    bundles = argv if argv is not None else sys.argv[1:]
    for bundle in bundles:
        try:
            result = probe_bundle(bundle)
        except Exception as exc:  # pragma: no cover - last-resort guard
            result = {"module": str(bundle), "error": f"{type(exc).__name__}: {exc}"}
        sys.stdout.write(json.dumps(result) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
