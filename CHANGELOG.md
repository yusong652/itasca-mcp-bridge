# Changelog

All notable changes to `itasca-mcp-bridge` are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.6.0] - 2026-09-23

### Added
- The bridge now records what the person at the keyboard types into the
  product GUI and hands it to the client through a new `console_history`
  command. Both places one can type are covered: cells run in the IPython
  pane (input, printed output, expression result, whether it raised) and
  lines entered at the product's own command prompt (the line and what
  the engine printed for it, flagged when the engine reported an error).
  Entries carry a `source` of `python` or `command`. The bridge keeps a
  delivery cursor, so each call returns only what the client has not seen
  yet, and both entries and cursor persist in `.itasca-mcp-bridge/` across
  a bridge restart. A `console_entry` doorbell is pushed over `GET /events`
  as entries arrive.

  The IPython pane is hooked through the shell's `pre_run_cell` /
  `post_run_cell` events, in both the calling convention IPython 6.2 uses
  (6.0/7.0 products) and the one IPython 8 uses (9.x products). The command
  prompt is hooked through the prompt widget's own `myReturnPressed`
  signal, with an event filter on the Return key as the fallback where the
  Qt binding cannot connect it; the command's output is cut out of the
  console pane between its echo line and the next. The prompt widget is
  matched by class name without its namespace, since the 3D products keep
  it in `itasca3d::` and the 2D products in `itasca2d::`. Measured on
  PFC3D 6.0 (Python 3.6, IPython 6.2, PySide2 5.11), PFC3D 7.0, PFC3D 9.7
  (Python 3.10, IPython 8.16, PySide6 6.8) and MPoint2D 9.7 with the same
  sequence: lines entered faster than the engine runs them, repeats of
  the same line, and engine errors are all attributed correctly. GUI
  only: a console build has neither pane nor prompt widget, and
  `console_history` there answers with no entries.

### Changed
- README, package metadata, docstrings and comments now describe the
  bridge in terms of the ITASCA product it runs in rather than PFC. The
  Requirements section lists every product the bridge is verified on (PFC,
  FLAC3D, 3DEC, MPoint, MassFlow; 6.0, 7.0 and 9.x generations, GUI and
  console builds), and engine behaviours that differ by generation are
  stated per generation ("the 6.0 engine") with the host they were
  measured on kept as evidence.
- The two cycle callbacks the bridge registers with the engine are now
  named `_mcp_bridge_interrupt_check` and `_mcp_bridge_executor_callback`
  (were `_pfc_interrupt_check` / `_pfc_executor_callback`). The names are
  what the engine prints when a callback raises, and what the bridge
  matches on to report a task as `interrupted`. Callbacks left under the
  old names by a bridge that ran earlier in the same session are removed
  at the next `start()`.

## [0.5.5] - 2026-09-15

### Fixed
- The bridge no longer disappears from its client when an engine command
  fails while the product is set to raise a dialog on errors. Itasca
  products can show a modal `QMessageBox` on every engine error -- "Raise
  Dialog on Error", under Tools > Options > Console, stored per user in
  `HKCU\Software\Itasca\<product>\Terminal\ErrorDialog`. It is on by
  default in the 6.0 generation of the products and off from 7.0 on, which
  is why the same bridge build wedged on one person's machine and not the
  next one's, with nothing in the bridge to explain the difference.

  The box is shown with `exec()`, so the C-level `itasca.command` does not
  return until someone clicks OK. The engine holds the GIL for that whole
  call, so every bridge thread freezes with it -- the HTTP server included,
  which is why requests did not even arrive and every tool reported
  `bridge_unavailable`. Measured on PFC3D 6.00.030: one mistyped command
  held the bridge for 48s, until a human clicked.

  A separate `QTimer` does keep ticking inside the box's nested event loop.
  Qt refuses only to redeliver a timer whose own handler is already on the
  stack, and that is exactly the bridge's task pump (`_process_tick` ->
  `process_tasks` -> the snippet -> `itasca.command` -> the box). Measured
  during a 2.1s box: 67 ticks of a 25ms timer against 0 of the pump. The
  new `utils.modal_guard` uses that window to press OK.

  Its scope is deliberately narrow, because it runs inside someone's GUI:
  only while the bridge itself is inside an engine command, and only for a
  `QMessageBox` carrying exactly one button, which offers no choice to
  make. Anything else is left alone and reported once to `bridge.log` with
  its class, title and button labels.

  The error is otherwise untouched: still printed to the console, still in
  the task log, still raised out of `itasca.command`, and PFC 6's cycling
  resume still takes over when it happened in a cycle callback. Only the
  error box blocks -- the "List of Warnings" dialog is non-modal by design,
  and so is the cycling dialog.

  Verified live on PFC3D 6.00.030 (Python 3.6.1, PySide2): dismissed in
  0.111s with the `RuntimeError` still raised; a single-button box left
  alone while the bridge was idle; a two-button box left alone while it was
  busy.

