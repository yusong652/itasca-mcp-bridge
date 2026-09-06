"""
Interrupt Manager - Global interrupt callback registration and task interrupt management.

This module provides a mechanism to interrupt long-running ITASCA simulations
via itasca.set_callback(). The callback checks interrupt flags each cycle
and raises InterruptedError to stop execution.

Key constraints:
- ITASCA callback looks up function by name in __main__ namespace
- Functions defined in exec() context are not visible to ITASCA
- Must register global function in __main__ before use

Architecture:
- HTTP request thread: calls request_interrupt(task_id) when user cancels
- Main thread: script execution with set_current_task()/clear_current_task()
- ITASCA callback: _pfc_interrupt_check() checks flag each cycle

Python 3.6 compatible implementation.
"""

import re
import threading
import logging
from typing import Any, Optional

from .positions import INTERRUPT_CALLBACK_POSITION, EXECUTOR_CALLBACK_POSITION, register_cycle_callback
from ..utils.command_log import live_capture_paused
from ..utils.program_call import expand_program_call

# Module logger
logger = logging.getLogger("itasca-mcp-bridge")


def _in_cycle_callback():
    # type: () -> bool
    """True while an execute_code snippet is running in the cycle callback."""
    try:
        from .cycle_executor import in_cycle_callback
    except ImportError:
        return False
    return in_cycle_callback()


# =============================================================================
# Cycling Resume After a Callback Command Error (PFC 6)
# =============================================================================
#
# On PFC 6 any engine command that fails inside a cycle callback makes the
# engine abort the cycling that callback interrupted: the task's running
# `model cycle` / `model solve` returns at that cycle with no "limit met"
# line, the script's itasca.command() returns normally and the task ends
# `completed` with the cycles silently missing (a `model solve` looks like
# it converged). Bridge 0.4.4 removed the bridge's own trigger for this
# (the `show-message` keyword); a mistyped command sent through
# execute_code while the task cycles takes the same path, and nothing on
# the snippet side prevents it -- the abort happens whether or not the
# snippet catches the exception. Verified live on PFC3D 6.00.030,
# 2026-09-06; PFC 7.00.161 keeps cycling and is unaffected.
#
# The wrapped itasca.command sees both sides: the snippet's failing
# command (inside the callback, depth > 0) and the task's cycling command
# (the outer call, which the callback is nested in). The inner failure is
# recorded with the engine's cycle count; when the outer cycling command
# returns at exactly that cycle, the engine aborted it and the wrapper
# re-issues the remainder. On engines that keep cycling the outer command
# returns later and nothing is re-issued.

# Monotonic count of engine command failures raised inside a cycle
# callback, and the cycle count at the latest one. The outer cycling
# command compares the count before/after its engine call, so the
# snippet's later commands (its own capture control, or a command it
# runs after catching the failure) cannot mask the failure.
_callback_failure_seq = 0
_callback_failure_cycle = None  # type: Optional[int]
_callback_failure_command = ""

# `model cycle <n> [rest]` -- the count is the only relative limit.
_MODEL_CYCLE_RE = re.compile(r"^\s*mo\w*\s+cy\w*\s+(\d+)(.*)$", re.IGNORECASE | re.DOTALL)
# `model solve ...`
_MODEL_SOLVE_RE = re.compile(r"^\s*mo\w*\s+so\w*(?:\s|$)", re.IGNORECASE)
# A solve's relative `cycles <i>` limit ("during this solve operation"),
# in any process scope. `cycles-total` is absolute and must not match:
# the hyphen breaks the required whitespace after the keyword letters.
_SOLVE_CYCLES_RE = re.compile(r"(?<![\w-])(cy[a-z]*)(\s+)(\d+)", re.IGNORECASE)
# Relative limits the bridge cannot rewrite from the cycle count (`time`,
# `clock`) and the stateful two-step `elastic` solve. `time-total` is
# absolute and does not match (same hyphen rule).
_SOLVE_NOT_RESUMABLE_RE = re.compile(r"(?<![\w-])(ti[a-z]*|cl[a-z]*|el[a-z]*)(?![\w-])", re.IGNORECASE)
# Bound on resumes per outer command; each needs a fresh callback failure.
_MAX_RESUMES = 100


