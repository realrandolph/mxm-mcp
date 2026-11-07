# -*- mode: python ; coding: utf-8 -*-
"""Cross-platform PyInstaller specification for the MXM MCP executable."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata


ROOT = Path(SPECPATH).resolve().parent
SOURCE = ROOT / "src"

# The entry wrapper invokes src/lmms_mcp/server.py as lmms_mcp.server:main.
datas = []
for package in ("mcp", "rdflib", "pydantic"):
    datas += collect_data_files(package)

datas += [
    (str(SOURCE / "lmms_mcp" / "carla_surge_state.b64"), "lmms_mcp"),
]

hiddenimports = []
for package in (
    "mcp",
    "rdflib",
    "pydantic",
    "anyio",
    "starlette",
    "jsonschema",
    "httpx",
    "httpcore",
    "httpx_sse",
    "sse_starlette",
    "uvicorn",
    "referencing",
    "isodate",
    "pyparsing",
    "typing_extensions",
    "typing_inspection",
    "annotated_types",
):
    if package == "mcp":
        hiddenimports += collect_submodules(
            package,
            filter=lambda name: not name.startswith("mcp.cli"),
        )
    else:
        hiddenimports += collect_submodules(package)

datas += copy_metadata("mcp")
datas += copy_metadata("rdflib")

a = Analysis(
    [str(ROOT / "packaging" / "mxm_mcp_entry.py")],
    pathex=[str(SOURCE)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="mxm-mcp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
