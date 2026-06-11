# itasca-mcp-bridge

[English](README.md) | [简体中文](README.zh-CN.md)

[![PyPI](https://img.shields.io/pypi/v/itasca-mcp-bridge)](https://pypi.org/project/itasca-mcp-bridge/)

Runtime bridge that runs inside an ITASCA product process (PFC, FLAC, ...)
and exposes the product's Python SDK as a WebSocket API, enabling execution
tools for MCP servers such as [pfc-mcp](https://pypi.org/project/pfc-mcp/).

The bridge is product-neutral: it drives the host through the shared ITASCA
command language / Python SDK rather than any product-specific API.

## Features

- **Async tasks with progress polling.** Submit a long simulation script
  (`execute_task` message) and poll its status and paginated output while
  it runs (`check_task_status`).
- **Live REPL during a run.** Send `execute_code` against the running
  task's namespace at any time to inspect state or tune parameters
  mid-cycle — no need to bake probes into the script up front.
- **Graceful interrupt.** Stop a long cycling task on request
  (`interrupt_task`) without killing the product.
- **Unified output capture.** Python `print` and product console output
  (`itasca.command()` tables, list dumps, summaries) are interleaved in
  execution order in the task log.

## Architecture

ITASCA's Python SDK is main-thread-only, so the bridge keeps the
simulation on the main thread and serves remote requests around it with
three parts:

```mermaid
flowchart TD
    C[MCP client] -->|WebSocket| S[asyncio server<br/>background thread]
    S -->|submit → Future| Q[MainThreadExecutor<br/>queue]
    Q -->|Qt timer / blocking poll| M[PFC main thread<br/>itasca SDK + solver]
    M -.->|callback at cycle| CB[interrupt check<br/>+ snippet executor]
    CB -.-> M
```

- **WebSocket server (background thread).** An asyncio server receives
  messages, hands the work to the main thread, and awaits a `Future`. It
  never touches the SDK directly, so lightweight calls (status, interrupt)
  stay responsive even while a long task runs.
- **Main-thread queue.** `MainThreadExecutor` holds a thread-safe queue
  that the main thread drains — via a Qt timer in GUI mode, or a blocking
  poll in console mode. Submitted task scripts (`execute_task`) run here.
- **Cycle-gap callbacks.** A cycling task holds the main thread, so two
  `itasca.set_callback` hooks keep it reachable: an interrupt check that
  stops the run (`interrupt_task`), and a snippet executor that runs
  `execute_code` REPL calls in the gaps between cycles — sharing the
  task's `__main__` namespace for live inspection and tuning.

## WebSocket protocol

The bridge is the source of truth for the wire contract — MCP servers such
as pfc-mcp and flac-mcp are clients of it. A request is a JSON object with a
`type` and a `request_id`; the response echoes the `request_id`. The message
types are product-neutral:

| `type` (request) | Purpose | Key fields |
|---|---|---|
| `execute_task` | Submit a file-backed script as a tracked async task | `task_id`, `script_path`, `description` |
| `check_task_status` | Poll a task's status and paginated log | `task_id`, `skip_newest`, `limit`, `filter_text` |
| `list_tasks` | List known tasks | `offset`, `limit` |
| `interrupt_task` | Request a graceful interrupt of a running task | `task_id` |
| `execute_code` | Run a snippet in the running task's `__main__` (sync REPL) | `code`, `timeout_ms` |
| `get_working_directory` | Report the product process working directory | — |
| `ping` | Liveness check | — |

> `pfc_task` is still accepted as a deprecated alias for `execute_task`, so
> older clients keep working during the transition.

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

On every `start()` the bridge checks PyPI for a newer release (5-second
timeout; the Tsinghua mirror is tried when pypi.org is unreachable) and
self-upgrades before starting. The check is best-effort -- offline
machines and failed installs fall back to the installed version. To pin
the installed version, call `start(auto_upgrade=False)` or set the
environment variable `ITASCA_MCP_BRIDGE_AUTO_UPGRADE=0`. Corporate
mirrors can be configured with `ITASCA_MCP_PIP_INDEX_URL`.

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
  Version:  0.2.0
  URL:      ws://localhost:9001
  Log:      /your-working-dir/.itasca-mcp-bridge/bridge.log
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