## [0.5.4] - 2026-09-14

### Fixed
- The bridge no longer picks the Qt timer pump in a product console build,
  where it silently never runs a single task. The console executables
  (`pfc3d9_console.exe` and friends) construct a plain `QCoreApplication`
  for Qt's non-GUI infrastructure and never call `exec()`, so the old test
  — `QCoreApplication.instance() is not None` — passed there: `start()`
  reported `Mode: Qt timer`, attached a `QTimer` to an event loop that does
  not exist, and returned. The HTTP server stayed reachable while nothing
  drained the task queue, so every request hung until it timed out and the
  MCP side reported `bridge_unavailable` — a failure that looks like a dead
  bridge rather than a wrong pump. Confirmed on PFC3D 9.7 console:
  `isinstance(app, QGuiApplication)` is False and
  `QThread.currentThread().loopLevel()` is 0.

  The Qt pump now requires positive evidence of an event loop: either the
  host application is a GUI one, or a loop is already spinning on this
  thread. `mode="auto"` therefore falls back to the blocking poll pump in a
  console, and `mode="gui"` fails fast with an explanation instead of
  printing a banner for a pump that will never tick.

  The application class is read from the C++ metaobject chain, not from the
  Python type. PySide2 will not downcast an application object it did not
  create: on PFC 7.0 GUI, `type(app)` and `isinstance(app, QGuiApplication)`
  both report a plain `QCoreApplication` -- byte for byte what PFC 9.7
  *console* reports -- while the process really is a GUI running an event
  loop. PySide6 does downcast, so the Python type describes the binding's
  capabilities rather than the host, and cannot gate this. `metaObject()`
  asks the C++ object what it is and is unaffected; the whole superclass
  chain is walked, because the products subclass their application twice
  (`itasca3d::ItascaApplication` -> `itasca3d::Application` ->
  `QApplication` on PFC 7.0).

  `QThread.loopLevel()` is consulted as a second, independent witness, and
  the blocking pump is chosen only when both say there is no event loop.
  The two misreadings are not equally bad: the Qt pump without a loop
  leaves requests to time out, while the blocking pump inside a GUI seizes
  the main thread and freezes the product window with no way back except
  killing it. Neither witness is sufficient alone -- `loopLevel()` is 0 in a
  GUI whenever `start()` runs before `exec()` begins.

  Detection still imports nothing beyond `QtCore`. Reaching for a second Qt
  module (`QtGui`, for an isinstance check) is a step that can fail on its
  own, and a failure inside detection is indistinguishable from "no Qt at
  all" -- the reading that freezes the GUI.

  Verified end to end on PFC 6.0 GUI and 7.0 GUI (PySide2, no downcast),
  9.7 GUI (PySide6, downcast) and 9.7 console (no event loop).

### Added
- The README documents the headless workflow this fix makes possible: a
  console build runs the data file passed as its first argument, so an
  agent can start the engine and the bridge itself, with no GUI and nobody
  at the keyboard.
- `scratch/probe_pump_mode.py` reports which pump `start()` would pick on a
  host, without starting anything: the Qt binding, the application's Python
  type and C++ metaobject chain, the thread's loop level, and each witness
  separately, distinguishing "the answer is no" from "this Qt binding has
  no such method". The safe way to check a product version that has not
  been verified yet, since a wrong reading in a GUI costs a frozen window.

## [0.5.3] - 2026-09-11

