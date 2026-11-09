"""MXM MCP Server - AI-powered music production with MXM."""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

try:
    __version__ = version("mxm-mcp")
except PackageNotFoundError:
    try:
        txt = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
        m = re.search(r'version\s*=\s*"([^"]+)"', txt)
        __version__ = m.group(1) if m else "0.3.0"
    except Exception:
        __version__ = "0.3.0"

__author__ = "TypeWolf"
__description__ = "MCP Server for MXM - Create and modify MXM music projects with AI"
