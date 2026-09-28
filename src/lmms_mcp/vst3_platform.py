"""Per-platform VST3 facts shared by discovery (:mod:`vst3`) and the probe.

Mirrors the VST3 SDK loaders vendored by MXM (``module_linux.cpp``,
``module_win32.cpp``, ``module_mac.mm``) and MXM's ``Vst3Manager``.  MXM
currently compiles its native VST3 host only for Linux and Windows, but the
macOS bundle layout below is the SDK's and applies if MXM is built there too.
"""

from __future__ import annotations

import os
import platform
import struct
import sys
from pathlib import Path

WINDOWS = sys.platform.startswith("win")
MACOS = sys.platform == "darwin"
LINUX = sys.platform.startswith("linux")

#! IPluginFactory2/3 interface ids from pluginterfaces/base/ipluginbase.h's
#! DECLARE_CLASS_IID, as the four 32-bit words given there.
_FACTORY_WORDS = {
    "factory2": (0x0007B650, 0xF24B4C0B, 0xA464EDB9, 0xF00B2ABB),
    "factory3": (0x4555A2AB, 0xC1234E57, 0x9B122910, 0x36878931),
}

#! Architecture folders a Windows VST3 package may use (module_win32.cpp).
_WIN_ARCHS = ("x86_64-win", "arm64-win", "arm64ec-win", "arm64x-win",
              "x86-win", "arm-win")


def discovery_supported() -> bool:
    """Whether the MCP's probe understands this platform's bundle layout."""
    return LINUX or WINDOWS or MACOS


def com_compatible() -> bool:
    """The SDK sets COM_COMPATIBLE on Windows, which flips UID byte order."""
    return WINDOWS


def iid_bytes(name: str) -> bytes:
    """Raw TUID bytes for a factory interface id, per the SDK's INLINE_UID."""
    l1, l2, l3, l4 = _FACTORY_WORDS[name]
    if com_compatible():
        # Windows GUID layout: Data1/2/3 little-endian, Data4 in order.
        return (struct.pack("<I", l1) + struct.pack("<HH", l2 >> 16, l2 & 0xFFFF)
                + struct.pack(">I", l3) + struct.pack(">I", l4))
    return b"".join(struct.pack(">I", word) for word in (l1, l2, l3, l4))


def format_cid(raw: bytes) -> str:
    """Format 16 raw TUID bytes exactly like MXM's ``UID::toString()``."""
    if com_compatible():
        first, second, third = struct.unpack_from("<IHH", raw)
        return f"{first:08X}{second:04X}{third:04X}{raw[8:].hex().upper()}"
    return raw.hex().upper()


def bundle_binaries(bundle: Path) -> list[Path]:
    """Candidate loadable binaries inside a ``.vst3`` bundle, SDK order."""
    if MACOS:
        return [bundle / "Contents" / "MacOS" / bundle.stem]
    if WINDOWS:
        return [bundle / "Contents" / arch / bundle.name for arch in _WIN_ARCHS]
    return [bundle / "Contents" / f"{platform.machine() or 'x86_64'}-linux"
            / f"{bundle.stem}.so"]


def module_entry_names() -> tuple[str, str]:
    """(entry before GetPluginFactory, exit) exported by a VST3 binary."""
    if MACOS:
        return "bundleEntry", "bundleExit"
    if WINDOWS:
        return "InitDll", "ExitDll"
    return "ModuleEntry", "ModuleExit"


def standard_dirs(app_dir: Path | None = None) -> list[Path]:
    """Standard ``.vst3`` locations, in MXM's order (Module::getModulePaths)."""
    if WINDOWS:
        dirs = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Common" / "VST3",
            Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
            / "Common Files" / "VST3",
        ]
    elif MACOS:
        dirs = [Path.home() / "Library" / "Audio" / "Plug-Ins" / "VST3",
                Path("/Library/Audio/Plug-Ins/VST3")]
    else:
        dirs = [Path(home) / ".vst3" for home in (os.environ.get("HOME"),) if home]
        for base in (Path("/usr"), Path("/usr/local")):
            dirs += [base / "lib64" / "vst3", base / "lib" / "vst3"]
    return dirs + ([app_dir] if app_dir else [])


def app_dir_for_exe(exe: Path | None) -> Path | None:
    """MXM's application-level VST3 directory for an executable, if present."""
    if exe is None:
        return None
    exe = Path(os.path.realpath(exe))
    if MACOS:
        bundle = next((p for p in exe.parents if p.suffix == ".app"), None)
        candidate = (bundle / "Contents" / "VST3") if bundle else None
    else:
        candidate = exe.parent / ("VST3" if WINDOWS else "vst3")
    return candidate if candidate and candidate.is_dir() else None