### Changed
- The startup banner now carries the task-loop mode as a field
  (`Mode: Qt timer` / `Mode: blocking poll`) instead of a loose line
  printed after it, and no longer prints the pump's `interval` and
  `max_tasks_per_tick` — tuning internals nobody reading a startup
  banner acts on. They, the engine binary and the directory the package
  was imported from are logged to `bridge.log` instead, where they are
  useful for diagnosis. The pump is now started before the banner so the
  banner can state which one won; a `mode="gui"` start with no Qt
  available now raises before printing a banner rather than after.

### Fixed
- The bridge no longer goes unreachable for the duration of a cycling
  command when the model was reset earlier in the same execution. The
  cycle callbacks are the only window into a running engine — it holds
  the GIL for the whole C call, so with an empty registry every bridge
  thread is frozen, HTTP included: `execute_code`, `check_task_status`,
  `interrupt_task` and the SSE stream all time out (the MCP side reports
  `bridge_unavailable`), and the task cannot be interrupted at all until
  the command returns on its own. Measured on PFC3D 6.00.030: a data file
  doing an abbreviated `mod new` and then `model solve fish-halt` froze
  the whole bridge for 76s, and the `interrupt_task` request never even
  registered.

  The registry is now re-registered before every engine command instead
  of after commands matching `model new` / `model restore`. That literal
  match never covered the real input space: the engine accepts
  abbreviations and arbitrary inner whitespace, so `mod new`, `mode new`,
  `model n`, `model ne`, `model  new`, `model<TAB>new`, `model re 'f'`
  and `model rest 'f'` all reset the model and all slipped past it — and
  resets that never reach the wrapper at all (typed in the GUI console,
  run from the File menu, issued inside a FISH `command` block) had no
  spelling that would have worked. The engine exposes no way to query the
  registry, so repairing unconditionally is the only option that closes
  the class. It costs 6–19 µs against 11–39 ms for the most trivial
  `itasca.command` on the same engine — at most 0.1%, measured on PFC
  6.00.030, 7.00.161 and 9.7. Commands running inside a cycle callback
  still skip it — mutating the live registry there crashes the engine.

  Verified on all three engines: every listed spelling resets the model
  on each, and after the fix the data file that used to freeze the bridge
  stays reachable throughout, interrupts in about a second, and an engine
  command issued from inside the cycle callback returns with the process
  alive.

## [0.5.2] - 2026-09-06

### Fixed
- PFC 6: a task's running `model cycle` / `model solve` is now resumed
  when an `execute_code` engine command fails while the task cycles.
  PFC 6 aborts the cycling a callback interrupted whenever a command
  fails inside that callback, so a mistyped command sent through
  `execute_code` cut the task's cycling short at that cycle: no "limit
  met" line, the script's `itasca.command()` returned normally and the
  task ended `completed` with the cycles silently missing (a
  `model solve` looked converged). Nothing on the snippet side prevents
  the abort, whether or not it catches the error. The wrapped
  `itasca.command` now records the failure with the engine's cycle
  count and, when the cycling command returns at exactly that cycle,
  re-issues the remainder: `model cycle` with the cycles left,
  `model solve` with its relative `cycles` limits rewritten (absolute
  `cycles-total` / `time-total` / ratio limits verbatim), looping if
  the re-issued command is aborted again. Each resume is noted in the
  task log, and the snippet's error says what may have happened to the
  task. A solve carrying `time`, `clock` or `elastic` is not resumed;
  the log line says so. A pending `interrupt_task` still wins. PFC 7
  keeps cycling and is unaffected.
- PFC 6: `model new` / `model restore` sent through `execute_code`
  while a task is cycling no longer kills the engine. The snippet runs
  in the executor's cycle callback, where the engine is mid-cycle and
  iterating its callback registry; the wrapped command's model-reset
  repair hook re-registered the bridge's callbacks right there, and
  mutating the live registry exited PFC3D 6.00.030 on the spot. The
  repair is now deferred to the next execution entry point while a
  cycle callback is running. The task the reset pulled the model from
  under still fails with the engine's own error, as it should.
