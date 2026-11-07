#!/usr/bin/env bash
# MXM MCP installer for Linux desktops (Linux Mint/Nemo, Ubuntu/Nautilus,
# Debian and similar). Runs with a Zenity wizard when a graphical session is
# available and falls back to terminal prompts otherwise.
#
# Usage:
#   ./install.sh             Interactive install
#   ./install.sh --quiet     Non-interactive install (all detected harnesses)
#
# Environment:
#   CI=1                     Same as --quiet
#   MXM_MCP_INSTALL_DIR=DIR  Install somewhere other than ~/.local/bin

set -euo pipefail

MXM_DOWNLOAD_URL="https://github.com/realrandolph/mxm/releases"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MXM_MCP_BINARY="${SCRIPT_DIR}/mxm-mcp"
CONFIG_HELPER="${SCRIPT_DIR}/configure_harnesses.py"
INSTALL_DIR="${MXM_MCP_INSTALL_DIR:-${HOME}/.local/bin}"
TARGET="${INSTALL_DIR}/mxm-mcp"
PYTHON="${PYTHON:-python3}"
PATH_MARKER="# MXM MCP: add the local binary directory to PATH"

QUIET=0
USE_GUI=0
SELECTED=()
DAW_PATH=""

usage() {
    cat <<EOF
MXM MCP installer for Linux

Usage: $(basename "$0") [--quiet] [--help]

  --quiet, -q   Install without prompts and configure every detected harness
  --help, -h    Show this help

Install directory: ${INSTALL_DIR} (override with MXM_MCP_INSTALL_DIR)
EOF
}

for argument in "$@"; do
    case "${argument}" in
        --quiet|-q) QUIET=1 ;;
        --help|-h) usage; exit 0 ;;
        *)
            echo "Unknown option: ${argument}" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ -n "${CI:-}" ]; then
    QUIET=1
fi

if [ "${QUIET}" -eq 0 ] && { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; } \
    && command -v zenity >/dev/null 2>&1; then
    USE_GUI=1
fi

zenity_info() {
    zenity --info --title="MXM MCP Installer" --width=560 --text="$1" 2>/dev/null || true
}

zenity_error() {
    zenity --error --title="MXM MCP Installer" --width=560 --text="$1" 2>/dev/null || true
}

die() {
    if [ "${USE_GUI}" -eq 1 ]; then
        zenity_error "$1"
    fi
    echo "Error: $1" >&2
    exit 1
}

prompt_yes() {
    local prompt="$1"
    local answer=""
    if [ ! -t 0 ]; then
        return 0
    fi
    read -r -p "${prompt}" answer || answer=""
    case "${answer}" in
        n|N|no|NO|No) return 1 ;;
        *) return 0 ;;
    esac
}

open_download_page() {
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "${MXM_DOWNLOAD_URL}" >/dev/null 2>&1 && return 0
    fi
    if command -v gio >/dev/null 2>&1; then
        gio open "${MXM_DOWNLOAD_URL}" >/dev/null 2>&1 && return 0
    fi
    if command -v "${PYTHON}" >/dev/null 2>&1; then
        timeout 15 "${PYTHON}" -m webbrowser "${MXM_DOWNLOAD_URL}" >/dev/null 2>&1 \
            && return 0
    fi
    return 1
}

ensure_path_entry() {
    local rc_file="$1"
    if [ -f "${rc_file}" ] && grep -Fq "${PATH_MARKER}" "${rc_file}"; then
        return 0
    fi
    mkdir -p "$(dirname -- "${rc_file}")"
    {
        printf '\n%s\n' "${PATH_MARKER}"
        printf 'case ":$PATH:" in *":%s:"*) ;; *) export PATH="%s:$PATH" ;; esac\n' \
            "${INSTALL_DIR}" "${INSTALL_DIR}"
    } >>"${rc_file}"
}

describe_harnesses() {
    DETECT_JSON="$1" "${PYTHON}" - <<'PY'
import json
import os

data = json.loads(os.environ["DETECT_JSON"])
descriptions = {
    "opencode": "OpenCode CLI, TUI and desktop",
    "codex": "OpenAI Codex CLI",
    "claude": "Claude Desktop and Claude Code",
}
for name, info in data.get("harnesses", {}).items():
    if info.get("detected"):
        print(f"{name}\t{descriptions.get(name, name)}")
PY
}

# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------

if ! command -v "${PYTHON}" >/dev/null 2>&1; then
    die "Python 3 is required. Install python3 and run this installer again."
fi

if [ ! -f "${CONFIG_HELPER}" ]; then
    die "configure_harnesses.py was not found next to install.sh. Extract the full archive and try again."
fi

if [ ! -f "${MXM_MCP_BINARY}" ]; then
    die "The mxm-mcp binary was not found next to install.sh. Extract the full archive and try again."
fi

if [ "${USE_GUI}" -eq 1 ]; then
    zenity_info "Welcome to MXM MCP Installer

This wizard installs the MXM MCP server and registers it with the AI harnesses on this computer."
else
    echo "Welcome to MXM MCP Installer"
    echo
fi

# ---------------------------------------------------------------------------
# MXM DAW check
# ---------------------------------------------------------------------------

if ! DETECT_OUTPUT="$("${PYTHON}" "${CONFIG_HELPER}" detect 2>/dev/null)"; then
    die "Could not inspect the installed harnesses. Run '${PYTHON} ${CONFIG_HELPER} detect' for details."
fi

DAW_PATH="$(printf '%s' "${DETECT_OUTPUT}" | "${PYTHON}" -c '
import json
import sys

data = json.load(sys.stdin)
daw = data.get("mxm_daw") or {}
print(daw.get("path") or "")
')"

if [ -z "${DAW_PATH}" ]; then
    if [ "${USE_GUI}" -eq 1 ]; then
        if zenity --question \
            --title="MXM MCP Installer" \
            --width=620 \
            --text="MXM DAW was not found on this computer.

MXM MCP can still be installed, but rendering, preset loading and MXM version checks require the MXM DAW.

Install MXM DAW from:
${MXM_DOWNLOAD_URL}

Open the download page now?" \
            --ok-label="Open download page" \
            --cancel-label="Continue without MXM"; then
            if ! open_download_page; then
                zenity_info "Open this address in a browser to install MXM DAW:

${MXM_DOWNLOAD_URL}"
            fi
        fi
    else
        echo "MXM DAW was not found on this computer."
        echo "MXM MCP can still be installed, but rendering, preset loading and"
        echo "MXM version checks require the MXM DAW."
        echo
        echo "Install MXM DAW from: ${MXM_DOWNLOAD_URL}"
        echo
        if [ "${QUIET}" -eq 1 ]; then
            echo "Continuing without MXM DAW."
        elif prompt_yes "Open the download page now? [Y/n] "; then
            if ! open_download_page; then
                echo "Open this address in a browser: ${MXM_DOWNLOAD_URL}"
            fi
        fi
    fi
    echo
fi

# ---------------------------------------------------------------------------
# Harness selection
# ---------------------------------------------------------------------------

HARNESS_ROWS="$(describe_harnesses "${DETECT_OUTPUT}")"

if [ -n "${HARNESS_ROWS}" ]; then
    if [ "${USE_GUI}" -eq 1 ]; then
        checklist=(
            --list
            --checklist
            --title="MXM MCP Installer"
            --width=720
            --height=360
            --text="Select the harnesses that should use MXM MCP.

Every detected harness is selected by default. Uncheck any harness you do not want to configure."
            --column="Select"
            --column="Harness"
            --column="Description"
            --print-column=2
            --separator=","
        )
        while IFS=$'\t' read -r harness description; do
            [ -z "${harness}" ] && continue
            checklist+=("TRUE" "${harness}" "${description}")
        done <<<"${HARNESS_ROWS}"

        if ! CHECKLIST_RESULT="$(zenity "${checklist[@]}" 2>/dev/null)"; then
            zenity_info "Installation cancelled. Nothing was changed."
            exit 0
        fi
        if [ -n "${CHECKLIST_RESULT}" ]; then
            IFS=',' read -r -a SELECTED <<<"${CHECKLIST_RESULT}"
        fi
    else
        echo "Detected harnesses:"
        while IFS=$'\t' read -r harness description; do
            [ -z "${harness}" ] && continue
            if [ "${QUIET}" -eq 1 ]; then
                SELECTED+=("${harness}")
                echo "  [x] ${description} (${harness})"
            elif prompt_yes "  Configure ${description} (${harness})? [Y/n] "; then
                SELECTED+=("${harness}")
            fi
        done <<<"${HARNESS_ROWS}"
        echo
    fi
