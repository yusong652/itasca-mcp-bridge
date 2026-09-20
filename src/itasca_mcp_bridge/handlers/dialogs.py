"""
Window handlers: what the product is asking, and what to answer.

A dialog raised before the first engine command is outside
``utils/modal_guard``'s reach -- that only polls while the bridge is inside
an engine command, and by definition nothing here is yet -- so a bridge that
only ever *reports* one leaves the decision to whoever is sitting at the
machine. For PFC2D 7.00.161's ``Ok``-only "model state is currently marked as
unrepeatable" box, nobody is, and there is no way past it.

These two commands hand that decision to the client instead. ``list_dialogs``
returns what is on screen -- title, body text and button labels, read off the
widgets by the window watch on the GUI thread -- and ``answer_dialog`` clicks
one of those buttons by name. The bridge holds no policy here beyond the one
in :mod:`itasca_mcp_bridge.autostart`: it will not answer anything on its own
unless the caller asks, and the caller is told exactly what it is answering.

Both are covered by the same rule as everything else on this transport. Data
crosses threads; widgets do not. The read happens on the GUI thread and what
arrives here is already strings.

Scope, and why this is not ``modal_guard``
------------------------------------------
These reach a box that is *idle*: the product is waiting on it with Qt's
nested event loop running and Python still moving, which is what a startup
question is. They cannot reach a box raised from inside an engine command --
"Raise Dialog on Error" holds the GIL for the whole of ``exec()``, so no
Python runs on any thread and the requests do not even arrive. That box is
``modal_guard``'s, and it is the only one there that can be reached at all.
The two halves do not overlap: ``modal_guard`` says so itself, it polls only
while the bridge is inside an engine command, and by definition nothing here
is yet.
"""

import logging
from typing import Any, Dict

from .. import autostart
from .context import ServerContext

logger = logging.getLogger("itasca-mcp-bridge")


def dialogs_payload():
    # type: () -> Dict[str, Any]
    """What is on screen, for the ``GET /dialogs`` route.

    Separate from the handler below because GET routes do not go through the
    command table; they are served straight off the request handler, which
    has no :class:`ServerContext` to pass.
    """
    return {
        "status": "success",
        "data": {"dialogs": autostart.dialogs()},
    }


def handle_list_dialogs(ctx, data):
    # type: (ServerContext, Dict[str, Any]) -> Dict[str, Any]
    """Handle the ``list_dialogs`` command."""
    return dict(
        dialogs_payload(),
        type="list_dialogs_result",
        request_id=data.get("request_id", "unknown"),
        message="",
    )


def handle_answer_dialog(ctx, data):
    # type: (ServerContext, Dict[str, Any]) -> Dict[str, Any]
    """Handle the ``answer_dialog`` command.

    ``{"id": 3, "button": "Ok"}``, with the id and the label both taken from
    a preceding ``list_dialogs``. The label is matched case-insensitively
    against the buttons actually on the dialog, so a caller cannot click
    something that is not there -- and a dialog that has since gone away is
    an error rather than a silent click on whatever is in its place.
    """
    request_id = data.get("request_id", "unknown")

    dialog_id = data.get("id")
    button = data.get("button")
    if dialog_id is None or not button:
        return _error(
            request_id,
            "missing_field",
            "'id' and 'button' are both required; take them from list_dialogs",
        )
    try:
        dialog_id = int(dialog_id)
    except (TypeError, ValueError):
        return _error(request_id, "invalid_field", "'id' must be an integer")

    result = autostart.answer_dialog(dialog_id, button)
    ok = result.get("status") == "success"
    logger.info(
        "[%s] answer_dialog id=%s button=%r -> %s",
        str(request_id)[:8],
        dialog_id,
        button,
        result.get("message", ""),
    )
    return {
        "type": "answer_dialog_result",
        "request_id": request_id,
        "status": "success" if ok else "error",
        "message": result.get("message", ""),
        "error": None
        if ok
        else {"code": "answer_failed", "message": result.get("message", "")},
        "data": {"dialogs": autostart.dialogs()},
    }


def _error(request_id, code, message):
    # type: (Any, str, str) -> Dict[str, Any]
    return {
        "type": "answer_dialog_result",
        "request_id": request_id,
        "status": "error",
        "message": message,
        "error": {"code": code, "message": message},
        "data": None,
    }
