# Changelog

All notable changes to `itasca-mcp-bridge` are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.2] - 2026-05-29

### Fixed
- `capture_pfc_console` now resolves the command-log path with
  `os.path.abspath()`. The Itasca command interpreter resolves relative
  `program log-file` paths against its *own* working directory, which is
  not guaranteed to equal Python's `os.getcwd()` — on the headless Linux
  console the engine sits at `/tmp` while Python runs from the launch
  directory. The log was then written and read at two different locations,
  so `itasca.command()` output came back empty; when the engine's target
  directory didn't exist the call failed outright with `ValueError: Error
  write to file ...`. Resolving to an absolute path aligns both sides
  regardless of their independent CWDs. No-op where the two already
  coincide (e.g. the Windows GUI, where loading a project aligns them).
  Affects both PFC and FLAC, which share this bridge.

## [0.1.1] - 2026-05-22

### Fixed
- `command_splitter` now recognizes `import itasca as <alias>` and
  `from itasca import command [as <alias>]`. Previously the multi-line
  splitter only matched the literal `itasca.command(...)` form, so the
  canonical PFC convention `import itasca as it` skipped splitting
  entirely — the block ran as one C batch, holding the GIL while PFC
  echoed every sub-command into IPython's ZMQIOStream. Its `flush`
  timed out trying to reach a Qt event loop the same GIL was blocking,
  deadlocking the bridge until requests began timing out as
  `bridge_unavailable` on the MCP side.

### Added
- DEBUG diagnostic for multi-line `<name>.command(...)` calls whose
  receiver isn't a known itasca alias (e.g. reassignment patterns like
  `_it = itasca`). Silent at default INFO level; gives a grep target
  in `bridge.log` when the same stall recurs through a path the
  splitter can't statically prove.

## [0.1.0] - 2026-05-19

Initial release of `itasca-mcp-bridge` as a standalone, product-neutral
package. Extracted from `pfc-mcp-bridge` 0.3.2 via `git subtree split`
(commit history preserved) so that `pfc-mcp`, a future `flac-mcp`, and
other MCP servers can share one bridge runtime.

Changes relative to the `pfc-mcp-bridge` 0.3.2 baseline:

- Renamed package `pfc_mcp_bridge` → `itasca_mcp_bridge`, distribution
  `pfc-mcp-bridge` → `itasca-mcp-bridge`, console script `itasca-mcp-bridge`.
- Unified runtime data directory to `.itasca-mcp-bridge/` (was split across
  `.pfc-mcp/` and `.pfc-mcp-bridge/`).
- Product-neutral branding in startup banner, logger name, CLI help, and
  the missing-`itasca`-module error message (now names PFC / FLAC3D).
- Centralized cycle-callback positions into `signals.positions`
  (`INTERRUPT_CALLBACK_POSITION`, `EXECUTOR_CALLBACK_POSITION`), removing
  magic numbers scattered across four call sites and fixing a hardcoded
  re-registration position. Values are unchanged from the PFC-verified
  baseline; a product-neutral once-per-cycle scheme is a tracked
  follow-up pending PFC + FLAC3D validation.

`pfc-mcp` continues to ship its own bundled bridge and is unaffected by
this package; adopting `itasca-mcp-bridge` there is a future breaking change.

---

The entries below are inherited `pfc-mcp-bridge` history, retained for
provenance. Their version numbers refer to `pfc-mcp-bridge`, not
`itasca-mcp-bridge`.

## [pfc-mcp-bridge 0.3.2] - 2026-05-14

Adds two-layer cancellation for `execute_code` so runaway snippets
(`while True`, long `model cycle`, etc.) no longer jam the bridge until
restart. L1 sets an interrupt flag the cycle callback polls; L2
async-raises `BridgeTimeout` on the registered exec thread for code that
never yields. Wire status now reports `terminated`, `interrupted`, or
`timeout` with `details.method` set to `stuck_in_c` / `flag_only` when
termination can't complete. Snippets that interleave inside a running
task's cycle gap no longer clobber the outer task's interrupt id.

Internal rename: `signals/script_executor` → `signals/cycle_executor`,
`handlers/script_executor` → `handlers/exec_strategy`; both
`execute_code` paths now share `execution/snippet.run_snippet()` and
the callback path stops round-tripping code through a temp file.

## [pfc-mcp-bridge 0.3.1] - 2026-05-14

Compatibility release shipping alongside an updated `addon.py` bootstrap that
falls back to a Tsinghua mirror when PyPI is unreachable, so PFC 6/7 users
behind corporate proxies or slow international routes can install the bridge
reliably. No code changes to the bridge package itself.
