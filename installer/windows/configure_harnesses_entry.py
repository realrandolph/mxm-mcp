"""Windows command-line adapter for the MXM MCP harness helper."""

from __future__ import annotations

import argparse

from configure_harnesses import main as configure_harnesses_main


def main() -> int:
    parser = argparse.ArgumentParser(description="Configure MXM MCP harnesses.")
    parser.add_argument("--target-bin", required=True)
    parser.add_argument("--opencode", action="store_true")
    parser.add_argument("--codex", action="store_true")
    parser.add_argument("--claude", action="store_true")
    options = parser.parse_args()

    arguments = ["configure", "--binary", options.target_bin]
    for harness in ("opencode", "codex", "claude"):
        if getattr(options, harness):
            arguments.append(f"--{harness}")
    return configure_harnesses_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
