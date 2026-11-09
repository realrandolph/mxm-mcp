"""Entry point for running the MXM MCP server."""

import sys


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "-m" and "vst3_probe" in sys.argv[2]:
        from .vst3_probe import main as probe_main
        sys.exit(probe_main(sys.argv[3:]))
    from .server import main as server_main
    server_main()


if __name__ == "__main__":
    main()