- The bridge's own capture-control commands (`program log on/off`,
  `program log-file`) are no longer interrupt points. 0.5.1 made every
  command boundary honor a pending `interrupt_task`, but the log-off
  that closes each command's capture runs while the flag is still set,
  so it raised, the engine's log session stayed open and the
  interrupted command's captured output was dropped. The next task then
  inherited the live session: a stale banner led its log on every
  engine, and on PFC 6 Python prints were delivered twice again.

## [0.5.1] - 2026-09-06

### Fixed
- PFC 6: the `[bridge] program call` header and the data file's comment
  lines no longer appear twice in the task log. PFC 6 records the GUI
  console's Python output in the `program log` file too, so lines
  printed while the file's live log session was on were delivered once
  from stdout and once from the captured chunk (the second copy read
  back from the ANSI-encoded engine log, so non-ASCII text in it was
  mojibake). The expander now pauses the capture session around those
  prints. PFC 7/9 were unaffected.
- A pending `interrupt_task` is now honored at every command boundary,
  not only in the cycle callback. PFC 6 swallows the callback's
  `InterruptedError` when cycling was started from inside a FISH
  `command … endcommand` block: it printed the traceback, returned from
  the FISH call normally and carried on with the next command, so an
  interrupted task ran its data file to the end and finished
  `completed`. The wrapped `itasca.command` now checks the flag before
  starting a command and after it returns, so the task aborts at the
  next command instead. Also makes an interrupt effective for scripts
  that are between commands (for example in `time.sleep`) at their next
  engine call.
- `program call` issued from an `execute_code` snippet while a task is
  cycling inside a nested called file is now expanded and resolved
  against the working directory. The expander's directory stack was
  process-wide, so the snippet's top-level call resolved against the
  task's current file directory, did not resolve, and went to the
  engine as one opaque C call. Stacks are now kept per execution
  context (task or snippet).

## [0.5.0] - 2026-09-06

### Changed
- `program call '<file>'` issued through `itasca.command` now runs the
  data file inline, one command per engine call, instead of handing the
  whole file to the engine as a single C call. Behaviour change for
  users: a called file that contains `model new` / `model restore` no
  longer leaves the bridge unreachable and the task uninterruptible for
  the file's duration — `check_task_status`, `execute_code` interleaving
  and `interrupt_task` all work inside the file, and the task log shows
  the file's console output and its comment lines command by command as
  they happen, not in one block at the end. Engine semantics are
  reproduced (each verified live on PFC3D 9.7): nested relative paths
  resolve against the calling file's directory, a missing extension is
  defaulted, `program return` ends the file, `:label` lines are comments
  and the targets of `label`, `line <i>` starts at that line, `suppress`
  is accepted. Errors carry the offending command and file name. Forms
  the bridge cannot honor exactly go to the engine unchanged: several
  `call` keywords on one line, unknown keywords, `.py` targets, files
  that do not resolve on disk, non-text (`program encrypt`) files,
  nesting deeper than 16. Remaining gap: `model new` inside a FISH
  `command … endcommand` block followed by cycling in the same block is
  still one C call.

## [0.4.5] - 2026-09-06

### Fixed
- The bridge no longer goes silent when the agent starts cycling after
  the model was reset outside Python. `model new` / `model restore`
  wipe the engine's cycle-callback registry, and the bridge's repair
  hook only sees resets issued through the Python `itasca.command`. A
  reset typed in the GUI console, run from the File menu, or executed
  inside a `program call` script left the registry empty with nothing
  to repair it — and since the engine holds the GIL for the whole
  duration of a C-level command, the bridge's own cycle callbacks are
  the only windows where the HTTP thread, status polls, `execute_code`
  interleaving and interrupt can run. The agent's next `model cycle`
  wedged the entire bridge for its duration with no error anywhere.
  Every execution entry point (task scripts, idle-path `execute_code`)
  now re-registers the callbacks unconditionally before user code runs
  (remove-then-set, two C calls). Verified on PFC3D 9.7: after a
  GUI-console `model new`, a 20000-cycle task kept `/health`,
  `check_task_status` and `execute_code` reachable throughout.
- Corrects the earlier reading of the `program call` wedge as a
  version-specific GIL bug that 9.7 fixed: it is engine-independent
  and depends only on whether the callback registry is intact. Cycling
  inside a `program call` script whose file contains `model new` still
  wedges for the file's duration (there is no Python boundary inside
  the file to repair at); the bridge now self-heals as soon as the file
  returns instead of staying dead until the next Python-issued reset.