def _engine_cycle(itasca_module):
    # type: (Any) -> Optional[int]
    """The engine's cycle count, or None when the module cannot report it."""
    cycle = getattr(itasca_module, "cycle", None)
    if cycle is None:
        return None
    try:
        return int(cycle())
    except Exception:
        return None


def _note_callback_command_failure(itasca_module, cmd, exc):
    # type: (Any, str, BaseException) -> None
    """Record an engine command failure raised inside the cycle callback
    and tell the snippet's caller what may have happened to the task."""
    global _callback_failure_seq, _callback_failure_cycle, _callback_failure_command
    _callback_failure_seq += 1
    _callback_failure_cycle = _engine_cycle(itasca_module)
    _callback_failure_command = cmd.strip().split("\n", 1)[0]
    logger.warning(
        "Engine command failed inside the cycle callback at cycle %s: %s",
        _callback_failure_cycle, _callback_failure_command,
    )
    try:
        note = (
            "\n    [bridge] This command failed inside the cycle callback of a "
            "running task. Engines that abort cycling on a callback command "
            "error (PFC 6) stop the task's current model cycle/solve here; "
            "the bridge re-issues the remainder and notes it in the task log."
        )
        if exc.args and isinstance(exc.args[0], str):
            exc.args = (exc.args[0] + note,) + tuple(exc.args[1:])
    except Exception:
        pass


def _resume_command(cmd, cycles_done):
    # type: (str, int) -> Optional[str]
    """
    The command that continues ``cmd`` after ``cycles_done`` of its cycles.

    Returns None when ``cmd`` is not a cycling command, when its relative
    limits are already met, or when it carries limits the bridge cannot
    rewrite from the cycle count (``time``, ``clock``, ``elastic``).
    """
    m = _MODEL_CYCLE_RE.match(cmd)
    if m:
        remaining = int(m.group(1)) - cycles_done
        if remaining <= 0:
            return None
        head = cmd[: m.start(1)]
        return "{}{}{}".format(head, remaining, m.group(2))
    if not _MODEL_SOLVE_RE.match(cmd):
        return None
    if _SOLVE_NOT_RESUMABLE_RE.search(cmd):
        return None
    exhausted = [False]

    def _rewrite(match):
        remaining = int(match.group(3)) - cycles_done
        if remaining <= 0:
            exhausted[0] = True
            remaining = 0
        return "{}{}{}".format(match.group(1), match.group(2), remaining)

    rewritten = _SOLVE_CYCLES_RE.sub(_rewrite, cmd)
    if exhausted[0]:
        return None
    return rewritten


def _echo_task_log(line):
    # type: (str) -> None
    """Print a bridge line into the task log, outside the live capture
    session (PFC 6 would log the console copy and deliver it twice)."""
    try:
        with live_capture_paused():
            print(line)
            _sys_stdout_flush()
    except Exception:
        pass


def _sys_stdout_flush():
    # type: () -> None
    import sys
    sys.stdout.flush()


