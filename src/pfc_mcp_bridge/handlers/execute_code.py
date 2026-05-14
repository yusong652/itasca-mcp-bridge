"""
Execute code message handler.

Thin wrapper over ``handlers.exec_strategy.execute_snippet``: shapes the
wire response with the unified envelope (status, message, data, error).

Timeouts are handled bridge-side by ``execute_snippet`` (which drives
the two-layer termination flow). This handler only formats the result.
"""

import logging
from typing import Any, Dict

from .context import ServerContext
from .exec_strategy import execute_snippet
from .helpers import require_field
from ..utils.response import _truncate_output

logger = logging.getLogger("PFC-Server")


async def handle_execute_code(ctx, data):
    # type: (ServerContext, Dict[str, Any]) -> Dict[str, Any]
    """Handle ``execute_code`` message."""
    request_id = data.get("request_id", "unknown")

    code, err = require_field(data, "code", request_id, "execute_code_result")
    if err:
        return err

    timeout_ms = data.get("timeout_ms", 10000)
    timeout_s = timeout_ms / 1000.0

    try:
        result, path = await execute_snippet(ctx, code, request_id, timeout_s)
    except Exception as e:
        logger.error("Code execution failed: {}".format(e))
        return {
            "type": "execute_code_result",
            "request_id": request_id,
            "status": "error",
            "message": str(e),
            "error": {
                "code": "execute_code_failed",
                "message": str(e),
            },
            "data": None,
        }

    status = result.get("status", "unknown")
    message = result.get("message", "")
    response = {
        "type": "execute_code_result",
        "request_id": request_id,
        "execution_path": path,
        "status": status,
        "message": message,
        "data": {
            "output": _truncate_output(result.get("output", "")),
            "result": result.get("result"),
        },
    }

    if status in ("error", "terminated", "timeout", "interrupted"):
        error_code = result.get("_error_code", status)
        error_details = result.get("_error_details")
        error_block = {
            "code": error_code,
            "message": message,
        }  # type: Dict[str, Any]
        if error_details:
            error_block["details"] = error_details
        response["error"] = error_block

    return response