else
    if [ "${USE_GUI}" -eq 1 ]; then
        zenity_info "No supported AI harness configuration was detected.

MXM MCP will be installed, but no harness will be configured. Install OpenCode, Codex or Claude, then run:
${PYTHON} ${CONFIG_HELPER} configure --binary ${TARGET}

Manual configuration details are in README.md."
    else
        echo "No supported AI harness configuration was detected."
        echo "MXM MCP will be installed without configuring a harness."
        echo
    fi
fi

# ---------------------------------------------------------------------------
# Install binary and update PATH
# ---------------------------------------------------------------------------

if ! mkdir -p "${INSTALL_DIR}" 2>/dev/null; then
    die "Cannot create ${INSTALL_DIR}. Set MXM_MCP_INSTALL_DIR to a writable directory."
fi

if ! install -m 0755 "${MXM_MCP_BINARY}" "${TARGET}" 2>/dev/null; then
    die "Cannot install mxm-mcp to ${TARGET}. Check permissions or set MXM_MCP_INSTALL_DIR."
fi

if [ "${INSTALL_DIR}" = "${HOME}/.local/bin" ]; then
    if [ -f "${HOME}/.bashrc" ]; then
        PATH_RC="${HOME}/.bashrc"
    else
        PATH_RC="${HOME}/.profile"
    fi
    if ! ensure_path_entry "${PATH_RC}"; then
        echo "Warning: could not add ${INSTALL_DIR} to PATH in ${PATH_RC}." >&2
        echo "Add this line manually: export PATH=\"${INSTALL_DIR}:\$PATH\"" >&2
    fi
fi

# ---------------------------------------------------------------------------
# Configure selected harnesses
# ---------------------------------------------------------------------------

CONFIGURED_NAMES=()
if [ "${#SELECTED[@]}" -gt 0 ]; then
    CONFIGURE_ARGS=(configure --binary "${TARGET}")
    for harness in "${SELECTED[@]}"; do
        case "${harness}" in
            opencode) CONFIGURE_ARGS+=(--opencode); CONFIGURED_NAMES+=("OpenCode") ;;
            codex) CONFIGURE_ARGS+=(--codex); CONFIGURED_NAMES+=("Codex") ;;
            claude) CONFIGURE_ARGS+=(--claude); CONFIGURED_NAMES+=("Claude") ;;
        esac
    done
    if ! "${PYTHON}" "${CONFIG_HELPER}" "${CONFIGURE_ARGS[@]}"; then
        die "MXM MCP was installed at ${TARGET}, but harness configuration failed. Run '${PYTHON} ${CONFIG_HELPER} configure --binary ${TARGET}' to retry."
    fi
fi

if [ "${#CONFIGURED_NAMES[@]}" -gt 0 ]; then
    CONFIGURED_TEXT="$(printf '%s, ' "${CONFIGURED_NAMES[@]}")"
    CONFIGURED_TEXT="${CONFIGURED_TEXT%, }"
else
    CONFIGURED_TEXT="none"
fi

if [ -n "${DAW_PATH}" ]; then
    DAW_TEXT="installed at ${DAW_PATH}"
else
    DAW_TEXT="not found - install from ${MXM_DOWNLOAD_URL}"
fi

SUMMARY="MXM MCP installed successfully.

Binary: ${TARGET}
Harnesses configured: ${CONFIGURED_TEXT}
MXM DAW: ${DAW_TEXT}

Restart your AI harness so it loads the MXM MCP server."

if [ "${USE_GUI}" -eq 1 ]; then
    zenity_info "${SUMMARY}"
else
    echo "${SUMMARY}"
fi
