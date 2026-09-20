"""Allow running as: python -m itasca_mcp_bridge

Two things are available from the command line:

    python -m itasca_mcp_bridge [--host H] [--port P] [--mode M]
        Start the bridge here, as before.

    python -m itasca_mcp_bridge autostart install|remove|status
        Manage the sitecustomize hook that starts the bridge whenever an
        ITASCA product starts, so nobody has to type anything into the
        product's console.

The start options stay on the top-level parser rather than moving under an
explicit `start` subcommand, so `python -m itasca_mcp_bridge --port 9002`
keeps working exactly as it did.
"""

import argparse
import os
import sys


def _autostart_parser():
    parser = argparse.ArgumentParser(
        prog="itasca-mcp-bridge autostart",
        description=(
            "Install a sitecustomize hook that starts the bridge whenever an "
            "ITASCA product starts."
        ),
    )
    parser.add_argument(
        "action",
        choices=["install", "remove", "status"],
        help="install, remove, or report the hook's state",
    )
    parser.add_argument(
        "--root",
        action="append",
        default=None,
        help="ITASCA install root to search (repeatable; default: the usual ones)",
    )
    return parser


def _autostart(args):
    from itasca_mcp_bridge import autostart

    products = autostart.product_python_dirs(args.root)
    if not products:
        roots = args.root or autostart.default_roots()
        print("No ITASCA products found under: {}".format(", ".join(roots)))
        print("Pass --root to point at the install directory.")
        return 1

    changed = 0
    for product, site_packages in products:
        path = autostart.target_of(site_packages)
        state = autostart.state_of(path)
        label = "{}  ({})".format(product, site_packages)

        if args.action == "status":
            print("  {:<72} {}".format(label, state))
            continue

        try:
            result = (
                autostart.remove(site_packages)
                if args.action == "remove"
                else autostart.install(site_packages)
            )
        except Exception as exc:
            print("  {:<72} FAILED: {}".format(label, exc))
            continue
        changed += 1
        print("  {:<72} {} -> {}".format(label, state, result))

    if args.action == "install":
        print("")
        print("Start the product. Within a few seconds the bridge should answer on")
        print(
            "http://localhost:{}/health with runtime_mode 'gui'.".format(
                os.environ.get(autostart.ENV_PORT, autostart.DEFAULT_PORT)
            )
        )
        print("The hook logs to {}.".format(autostart.log_path()))
        print("")
        print("Dialogs are left alone by default; the hook says in its log when one")
        print("is waiting for a human. Set")
        print(
            "{}=1 to close the revision".format(autostart.ENV_DISMISS_WINDOWS)
        )
        print("notice and to dismiss dialogs that offer no choice (useful unattended).")
    elif args.action == "remove" and changed:
        print("")
        print("Restart the product for the change to take effect.")
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="itasca-mcp-bridge",
        description="Itasca MCP Bridge - HTTP bridge for ITASCA codes (PFC, FLAC3D, ...)",
    )
    from itasca_mcp_bridge import __version__

    parser.add_argument(
        "--version", "-v", action="version", version="itasca-mcp-bridge {}".format(__version__)
    )
    parser.add_argument("--host", default="localhost", help="server host (default: localhost)")
    parser.add_argument("--port", type=int, default=9001, help="server port (default: 9001)")
    parser.add_argument("--mode", choices=["auto", "gui", "console"], default="auto",
                        help="task pump mode (default: auto)")
    parser.add_argument("--no-upgrade", action="store_true",
                        help="skip the PyPI update check and start the installed version")
    parser.add_argument(
        "command",
        nargs="?",
        choices=["autostart"],
        help="subcommand; omit to start the bridge",
    )
    parser.add_argument(
        "rest",
        nargs=argparse.REMAINDER,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    if args.command == "autostart":
        return _autostart(_autostart_parser().parse_args(args.rest))

    from itasca_mcp_bridge import start

    start(host=args.host, port=args.port, mode=args.mode, auto_upgrade=not args.no_upgrade)
    return 0


if __name__ == "__main__":
    sys.exit(main())