def _resume_if_aborted(itasca_module, run, cmd, start_cycle, seq_before):
    # type: (Any, Any, str, Optional[int], int) -> None
    """
    Re-issue the remainder of a cycling command the engine aborted.

    Called after the outer (task-level) ``cmd`` returned. A callback
    command failure recorded during it, with the engine now sitting at
    that failure's cycle, means the engine aborted ``cmd`` there.
    Loops, since the re-issued command can be aborted the same way. A
    pending task interrupt takes precedence at every step.
    """
    if _in_cycle_callback():
        return
    if not (_MODEL_CYCLE_RE.match(cmd) or _MODEL_SOLVE_RE.match(cmd)):
        return
    resumes = 0
    while _callback_failure_seq > seq_before and resumes < _MAX_RESUMES:
        seq_before = _callback_failure_seq
        now = _engine_cycle(itasca_module)
        if start_cycle is None or now is None or now != _callback_failure_cycle:
            # Cycle count unavailable, or the engine kept cycling past
            # the failure (PFC 7+): nothing was aborted.
            return
        first = cmd.strip().split("\n", 1)[0]
        resume = _resume_command(cmd, now - start_cycle)
        if resume is None:
            _echo_task_log(
                "[bridge] cycling stopped at cycle {}: the engine aborted `{}` "
                "after an execute_code command failed in the cycle callback "
                "(`{}`). Not resumed: its limits are met, or it carries "
                "time/clock/elastic limits the bridge cannot rewrite; "
                "re-issue it if the run is short.".format(now, first, _callback_failure_command)
            )
            return
        _echo_task_log(
            "[bridge] cycling stopped at cycle {}: the engine aborted `{}` "
            "after an execute_code command failed in the cycle callback "
            "(`{}`); resuming with `{}`.".format(now, first, _callback_failure_command, resume)
        )
        resumes += 1
        _pfc_interrupt_check()
        run(resume)
        _pfc_interrupt_check()
        cmd = resume
        start_cycle = now


# =============================================================================
# Interrupt Flag Management (Thread-safe)
# =============================================================================

# Interrupt flags: set of task IDs with pending interrupt requests
_interrupt_flags = set()  # type: set
_flags_lock = threading.Lock()


def request_interrupt(task_id):
    # type: (str) -> bool
    """
    Request interrupt for a running task.

    Called from HTTP request handler thread when user requests cancellation.

    Args:
        task_id: Task ID to interrupt

    Returns:
        bool: True if request registered, False if task_id empty
    """
    if not task_id:
        return False

    with _flags_lock:
        _interrupt_flags.add(task_id)

    logger.info("Interrupt requested: %s", task_id)
    return True


def check_interrupt(task_id):
    # type: (str) -> bool
    """
    Check if interrupt requested for a task.

    Called from ITASCA callback during cycle execution.
    Must be fast - runs every cycle during simulation.

    Args:
        task_id: Task ID to check

    Returns:
        bool: True if interrupt requested, False otherwise
    """
    # Fast path: set membership is atomic in CPython due to GIL
    return task_id in _interrupt_flags


def clear_interrupt(task_id):
    # type: (str) -> None
    """
    Clear interrupt flag for a task.

    Called after task completion or interruption to clean up.

    Args:
        task_id: Task ID to clear
    """
    with _flags_lock:
        if task_id in _interrupt_flags:
            _interrupt_flags.discard(task_id)
            logger.debug("Cleared interrupt flag: %s", task_id)


# =============================================================================
# Current Task Tracking (Main Thread Only)
# =============================================================================

# Current task being executed in main thread
_current_task_id = None  # type: Optional[str]


def set_current_task(task_id):
    # type: (str) -> None
    """
    Set current task ID before script execution.

    Called in main thread before exec() to enable interrupt checking.

    Args:
        task_id: Task ID being executed
    """
    global _current_task_id
    _current_task_id = task_id
    logger.debug("Current task set: %s", task_id)


def clear_current_task():
    # type: () -> None
    """
    Clear current task ID after script execution.

    Called in main thread after exec() completes (success or failure).
    """
    global _current_task_id
    prev_task = _current_task_id
    _current_task_id = None
    if prev_task:
        logger.debug("Current task cleared: %s", prev_task)


def peek_current_task():
    # type: () -> Optional[str]
    """
    Return the currently-set task/request id, or None.

    Used by ``run_snippet`` to implement a save-and-restore pattern:
    when an execute_code snippet runs *inside* a running task's
    cycle-callback gap, we must not clobber the outer task's
    ``_current_task_id`` on the way out — that would silently disable
    ``interrupt_task`` for the still-running task (its ITASCA callback
    checks ``_current_task_id`` and a cleared value means "no task to
    interrupt").
    """
    return _current_task_id


# =============================================================================
# Exec Thread Registry (for execute_code L2 async-exc cancellation)
# =============================================================================

