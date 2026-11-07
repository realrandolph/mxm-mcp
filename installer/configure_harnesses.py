#!/usr/bin/env python3
"""Detect and configure AI harnesses for the MXM MCP server.

This module uses only the Python standard library so it can run from a
downloaded installer archive without installing anything first.

Commands
--------
``detect``
    Print a JSON summary of detected harnesses and MXM DAW availability.
``configure --binary PATH [--opencode] [--codex] [--claude]``
    Safely register the MXM MCP server with the selected harnesses.
``status``
    Print a human-readable installation and configuration report.

Configuration files are merged, never replaced wholesale. Unrelated settings
are preserved, missing parent directories are created, and JSON is written
with two-space indentation.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

MXM_DOWNLOAD_URL = "https://github.com/realrandolph/mxm/releases"
SERVER_NAME = "mxm"
HARNESSES: tuple[str, ...] = ("opencode", "codex", "claude")

HARNESS_DESCRIPTIONS = {
    "opencode": "OpenCode (CLI, TUI and desktop)",
    "codex": "OpenAI Codex CLI",
    "claude": "Claude Desktop and Claude Code",
}


class ConfigError(RuntimeError):
    """Raised when an existing configuration file cannot be updated safely."""


# ---------------------------------------------------------------------------
# Environment and path helpers
# ---------------------------------------------------------------------------


def _environment(env: Mapping[str, str] | None) -> dict[str, str]:
    return dict(os.environ if env is None else env)


def _home(env: Mapping[str, str] | None = None) -> Path:
    """Return the user's home directory for the supplied environment."""
    values = _environment(env)
    candidate = values.get("USERPROFILE") or values.get("HOME")
    return Path(candidate).expanduser() if candidate else Path.home()


def _appdata(env: Mapping[str, str], home: Path) -> Path:
    value = env.get("APPDATA")
    if value:
        return Path(value).expanduser()
    return home / "AppData" / "Roaming"


def _is_windows(system: str | None) -> bool:
    return (system or platform.system()).lower().startswith("win")


