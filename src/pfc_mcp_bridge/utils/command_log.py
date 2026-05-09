"""
Capture PFC console output from itasca.command() calls.

The Python SDK's itasca.command() returns nothing — PFC sends command output
(tables, list dumps, summaries) to its own console pane, invisible to Python.
This module monkey-patches itasca.command within a scoped block so each call
is wrapped with PFC's `program log on/off` commands. Captured output is
written to a caller-supplied sink (typically the active sys.stdout, which
already routes through TeeBuffer to the task's FileBuffer), preserving exact
interleaving with Python print() statements.

Why per-call (vs. one log session per snippet):

PFC opens the log file with exclusive write share mode while logging is on.
On Windows, Python cannot read the file until `program log off` releases the
lock. Per-call on/off is the only way to read incrementally; the per-pair
overhead measured ~1.4–1.8 ms (negligible for typical snippet sizes).

Python 3.6 compatible.
"""

import logging
import os
import uuid
from contextlib import contextmanager

logger = logging.getLogger("PFC-Server")


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


@contextmanager
def capture_pfc_console(stdout_sink, log_dir):
    # type: (object, str) -> object
    """
    Within this block, monkey-patch itasca.command() so each call's PFC
    console output is captured and written to `stdout_sink` immediately
    after the call returns.

    Args:
        stdout_sink: file-like object with .write(str) — typically the active
                     sys.stdout (TeeBuffer → FileBuffer in script execution).
        log_dir: directory for the temporary PFC log file (created if missing).

    Effect on per-command behavior:
        Each user `itasca.command(cmd)` becomes 3 PFC commands:
            program log on truncate show-message off
            <cmd>
            program log off
        The per-cmd output is then read from disk and written to stdout_sink.

    Restoration:
        itasca.command is always restored on exit (including exceptions).
        Errors raised by user commands propagate; partial output captured
        before the error is still flushed to stdout_sink.
    """
    import itasca

    if log_dir and not os.path.exists(log_dir):
        try:
            os.makedirs(log_dir)
        except OSError:
            pass

    log_path = os.path.join(log_dir, f"cmdtmp_{uuid.uuid4().hex[:8]}.log")
    log_path_pfc = log_path.replace("\\", "/")

    orig_command = itasca.command

    def _read_and_strip():
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                return _strip_footer(f.read())
        except OSError:
            return ""

    def patched(cmd):
        orig_command("program log on truncate show-message off")
        try:
            orig_command(cmd)
        finally:
            orig_command("program log off")
            chunk = _read_and_strip()
            if chunk:
                try:
                    stdout_sink.write(chunk)
                except Exception as e:
                    logger.warning("capture_pfc_console: stdout write failed: %s", e)

    # Set the log file path once; only on/off toggles per call.
    orig_command(f"program log-file '{log_path_pfc}'")

    itasca.command = patched
    try:
        yield
    finally:
        itasca.command = orig_command
        try:
            if os.path.exists(log_path):
                os.remove(log_path)
        except OSError:
            pass