# Thread registry: request_id -> threading.get_ident() of the worker
# currently running that request. Populated at the top of run_snippet,
# cleaned in its finally. Read by the execute_code timeout handler to
# target ``PyThreadState_SetAsyncExc``.
_exec_thread_ids = {}  # type: dict
_exec_thread_lock = threading.Lock()


def register_exec_thread(request_id, thread_id):
    # type: (str, int) -> None
    """
    Record which OS thread is currently running ``request_id``.

    As a cheap leak-defence, scrubs any pre-existing entries whose
    recorded thread is no longer alive. Leaks would only occur if
    ``run_snippet`` exited through a path that skipped its ``finally``
    - vanishingly rare, but the scrub keeps the registry from growing
    unboundedly over a long-lived bridge session.
    """
    with _exec_thread_lock:
        if _exec_thread_ids:
            alive = set(t.ident for t in threading.enumerate() if t.is_alive())
            stale = [rid for rid, tid in _exec_thread_ids.items() if tid not in alive]
            for rid in stale:
                _exec_thread_ids.pop(rid, None)
        _exec_thread_ids[request_id] = thread_id


def unregister_exec_thread(request_id):
    # type: (str) -> None
    """Drop the thread record for ``request_id``. Idempotent."""
    with _exec_thread_lock:
        _exec_thread_ids.pop(request_id, None)


def get_exec_thread(request_id):
    # type: (str) -> Optional[int]
    """Return the recorded thread id for ``request_id``, or None."""
    with _exec_thread_lock:
        return _exec_thread_ids.get(request_id)


# =============================================================================
# Global Interrupt Check Function (Registered with ITASCA)
# =============================================================================

def _pfc_interrupt_check():
    # type: () -> None
    """
    Global function called by ITASCA each cycle.

    Checks if current task has interrupt request and raises InterruptedError.
    This function is injected into __main__ namespace and registered with ITASCA.

    Note:
        Must be fast - runs every cycle during simulation.
        Only checks current task's flag, no locking needed (GIL atomic read).

    Raises:
        InterruptedError: If current task has pending interrupt request
    """
    task_id = _current_task_id
    if task_id and check_interrupt(task_id):
        logger.info("Interrupting task: %s", task_id)
        raise InterruptedError("Task {} interrupted by user".format(task_id))


# =============================================================================
# ITASCA Callback Registration
# =============================================================================

# The itasca module handed to register_interrupt_callback(); kept so
# ensure_cycle_callbacks() can re-register without threading the module
# through every execution entry point.
_itasca_module = None  # type: Any
_interrupt_position = INTERRUPT_CALLBACK_POSITION  # type: float


def _re_register_callback(itasca_module, position=INTERRUPT_CALLBACK_POSITION):
    # type: (Any, float) -> None
    """
    Re-register interrupt callback with ITASCA.

    Called after model new/restore commands which clear ITASCA's callback registry.
    Also re-registers executor callback if it was registered.
    """
    # Never mutate the registry from inside a cycle callback: the engine is
    # mid-cycle and iterating it, and remove_callback/set_callback there
    # hard-crashes the process (PFC3D 6.00.030 exits on the spot; verified
    # 2026-09-06 with `model new` sent through execute_code while a task was
    # cycling, which reaches this function through _wrapped_command's
    # model-reset repair hook). Skipping is safe: the wiped registry is
    # repaired by ensure_cycle_callbacks() at the next execution entry point,
    # and the cycle the reset interrupted is finished either way.
    if _in_cycle_callback():
        logger.warning(
            "Model reset inside a cycle callback: deferring callback "
            "re-registration to the next execution entry point"
        )
        return

    import __main__
    __main__._pfc_interrupt_check = _pfc_interrupt_check  # type: ignore[attr-defined]
    register_cycle_callback(itasca_module, "_pfc_interrupt_check", position)
    logger.debug("Interrupt callback (re)registered")

    # Also re-register executor callback if it was registered
    try:
        from .cycle_executor import _pfc_executor_callback, is_executor_callback_registered
        if is_executor_callback_registered():
            __main__._pfc_executor_callback = _pfc_executor_callback  # type: ignore[attr-defined]
            register_cycle_callback(itasca_module, "_pfc_executor_callback", EXECUTOR_CALLBACK_POSITION)
            logger.debug("Executor callback (re)registered")
    except ImportError:
        pass  # cycle_executor not available


