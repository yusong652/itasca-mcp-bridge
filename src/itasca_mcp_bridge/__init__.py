"""Itasca MCP Bridge - HTTP bridge for ITASCA codes.

Runs inside an ITASCA product GUI (PFC, FLAC3D, ...) Python environment
and exposes the product SDK as a remote HTTP + SSE API for MCP clients
and other tools.

Usage (in the product GUI Python console):
    import itasca_mcp_bridge
    itasca_mcp_bridge.start()

Usage (in the product console CLI):
    import itasca_mcp_bridge
    itasca_mcp_bridge.start(mode="console")

By default `start()` first checks PyPI for a newer bridge release and
installs it before starting (best-effort: offline or failed checks fall
back to the installed version). Pass `auto_upgrade=False` or set the
environment variable `ITASCA_MCP_BRIDGE_AUTO_UPGRADE=0` to pin the
installed version.
"""

__version__ = "0.4.5"

from .announce import whats_new  # noqa: F401  (console convenience)
from .runtime import (  # noqa: F401  (re-exported for compatibility)
    DEFAULT_TIMER_INTERVAL_MS,
    DEFAULT_MAX_TASKS_PER_TICK,
    VALID_RUNTIME_MODES,
)


def start(
    host="localhost",
    port=9001,
    mode="auto",
    auto_upgrade=True,
):
    """Start the Itasca MCP Bridge server.

    Optionally self-upgrades to the latest published release first, then
    starts an HTTP + SSE server in a background thread and the main-thread
    task pump.

    Args:
        host: Server host address.
        port: Server port number.
        mode: Task pump mode - "auto" (try Qt, fall back to blocking),
            "gui" (Qt only), or "console" (blocking only).
        auto_upgrade: Check PyPI for a newer bridge release and install it
            before starting. Best-effort: any network or pip failure falls
            back to starting the installed version. Also disabled by the
            environment variable ITASCA_MCP_BRIDGE_AUTO_UPGRADE=0.
    """
    if auto_upgrade:
        from . import upgrade

        if upgrade.env_allows_upgrade():
            import os

            upgraded = False
            try:
                upgraded = upgrade.maybe_upgrade(__version__)
            except Exception as e:
                print(
                    "itasca-mcp-bridge: update check failed ({}); "
                    "starting installed version {}.".format(e, __version__)
                )
            if upgraded:
                fresh = upgrade.reload_bridge()
                if getattr(fresh, "__version__", __version__) != __version__:
                    # Hand the version jump to the fresh start() via the
                    # environment so its banner can report it; a kwarg would
                    # couple old and new start() signatures.
                    os.environ[upgrade.ENV_UPGRADED_FROM] = __version__
                    return fresh.start(
                        host=host,
                        port=port,
                        mode=mode,
                        auto_upgrade=False,
                    )

    from . import runtime

    return runtime.start(
        host=host,
        port=port,
        mode=mode,
    )
