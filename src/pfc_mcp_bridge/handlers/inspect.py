"""
Inspect message handler.

Handles synchronous code snippet execution for pfc_inspect tool.
Reuses the diagnostic execution strategy (queue/callback switching).
"""

import asyncio
import logging
import os
import time
import uuid
from io import StringIO
from typing import Any, Dict, Tuple

from .context import ServerContext
from .diagnostics import _execute_diagnostic
from .helpers import require_field

logger = logging.getLogger("PFC-Server")


def _write_temp_script(working_dir, code):
    # type: (str, str) -> str
    """Write code snippet to a temp file and return the path."""
    inspect_dir = os.path.join(working_dir, ".pfc-mcp-bridge", "inspect")
    if not os.path.exists(inspect_dir):
        os.makedirs(inspect_dir)
    filename = "inspect_{}.py".format(uuid.uuid4().hex[:8])
    path = os.path.join(inspect_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(code)
    return path


def _cleanup_temp_script(path):
    # type: (str) -> None
    """Remove temp script file."""
    try:
        os.remove(path)
    except Exception:
        pass


async def handle_inspect_execute(ctx, data):
    # type: (ServerContext, Dict[str, Any]) -> Dict[str, Any]
    """
    Handle inspect_execute message.

    Executes a code snippet synchronously and returns stdout.
    Uses the same queue/callback strategy as diagnostic execution.
    """
    import time as time_module

    request_id = data.get("request_id", "unknown")

    code, err = require_field(data, "code", request_id, "inspect_result")
    if err:
        return err

    timeout_ms = data.get("timeout_ms", 10000)
    start_time = time_module.time()
    total_timeout = timeout_ms / 1000.0

    def remaining():
        return max(total_timeout - (time_module.time() - start_time), 0.5)

    script_path = None
    try:
        # Write code to temp file
        working_dir = os.getcwd()
        script_path = _write_temp_script(working_dir, code)

        task_id = uuid.uuid4().hex[:8]
        output_buffer = StringIO()

        # Execute with same strategy as diagnostics (queue/callback switching)
        result, path = await _execute_diagnostic(
            ctx=ctx,
            script_path=script_path,
            script_content=code,
            output_buffer=output_buffer,
            task_id=task_id,
            remaining_time_func=remaining,
            attempt=0,
            max_attempts=2,
        )

        if result is not None:
            return {
                "type": "inspect_result",
                "request_id": request_id,
                "execution_path": path,
                "status": result.get("status", "unknown"),
                "message": result.get("message", ""),
                "data": {
                    "output": result.get("output", ""),
                    "result": result.get("result"),
                },
            }

        return {
            "type": "inspect_result",
            "request_id": request_id,
            "status": "timeout",
            "message": "Inspect timed out after {}ms".format(timeout_ms),
            "error": {
                "code": "timeout",
                "message": "Inspect timed out after {}ms".format(timeout_ms),
            },
            "data": None,
        }

    except Exception as e:
        logger.error("Inspect execution failed: {}".format(e))
        return {
            "type": "inspect_result",
            "request_id": request_id,
            "status": "error",
            "message": str(e),
            "error": {
                "code": "inspect_execute_failed",
                "message": str(e),
            },
            "data": None,
        }

    finally:
        if script_path:
            _cleanup_temp_script(script_path)