def ensure_cycle_callbacks():
    # type: () -> bool
    """
    Unconditionally (re)register the bridge's cycle callbacks.

    Called at every execution entry point that reaches the engine from
    the idle main-thread queue (execute_code queue path, execute_task
    scripts), right before user code runs.

    Why unconditional: the engine holds the GIL for the whole duration
    of a C-level ``itasca.command``; the only windows where the HTTP
    thread, status polls, ``execute_code`` interleaving and interrupt
    can run are the bridge's own Python cycle callbacks. ``model new`` /
    ``model restore`` wipe the engine's cycle-callback registry, and the
    ``_wrapped_command`` repair hook only sees resets issued through the
    Python ``itasca.command`` — a reset typed in the GUI console, run
    from the File menu, or executed inside a ``program call`` script
    leaves the registry empty with nothing to repair it. The next
    ``model cycle`` then wedges the whole bridge for its duration.
    There is no engine API to list cycle callbacks, so the entry points
    cannot check; they re-register (remove-then-set, idempotent on every
    engine version) at a cost of two C calls per execution.

    Must not be called from inside a cycle callback (the callback-path
    ``execute_code`` route): the registry is live there by definition,
    and mutating it mid-cycle is not something the engine promises to
    tolerate.

    Returns:
        bool: True if callbacks were (re)registered, False if the
        interrupt callback was never registered (bridge not started)
        or the engine rejected the registration.
    """
    if _itasca_module is None:
        return False
    try:
        _re_register_callback(_itasca_module, _interrupt_position)
        return True
    except Exception as e:
        logger.warning("Cycle callback re-registration failed: %s", e)
        return False


# Commands that clear ITASCA's callback registry
_MODEL_RESET_COMMANDS = ("model new", "model restore")

# The bridge's own console-capture control commands (``utils.command_log``
# wraps every user command in ``program log on`` / ``program log off`` and
# switches files with ``program log-file``). These must never become
# interrupt points: they run in cleanup paths — the ``program log off`` in
# ``_patched``'s ``finally`` executes while the task's interrupt flag is
# still set — and raising there would skip the log-off, leaving the engine's
# log session open and dropping the interrupted command's captured output.
# The next command then inherits a live session: on PFC 6 its Python prints
# are logged and delivered twice, and ``program log on truncate`` does not
# truncate, so a stale banner leads the next task's log.
_LOG_CONTROL_RE = re.compile(r"^\s*pro\w*\s+log\b", re.IGNORECASE)


