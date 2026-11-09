"""MXM MCP Server - AI-powered music production with MXM."""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

try:
    __version__ = version("mxm-mcp")
except PackageNotFoundError:
    try:
        __version__ = tomllib.loads(
            (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]["version"]
    except Exception:
        __version__ = "0.0.0"

__author__ = "TypeWolf"
__description__ = "MCP Server for MXM - Create and modify MXM music projects with AI"
