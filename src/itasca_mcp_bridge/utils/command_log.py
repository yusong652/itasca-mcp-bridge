"""
Capture ITASCA console output from itasca.command() calls.

The Python SDK's itasca.command() returns nothing — ITASCA sends command output
(tables, list dumps, summaries) to its own console pane, invisible to Python.
This module monkey-patches itasca.command within a scoped block so each call
is wrapped with ITASCA's `program log on/off` commands. Captured output is
written to a caller-supplied sink (typically the active sys.stdout, which
already routes through TeeBuffer to the task's FileBuffer), preserving exact
interleaving with Python print() statements.

Why per-call (vs. one log session per snippet):

ITASCA opens the log file with exclusive write share mode while logging is on.
On Windows, Python cannot read the file until `program log off` releases the
lock. Per-call on/off is the only way to read incrementally; the per-pair
overhead measured ~1.4–1.8 ms (negligible for typical snippet sizes).

Why one patch with a context stack (vs. one patch per scope):

Capture scopes nest: a snippet arriving through the cycle-gap callback while
a task script is cycling opens a second scope inside the task's. With one
patch per scope, the inner scope grabs the outer *wrapper* as its "original",
so even its own log-control commands get re-wrapped in another on/off
triple — and that extra `program log on truncate` wipes the inner scope's
log file before it is read, silently emptying the capture (#28). The patch
is therefore installed once, module-owned, over the real SDK function; each
scope pushes its log file + sink onto a stack and the wrapper always targets
the innermost scope. Control commands never pass through another wrapper.

No locking on the stack: every capture scope runs on ITASCA's main thread
(task scripts via MainThreadExecutor, snippets via the idle queue or the
cycle-gap callback), so scopes strictly nest and never race.

Python 3.6 compatible.
"""

import logging
import os
import uuid
from contextlib import contextmanager

logger = logging.getLogger("itasca-mcp-bridge")

# Innermost capture scope last. Non-empty exactly while the patch is
# installed and `_orig_command` holds the real itasca.command.
_stack = []
_orig_command = None


class _CaptureContext(object):
    __slots__ = ("log_path", "log_path_pfc", "sink", "in_command")

    def __init__(self, log_path, sink):
        # type: (str, object) -> None
        self.log_path = log_path
        self.log_path_pfc = log_path.replace("\\", "/")
        self.sink = sink
        # True while this scope's wrapped user command is executing —
        # i.e. its log session is live and an inner scope entering now
        # is interrupting it (and must resume it on exit).
        self.in_command = False


def _strip_footer(content):
    # type: (str) -> str
    """Strip the trailing `program log off` echo + 3-line banner footer."""
    if not content:
        return content
    lines = content.splitlines(keepends=True)
    for i in range(len(lines) - 1, -1, -1):
        if "program log off" in lines[i]:
            return "".join(lines[:i])
    return content


def _read_and_strip(log_path):
    # type: (str) -> str
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            return _strip_footer(f.read())
    except OSError:
        return ""


def _in_cycle_callback():
    # type: () -> bool
    """True when the innermost scope is nested inside another scope's
    live command — i.e. we are executing in the cycle-gap callback of a
    task that is currently inside `model cycle`."""
    return any(outer.in_command for outer in _stack[:-1])


def _patched(cmd):
    # type: (str) -> None
    ctx = _stack[-1]
    # `show-message off` suppresses the logging banners in the GUI
    # console, but PFC 6 does not know the keyword and complains
    # ("Unused extra parameter") — and a command complaint raised inside
    # the cycle callback makes PFC 6 silently abort the outer task's
    # running `model cycle` (verified live on 6.00.030, 2026-08-05;
    # PFC 7 accepts the keyword). Drop it whenever this command executes
    # in the cycle callback: the banners are the lesser evil there.
    if _in_cycle_callback():
        _orig_command("program log on truncate")
    else:
        _orig_command("program log on truncate show-message off")
    ctx.in_command = True
    try:
        _orig_command(cmd)
    finally:
        ctx.in_command = False
        _orig_command("program log off")
        chunk = _read_and_strip(ctx.log_path)
        if chunk:
            try:
                ctx.sink.write(chunk)
            except Exception as e:
                logger.warning("capture_pfc_console: stdout write failed: %s", e)


@contextmanager
def capture_pfc_console(stdout_sink, log_dir):
    # type: (object, str) -> object
    """
    Within this block, monkey-patch itasca.command() so each call's ITASCA
    console output is captured and written to `stdout_sink` immediately
    after the call returns.

    Args:
        stdout_sink: file-like object with .write(str) — typically the active
                     sys.stdout (TeeBuffer → FileBuffer in script execution).
        log_dir: directory for the temporary ITASCA log file (created if missing).

    Effect on per-command behavior:
        Each user `itasca.command(cmd)` becomes 3 ITASCA commands:
            program log on truncate show-message off
            <cmd>
            program log off
        The per-cmd output is then read from disk and written to stdout_sink.

    Nesting:
        Scopes may nest (snippet inside a cycling task). The inner scope
        captures into its own file and sink; on exit the outer scope's log
        file is restored, and if the inner scope interrupted a live outer
        log session (task mid-command), that session is resumed in append
        mode so the outer command's remaining output is still captured.

    Restoration:
        itasca.command is always restored on exit of the outermost scope
        (including exceptions). Errors raised by user commands propagate;
        partial output captured before the error is still flushed to
        stdout_sink.
    """
    global _orig_command
    import itasca

    # Resolve to an absolute path: the ITASCA command interpreter resolves
    # relative file paths against its OWN working directory, which is not
    # guaranteed to equal Python's os.getcwd() (e.g. headless Linux console
    # leaves Python at the launch dir but ITASCA at /tmp). A relative log_path
    # would then be written by ITASCA and read back by Python at two different
    # locations, yielding empty captures or a hard write error.
    log_dir = os.path.abspath(log_dir) if log_dir else log_dir

    if log_dir and not os.path.exists(log_dir):
        try:
            os.makedirs(log_dir)
        except OSError:
            pass

    ctx = _CaptureContext(
        os.path.join(log_dir, f"cmdtmp_{uuid.uuid4().hex[:8]}.log"), stdout_sink
    )

    if not _stack:
        _orig_command = itasca.command

    # Point ITASCA's log file at this scope's file. Entering an inner scope
    # while the outer one is mid-command ends the outer's live log session;
    # it is resumed on exit below.
    try:
        _orig_command(f"program log-file '{ctx.log_path_pfc}'")
    except BaseException:
        if not _stack:
            _orig_command = None
        raise

    _stack.append(ctx)
    itasca.command = _patched
    try:
        yield
    finally:
        _stack.pop()
        if _stack:
            outer = _stack[-1]
            try:
                _orig_command(f"program log-file '{outer.log_path_pfc}'")
                if outer.in_command:
                    # Resume the outer session in append mode (no truncate):
                    # its pre-interruption output is still in the file.
                    # No `show-message off` here — this command always runs
                    # inside the cycle callback (see _patched above for the
                    # PFC 6 abort it would otherwise trigger).
                    _orig_command("program log on")
            except Exception as e:
                logger.warning(
                    "capture_pfc_console: failed to restore outer log session: %s",
                    e,
                )
        else:
            itasca.command = _orig_command
            _orig_command = None
        try:
            if os.path.exists(ctx.log_path):
                os.remove(ctx.log_path)
        except OSError:
            pass