## [0.4.4] - 2026-08-05

### Fixed
- FISH definition blocks inside multi-line `itasca.command()` calls no
  longer wedge async tasks. The command splitter split such calls line
  by line, so `fish define <name>` reached the engine alone — the
  console dropped into interactive FISH mode and the call blocked
  holding the GIL waiting for the function body, leaving the whole
  bridge unreachable until someone typed `end` in the GUI. `fish
  define` / `fish operator` / legacy bare `define` ... `end` blocks are
  now kept whole as one multi-line command (definitions execute
  instantly, so nothing is lost by not splitting them).
- A `model new` / `model restore` sitting mid-string in a multi-line
  command no longer leaves the engine's cycle-callback registry dead.
  Those commands clear the registry (killing status polls,
  `execute_code` interleaving, and interrupt — all of which ride on the
  bridge's cycle callbacks); the re-registration hook only checked the
  start of the command string, so a mid-string reset was never
  repaired. The hook now scans every line.
- `execute_code` snippets get the same multi-line command splitting as
  task scripts. Unsplit batches run as one C call, so an embedded
  `model new` wiped the callback registry with no repair until the
  whole batch — including any subsequent cycling — finished with the
  bridge unreachable and the timeout unable to inject. Verified on
  PFC 6 and PFC 7 alike: the behaviour is engine-independent.
- Interrupting a task now reports `interrupted` on every engine.
  PFC 6 wraps the callback-raised `InterruptedError` in an opaque
  `RuntimeError` that the string-matching classifier (tuned to
  PFC 7's `ValueError` wrapping) missed, so a successful interrupt
  came back as `failed`. Classification now falls back to the task's
  still-pending interrupt flag, which identifies the abort regardless
  of how the engine wrapped it.
- A live `execute_code` during a cycling task no longer silently stops
  the run on PFC 6. The console-capture machinery resumed the task's
  log session with `program log on show-message off` from inside the
  cycle callback; PFC 6 does not know the `show-message` keyword, and
  a command complaint raised in the callback makes it abort the
  running `model cycle` (stopping at the interleave point with no
  "cycle limit met"). Capture commands that execute in the callback
  now omit the keyword; sequential-context commands keep it so GUI
  console banners stay suppressed on engines that support it.
- The splitter's diagnostic for multi-line `.command()` calls on
  receivers it cannot prove alias itasca (e.g. `_it = itasca`) was
  logged at DEBUG — below the root logger's INFO level, so it appeared
  nowhere. It is now a WARNING and is also printed into the task or
  snippet output, where the submitting agent actually reads, with a
  hint to call `itasca.command` via its import name.

### Changed
- Internal names finished migrating off the pfc-mcp-bridge era:
  `preprocess_script` → `preprocess_source` (it now serves both the
  script and snippet paths), `capture_pfc_console` →
  `capture_engine_console`, `split_pfc_commands` →
  `split_engine_commands`. The engine-registered callback names are
  deliberately unchanged (renaming them would strand mid-session
  self-upgrades with stale registrations).

## [0.4.3] - 2026-07-09

### Fixed
- Self-upgrade no longer dies on GUI consoles whose stdout lacks `isatty`.
  Some product consoles replace `sys.stdout`/`sys.stderr` with a channel
  object (Itasca's `RedirectstdChannel`) that has `write`/`flush` but no
  `isatty`; pip's download progress bar calls `file.isatty()`
  unconditionally during construction, so the wheel download aborted with
  `AttributeError` and every start fell back to the installed version —
  affected installs could never receive updates automatically. Verified
  against stock pip 9.0.1 (PFC 6/7 embedded Python) and pip 21.3.1 alike.
  `_run_pip` now wraps both streams in a delegating proxy answering
  `isatty() → False`, installed before pip is first imported (pip binds
  `sys.stdout` onto its progress-bar classes at import time), and the
  `--progress-bar off` guard keys on pip ≥ 10 instead of Python ≥ 3.10.

  **Installs already hit by this cannot self-upgrade past it** — run the
  manual upgrade once, in a terminal, with the product's own Python:
  `"<product dir>\exe64\python36\python.exe" -m pip install --user -U itasca-mcp-bridge`
- The upgrade-failed hint suggested a bare `python -m pip install ...`,
  which a terminal resolves to the *system* Python — the package landed in
  the wrong site-packages and the bridge inside the product never saw it.
  The hint now prints the full quoted path of the product's bundled
  interpreter (derived from `sys.exec_prefix`; `sys.executable` is
  unusable — inside the GUI it is the product binary itself, e.g.
  `pfc2d700_gui.exe`) and explicitly warns against plain `python`.

## [0.4.2] - 2026-07-07

### Documentation
- READMEs (English and Simplified Chinese) rewritten for the stdlib
  HTTP + SSE transport that shipped in 0.4.0: architecture diagram,
  protocol table (`POST /<command>` requests, `GET /events` SSE
  doorbell), install steps, and requirements no longer describe the
  removed WebSocket stack. This release carries no code change; it
  exists to refresh the PyPI project page, which still rendered the
  WebSocket-era README from the 0.4.1 tag.

## [0.4.1] - 2026-06-28

### Fixed
- Stop a client disconnect from dumping a traceback into the engine GUI
  console. On Windows a dropped socket is aborted with **WinError 10053**,
  raised as `ConnectionAbortedError` — a sibling of the already-caught
  `ConnectionResetError`/`BrokenPipeError` under the common `ConnectionError`
  base. Both socket-write paths — the long-lived `GET /events` SSE
  keepalive/data push and the `POST` response writer — caught only the latter
  two, so a client dropping mid-write (e.g. an MCP client's SSE stream
  reconnecting) surfaced as a noisy `ConnectionAbortedError` traceback in the
  product console instead of the connection deregistering quietly. Both
  handlers now widen to the `ConnectionError` base, which covers all three.

## [0.4.0] - 2026-06-27

### Changed
- **Transport replaced: WebSocket → stdlib HTTP + SSE.** The bridge no longer
  speaks WebSocket. Requests are served over plain HTTP (`http.server`), and
  the server→client doorbell rides a payload-free Server-Sent Events stream.
  This removes the last third-party-shaped surface from the transport: the
  bridge stays stdlib-only and installs cleanly into any ITASCA embedded
  Python (3.6+) with no pins. The request → execute → result interaction is
  simple enough that a transport library's duplex/heartbeat/version machinery
  was never load-bearing here.

  **Breaking:** clients must speak HTTP + SSE. Use `itasca-mcp` (>= 0.6.0) or
  another `*-mcp` client built against this transport. WebSocket-era clients
  (`pfc-mcp` <= 0.5.0, `flac-mcp` <= 0.5.1) cannot connect until upgraded —
  and because the bridge self-upgrades from PyPI on every start, an existing
  install will pick this up automatically, so those clients must upgrade in
  step.

## [0.3.0] - 2026-06-18

### Fixed
- Close the L2 logging-lock deadlock. The timeout terminator async-raises
  `BridgeTimeout` into the snippet thread via `PyThreadState_SetAsyncExc`,
  which fires at an arbitrary bytecode edge. CPython's stdlib
  `logging.Handler.handle` (Python <= 3.10, which every PFC ships) guards
  `emit` with `acquire()/try/finally`, leaving a bytecode gap between the
  lock acquisition and the `finally` registration; an injection landing
  there orphaned the handler `RLock` and froze the background WebSocket
  thread until the product was restarted. The root file/stream handlers now
  use the gap-free `with self.lock` form — the same fix CPython itself
  shipped in 3.11. Measured on PFC3D 9 / Python 3.10.5: the old form leaked
  the lock in 21/120 injections, the fix in 0/120.

### Removed
- The deprecated `pfc_task` message-type alias (use `execute_task`) and the
  `PFC_MCP_PIP_INDEX_URL` legacy environment variable (use
  `ITASCA_MCP_PIP_INDEX_URL`). The frozen `pfc-mcp-bridge` package was the
  only consumer of this back-compatibility surface.

### Changed
- Generalized residual PFC-specific wording in comments, docstrings, and
  user-facing messages to ITASCA/product, reflecting the bridge's
  product-neutral scope. Version-specific facts and PFC-named identifiers
  are unchanged.

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
