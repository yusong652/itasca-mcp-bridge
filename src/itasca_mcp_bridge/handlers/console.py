# -*- coding: utf-8 -*-
"""``console_history``: hand the client what the person typed since it last asked."""

import logging

logger = logging.getLogger("itasca-mcp-bridge")


def handle_console_history(ctx, data):
    """Return undelivered console entries and advance the delivery cursor.

    Request: ``{"limit": <int, default 20>}``. Response ``data`` carries
    ``entries`` (oldest first, at most ``limit`` and the newest of those),
    ``cursor`` (id of the last delivered entry) and ``has_more``.
    """
    request_id = data.get("request_id", "unknown")
    history = getattr(ctx, "console_history", None)
    if history is None:
        return {
            "type": "console_history_result",
            "request_id": request_id,
            "status": "error",
            "message": "Console history not available in this runtime",
            "error": {
                "code": "console_history_unavailable",
                "message": "Console history not available in this runtime",
                "details": {"runtime_mode": getattr(ctx, "runtime_mode", "unknown")},
            },
            "data": None,
        }

    result = history.consume(limit=data.get("limit", 20))
    return {
        "type": "console_history_result",
        "request_id": request_id,
        "status": "success",
        "message": "OK",
        "data": result,
    }
