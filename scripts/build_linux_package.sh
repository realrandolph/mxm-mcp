#!/usr/bin/env bash
# Build the standalone MXM MCP Linux package.
#
# Output:
#   dist/mxm-mcp-linux-x86_64.tar.gz
#
# The archive contains the PyInstaller binary, the Linux installer, the
# harness configuration helper, and a README. install.sh and mxm-mcp are
# stored with mode 0755 so the archive works immediately after extraction.

set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist"
BUILD_DIR="${ROOT_DIR}/build/mxm-mcp-package"
PACKAGE_NAME="mxm-mcp-linux-x86_64"
ARCHIVE="${DIST_DIR}/${PACKAGE_NAME}.tar.gz"
STAGE_DIR="${DIST_DIR}/${PACKAGE_NAME}"
ENTRY_SCRIPT="${BUILD_DIR}/mxm_mcp_entry.py"
PYINSTALLER_DIST="${BUILD_DIR}/pyinstaller-dist"
PYINSTALLER_WORK="${BUILD_DIR}/pyinstaller-work"
PYINSTALLER_SPEC="${BUILD_DIR}/pyinstaller-spec"

fail() {
    echo "Error: $*" >&2
    exit 1
}

command -v tar >/dev/null 2>&1 || fail "tar is required"
command -v gzip >/dev/null 2>&1 || fail "gzip is required"

MACHINE="$(uname -m)"
if [ "${MACHINE}" != "x86_64" ]; then
    fail "This package targets x86_64; current machine is ${MACHINE}"
fi

for required_file in \
    "${ROOT_DIR}/installer/linux/install.sh" \
    "${ROOT_DIR}/installer/configure_harnesses.py" \
    "${ROOT_DIR}/installer/README.md" \
    "${ROOT_DIR}/pyproject.toml"; do
    [ -f "${required_file}" ] || fail "Missing required file: ${required_file}"
done

mkdir -p "${DIST_DIR}" "${BUILD_DIR}"
rm -rf "${PYINSTALLER_DIST}" "${PYINSTALLER_WORK}" "${PYINSTALLER_SPEC}" "${STAGE_DIR}"

cat >"${ENTRY_SCRIPT}" <<'PY'
"""PyInstaller entry point for the MXM MCP server."""

from mxm_mcp.server import main

if __name__ == "__main__":
    main()
PY

echo "Building mxm-mcp with PyInstaller..."

if command -v uv >/dev/null 2>&1; then
    PYINSTALLER_CMD=(
        uv run --project "${ROOT_DIR}" --with pyinstaller
        pyinstaller
    )
elif command -v pyinstaller >/dev/null 2>&1; then
    PYINSTALLER_CMD=(pyinstaller)
elif "${PYTHON:-python3}" -c "import PyInstaller" >/dev/null 2>&1; then
    PYINSTALLER_CMD=("${PYTHON:-python3}" -m PyInstaller)
else
    fail "PyInstaller is not available. Install it with 'uv tool install pyinstaller' or 'pip install pyinstaller'."
fi

"${PYINSTALLER_CMD[@]}" \
    --noconfirm \
    --clean \
    --onefile \
    --name mxm-mcp \
    --distpath "${PYINSTALLER_DIST}" \
    --workpath "${PYINSTALLER_WORK}" \
    --specpath "${PYINSTALLER_SPEC}" \
    --paths "${ROOT_DIR}/src" \
    --collect-all mxm_mcp \
    --collect-submodules mcp.server \
    --collect-submodules mcp.shared \
    --collect-submodules mcp.types \
    --collect-submodules mcp.os \
    --collect-submodules mcp.client \
    --copy-metadata mcp \
    --copy-metadata mcp-types \
    "${ENTRY_SCRIPT}"

BUILT_BINARY="${PYINSTALLER_DIST}/mxm-mcp"
[ -f "${BUILT_BINARY}" ] || fail "PyInstaller did not produce ${BUILT_BINARY}"
chmod 0755 "${BUILT_BINARY}"

echo "Staging package..."

mkdir -p "${STAGE_DIR}"
install -m 0755 "${BUILT_BINARY}" "${STAGE_DIR}/mxm-mcp"
install -m 0755 "${ROOT_DIR}/installer/linux/install.sh" "${STAGE_DIR}/install.sh"
install -m 0644 "${ROOT_DIR}/installer/configure_harnesses.py" "${STAGE_DIR}/configure_harnesses.py"
install -m 0644 "${ROOT_DIR}/installer/README.md" "${STAGE_DIR}/README.md"

# Belt and braces: the extraction must be executable before tar reads it.
chmod 0755 "${STAGE_DIR}/install.sh" "${STAGE_DIR}/mxm-mcp"

echo "Creating ${ARCHIVE}..."
rm -f "${ARCHIVE}"
tar -czf "${ARCHIVE}" -C "${DIST_DIR}" "${PACKAGE_NAME}"

# Verify archive contents and exact permission bits.
"${PYTHON:-python3}" - "${ARCHIVE}" "${PACKAGE_NAME}" <<'PY'
import sys
import tarfile

archive, package = sys.argv[1], sys.argv[2]
required = {
    f"{package}/install.sh": 0o755,
    f"{package}/mxm-mcp": 0o755,
    f"{package}/configure_harnesses.py": 0o644,
    f"{package}/README.md": 0o644,
}
with tarfile.open(archive, "r:gz") as handle:
    members = {member.name: member for member in handle.getmembers()}

missing = [name for name in required if name not in members]
if missing:
    raise SystemExit(f"Archive is missing: {', '.join(missing)}")

for name, expected in required.items():
    actual = members[name].mode & 0o777
    if actual != expected:
        raise SystemExit(
            f"{name} has mode {actual:o}, expected {expected:o}"
        )

print("Verified archive contents and permission bits.")
PY

echo
echo "Package ready: ${ARCHIVE}"
echo "Extract and run:"
echo "  tar -xzf ${ARCHIVE}"
echo "  ./${PACKAGE_NAME}/install.sh"
