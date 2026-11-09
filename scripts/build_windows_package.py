"""Build the MXM MCP Windows executable and per-user setup package."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "packaging" / "mxm-mcp.spec"
INSTALLER = ROOT / "installer" / "windows" / "mxm-mcp-setup.iss"
HARNESS_SOURCE = ROOT / "installer" / "configure_harnesses.py"
HARNESS_ENTRY = ROOT / "installer" / "windows" / "configure_harnesses_entry.py"
DIST = ROOT / "dist"
WORK = ROOT / "build" / "windows-package"
OUTPUT = DIST / "mxm-mcp-setup-windows-x64.exe"


def find_iscc() -> Path | None:
    """Find the Inno Setup 6 compiler on PATH or in its standard locations."""
    on_path = shutil.which("iscc") or shutil.which("iscc.exe")
    if on_path:
        return Path(on_path)

    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Inno Setup 6"
        / "iscc.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Inno Setup 6"
        / "iscc.exe",
    ]
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def run(command: list[str], *, cwd: Path = ROOT) -> None:
    print("+", subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def main() -> int:
    if sys.platform != "win32":
        raise SystemExit("The Windows setup package must be built on Windows.")
    if not SPEC.is_file() or not INSTALLER.is_file():
        raise SystemExit("The MXM MCP PyInstaller specification or setup script is missing.")
    if not HARNESS_SOURCE.is_file() or not HARNESS_ENTRY.is_file():
        raise SystemExit("The MXM MCP harness helper or Windows adapter is missing.")

    iscc = find_iscc()
    if iscc is None:
        raise SystemExit(
            "Inno Setup 6 was not found. Install it or add iscc.exe to PATH."
        )

    DIST.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)

    run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--clean",
            "--noconfirm",
            "--distpath",
            str(DIST),
            "--workpath",
            str(WORK / "application"),
            str(SPEC),
        ]
    )
    run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--onefile",
            "--clean",
            "--noconfirm",
            "--name",
            "configure_harnesses",
            "--paths",
            str(HARNESS_SOURCE.parent),
            "--distpath",
            str(DIST),
            "--workpath",
            str(WORK / "harness"),
            str(HARNESS_ENTRY),
        ]
    )
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    run([str(iscc), f"/DProductVersion={version}", INSTALLER.name], cwd=INSTALLER.parent)

    if not OUTPUT.is_file() or OUTPUT.stat().st_size == 0:
        raise SystemExit(f"Inno Setup did not produce the expected package: {OUTPUT}")
    print(f"Created {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
