"""Linux confinement for the native VST3 metadata probe.

This is intentionally a small, dependency-free sandbox, not a general-purpose
plugin host. It is applied in the probe process before loading any plugin code.
Windows and macOS need their native restricted-token/Seatbelt equivalents and
are not represented as sandboxed by this module.
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import sys
from pathlib import Path

_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2
_SECCOMP_RET_KILL_PROCESS = 0x80000000
_SECCOMP_RET_ERRNO = 0x00050000
_SECCOMP_RET_ALLOW = 0x7FFF0000

_FS_EXECUTE = 1 << 0
_FS_WRITE_FILE = 1 << 1
_FS_READ_FILE = 1 << 2
_FS_READ_DIR = 1 << 3
_FS_REMOVE_DIR = 1 << 4
_FS_REMOVE_FILE = 1 << 5
_FS_MAKE_CHAR = 1 << 6
_FS_MAKE_DIR = 1 << 7
_FS_MAKE_REG = 1 << 8
_FS_MAKE_SOCK = 1 << 9
_FS_MAKE_FIFO = 1 << 10
_FS_MAKE_BLOCK = 1 << 11
_FS_MAKE_SYM = 1 << 12
_FS_REFER = 1 << 13
_FS_TRUNCATE = 1 << 14
_FS_IOCTL_DEV = 1 << 15

_ARCH_SYSCALLS = {
    "x86_64": {
        "audit": 0xC000003E,
        "blocked": (29, 30, 31, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51,
                    52, 53, 62, 64, 65, 66, 67, 68, 69, 70, 71, 101, 109,
                    112, 129, 155, 161, 165, 166, 200, 234, 248, 249, 250,
                    272, 297, 298, 304, 308, 310, 311, 312, 321, 323, 424,
                    425, 428, 429, 430, 431, 432, 434, 438, 442, 448),
    },
    "amd64": {
        "audit": 0xC000003E,
        "blocked": (41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53,
                    62, 101, 129, 155, 161, 165, 166, 200, 234, 248, 249,
                    250, 272, 297, 298, 304, 308, 310, 311, 321, 323, 424,
                    425, 428, 429, 430, 431, 432, 434, 438, 442, 448),
    },
    "aarch64": {
        "audit": 0xC00000B7,
        "blocked": (186, 187, 188, 189, 190, 191, 192, 193, 194, 195, 196,
                    197, 198, 199, 200, 201, 202, 203, 204, 205, 206, 207,
                    208, 209, 210, 211, 212, 39, 40, 41, 51, 97, 117, 129,
                    130, 131, 138, 154, 157, 217, 218, 219, 240, 241, 265,
                    268, 270, 271, 280, 282, 424, 425, 428, 429, 430, 431,
                    432, 434, 438, 442, 448),
    },
    "arm64": {
        "audit": 0xC00000B7,
        "blocked": (186, 187, 188, 189, 190, 191, 192, 193, 194, 195, 196,
                    197, 198, 199, 200, 201, 202, 203, 204, 205, 206, 207,
                    208, 209, 210, 211, 212, 39, 40, 41, 51, 97, 117, 129,
                    130, 131, 138, 154, 157, 217, 218, 219, 240, 241, 265,
                    268, 270, 271, 280, 282, 424, 425, 428, 429, 430, 431,
                    432, 434, 438, 442, 448),
    },
}
_ARCH_SYSCALLS["amd64"] = _ARCH_SYSCALLS["x86_64"]
_ARCH_SYSCALLS["arm64"] = _ARCH_SYSCALLS["aarch64"]


class _LandlockRulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _LandlockPathBeneathAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class _SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32)]


class _SockFprog(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter))]


def _libc():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return libc


def _landlock_abi() -> int:
    if not sys.platform.startswith("linux"):
        return 0
    result = _libc().syscall(444, None, 0, _LANDLOCK_CREATE_RULESET_VERSION)
    return int(result) if result > 0 else 0


def sandbox_available() -> bool:
    """Whether this Linux/kernel/architecture supports the enforced profile."""
    machine = platform.machine().lower()
    return (sys.platform.startswith("linux") and machine in _ARCH_SYSCALLS
            and _landlock_abi() >= 1)


def _handled_fs_access(abi: int) -> int:
    rights = 0x1FFF
    if abi >= 2:
        rights |= _FS_REFER
    if abi >= 3:
        rights |= _FS_TRUNCATE
    if abi >= 5:
        rights |= _FS_IOCTL_DEV
    return rights


def _allowed_roots(bundle: Path, allowed_root: Path, scratch: Path) -> list[Path]:
    candidates = [
        Path("/usr"), Path("/lib"), Path("/lib64"), Path("/bin"),
        Path("/sbin"), Path("/etc"), Path("/dev/null"), Path("/dev/zero"),
        Path("/dev/random"), Path("/dev/urandom"), bundle, allowed_root,
        Path(__file__).resolve().parent.parent, Path(sys.executable).resolve().parent,
        Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(),
    ]
    runtime_paths = [*sys.path, *os.environ.get("PATH", "").split(os.pathsep),
                     *os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep)]
    for item in runtime_paths:
        if not item:
            continue
        try:
            candidates.append(Path(item).resolve(strict=True))
        except (OSError, RuntimeError):
            continue
    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            roots.append(resolved)
    scratch = scratch.resolve(strict=True)
    if str(scratch) not in seen:
        roots.append(scratch)
    return roots


def _install_landlock(bundle: Path, allowed_root: Path, scratch: Path) -> None:
    abi = _landlock_abi()
    if abi < 1:
        raise OSError(errno.ENOSYS, "Linux Landlock is unavailable")
    libc = _libc()
    handled = _handled_fs_access(abi)
    attr = _LandlockRulesetAttr(handled)
    ruleset_fd = libc.syscall(444, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if ruleset_fd < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), "landlock_create_ruleset")
    try:
        # The root rule permits path lookup only. File contents/exec are granted
        # below for system runtime, the selected bundle, and the isolated scratch.
        roots = _allowed_roots(bundle, allowed_root, scratch)
        root_fd = os.open("/", getattr(os, "O_PATH", os.O_RDONLY) | os.O_CLOEXEC)
        try:
            root_rule = _LandlockPathBeneathAttr(_FS_READ_DIR, root_fd)
            if libc.syscall(445, ruleset_fd, _LANDLOCK_RULE_PATH_BENEATH,
                            ctypes.byref(root_rule), 0) < 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error), "landlock_add_rule(/)")
        finally:
            os.close(root_fd)

        read_access = _FS_EXECUTE | _FS_READ_FILE | _FS_READ_DIR
        for path in roots:
            path_fd = os.open(path, getattr(os, "O_PATH", os.O_RDONLY) | os.O_CLOEXEC)
            try:
                if path == scratch.resolve():
                    allowed = handled
                elif path.is_dir():
                    allowed = read_access
                else:
                    allowed = _FS_EXECUTE | _FS_READ_FILE
                rule = _LandlockPathBeneathAttr(allowed, path_fd)
                if libc.syscall(445, ruleset_fd, _LANDLOCK_RULE_PATH_BENEATH,
                                ctypes.byref(rule), 0) < 0:
                    error = ctypes.get_errno()
                    raise OSError(error, os.strerror(error), f"landlock_add_rule({path})")
            finally:
                os.close(path_fd)
        if libc.syscall(446, ruleset_fd, 0) < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), "landlock_restrict_self")
    finally:
        os.close(ruleset_fd)


def _install_seccomp() -> None:
    profile = _ARCH_SYSCALLS.get(platform.machine().lower())
    if profile is None:
        raise OSError(errno.ENOTSUP, "no seccomp syscall table for this architecture")
    libc = _libc()
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), "PR_SET_NO_NEW_PRIVS")

    # BPF loads seccomp_data.arch, kills on an unexpected ABI, rejects network
    # and cross-process control syscalls, and permits ordinary plugin loading.
    instructions = [
        _SockFilter(0x20, 0, 0, 4),                       # LD W ABS arch
        _SockFilter(0x15, 1, 0, profile["audit"]),       # JEQ expected arch
        _SockFilter(0x06, 0, 0, _SECCOMP_RET_KILL_PROCESS),
        _SockFilter(0x20, 0, 0, 0),                       # LD W ABS nr
    ]
    for number in profile["blocked"]:
        instructions.append(_SockFilter(0x15, 0, 1, number))
        instructions.append(_SockFilter(0x06, 0, 0, _SECCOMP_RET_ERRNO | errno.EPERM))
    instructions.append(_SockFilter(0x06, 0, 0, _SECCOMP_RET_ALLOW))
    array = (_SockFilter * len(instructions))(*instructions)
    program = _SockFprog(len(instructions), array)
    if libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(program)) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), "PR_SET_SECCOMP")


def enter_linux_sandbox(
    bundle: str | Path,
    scratch: str | Path,
    allowed_root: str | Path | None = None,
) -> None:
    """Confine this probe process before it loads code from *bundle*."""
    if not sandbox_available():
        raise OSError(errno.ENOTSUP, "Landlock/seccomp sandbox is unavailable")
    import resource

    bundle_path = Path(bundle).resolve(strict=True)
    scratch_path = Path(scratch).resolve(strict=True)
    root_path = Path(allowed_root).resolve(strict=True) if allowed_root else bundle_path
    try:
        bundle_path.relative_to(root_path)
    except ValueError as exc:
        raise OSError(errno.EPERM, "VST3 bundle is outside the permitted probe root") from exc
    os.chdir(scratch_path)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    resource.setrlimit(resource.RLIMIT_FSIZE, (4 * 1024 * 1024, 4 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    if resource.RLIMIT_AS is not None:
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    libc = _libc()
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), "PR_SET_NO_NEW_PRIVS")
    _install_landlock(bundle_path, root_path, scratch_path)
    _install_seccomp()
