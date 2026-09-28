"""Standalone VST3 module introspection for MXM's native VST3 host.

Dependency-free (stdlib + :mod:`ctypes`) and run in a separate process by
:mod:`lmms_mcp.vst3`: loading third-party binaries in-process is unsafe, so a
faulty module only kills its own subprocess.

Mirrors MXM's ``Vst3Manager``: load the bundle's ``ModuleEntry`` /
``GetPluginFactory``, enumerate the classes, and report each class id exactly
as ``classInfo.ID().toString()`` (32 upper-case hex), which is what MXM
matches against the project's ``<key>`` on load.

Run as ``python -m lmms_mcp.vst3_probe <bundle> [...]``; prints one JSON object
per bundle: ``{"module": ..., "classes": [...]}`` or ``{"module": ..., "error": ...}``.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
from pathlib import Path

#! IPluginFactory2/3 interface ids (pluginterfaces/base/ipluginbase.h).
_IID_IPLUGIN_FACTORY2 = bytes.fromhex("0007B650F24B4C0BA464EDB9F00B2ABB")
_IID_IPLUGIN_FACTORY3 = bytes.fromhex("4555A2ABC1234E579B12291036878931")

#! FUnknown/IPluginFactory vtable slots: queryInterface=0, release=2,
#! getFactoryInfo=3, countClasses=4, getClassInfo=5, getClassInfo2=7.


class _PFactoryInfo(ctypes.Structure):
    _fields_ = [("vendor", ctypes.c_char * 64), ("url", ctypes.c_char * 256),
                ("email", ctypes.c_char * 128), ("flags", ctypes.c_int32)]


class _PClassInfo2(ctypes.Structure):
    # cid is c_ubyte (not c_char) so ctypes returns the raw 16 bytes; c_char
    # arrays become NUL-terminated bytes and truncate ids ending in zero bytes
    # (common, e.g. Dragonfly/Zam*). The leading fields also match PClassInfo,
    # so a plain IPluginFactory's getClassInfo writes safely into this buffer.
    _fields_ = [
        ("cid", ctypes.c_ubyte * 16), ("cardinality", ctypes.c_int32),
        ("category", ctypes.c_char * 32), ("name", ctypes.c_char * 64),
        ("class_flags", ctypes.c_uint32), ("sub_categories", ctypes.c_char * 128),
        ("vendor", ctypes.c_char * 64), ("version", ctypes.c_char * 64),
        ("sdk_version", ctypes.c_char * 64),
    ]


def _decode(value: bytes) -> str:
    return value.split(b"\x00", 1)[0].decode("utf-8", "replace")


def _address(value) -> int:
    return int(value.value or 0) if isinstance(value, ctypes.c_void_p) else int(value)


def _bind(handle, slot: int, restype, argtypes):
    """Return a callable for vtable *slot* of *handle*."""
    vtable = ctypes.cast(ctypes.c_void_p(_address(handle)), ctypes.POINTER(ctypes.c_void_p))[0]
    address = ctypes.cast(ctypes.c_void_p(vtable), ctypes.POINTER(ctypes.c_void_p))[slot]
    return ctypes.CFUNCTYPE(restype, *argtypes)(address)


def _query_interface(handle, iid: bytes):
    """FUnknown::queryInterface; return the interface address or None."""
    query = _bind(handle, 0, ctypes.c_int32,
                  (ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)))
    iid_buffer = (ctypes.c_char * len(iid)).from_buffer_copy(iid)
    result = ctypes.c_void_p()
    if query(_address(handle), ctypes.byref(iid_buffer), ctypes.byref(result)) == 0:
        return result.value or None
    return None


def _release(handle) -> None:
    _bind(handle, 2, ctypes.c_uint32, (ctypes.c_void_p,))(_address(handle))


def vst3_bundle_so_path(bundle: str | Path) -> Path | None:
    """Return the loadable ``.so`` in a Linux bundle (``Contents/<machine>-linux``)."""
    path = Path(bundle)
    if not path.is_dir():
        return None
    stem = path.name[:-5] if path.name.lower().endswith(".vst3") else path.name
    uname = getattr(os, "uname", None)
    machines = [uname().machine, "x86_64", "aarch64"] if uname else ["x86_64", "aarch64"]
    for machine in machines:
        candidate = path / "Contents" / f"{machine}-linux" / f"{stem}.so"
        if candidate.is_file():
            return candidate
    return None


def describe_factory(factory) -> list[dict]:
    """Enumerate the classes exported by a VST3 ``IPluginFactory``."""
    count = _bind(factory, 4, ctypes.c_int32, (ctypes.c_void_p,))(_address(factory))
    # Prefer IPluginFactory2's getClassInfo2, which carries subcategories (how
    # instruments are told from effects); otherwise fall back to getClassInfo.
    factory2 = _query_interface(factory, _IID_IPLUGIN_FACTORY2)
    factory3 = _query_interface(factory, _IID_IPLUGIN_FACTORY3)
    if factory3:
        _release(factory3)
    target = factory2 or _address(factory)
    get_class_info = _bind(target, 7 if factory2 else 5, ctypes.c_int32,
                           (ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p))

    classes: list[dict] = []
    for index in range(max(0, count)):
        info = _PClassInfo2()
        if get_class_info(target, index, ctypes.byref(info)) != 0:
            continue
        classes.append({
            "cid": bytes(info.cid).hex().upper(),
            "name": _decode(info.name),
            "category": _decode(info.category),
            "sub_categories": _decode(info.sub_categories),
            "vendor": _decode(info.vendor),
            "version": _decode(info.version),
            "class_flags": int(info.class_flags),
        })
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
        module.ModuleEntry.restype = ctypes.c_bool
        module.ModuleEntry.argtypes = [ctypes.c_void_p]
        if not module.ModuleEntry(ctypes.c_void_p(module._handle)):
            return {"module": str(path), "error": "ModuleEntry failed"}
        module.GetPluginFactory.restype = ctypes.c_void_p
        factory = ctypes.c_void_p(module.GetPluginFactory())
        if not factory.value:
            return {"module": str(path), "error": "GetPluginFactory returned null"}
    except (OSError, AttributeError) as exc:
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
        try:
            result = probe_bundle(bundle)
        except Exception as exc:  # pragma: no cover - last-resort guard
            result = {"module": str(bundle), "error": f"{type(exc).__name__}: {exc}"}
        sys.stdout.write(json.dumps(result) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
