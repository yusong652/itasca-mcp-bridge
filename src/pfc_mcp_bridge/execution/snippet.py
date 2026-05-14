"""
Snippet executor for pfc_execute_code.

Both execute_code paths — the idle MainThreadExecutor queue path and the
busy cycle-gap callback path — funnel through ``run_snippet`` so the
caller sees identical behaviour regardless of which scheduler delivers
the code to PFC's main thread.

Distinct from ``execution.script.ScriptRunner``, which serves tracked
pfc_task scripts (file-backed, registered with TaskManager).

Two cancellation paths land here:

* L1 (interrupt flag): the PFC interrupt callback sets a flag that
  ``_pfc_interrupt_check`` reads each cycle, raising ``InterruptedError``
  at the next cycle boundary. Pairs with ``set_current_task`` /
  ``clear_interrupt`` below.
* L2 (async exc): the timeout handler injects ``BridgeTimeout`` into
  this snippet's thread via ``PyThreadState_SetAsyncExc``. Pairs with
  ``register_exec_thread`` / ``unregister_exec_thread``.

``except BridgeTimeout`` is load-bearing: ``BridgeTimeout`` inherits
``BaseException`` and MUST NOT escape ``run_snippet``. If it did, it
would slip past ``MainThreadExecutor.process_tasks`` 's ``except
Exception`` (which doesn't catch BaseException) and kill the pump
thread permanently.

Python 3.6 compatible implementation.
"""

import logging
import os
import sys
import threading
import traceback
from typing import Any, Dict, Optional

from ..utils import TeeBuffer, capture_pfc_console
from ..signals import (
    set_current_task,
    clear_current_task,
    peek_current_task,
    clear_interrupt,
    register_exec_thread,
    unregister_exec_thread,
)
from .termination import BridgeTimeout

logger = logging.getLogger("PFC-Server")

# Synthetic filename used for compile()/traceback so error frames render
# as ``<execute_code>`` instead of an internal temp path.
SNIPPET_LABEL = "<execute_code>"


def run_snippet(code, output_buffer, request_id=None):
    # type: (str, Any, Optional[str]) -> Dict[str, Any]
    """
    Compile and execute ``code`` against the PFC ``__main__`` namespace.

    Captures stdout (both Python ``print`` and PFC console output) into
    ``output_buffer``. Always returns a result dict; never raises for
    user-code errors or bridge-initiated termination.

    Args:
        code: Python source. Tried as an expression first; falls back to
            ``exec`` on SyntaxError, in which case a top-level ``result``
            variable is picked up as the return value.
        output_buffer: Stream-like buffer (StringIO or FileBuffer).
        request_id: Identifier for the timeout handler. When provided,
            this thread is registered so the handler can target
            ``PyThreadState_SetAsyncExc`` here; the same id doubles as
            the L1 interrupt-flag key.

    Returns:
        Dict with ``status`` (``"success"`` / ``"error"`` /
        ``"terminated"`` / ``"interrupted"``), ``message``, ``output``,
        and ``result``.
    """
    old_stdout = sys.stdout
    terminal = sys.__stdout__ if sys.__stdout__ is not None else old_stdout
    sys.stdout = TeeBuffer(terminal, output_buffer)

    # Save the outer ``_current_task_id`` (if any) so we can restore it
    # on the way out. When this snippet runs inside a busy task's cycle
    # callback, the outer task already owns ``_current_task_id``; if we
    # cleared it unconditionally on exit, the still-running task would
    # silently lose ``pfc_interrupt_task`` support — the PFC interrupt
    # callback reads ``_current_task_id`` and an empty value means
    # "no task to interrupt".
    prior_task = None  # type: Optional[str]
    if request_id is not None:
        register_exec_thread(request_id, threading.get_ident())
        prior_task = peek_current_task()
        set_current_task(request_id)

    try:
        import __main__

        exec_globals = __main__.__dict__
        # Don't let a prior snippet's `result` leak into this one.
        exec_globals.pop("result", None)

        cmdlog_dir = os.path.join(".pfc-mcp", "logs")
        with capture_pfc_console(sys.stdout, cmdlog_dir):
            try:
                code_obj = compile(code, SNIPPET_LABEL, "eval")
                result = eval(code_obj, exec_globals, exec_globals)
            except SyntaxError:
                code_obj = compile(code, SNIPPET_LABEL, "exec")
                exec(code_obj, exec_globals, exec_globals)
                result = exec_globals.get("result", None)

        return {
            "status": "success",
            "message": "Code executed successfully",
            "output": output_buffer.getvalue(),
            "result": _serialize(result),
        }

    except BridgeTimeout:
        # Bridge-initiated termination. Return a marker so the outer
        # handler reports status="terminated" rather than treating this
        # as a user error. Critical: do NOT let this exception escape -
        # see module docstring.
        return {
            "status": "terminated",
            "message": "Execution aborted by bridge timeout",
            "output": output_buffer.getvalue(),
            "result": None,
        }

    except InterruptedError as e:
        # L1 path - PFC interrupt callback raised at a cycle gap.
        return {
            "status": "interrupted",
            "message": "Execution interrupted: {}".format(str(e)),
            "output": output_buffer.getvalue(),
            "result": None,
        }

    except BaseException as e:
        # PFC wraps callback exceptions in ValueError; recover the
        # original InterruptedError so the caller sees the L1 path.
        if isinstance(e, ValueError):
            msg = str(e)
            if "InterruptedError" in msg and "_pfc_interrupt_check" in msg:
                return {
                    "status": "interrupted",
                    "message": "Execution interrupted by user",
                    "output": output_buffer.getvalue(),
                    "result": None,
                }

        output_text = output_buffer.getvalue()
        logger.error("Snippet execution failed:\n%s", traceback.format_exc())

        # Filter traceback to user frames only (filename == SNIPPET_LABEL)
        # so internal bridge frames don't leak into the user response.
        tb = sys.exc_info()[2]
        user_frames = []
        while tb is not None:
            filename = tb.tb_frame.f_code.co_filename
            if filename == SNIPPET_LABEL:
                user_frames.append(
                    (filename, tb.tb_lineno, tb.tb_frame.f_code.co_name)
                )
            tb = tb.tb_next

        if user_frames:
            parts = ["Code execution failed:\n"]
            for filename, lineno, name in user_frames:
                parts.append(
                    '  File "{}", line {}, in {}\n'.format(filename, lineno, name)
                )
            parts.append("{}: {}".format(type(e).__name__, str(e)))
            error_message = "".join(parts)
        else:
            error_message = "Code execution failed: {}: {}".format(
                type(e).__name__, str(e)
            )

        return {
            "status": "error",
            "message": error_message,
            "output": output_text,
            "result": None,
        }
    finally:
        sys.stdout = old_stdout
        if request_id is not None:
            # Restore the outer task's current_task_id (or clear if no
            # outer task). Must NOT unconditionally clear: see comment
            # at the entry-time save above.
            if prior_task is not None:
                set_current_task(prior_task)
            else:
                clear_current_task()
            clear_interrupt(request_id)
            unregister_exec_thread(request_id)


def _serialize(result):
    # type: (Any) -> Any
    """Convert PFC SDK objects into JSON-serialisable values."""
    if result is None or isinstance(result, (str, int, float, bool)):
        return result
    if isinstance(result, (list, tuple)):
        return [_serialize(item) for item in result]
    if isinstance(result, dict):
        return {k: _serialize(v) for k, v in result.items()}
    return str(result)
