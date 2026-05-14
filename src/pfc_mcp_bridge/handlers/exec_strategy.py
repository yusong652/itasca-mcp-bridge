"""
Execution-strategy router for execute_code snippets.

Picks one of two paths based on bridge state:

* ``"queue"``    - main thread is idle; submit straight to
                   ``MainThreadExecutor``
* ``"callback"`` - main thread is busy with a tracked task; queue the
                   snippet for the PFC cycle-gap callback to drain

Both paths are MainThread-bound. Actual snippet execution lives in
``execution.snippet.run_snippet``.

On timeout this module also drives ``_terminate_stuck_execution``,
which combines:

* L1 - ``request_interrupt`` (PFC interrupt callback raises at the next
  cycle boundary; pairs with ``set_current_task`` in ``run_snippet``).
* L2 - ``PyThreadState_SetAsyncExc(BridgeTimeout)`` against the
  registered exec thread; aborts pure Python loops that never hit a
  cycle boundary (``while True``, ``time.sleep``, ...).

L2 only fires for threads ``is_safe_to_async_raise`` accepts. ``Dummy-N``
threads (PFC ``boost::python`` callbacks) are refused because injecting
into them would escape into C++ and trigger ITASCA's FATAL handler.

Python 3.6 compatible implementation.
"""

import asyncio
import concurrent.futures
import logging
from io import StringIO
from typing import Any, Dict, Optional, Tuple

from .context import ServerContext
from ..execution.termination import (
    BridgeTimeout,
    fire_async_exception,
    is_safe_to_async_raise,
)
from ..signals import get_exec_thread, request_interrupt

logger = logging.getLogger("PFC-Server")

# How long to wait for the pump thread to unwind after we inject
# ``BridgeTimeout``. A pure-Python loop aborts within a handful of
# bytecode instructions (milliseconds); 0.5s covers worst-case user-code
# ``finally`` blocks without letting "stuck in C" cases stall the
# handler response.
_TERMINATION_GRACE_S = 0.5


async def _terminate_stuck_execution(request_id, future):
    # type: (str, concurrent.futures.Future) -> Dict[str, Any]
    """
    Best-effort cancellation of a snippet submission that blew its timeout.

    Returns a dict summarising the outcome:

    * ``resolved``: bool - did the worker thread settle the future
      within the grace period? When True, the bridge is healthy and the
      next request can run; when False, the worker may still be blocked.
    * ``method``: ``"self"`` (future already resolved before we got
      here), ``"async_exc"`` (SetAsyncExc succeeded and the future
      settled), ``"flag_only"`` (couldn't SetAsyncExc - thread gone or
      nested boost::python; fell back to the flag), or ``"stuck_in_c"``
      (SetAsyncExc fired but the worker didn't respond within the grace
      period - likely in a C extension).
    * ``reason``: machine-readable reason when ``method == "flag_only"``.
    * ``result``: the future's result dict when resolved, else None.
    """
    # Always fire the flag first - cheap, lets PFC's interrupt callback
    # raise InterruptedError at the next cycle boundary even if L2 is
    # refused or never reaches a bytecode edge.
    request_interrupt(request_id)

    tid = get_exec_thread(request_id)

    # Registry already cleared -> run_snippet's finally ran -> the
    # worker is free. The future should be resolved or about to be.
    if tid is None:
        if future.done():
            try:
                return {"resolved": True, "method": "self", "result": future.result()}
            except BaseException:
                return {"resolved": True, "method": "self", "result": None}
        return {"resolved": False, "method": "self", "result": None}

    safe, reason = is_safe_to_async_raise(tid)
    if not safe:
        # Can't safely inject (Dummy-N nested case, or thread gone).
        # Fall back to flag-only cancellation; PFC's cycle callback may
        # still pick up the flag at the next cycle boundary.
        if future.done():
            try:
                result = future.result()
            except BaseException:
                result = None
            return {
                "resolved": True,
                "method": "flag_only",
                "reason": reason,
                "result": result,
            }
        return {
            "resolved": False,
            "method": "flag_only",
            "reason": reason,
            "result": None,
        }

    fire_async_exception(tid, BridgeTimeout)

    try:
        result = await asyncio.wait_for(
            asyncio.wrap_future(future), timeout=_TERMINATION_GRACE_S
        )
        return {"resolved": True, "method": "async_exc", "result": result}
    except (asyncio.TimeoutError, concurrent.futures.TimeoutError):
        return {"resolved": False, "method": "stuck_in_c", "result": None}


