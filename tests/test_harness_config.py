"""Tests for the MXM MCP harness configuration helper and Linux installer."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIGURE_SCRIPT = ROOT / "installer" / "configure_harnesses.py"
INSTALL_SCRIPT = ROOT / "installer" / "linux" / "install.sh"
DOWNLOAD_URL = "https://github.com/realrandolph/mxm/releases"


@pytest.fixture(scope="module")
def harness_config() -> ModuleType:
    """Load installer/configure_harnesses.py as a standalone module."""
    spec = importlib.util.spec_from_file_location(
        "mxm_configure_harnesses", CONFIGURE_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def linux_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PATH": str(tmp_path / "empty-bin"),
        "LANG": "C.UTF-8",
    }


def write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_no_harness_detected_in_empty_home(
    harness_config: ModuleType, tmp_path: Path
) -> None:
    detected = harness_config.detect_harnesses(
        system="Linux", env=linux_env(tmp_path)
    )

    assert set(detected) == {"opencode", "codex", "claude"}
    assert all(not info["detected"] for info in detected.values())
    for info in detected.values():
        assert info["configs"]
        assert all(not item["exists"] for item in info["configs"])


def test_detect_harnesses_from_config_files(
    harness_config: ModuleType, tmp_path: Path
) -> None:
    env = linux_env(tmp_path)
    home = Path(env["HOME"])
    write_json(home / ".config" / "opencode" / "opencode.json", {})
    write_json(home / ".codex" / "config.json", {})
    write_json(home / ".claude.json", {})

    detected = harness_config.detect_harnesses(system="Linux", env=env)

    assert detected["opencode"]["detected"] is True
    assert detected["codex"]["detected"] is True
    assert detected["claude"]["detected"] is True
    opencode_path = Path(detected["opencode"]["configs"][0]["path"])
    assert opencode_path == home / ".config" / "opencode" / "opencode.json"
    assert detected["claude"]["configs"][1]["exists"] is True


def test_detect_harness_from_config_directory(
    harness_config: ModuleType, tmp_path: Path
) -> None:
    env = linux_env(tmp_path)
    (Path(env["HOME"]) / ".config" / "codex").mkdir(parents=True)

    detected = harness_config.detect_harnesses(system="Linux", env=env)

    assert detected["codex"]["detected"] is True
    assert detected["codex"]["configs"][1]["exists"] is False
    assert detected["codex"]["configs"][1]["config_directory_exists"] is True


def test_home_level_claude_file_does_not_mark_every_home_as_detected(
    harness_config: ModuleType, tmp_path: Path
) -> None:
    env = linux_env(tmp_path)

    detected = harness_config.detect_harnesses(system="Linux", env=env)

    assert detected["claude"]["detected"] is False


def test_windows_config_paths(harness_config: ModuleType, tmp_path: Path) -> None:
    env = {
        "USERPROFILE": str(tmp_path / "home"),
        "APPDATA": str(tmp_path / "appdata"),
    }
    configs = harness_config.harness_configs(system="Windows", env=env)
    paths = {name: [str(item["path"]) for item in items] for name, items in configs.items()}

    assert paths["opencode"] == [
        str(tmp_path / "home" / ".config" / "opencode" / "opencode.json"),
        str(tmp_path / "appdata" / "opencode" / "opencode.json"),
    ]
    assert paths["codex"] == [
        str(tmp_path / "home" / ".codex" / "config.json"),
        str(tmp_path / "appdata" / "codex" / "config.json"),
    ]
    assert paths["claude"] == [
        str(tmp_path / "appdata" / "Claude" / "claude_desktop_config.json"),
        str(tmp_path / "home" / ".claude.json"),
    ]


# ---------------------------------------------------------------------------
# Safe configuration updates
# ---------------------------------------------------------------------------


def test_update_opencode_preserves_unrelated_settings(
    harness_config: ModuleType, tmp_path: Path
) -> None:
    config_path = write_json(
        tmp_path / "opencode.json",
        {
            "theme": "dark",
            "mcp": {
                "other": {"type": "local", "command": ["other-server"]},
                "mxm": {"enabled": False, "environment": {"MXM_PROJECTS_DIR": "/music"}},
            },
        },
    )
    binary = make_executable(tmp_path / "bin" / "mxm-mcp")

    updated = harness_config.update_opencode_config(config_path, binary)

    assert updated["theme"] == "dark"
    assert updated["mcp"]["other"] == {
        "type": "local",
        "command": ["other-server"],
    }
    assert updated["mcp"]["mxm"]["type"] == "local"
    assert updated["mcp"]["mxm"]["command"] == [str(binary)]
    assert updated["mcp"]["mxm"]["enabled"] is False
    assert updated["mcp"]["mxm"]["environment"]["MXM_PROJECTS_DIR"] == "/music"
    on_disk = json.loads(config_path.read_text(encoding="utf-8"))
    assert on_disk == updated
    text = config_path.read_text(encoding="utf-8")
    assert '\n  "theme": "dark"' in text
    assert text.endswith("\n")


def test_update_opencode_creates_missing_file_and_directory(
    harness_config: ModuleType, tmp_path: Path
) -> None:
    config_path = tmp_path / "deep" / "nested" / "opencode.json"
    binary = make_executable(tmp_path / "bin" / "mxm-mcp")

    updated = harness_config.update_opencode_config(config_path, binary)

    assert config_path.is_file()
    assert updated == {
        "mcp": {"mxm": {"type": "local", "command": [str(binary)]}}
    }


def test_update_codex_preserves_settings_and_sets_args(
    harness_config: ModuleType, tmp_path: Path
) -> None:
    config_path = write_json(
        tmp_path / "codex" / "config.json",
        {
            "model": "gpt-5",
            "mcpServers": {
                "other": {"command": "other", "args": ["--flag"]},
                "mxm": {"command": "old", "args": ["--old"], "disabled": True},
            },
        },
    )
    binary = make_executable(tmp_path / "bin" / "mxm-mcp")

    updated = harness_config.update_codex_config(config_path, binary)

    assert updated["model"] == "gpt-5"
    assert updated["mcpServers"]["other"] == {
        "command": "other",
        "args": ["--flag"],
    }
    assert updated["mcpServers"]["mxm"]["command"] == str(binary)
    assert updated["mcpServers"]["mxm"]["args"] == []
    assert updated["mcpServers"]["mxm"]["disabled"] is True


def test_update_claude_preserves_settings(
    harness_config: ModuleType, tmp_path: Path
) -> None:
    config_path = write_json(
        tmp_path / ".claude.json",
        {"oauthAccount": {"email": "user@example.com"}, "mcpServers": {}},
    )
    binary = make_executable(tmp_path / "bin" / "mxm-mcp")

    updated = harness_config.update_claude_config(config_path, binary)

    assert updated["oauthAccount"]["email"] == "user@example.com"
    assert updated["mcpServers"]["mxm"] == {
        "command": str(binary),
        "args": [],
    }


def test_invalid_json_is_not_overwritten(
    harness_config: ModuleType, tmp_path: Path
) -> None:
    config_path = tmp_path / "opencode.json"
    original = '{"mcp": {"other": {}}, invalid'
    config_path.write_text(original, encoding="utf-8")

    with pytest.raises(harness_config.ConfigError):
        harness_config.update_opencode_config(
            config_path, tmp_path / "bin" / "mxm-mcp"
        )

    assert config_path.read_text(encoding="utf-8") == original


def test_non_object_json_is_rejected(
    harness_config: ModuleType, tmp_path: Path
) -> None:
    config_path = write_json(tmp_path / "opencode.json", ["not", "an", "object"])

    with pytest.raises(harness_config.ConfigError):
        harness_config.update_opencode_config(
            config_path, tmp_path / "bin" / "mxm-mcp"
        )


def test_non_object_mcp_section_is_rejected(
    harness_config: ModuleType, tmp_path: Path
) -> None:
    config_path = write_json(tmp_path / "opencode.json", {"mcp": "wrong"})

    with pytest.raises(harness_config.ConfigError):
        harness_config.update_opencode_config(
            config_path, tmp_path / "bin" / "mxm-mcp"
        )


def test_empty_json_file_is_treated_as_empty_object(
    harness_config: ModuleType, tmp_path: Path
) -> None:
    config_path = tmp_path / "opencode.json"
    config_path.write_text("   \n", encoding="utf-8")

    assert harness_config.read_json_config(config_path) == {}

    harness_config.update_opencode_config(
        config_path, tmp_path / "bin" / "mxm-mcp"
    )
    assert "mcp" in json.loads(config_path.read_text(encoding="utf-8"))


def test_configure_harness_writes_every_known_path(
    harness_config: ModuleType, tmp_path: Path
) -> None:
    env = linux_env(tmp_path)
    binary = make_executable(tmp_path / "bin" / "mxm-mcp")

    results = harness_config.configure_harness(
        "claude", binary, system="Linux", env=env
    )

    assert len(results) == 2
    assert all(result["configured"] for result in results)
    for result in results:
        payload = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
        assert payload["mcpServers"]["mxm"]["command"] == str(binary)


def test_configure_harness_detected_only_targets_only_existing(
    harness_config: ModuleType, tmp_path: Path
) -> None:
    env = linux_env(tmp_path)
    home = Path(env["HOME"])
    write_json(home / ".claude.json", {"mcpServers": {}})
    binary = make_executable(tmp_path / "bin" / "mxm-mcp")

    results = harness_config.configure_harness(
        "claude", binary, system="Linux", env=env, detected_only=True
    )

    assert len(results) == 1
    assert results[0]["path"] == str(home / ".claude.json")
    desktop_config = home / ".config" / "Claude" / "claude_desktop_config.json"
    assert not desktop_config.exists()


# ---------------------------------------------------------------------------
# MXM DAW detection
# ---------------------------------------------------------------------------


def test_mxm_daw_missing_reports_download_url(
    harness_config: ModuleType, tmp_path: Path
) -> None:
    result = harness_config.detect_mxm_daw(
        system="Linux",
        env=linux_env(tmp_path),
        candidates=[str(tmp_path / "missing" / "mxm")],
        which=lambda name: None,
    )

    assert result["installed"] is False
    assert result["found"] is False
    assert result["path"] is None
    assert result["download_url"] == DOWNLOAD_URL


def test_mxm_daw_found_in_known_candidate(
    harness_config: ModuleType, tmp_path: Path
) -> None:
    binary = make_executable(tmp_path / "bin" / "mxm")

    result = harness_config.detect_mxm_daw(
        system="Linux",
        env=linux_env(tmp_path),
        candidates=[str(tmp_path / "missing" / "mxm"), str(binary)],
        which=lambda name: None,
    )

    assert result["installed"] is True
    assert result["path"] == str(binary)
    assert str(binary) in result["candidates"]


def test_mxm_daw_found_through_which(
    harness_config: ModuleType, tmp_path: Path
) -> None:
    binary = make_executable(tmp_path / "bin" / "mxm")

    result = harness_config.detect_mxm_daw(
        system="Linux",
        env=linux_env(tmp_path),
        candidates=[],
        which=lambda name: str(binary),
    )

    assert result["installed"] is True
    assert result["path"] == str(binary)


def test_mxm_executable_environment_override(
    harness_config: ModuleType, tmp_path: Path
) -> None:
    env = linux_env(tmp_path)
    override = make_executable(tmp_path / "custom" / "mxm")
    other = make_executable(tmp_path / "other" / "mxm")
    env["MXM_EXECUTABLE"] = str(override)

    result = harness_config.detect_mxm_daw(
        system="Linux", env=env, candidates=[str(other)], which=lambda name: None
    )

    assert result["path"] == str(override)


def test_linux_daw_candidates(harness_config: ModuleType, tmp_path: Path) -> None:
    env = linux_env(tmp_path)
    candidates = harness_config.mxm_daw_candidates(system="Linux", env=env)
    home = Path(env["HOME"])

    assert candidates == [
        str(home / ".local" / "bin" / "mxm"),
        "/usr/bin/mxm",
        "/usr/local/bin/mxm",
    ]


def test_windows_daw_candidates(harness_config: ModuleType, tmp_path: Path) -> None:
    env = {
        "USERPROFILE": str(tmp_path / "home"),
        "LOCALAPPDATA": str(tmp_path / "local"),
        "ProgramFiles": r"C:\Program Files",
        "ProgramFiles(x86)": r"C:\Program Files (x86)",
    }

    candidates = harness_config.mxm_daw_candidates(system="Windows", env=env)

    assert len(candidates) == 3
    assert candidates[0].startswith(r"C:\Program Files")
    assert candidates[0].endswith("MXM/mxm.exe")
    assert candidates[1].startswith(r"C:\Program Files (x86)")
    assert candidates[2].endswith(
        "local/Programs/MXM/mxm.exe"
    )


# ---------------------------------------------------------------------------
# Command-line interface
# ---------------------------------------------------------------------------


def run_cli(
    args: list[str], env: dict[str, str], cwd: Path = ROOT
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CONFIGURE_SCRIPT), *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def test_cli_detect_prints_json(
    tmp_path: Path,
) -> None:
    env = linux_env(tmp_path)
    write_json(Path(env["HOME"]) / ".config" / "opencode" / "opencode.json", {})

    result = run_cli(["detect"], env)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["harnesses"]["opencode"]["detected"] is True
    assert payload["harnesses"]["codex"]["detected"] is False
    assert "opencode" in payload["detected_harnesses"]
    assert payload["mxm_daw"]["download_url"] == DOWNLOAD_URL
    assert "installed" in payload["mxm_daw"]


def test_cli_configure_opencode(
    tmp_path: Path,
) -> None:
    env = linux_env(tmp_path)
    binary = make_executable(tmp_path / "bin" / "mxm-mcp")

    result = run_cli(["configure", "--binary", str(binary), "--opencode"], env)

    assert result.returncode == 0, result.stderr
    config_path = (
        Path(env["HOME"]) / ".config" / "opencode" / "opencode.json"
    )
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["mcp"]["mxm"] == {
        "type": "local",
        "command": [str(binary)],
    }
    assert "Created OpenCode configuration" in result.stdout


def test_cli_configure_all_harnesses_with_json_output(
    tmp_path: Path,
) -> None:
    env = linux_env(tmp_path)
    binary = make_executable(tmp_path / "bin" / "mxm-mcp")

    result = run_cli(
        [
            "configure",
            "--binary",
            str(binary),
            "--opencode",
            "--codex",
            "--claude",
            "--json",
        ],
        env,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert len(payload["configured"]) == 5
    assert payload["binary"] == str(binary)
    home = Path(env["HOME"])
    assert (home / ".config" / "opencode" / "opencode.json").is_file()
    assert (home / ".codex" / "config.json").is_file()
    assert (home / ".config" / "codex" / "config.json").is_file()
    assert (
        home / ".config" / "Claude" / "claude_desktop_config.json"
    ).is_file()
    assert (home / ".claude.json").is_file()


def test_cli_configure_detected_only_limits_written_files(
    tmp_path: Path,
) -> None:
    env = linux_env(tmp_path)
    binary = make_executable(tmp_path / "bin" / "mxm-mcp")
    home = Path(env["HOME"])
    write_json(home / ".config" / "opencode" / "opencode.json", {})

    result = run_cli(
        [
            "configure",
            "--binary",
            str(binary),
            "--opencode",
            "--claude",
            "--detected-only",
            "--json",
        ],
        env,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    configured_paths = [item["path"] for item in payload["configured"]]
    assert str(home / ".config" / "opencode" / "opencode.json") in configured_paths
    # Claude was not detected, so only fallback primary is configured, not all variants
    assert len([p for p in configured_paths if "claude" in p.lower() or "Claude" in p]) == 1


def test_cli_configure_without_flags_configures_every_harness(
    tmp_path: Path,
) -> None:
    env = linux_env(tmp_path)
    home = Path(env["HOME"])
    write_json(home / ".codex" / "config.json", {"model": "gpt-5"})
    binary = make_executable(tmp_path / "bin" / "mxm-mcp")

    result = run_cli(["configure", "--binary", str(binary)], env)

    assert result.returncode == 0, result.stderr
    payload = json.loads(
        (home / ".codex" / "config.json").read_text(encoding="utf-8")
    )
    assert payload["model"] == "gpt-5"
    assert payload["mcpServers"]["mxm"]["command"] == str(binary)
    assert (home / ".config" / "opencode" / "opencode.json").is_file()
    assert (home / ".claude.json").is_file()
    assert "OpenCode" in result.stdout
    assert "Claude" in result.stdout


def test_cli_configure_requires_binary(
    tmp_path: Path,
) -> None:
    result = run_cli(["configure", "--opencode"], linux_env(tmp_path))

    assert result.returncode == 2
    assert "--binary" in result.stderr


def test_cli_status_is_human_readable(
    tmp_path: Path,
) -> None:
    env = linux_env(tmp_path)
    home = Path(env["HOME"])
    binary = make_executable(tmp_path / "bin" / "mxm-mcp")
    run_cli(["configure", "--binary", str(binary), "--opencode"], env)

    result = run_cli(["status"], env)

    assert result.returncode == 0, result.stderr
    assert "MXM MCP harness status" in result.stdout
    assert "MXM DAW" in result.stdout
    assert "OpenCode" in result.stdout
    assert str(home / ".config" / "opencode" / "opencode.json") in result.stdout
    assert "[configured]" in result.stdout


# ---------------------------------------------------------------------------
# Linux installer
# ---------------------------------------------------------------------------


def test_installer_is_executable() -> None:
    assert INSTALL_SCRIPT.is_file()
    assert os.access(INSTALL_SCRIPT, os.X_OK)
    assert INSTALL_SCRIPT.stat().st_mode & 0o755 == 0o755


def test_installer_has_valid_bash_syntax() -> None:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is not available")
    result = subprocess.run(
        [bash, "-n", str(INSTALL_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_installer_quiet_mode_installs_and_configures(
    tmp_path: Path,
) -> None:
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    shutil.copy2(INSTALL_SCRIPT, package_dir / "install.sh")
    shutil.copy2(CONFIGURE_SCRIPT, package_dir / "configure_harnesses.py")
    make_executable(package_dir / "mxm-mcp")

    home = tmp_path / "home"
    home.mkdir()
    (home / ".bashrc").write_text("# user bashrc\n", encoding="utf-8")
    write_json(home / ".config" / "opencode" / "opencode.json", {"theme": "dark"})
    write_json(home / ".codex" / "config.json", {"model": "gpt-5"})

    env = {
        "HOME": str(home),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "CI": "",
    }

    result = subprocess.run(
        ["bash", str(package_dir / "install.sh"), "--quiet"],
        env=env,
        cwd=package_dir,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    target = home / ".local" / "bin" / "mxm-mcp"
    assert target.is_file()
    assert os.access(target, os.X_OK)
    assert (home / ".local" / "bin" / "mxm-mcp").read_bytes() == (
        package_dir / "mxm-mcp"
    ).read_bytes()

    opencode = json.loads(
        (home / ".config" / "opencode" / "opencode.json").read_text(
            encoding="utf-8"
        )
    )
    assert opencode["theme"] == "dark"
    assert opencode["mcp"]["mxm"]["command"] == [str(target)]

    codex = json.loads(
        (home / ".codex" / "config.json").read_text(encoding="utf-8")
    )
    assert codex["model"] == "gpt-5"
    assert codex["mcpServers"]["mxm"]["command"] == str(target)

    bashrc = (home / ".bashrc").read_text(encoding="utf-8")
    assert "# MXM MCP" in bashrc
    assert "export PATH=" in bashrc
    assert "MXM MCP installed successfully." in result.stdout
