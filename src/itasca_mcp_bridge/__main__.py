"""Allow running as: python -m itasca_mcp_bridge"""

import argparse

from itasca_mcp_bridge import __version__, start


def main():
    parser = argparse.ArgumentParser(
        prog="itasca-mcp-bridge",
        description="Itasca MCP Bridge - WebSocket bridge for ITASCA codes (PFC, FLAC3D, ...)",
    )
    parser.add_argument(
        "--version", "-v", action="version", version="itasca-mcp-bridge {}".format(__version__)
    )
    parser.add_argument("--host", default="localhost", help="server host (default: localhost)")
    parser.add_argument("--port", type=int, default=9001, help="server port (default: 9001)")
    parser.add_argument("--mode", choices=["auto", "gui", "console"], default="auto",
                        help="task pump mode (default: auto)")
    args = parser.parse_args()

    start(host=args.host, port=args.port, mode=args.mode)


main()