def harness_configs(
    *,
    system: str | None = None,
    env: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return every known configuration file for each supported harness.

    Each entry contains ``kind`` (the product that owns the file), ``path``,
    and ``detect_dir``. ``detect_dir`` is a directory whose presence is useful
    evidence that the harness is installed. It is ``None`` for files that live
    directly in the home directory, where the parent always exists.
    """
    values = _environment(env)
    home_path = Path(home).expanduser() if home is not None else _home(values)
    windows = _is_windows(system)

    def entry(kind: str, path: Path) -> dict[str, Any]:
        return {
            "kind": kind,
            "path": path,
            "detect_dir": path.parent if path.parent != home_path else None,
        }

    if windows:
        appdata = _appdata(values, home_path)
        configs: dict[str, list[dict[str, Any]]] = {
            "opencode": [
                entry("OpenCode", home_path / ".config" / "opencode" / "opencode.json"),
                entry("OpenCode", appdata / "opencode" / "opencode.json"),
            ],
            "codex": [
                entry("Codex", home_path / ".codex" / "config.json"),
                entry("Codex", appdata / "codex" / "config.json"),
            ],
            "claude": [
                entry(
                    "Claude Desktop",
                    appdata / "Claude" / "claude_desktop_config.json",
                ),
                entry("Claude Code", home_path / ".claude.json"),
            ],
        }
    else:
        configs = {
            "opencode": [
                entry("OpenCode", home_path / ".config" / "opencode" / "opencode.json"),
            ],
            "codex": [
                entry("Codex", home_path / ".codex" / "config.json"),
                entry("Codex", home_path / ".config" / "codex" / "config.json"),
            ],
            "claude": [
                entry(
                    "Claude Desktop",
                    home_path / ".config" / "Claude" / "claude_desktop_config.json",
                ),
                entry("Claude Code", home_path / ".claude.json"),
            ],
        }
    return configs


def get_config_paths(
    harness: str,
    *,
    system: str | None = None,
    env: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
) -> list[Path]:
    """Return the configuration file paths for one harness."""
    if harness not in HARNESSES:
        raise ValueError(f"Unknown harness: {harness}")
    return [
        Path(item["path"])
        for item in harness_configs(system=system, env=env, home=home)[harness]
    ]


def detect_harnesses(
    *,
    system: str | None = None,
    env: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Detect harnesses by looking for their configuration files/directories."""
    detected: dict[str, dict[str, Any]] = {}
    for harness, entries in harness_configs(
        system=system, env=env, home=home
    ).items():
        configs: list[dict[str, Any]] = []
        for item in entries:
            path = Path(item["path"])
            exists = path.is_file()
            detect_dir = item.get("detect_dir")
            directory_exists = bool(detect_dir and Path(detect_dir).is_dir())
            configs.append(
                {
                    "kind": item["kind"],
                    "path": str(path),
                    "exists": exists,
                    "config_directory_exists": directory_exists,
                }
            )
        detected[harness] = {
            "description": HARNESS_DESCRIPTIONS[harness],
            "detected": any(
                item["exists"] or item["config_directory_exists"]
                for item in configs
            ),
            "configs": configs,
        }
    return detected


# ---------------------------------------------------------------------------
# MXM DAW detection
# ---------------------------------------------------------------------------


def mxm_daw_candidates(
    *,
    system: str | None = None,
    env: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
) -> list[str]:
    """Return the platform-specific MXM DAW binary locations to check."""
    values = _environment(env)
    home_path = Path(home).expanduser() if home is not None else _home(values)
    if _is_windows(system):
        local_appdata = values.get("LOCALAPPDATA")
        local_root = (
            Path(local_appdata).expanduser()
            if local_appdata
            else home_path / "AppData" / "Local"
        )
        program_files = Path(values.get("ProgramFiles") or r"C:\Program Files")
        program_files_x86 = Path(
            values.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
        )
        return [
            str(program_files / "MXM" / "mxm.exe"),
            str(program_files_x86 / "MXM" / "mxm.exe"),
            str(local_root / "Programs" / "MXM" / "mxm.exe"),
        ]
    return [
        str(home_path / ".local" / "bin" / "mxm"),
        "/usr/bin/mxm",
        "/usr/local/bin/mxm",
    ]


def _is_executable(path: str | os.PathLike[str]) -> bool:
    candidate = Path(path).expanduser()
    try:
        return candidate.is_file() and os.access(candidate, os.X_OK)
    except OSError:
        return False


def detect_mxm_daw(
    *,
    system: str | None = None,
    env: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
    candidates: Sequence[str] | None = None,
    which: Any = None,
    checker: Any = None,
) -> dict[str, Any]:
    """Locate the MXM DAW binary.

    ``candidates``, ``which`` and ``checker`` are injection points used by the
    test suite; normal callers only need the platform/environment arguments.
    """
    values = _environment(env)
    checks: list[str] = []

    override = values.get("MXM_EXECUTABLE")
    if override:
        checks.append(str(Path(override).expanduser()))

    if candidates is None:
        checks.extend(mxm_daw_candidates(system=system, env=values, home=home))
    else:
        checks.extend(str(item) for item in candidates)

    which_impl = shutil.which if which is None else which
    found_which = which_impl("mxm")
    if found_which and found_which not in checks:
        checks.append(found_which)

    exists_impl = _is_executable if checker is None else checker
    found_path: str | None = None
    for candidate in checks:
        try:
            if exists_impl(candidate):
                found_path = str(Path(candidate).expanduser())
                break
        except OSError:
            continue

    return {
        "installed": found_path is not None,
        "found": found_path is not None,
        "path": found_path,
        "download_url": MXM_DOWNLOAD_URL,
        "candidates": checks,
    }


# ---------------------------------------------------------------------------
# Safe JSON configuration handling
# ---------------------------------------------------------------------------


def read_json_config(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read a JSON object, returning ``{}`` when the file does not exist."""
    config_path = Path(path)
    if not config_path.exists():
        return {}
    if not config_path.is_file():
        raise ConfigError(f"{config_path} is not a regular file")
    try:
        text = config_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"Cannot read {config_path}: {exc}") from exc
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{config_path} contains invalid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path} must contain a JSON object")
    return data


def write_json_config(
    path: str | os.PathLike[str], data: Mapping[str, Any]
) -> Path:
    """Atomically write a JSON object with two-space indentation."""
    config_path = Path(path)
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(
            f"Cannot create configuration directory {config_path.parent}: {exc}"
        ) from exc

    payload = json.dumps(dict(data), indent=2, ensure_ascii=False) + "\n"
    existing_mode: int | None = None
    try:
        existing_mode = stat.S_IMODE(config_path.stat().st_mode)
    except OSError:
        existing_mode = None

    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{config_path.name}.", dir=str(config_path.parent)
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
        os.chmod(temporary_name, existing_mode if existing_mode else 0o600)
        os.replace(temporary_name, config_path)
    except OSError as exc:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise ConfigError(f"Cannot write {config_path}: {exc}") from exc
    return config_path


def _object_child(
    parent: dict[str, Any], key: str, *, path_hint: str
) -> dict[str, Any]:
    value = parent.get(key)
    if value is None:
        value = {}
        parent[key] = value
    if not isinstance(value, dict):
        raise ConfigError(f'{path_hint}: "{key}" must be a JSON object')
    return value


def merge_opencode(config: dict[str, Any], binary_path: str) -> dict[str, Any]:
    """Merge the local MXM MCP server into an OpenCode configuration object."""
    mcp = _object_child(config, "mcp", path_hint="OpenCode configuration")
    previous = mcp.get(SERVER_NAME)
    entry = dict(previous) if isinstance(previous, dict) else {}
    entry["type"] = "local"
    entry["command"] = [binary_path]
    mcp[SERVER_NAME] = entry
    return config


def merge_mcp_servers(config: dict[str, Any], binary_path: str) -> dict[str, Any]:
    """Merge the MXM MCP server into a Codex or Claude configuration object."""
    servers = _object_child(
        config, "mcpServers", path_hint="Codex/Claude configuration"
    )
    previous = servers.get(SERVER_NAME)
    entry = dict(previous) if isinstance(previous, dict) else {}
    entry["command"] = binary_path
    entry["args"] = []
    servers[SERVER_NAME] = entry
    return config


def normalize_binary_path(binary_path: str | os.PathLike[str]) -> str:
    """Expand and absolutize the installed MCP server binary path."""
    return os.path.abspath(os.path.expanduser(str(binary_path)))


def update_opencode_config(
    path: str | os.PathLike[str], binary_path: str
) -> dict[str, Any]:
    """Safely add or update the MXM MCP server in an OpenCode config file."""
    config = read_json_config(path)
    merge_opencode(config, normalize_binary_path(binary_path))
    write_json_config(path, config)
    return config


def update_codex_config(
    path: str | os.PathLike[str], binary_path: str
) -> dict[str, Any]:
    """Safely add or update the MXM MCP server in a Codex config file."""
    config = read_json_config(path)
    merge_mcp_servers(config, normalize_binary_path(binary_path))
    write_json_config(path, config)
    return config


def update_claude_config(
    path: str | os.PathLike[str], binary_path: str
) -> dict[str, Any]:
    """Safely add or update the MXM MCP server in a Claude config file."""
    config = read_json_config(path)
    merge_mcp_servers(config, normalize_binary_path(binary_path))
    write_json_config(path, config)
    return config


def server_is_configured(config: Mapping[str, Any], harness: str) -> bool:
    """Return whether a configuration object already registers MXM MCP."""
    if harness == "opencode":
        section = config.get("mcp")
    else:
        section = config.get("mcpServers")
    if not isinstance(section, dict):
        return False
    entry = section.get(SERVER_NAME)
    if not isinstance(entry, dict):
        return False
    if harness == "opencode":
        command = entry.get("command")
        return isinstance(command, list) and bool(command)
    return isinstance(entry.get("command"), str) and bool(entry.get("command"))


def configure_harness(
    harness: str,
    binary_path: str,
    *,
    system: str | None = None,
    env: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    """Configure every known config file for one harness.

    Returns a list of result records, one per written file.
    """
    if harness not in HARNESSES:
        raise ValueError(f"Unknown harness: {harness}")

    binary = normalize_binary_path(binary_path)
    results: list[dict[str, Any]] = []
    for item in harness_configs(system=system, env=env, home=home)[harness]:
        path = Path(item["path"])
        existed = path.is_file()
        if harness == "opencode":
            update_opencode_config(path, binary)
        elif harness == "codex":
            update_codex_config(path, binary)
        else:
            update_claude_config(path, binary)
        results.append(
            {
                "harness": harness,
                "kind": item["kind"],
                "path": str(path),
                "created": not existed,
                "configured": True,
            }
        )
    return results


# ---------------------------------------------------------------------------
# Reporting and CLI
# ---------------------------------------------------------------------------


def detection_summary(
    *,
    system: str | None = None,
    env: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Build the JSON-serializable summary used by ``detect``."""
    harnesses = detect_harnesses(system=system, env=env, home=home)
    daw = detect_mxm_daw(system=system, env=env, home=home)
    return {
        "mxm_daw": daw,
        "mxm_installed": daw["installed"],
        "mxm_daw_path": daw["path"],
        "harnesses": harnesses,
        "detected_harnesses": [
            name for name, info in harnesses.items() if info["detected"]
        ],
    }


def _config_state(harness: str, path: Path) -> str:
    if not path.exists():
        return "missing"
    try:
        config = read_json_config(path)
    except ConfigError as exc:
        return f"invalid: {exc}"
    if server_is_configured(config, harness):
        return "configured"
    return "present, MXM MCP not configured"


def status_text(
    *,
    system: str | None = None,
    env: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
) -> str:
    """Build the human-readable report used by ``status``."""
    summary = detection_summary(system=system, env=env, home=home)
    daw = summary["mxm_daw"]
    lines = ["MXM MCP harness status", ""]
    if daw["installed"]:
        lines.append(f"MXM DAW: installed at {daw['path']}")
    else:
        lines.append(f"MXM DAW: not found - install from {MXM_DOWNLOAD_URL}")
    lines.append("")

    for harness, info in summary["harnesses"].items():
        state = "detected" if info["detected"] else "not detected"
        lines.append(f"{info['description']}: {state}")
        for item in info["configs"]:
            path = Path(item["path"])
            lines.append(
                f"  {item['kind']}: {path} [{_config_state(harness, path)}]"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _selected_harnesses(args: argparse.Namespace) -> list[str]:
    selected = [name for name in HARNESSES if getattr(args, name, False)]
    if args.all or not selected:
        # With no explicit flags the installer-independent default is every
        # supported harness. The Linux installer passes explicit flags for the
        # harnesses the user selected in its checklist.
        selected = list(HARNESSES)
    # Keep a stable, predictable order and remove duplicates.
    return [name for name in HARNESSES if name in selected]


def _command_detect(args: argparse.Namespace) -> int:
    summary = detection_summary()
    json.dump(summary, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def _command_status(args: argparse.Namespace) -> int:
    sys.stdout.write(status_text())
    return 0


def _command_configure(args: argparse.Namespace) -> int:
    selected = _selected_harnesses(args)
    if not selected:
        if args.json:
            json.dump({"configured": []}, sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            print("No harnesses selected; nothing to configure.")
        return 0

    binary = normalize_binary_path(args.binary)
    if not _is_executable(binary):
        print(
            f"Warning: {binary} is not an executable file. "
            "The configuration will be written anyway.",
            file=sys.stderr,
        )

    results: list[dict[str, Any]] = []
    failures = 0
    for harness in selected:
        try:
            results.extend(configure_harness(harness, binary))
        except (ConfigError, OSError, ValueError) as exc:
            failures += 1
            print(f"Could not configure {harness}: {exc}", file=sys.stderr)

    if args.json:
        json.dump({"binary": binary, "configured": results}, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        for result in results:
            action = "Created" if result["created"] else "Updated"
            print(f"{action} {result['kind']} configuration: {result['path']}")
        print(f"MXM MCP binary: {binary}")
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="configure_harnesses.py",
        description="Detect and configure AI harnesses for MXM MCP.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    detect_parser = subparsers.add_parser(
        "detect", help="print detected harnesses and MXM DAW status as JSON"
    )
    detect_parser.set_defaults(func=_command_detect)

    configure_parser = subparsers.add_parser(
        "configure", help="register MXM MCP with one or more harnesses"
    )
    configure_parser.add_argument(
        "--binary",
        required=True,
        help="path to the installed mxm-mcp executable",
    )
    configure_parser.add_argument(
        "--opencode", action="store_true", help="configure OpenCode"
    )
    configure_parser.add_argument(
        "--codex", action="store_true", help="configure OpenAI Codex"
    )
    configure_parser.add_argument(
        "--claude", action="store_true", help="configure Claude Desktop and Code"
    )
    configure_parser.add_argument(
        "--all",
        action="store_true",
        help="configure every supported harness",
    )
    configure_parser.add_argument(
        "--json", action="store_true", help="print a JSON summary"
    )
    configure_parser.set_defaults(func=_command_configure)

    status_parser = subparsers.add_parser(
        "status", help="print a human-readable status report"
    )
    status_parser.set_defaults(func=_command_status)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
