# Changelog

All notable changes to `itasca-mcp-bridge` are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.1] - 2026-06-11

### Added
- After a self-upgrade, the startup banner is followed by a short "What's
  new" list of the release highlights just received. The announcements ship
  inside the package (`announce.py`) keyed by version, so the list covers
  the whole `(old, new]` jump and works offline with no changelog fetch.
  `itasca_mcp_bridge.whats_new()` reprints it on demand.

### Fixed
- The self-upgrade version pre-check now scrapes the pypi.org simple-index
  HTML page before the JSON API. Observed right after the 0.2.0 release:
  the JSON API (and the PEP 691 JSON variant pip requests) lagged the HTML
  page by an hour on some CDN edges, which would have delayed upgrade
  detection by the same amount; a plain GET always receives the fresher
  HTML variant.

## [0.2.0] - 2026-06-11

### Added
- `start()` now self-upgrades the bridge from PyPI before starting, so the
  plain two-liner (`import itasca_mcp_bridge; itasca_mcp_bridge.start()`)
  keeps installs current without `addon.py`. A version pre-check with a 5 s
  hard timeout (PyPI JSON API, Tsinghua-mirror fallback) decides whether the
  in-process pip runs at all; every failure on the upgrade path — offline,
  blocked proxy, pip error — falls back to starting the installed version.
  After a successful install the loaded modules are reloaded so the new code
  serves the current session. Opt out per call with
  `start(auto_upgrade=False)`, globally with `ITASCA_MCP_BRIDGE_AUTO_UPGRADE=0`,
  or point at a corporate mirror with `ITASCA_MCP_PIP_INDEX_URL` (the legacy
  `PFC_MCP_PIP_INDEX_URL` is still honoured). The CLI gains `--no-upgrade`.
- The startup banner now shows the bridge version, and reports the version
  jump (`Upgraded: 0.1.6 -> 0.2.0`) when a self-upgrade just happened.

### Changed
- The runtime (server startup + task pumps) moved from `__init__.py` to a new
  `runtime.py`; the package entry is now a thin `start()` wrapper. The
  constants `DEFAULT_TIMER_INTERVAL_MS`, `DEFAULT_MAX_TASKS_PER_TICK` and
  `VALID_RUNTIME_MODES` remain re-exported from the package root.
- `start_bridge.py` (source-checkout entry) pins `auto_upgrade=False` so
  development code is never shadowed by the PyPI release.

## [0.1.6] - 2026-06-07

### Fixed
- Callback (re)registration is now idempotent across ITASCA versions, fixing
  a hard `RuntimeError: Failed to register interrupt callback` (and the
  matching executor error) on PFC 6.0. `itasca.set_callback` is **strict** on
  PFC 6.0 — re-registering an already-registered `(name, position)` raises
  `ValueError: Function <name> is already registered as a callback at position
  <p> in the cycle sequence` — whereas PFC 7.0 silently accepts it, which is
  why this never surfaced there. Compounding it, `model new` clears the
  cycle-callback registry but `model restore` does **not** on PFC 6.0, so the
  bridge's post-restore re-registration hit the strict path and aborted the
  whole `model restore`; a second `start()` (e.g. re-running `addon.py`) failed
  the same way. The two reserved cycle callbacks are now attached through a
  shared `register_cycle_callback` helper that removes before it registers
  (`remove_callback` is idempotent and keyed by name+position, so it only ever
  clears the bridge's own `_pfc_interrupt_check` / `_pfc_executor_callback` and
  never a user callback sharing the same cycle point). Earlier diagnosis
  blaming callbacks "baked into saves" was wrong: `.sav` files are version-
  independent JSON and contain no callback names — the root cause is the
  version-specific `set_callback` strictness, not save persistence. Affects
  both PFC and FLAC, which share this bridge.

### Documentation
- Corrected the architecture diagram callback label in both READMEs
  (`set_callback at cycle gaps` → `callback at cycle` / `每周期回调`): the
  interrupt check and snippet executor are registered at a fixed cycle point
  and fire once per cycle, not in the gaps between cycles.

## [0.1.5] - 2026-06-04

### Changed
- Renamed the WebSocket message type `pfc_task` → `execute_task`. The bridge
  is product-neutral (consumed by both pfc-mcp and flac-mcp), yet `pfc_task`
  was the only product-prefixed type in the protocol; `execute_task` is
  symmetric with the existing `execute_code`. The handler registry maps both
  keys to the same handler and the unknown-type fallback now defaults to
  `execute_task`.
- Neutralized remaining MCP tool-name references (`pfc_*`) in bridge
  docstrings/comments and in the output-truncation hint
  (`utils/response.py`), which previously named `pfc_check_task_status` in a
  message returned to clients.

### Deprecated
- `pfc_task` is still accepted as an alias for `execute_task`, so clients that
  have not switched yet keep working. It will be removed once downstream
  clients no longer emit it.

### Documentation
- Documented the WebSocket message types in both READMEs (EN + zh-CN) as the
  source-of-truth wire contract.

## [0.1.4] - 2026-06-03

### Fixed
- Qt task-pump now starts on PFC 9.7+. `_start_qt_pump` hard-imported
  `PySide2`, but PFC 9.7 (`pfc3d9_gui.exe`, Python 3.10) ships **PySide6**
  (Qt6) with no PySide2 present. The import failed, so `mode="auto"` fell
  back to the blocking console pump, whose `while True` loop froze the GUI
  main thread — the "hang" on startup. The bridge now probes Qt bindings
  newest-first (`PySide6`, then `PySide2`) and drives the `QTimer` task
  pump through whichever is available, so the same build runs non-blocking
  across PFC 6/7, early PFC 9 (PySide2) and PFC 9.7+ (PySide6). The
  `QTimer` / `QCoreApplication.instance()` / `timeout.connect()` APIs are
  identical across Qt5/Qt6, so no other changes were needed.

## [0.1.3] - 2026-06-03

### Fixed
- Task log, `tasks.json`, and command-log paths are now anchored to a bridge
  root frozen once at startup, instead of being re-resolved against the live
  working directory on every access. A user task script that calls
  `os.chdir()` moves the working directory of the whole embedded-Python
  interpreter; previously this sent `check_task_status` to read the task log
  at the post-chdir location, returning `(no output)` (`total_lines: 0`) for a
  task whose log had in fact been written correctly through its still-open file
  handle. `tasks.json` persistence was exposed to the same drift. Anchoring
  every `.itasca-mcp-bridge/` path to the frozen root keeps logs and task state
  at one stable on-disk location regardless of later chdir. Extends the
  abspath-vs-CWD fix from 0.1.2 (command log) to the task FileBuffer log and
  task persistence. Affects both PFC and FLAC, which share this bridge.

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
