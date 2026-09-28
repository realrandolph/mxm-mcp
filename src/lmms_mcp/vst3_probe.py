"""Standalone VST3 module introspection for MXM's native VST3 host.

Dependency-free (stdlib + :mod:`ctypes`) and run in a separate process by
:mod:`lmms_mcp.vst3`: loading third-party binaries in-process is unsafe, so a
faulty module only kills its own subprocess.

Mirrors the SDK loaders MXM vendors (module_linux/win32/mac) plus
``Vst3Manager``: load the bundle, get its ``IPluginFactory``, enumerate the
classes (preferring the UTF-16 ``IPluginFactory3`` info so non-ASCII names
survive), and report each class id exactly as ``UID::toString()`` formats it -
the value MXM matches against the project's ``<key>`` on load.

Run as ``python -m lmms_mcp.vst3_probe <bundle> [...]``; prints one JSON object
per bundle: ``{"module": ..., "classes": [...]}`` or ``{"module": ..., "error": ...}``.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
from pathlib import Path

from . import vst3_platform as plat

_IID_FACTORY2 = plat.iid_bytes("factory2")
_IID_FACTORY3 = plat.iid_bytes("factory3")

if plat.WINDOWS:
    _CDLL, _FUNCTYPE = ctypes.WinDLL, ctypes.WINFUNCTYPE
    try:  # plugins may use COM while initializing; MXM does the same.
        ctypes.windll.ole32.CoInitializeEx(None, 2)
    except Exception:  # pragma: no cover - best effort
        pass
else:
    _CDLL, _FUNCTYPE = ctypes.CDLL, ctypes.CFUNCTYPE

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


def bundle_binary(bundle: str | Path) -> Path | None:
    """The loadable VST3 binary for *bundle* (a package, or a bare module)."""
    path = Path(bundle)
    if path.is_file():
        return path
    if not path.is_dir():
        return None
    return next((p for p in plat.bundle_binaries(path) if p.is_file()), None)


def describe_factory(factory) -> list[dict]:
    """Enumerate the classes exported by a VST3 ``IPluginFactory``.

    Prefers ``IPluginFactory3::getClassInfoUnicode`` (UTF-16) and falls back
    to ``IPluginFactory2`` then v1, like the SDK's ``ClassInfo``.
    """
    count = _bind(factory, 4, ctypes.c_int32, (ctypes.c_void_p,))(_address(factory))
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


def main(argv: list[str] | None = None) -> int:
    for bundle in (argv if argv is not None else sys.argv[1:]):
        result = probe_bundle(bundle)
        sys.stdout.write(json.dumps(result) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
