# MXM MCP Installer

This archive installs the MXM MCP server for AI coding harnesses on Linux.

## Requirements

- Linux x86_64 (Linux Mint, Ubuntu, Debian and similar distributions)
- Python 3.10 or newer
- Optional: Zenity for the graphical installer wizard
- Optional: MXM DAW for rendering, preset loading and version checks

Install MXM DAW from <https://github.com/realrandolph/mxm/releases>.

## Install

Extract the archive and run the installer:

```bash
tar -xzf mxm-mcp-linux-x86_64.tar.gz
cd mxm-mcp-linux-x86_64
./install.sh
```

The installer:

1. Detects the MXM DAW and offers the download page when it is missing.
2. Detects OpenCode, Codex, Claude Desktop and Claude Code.
3. Shows a checklist with every detected harness selected by default.
4. Installs `mxm-mcp` to `~/.local/bin/mxm-mcp`.
5. Adds `~/.local/bin` to `PATH` in `~/.bashrc` or `~/.profile`.
6. Registers the MXM MCP server with the selected harnesses.
7. Leaves unrelated configuration keys untouched.

Restart the harness after installation so it loads the new server.

### Non-interactive install

```bash
./install.sh --quiet
```

Setting `CI=1` has the same effect. Non-interactive mode selects every
detected harness, skips dialogs, and continues when MXM DAW is missing.

### Custom install directory

```bash
MXM_MCP_INSTALL_DIR="$HOME/bin" ./install.sh
```

## What gets written

OpenCode (`~/.config/opencode/opencode.json` on Linux):

```json
{
  "mcp": {
    "mxm": {
      "type": "local",
      "command": ["/home/you/.local/bin/mxm-mcp"]
    }
  }
}
```

Codex (`~/.codex/config.json` or `~/.config/codex/config.json`):

```json
{
  "mcpServers": {
    "mxm": {
      "command": "/home/you/.local/bin/mxm-mcp",
      "args": []
    }
  }
}
```

Claude Desktop (`~/.config/Claude/claude_desktop_config.json`) and Claude Code
(`~/.claude.json`) use the same `mcpServers` structure.

## Manual configuration

Run the helper directly to configure one or more harnesses:

```bash
python3 configure_harnesses.py detect
python3 configure_harnesses.py configure --binary "$HOME/.local/bin/mxm-mcp" --opencode --codex --claude
python3 configure_harnesses.py status
```

`configure` preserves existing settings, creates missing configuration files,
and writes JSON with two-space indentation. Use `--json` for machine-readable
output.

## Troubleshooting

- **`mxm-mcp` is not found after install:** open a new terminal, or run
  `source ~/.bashrc`. Verify `~/.local/bin` is in `PATH`.
- **A harness does not list MXM MCP:** restart the harness and check
  `python3 configure_harnesses.py status`.
- **Rendering or preset tools report a missing DAW:** install MXM DAW from
  <https://github.com/realrandolph/mxm/releases>.
- **Invalid JSON warning:** fix the reported configuration file before
  rerunning the installer. The installer never overwrites a file it cannot
  parse.

## Uninstall

Remove the binary and the `mxm` entries from the harness configuration files:

```bash
rm ~/.local/bin/mxm-mcp
python3 configure_harnesses.py status
```

Edit the listed files to delete the `mxm` entry under `mcp` or `mcpServers`.
