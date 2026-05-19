# itasca-mcp-bridge

[English](README.md) | [简体中文](README.zh-CN.md)

[![PyPI](https://img.shields.io/pypi/v/itasca-mcp-bridge)](https://pypi.org/project/itasca-mcp-bridge/)

Runtime bridge that runs inside an ITASCA product process (PFC, FLAC3D, ...)
and exposes the product's Python SDK as a WebSocket API, enabling execution
tools for MCP servers such as [pfc-mcp](https://pypi.org/project/pfc-mcp/).

The bridge is product-neutral: its core mechanisms — console-output capture
via `program log`, cycle callbacks via `itasca.set_callback`, and Python
state persistence via `python-reset-state` — use the shared ITASCA command
language / SDK and have been verified to behave identically on PFC and
FLAC3D.

## Quick Start

Run inside the product's Python (GUI IPython console or console CLI):

### Install from PyPI

In the product's IPython console:

```python
from pip._internal.cli.main import main as pip_main
pip_main(["install", "--user", "itasca-mcp-bridge"])

import itasca_mcp_bridge
itasca_mcp_bridge.start()
```

`websockets` is pulled in automatically with a version matched to the
embedded Python (`9.1` for Python 3.6, `16.0` for Python 3.10). If it is
missing or mismatched, install it the same way (`pip_main(["install",
"--user", "websockets==9.1"])` on Python 3.6, `websockets==16.0` on 3.10).

### Run from a source checkout

```python
%run C:/path/to/itasca-mcp-bridge/start_bridge.py
```

> Use forward slashes in the path. Do not wrap it in quotes.

Code changes take effect on the next `%run`, so this is the preferred
workflow during development.

The bridge auto-detects the runtime: a Qt timer in GUI mode, a blocking
loop in console mode.

Expected output:

```text
============================================================
Itasca MCP Bridge Server
============================================================
  URL:         ws://localhost:9001
  Log:         /your-working-dir/.itasca-mcp-bridge/bridge.log
  Callbacks:   Interrupt, Executor (registered)
============================================================
```

## Requirements

- An ITASCA product with an embedded Python interpreter.
  - Verified: PFC 6.0 / 7.0 / 9.0.
  - FLAC3D: the bridge's core SDK/command mechanisms are verified
    compatible; full end-to-end validation is in progress.
- Python >= 3.6 (PFC 6/7 use Python 3.6; PFC 9 uses Python 3.10).
- `websockets` (`==9.1` on Python 3.6, `==16.0` on Python 3.10),
  installed automatically as a dependency.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Server won't start | Re-run the install/start steps in the product's IPython console; check `.itasca-mcp-bridge/bridge.log` |
| `websockets` version mismatch | In the product IPython console: `from pip._internal.cli.main import main as pip_main; pip_main(["install", "--user", "websockets==16.0"])` (use `9.1` on Python 3.6) |
| Port in use | `itasca_mcp_bridge.start(port=9002)`, then point your MCP client's bridge URL at `ws://localhost:9002` |
| Connection failed | Confirm the bridge is running and the port is reachable; see `.itasca-mcp-bridge/bridge.log` |
| No task execution / MCP cannot connect | If execution tools return `ok=false`, `error.code=bridge_unavailable`, `error.details.reason=cannot connect to bridge service`, confirm `itasca_mcp_bridge.start()` is running and your MCP client's bridge URL matches |

## Relationship to MCP servers

This package is the in-process runtime only. Pair it with an MCP server
that speaks its WebSocket protocol — for example
[pfc-mcp](https://pypi.org/project/pfc-mcp/) — for full client setup.

License: MIT.