def register_interrupt_callback(itasca_module, position=INTERRUPT_CALLBACK_POSITION):
    # type: (Any, float) -> bool
    """
    Register interrupt callback with ITASCA.

    Must be called once during server startup. This function:
    1. Injects _pfc_interrupt_check into __main__ namespace
    2. Registers callback with itasca.set_callback()
    3. Wraps itasca.command to auto-re-register after model new/restore

    Args:
        itasca_module: The itasca module (imported in ITASCA environment)
        position: Cycle point for set_callback
            (default: INTERRUPT_CALLBACK_POSITION; see signals.positions).
            - Negative values: before cycle starts
            - 0.0: timestep calculation
            - 10.0: kinematics
            - 20.0: time accumulation
            - 45.0+: after cycle completion

    Returns:
        bool: True if registered successfully
    """
    global _itasca_module, _interrupt_position
    try:
        # Inject function into __main__ namespace (required for ITASCA lookup)
        import __main__
        __main__._pfc_interrupt_check = _pfc_interrupt_check  # type: ignore[attr-defined]

        # Register with ITASCA (remove-before-register: idempotent across versions;
        # PFC 6.0 set_callback is strict and model restore does not clear the
        # registry, so a plain set_callback would collide. See register_cycle_callback.)
        register_cycle_callback(itasca_module, "_pfc_interrupt_check", position)

        # Wrap itasca.command to:
        #   1. Keep _pfc_interrupt_check visible in the current __main__ (some
        #      contexts like IPython %run temporarily replace sys.modules['__main__'],
        #      which hides the attribute injected at startup and makes ITASCA's
        #      callback lookup fail with "function is not defined").
        #   2. Auto-re-register callback after model new/restore (those commands
        #      clear ITASCA's internal callback registry).
        _original_command = itasca_module.command
        import sys as _sys

        def _wrapped_command(cmd):
            # Ensure callbacks are available in whichever module is currently __main__.
            # Idempotent; costs two attr ops per callback when already injected.
            main_mod = _sys.modules.get('__main__')
            if main_mod is not None:
                if getattr(main_mod, '_pfc_interrupt_check', None) is not _pfc_interrupt_check:
                    main_mod._pfc_interrupt_check = _pfc_interrupt_check  # type: ignore[attr-defined]
                # Also keep executor callback visible if it's been registered
                try:
                    from .cycle_executor import _pfc_executor_callback, is_executor_callback_registered
                    if is_executor_callback_registered() and getattr(main_mod, '_pfc_executor_callback', None) is not _pfc_executor_callback:
                        main_mod._pfc_executor_callback = _pfc_executor_callback  # type: ignore[attr-defined]
                except ImportError:
                    pass

            # `program call '<file>'` would run the whole file as one C
            # call; a `model new` inside it wipes the callback registry
            # with no boundary to repair at. Feed the file's commands
            # through this wrapper one at a time instead (each gets the
            # repair below and nested expansion). Falls through to the
            # engine for any form the expander cannot honor exactly.
            if expand_program_call(cmd, _wrapped_command):
                return None

            # Every command boundary is an interrupt point, not only
            # cycle callbacks. The engine does not always propagate the
            # callback's InterruptedError: PFC 6 swallows it when the
            # cycling was started from a FISH `command` block, prints
            # the traceback to the console and carries on with the next
            # command as if nothing happened (verified live on 6.00.030,
            # 2026-09-06). The task's flag is still set, so honor it here
            # — before starting a new command, and after this one returns
            # in case it was the last. The bridge's own capture-control
            # commands are exempt (see _LOG_CONTROL_RE): they run in
            # cleanup paths that must complete even while an interrupt
            # is pending.
            checked = _LOG_CONTROL_RE.match(cmd) is None
            if checked:
                _pfc_interrupt_check()
            seq_before = _callback_failure_seq
            start_cycle = _engine_cycle(itasca_module) if checked else None
            try:
                result = _original_command(cmd)
            except Exception as e:
                # A failing command inside the cycle callback makes PFC 6
                # abort the cycling it interrupted (see the resume section
                # above); record it for the outer cycling command.
                if _in_cycle_callback() and not isinstance(e, InterruptedError):
                    _note_callback_command_failure(itasca_module, cmd, e)
                raise
            if checked:
                _pfc_interrupt_check()
                _resume_if_aborted(itasca_module, _original_command, cmd, start_cycle, seq_before)
            # Check if command resets model (clears callback registry).
            # Scan every line: a multi-line batch can carry the reset
            # command mid-string (e.g. via the unsplit execute_code
            # path), where a whole-string startswith would miss it and
            # leave the registry dead until some later first-line match.
            for line in cmd.split("\n"):
                line_lower = line.strip().lower()
                if any(line_lower.startswith(rc) for rc in _MODEL_RESET_COMMANDS):
                    _re_register_callback(itasca_module, position)
                    break
            return result

        itasca_module.command = _wrapped_command

        # Remember the module so ensure_cycle_callbacks() can repair the
        # registry from the execution entry points.
        _itasca_module = itasca_module
        _interrupt_position = position

        logger.info("Interrupt callback registered (position=%.1f)", position)
        return True

    except Exception as e:
        logger.error("Failed to register interrupt callback: %s", e)
        return False