def _timeout_response(timeout_ms, termination):
    # type: (int, Dict[str, Any]) -> Dict[str, Any]
    """
    Build the inner response payload for an execute_code that timed out.

    * ``resolved=True`` -> status ``"terminated"``: bridge is free, but
      PFC state may be partially modified by the aborted code.
    * ``resolved=False`` -> status ``"timeout"``: cancellation couldn't
      complete; worker may still be blocked.
    """
    resolved = termination["resolved"]
    method = termination["method"]
    result = termination.get("result")

    # Output captured up to the abort point (present on async_exc and
    # self-resolve paths; empty otherwise).
    output = ""
    if isinstance(result, dict):
        output = result.get("output", "") or ""

    details = {"method": method}  # type: Dict[str, Any]
    if "reason" in termination:
        details["reason"] = termination["reason"]

    if resolved:
        message = (
            "Execution timed out after {}ms and was aborted. "
            "PFC state may be partially modified by the aborted code."
        ).format(timeout_ms)
        return {
            "status": "terminated",
            "message": message,
            "output": output,
            "result": None,
            "_error_code": "terminated",
            "_error_details": details,
        }

    if method == "flag_only":
        message = (
            "Execution timed out after {}ms. Full abort was not possible "
            "({}); the code may still be running in the background."
        ).format(timeout_ms, termination.get("reason"))
    elif method == "stuck_in_c":
        message = (
            "Execution timed out after {}ms. Bridge failed to terminate "
            "the code - it is likely stuck in a C extension. The bridge "
            "may recover when the C call returns; otherwise restart."
        ).format(timeout_ms)
    else:
        message = "Execution timed out after {}ms.".format(timeout_ms)

    return {
        "status": "timeout",
        "message": message,
        "output": output,
        "result": None,
        "_error_code": "timeout",
        "_error_details": details,
    }


async def execute_snippet(ctx, code, request_id, timeout_s):
    # type: (ServerContext, str, str, float) -> Tuple[Dict[str, Any], str]
    """
    Execute snippet with bridge-side timeout + termination.

    Selects queue vs callback based on ``has_running_tasks``, awaits the
    future for ``timeout_s`` (plus a small server-side buffer), and on
    timeout drives ``_terminate_stuck_execution`` so the worker thread
    is freed before the response returns.

    Returns:
        ``(payload, path)`` where ``payload`` is the inner response dict
        and ``path`` is ``"queue"`` or ``"callback"``.
    """
    from ..execution.snippet import run_snippet
    from ..signals import submit_snippet

    has_running = ctx.task_manager.has_running_tasks()

    if has_running:
        # Queue is blocked by a running task; ride the cycle callback.
        future = submit_snippet(code, request_id)  # type: concurrent.futures.Future
        path = "callback"
    else:
        # Main queue is idle; submit straight.
        output_buffer = StringIO()
        future = ctx.main_executor.submit(
            run_snippet, code, output_buffer, request_id
        )
        path = "queue"

    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, future.result, timeout_s),
            timeout=timeout_s + 0.5,
        )
        return result, path
    except (asyncio.TimeoutError, concurrent.futures.TimeoutError):
        timeout_ms = int(timeout_s * 1000)
        termination = await _terminate_stuck_execution(request_id, future)
        return _timeout_response(timeout_ms, termination), path
