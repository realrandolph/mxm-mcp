"""Static integrity checks for the MXM MCP packaging inputs."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_pyinstaller_spec_has_server_entry_and_required_package_data() -> None:
    spec = read_text("packaging/mxm-mcp.spec")
    entry = read_text("packaging/mxm_mcp_entry.py")

    assert "from mxm_mcp.server import main" in entry
    assert "carla_surge_state.b64" in spec
    assert (ROOT / "src/mxm_mcp/carla_surge_state.b64").is_file()
    for package in ("mcp", "rdflib", "pydantic", "anyio", "starlette"):
        assert f'"{package}"' in spec
    assert 'name="mxm-mcp"' in spec


def test_windows_installer_has_per_user_setup_and_opt_in_harness_tasks() -> None:
    installer = read_text("installer/windows/mxm-mcp-setup.iss")

    assert "DefaultDirName={localappdata}\\Programs\\mxm-mcp" in installer
    assert "PrivilegesRequired=lowest" in installer
    assert "OutputBaseFilename=mxm-mcp-setup-windows-x64" in installer
    for task, label in (
        ("opencode", "Configure OpenCode harness"),
        ("codex", "Configure Codex harness"),
        ("claude", "Configure Claude Desktop harness"),
    ):
        assert f'Name: "{task}"; Description: "{label}"; Flags: checkedonce' in installer
        assert f"WizardIsTaskSelected('{task}')" in installer
        assert f"--{task}" in installer
    assert "--target-bin" in installer
    assert "C:\\Program Files\\MXM\\mxm.exe" in installer
    assert "C:\\Program Files (x86)\\MXM\\mxm.exe" in installer
    assert "{localappdata}\\Programs\\MXM\\mxm.exe" in installer
    assert "https://github.com/realrandolph/mxm/releases" in installer
    assert "PathContainsEntry" in installer
    assert "RemoveApplicationFromUserPath" in installer


def test_windows_build_script_packages_with_spec_and_inno_setup() -> None:
    build = read_text("scripts/build_windows_package.py")
    adapter = read_text("installer/windows/configure_harnesses_entry.py")

    assert '"PyInstaller"' in build
    assert '"iscc"' in build
    assert "Inno Setup 6" in build
    assert "Program Files (x86)" in build
    assert "mxm-mcp-setup-windows-x64.exe" in build
    assert "configure_harnesses" in build
    assert "configure_harnesses_entry.py" in build
    assert 'parser.add_argument("--target-bin", required=True)' in adapter
    assert '"configure", "--binary", options.target_bin' in adapter


def test_package_workflow_covers_requested_triggers_and_platforms() -> None:
    workflow = read_text(".github/workflows/package.yml")

    assert "branches:" in workflow
    assert "master" in workflow
    assert '"v*"' in workflow
    assert "workflow_dispatch" in workflow
    assert "contents: write" in workflow
    assert "runs-on: ubuntu-22.04" in workflow
    assert "runs-on: windows-latest" in workflow
    assert "scripts/build_linux_package.sh" in workflow
    assert "scripts/build_windows_package.py" in workflow
    assert "0o755" in workflow
    assert "mxm-mcp-linux-x86_64.tar.gz" in workflow
    assert "mxm-mcp-setup-windows-x64.exe" in workflow
    assert "publish-release" in workflow


def test_general_ci_discovers_harness_and_packaging_tests() -> None:
    workflow = read_text(".github/workflows/ci.yml")

    assert "pytest -v -m \"not integration\" tests" in workflow
    assert (ROOT / "tests/test_harness_config.py").is_file()
