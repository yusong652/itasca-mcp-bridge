# -*- coding: utf-8 -*-
"""Dismiss the engine's blocking command-error dialog.

Itasca products can raise a modal message box on every engine error --
"Raise Dialog on Error", under Tools > Options > Console, stored per user
in ``HKCU\\Software\\Itasca\\<product>\\Terminal\\ErrorDialog``. It is on
by default in the 6.0 generation of the products and off from 7.0 on,
which is why the same bridge build wedges on one person's machine and not
the next one's, with nothing in the bridge to explain the difference.

What it does to the bridge: the box is a ``QMessageBox`` shown with
``exec()``, so the engine's C-level ``itasca.command`` does not return
until someone clicks OK. The engine holds the GIL for that whole call and
no Python runs on the main thread while it waits, so *every* bridge
thread -- the HTTP server included -- is frozen with it and requests do
not even arrive. From the client the bridge is simply gone. Measured on
PFC3D 6.00.030: one mistyped command held it 48s, until a human clicked.

Why a timer can still reach it: ``exec()`` spins a nested Qt event loop,
and that loop delivers timer events. What it will not do is redeliver a
timer whose own handler is already on the stack -- Qt's dispatcher
refuses that recursion -- and that is exactly the bridge's task pump:
``_process_tick`` -> ``process_tasks`` -> the snippet -> ``itasca.command``
-> the box. A *separate* timer is not on that stack and keeps ticking at
its normal rate. Measured on PFC3D 6.00.030: during a 2.1s box, 67 ticks
of a 25ms timer against 0 of the pump.

Scope, deliberately narrow -- this runs inside someone's GUI:

- Only while the bridge itself is inside an engine command. A box the
  person at the keyboard raised while the bridge was idle is theirs.
- Only a ``QMessageBox`` carrying exactly one button. One button offers
  no choice, so pressing it decides nothing the engine was asking about.
  Anything else is left alone and reported once to the log: a bridge that
  guesses at a dialog is worse than one that waits at it.

Nothing about the error itself changes. It still prints to the console,
still reaches the task log, and still raises out of ``itasca.command``.

Only the error box blocks. The "List of Warnings" dialog (Raise Dialog on
Warning) is non-modal by design -- the manual has it left open alongside
the program -- and so is the cycling dialog; neither holds the engine
call, and neither is this module's business.

Python 3.6 compatible implementation.
"""

import logging
from typing import Any, List, Optional, Tuple

logger = logging.getLogger("itasca-mcp-bridge")

# Poll cadence. The box is on screen for one tick plus the engine's own
# teardown, so this is essentially the whole added latency on a failing
# command: ~11ms for a trivial command, ~150ms measured when a box is
# dismissed. A tick does nothing at all while the bridge is idle.
DEFAULT_POLL_INTERVAL_MS = 25

# Held at module level for the same reason as the task pump's timer: a
# QTimer with no owning reference is garbage collected and stops firing.
_timer = None  # type: Any
_qt_widgets = None  # type: Any

# Depth of engine commands the bridge is currently inside. Every engine
# command the bridge issues goes through one wrapper
# (``signals.interrupt._wrapped_command``), which brackets the C call with
# ``entered()`` / ``left()``; nesting comes from ``program call`` expansion
# and from snippets run in a cycle callback. Commands run on the engine's
# main thread, so a plain counter is enough.
_depth = 0

# ids of modal widgets already reported as left alone, so a dialog that
# sits there does not write a log line every tick.
_reported = set()


def entered():
    # type: () -> None
    """Mark that the bridge has begun an engine command."""
    global _depth
    _depth += 1


def left():
    # type: () -> None
    """Mark that the bridge's engine command has returned."""
    global _depth
    if _depth > 0:
        _depth -= 1


def is_armed():
    # type: () -> bool
    """Whether the guard's timer is installed and running."""
    if _timer is None:
        return False
    try:
        return bool(_timer.isActive())
    except Exception:
        return False


def _import_qt():
    # type: () -> Tuple[Any, Any]
    """QtCore and QtWidgets from the binding this product ships, or (None, None)."""
    from ..runtime import _QT_BINDINGS

    for binding in _QT_BINDINGS:
        try:
            core = __import__(binding + ".QtCore", fromlist=["QtCore"])
            widgets = __import__(binding + ".QtWidgets", fromlist=["QtWidgets"])
        except Exception:
            continue
        return core, widgets
    return None, None


def _first_line(text):
    # type: (Any) -> str
    """The dialog's message reduced to one line for the log."""
    if not text:
        return ""
    line = str(text).strip().split("\n", 1)[0]
    if len(line) > 200:
        line = line[:197] + "..."
    return line


def _describe(widget):
    # type: (Any) -> str
    try:
        name = widget.metaObject().className()
    except Exception:
        name = type(widget).__name__
    try:
        title = widget.windowTitle()
    except Exception:
        title = ""
    return "{} {!r}".format(name, title)


def _report_once(widget, buttons):
    # type: (Any, Optional[List[Any]]) -> None
    """Log a modal dialog the guard declined to answer, once per dialog.

    The bridge is blocked behind it with no way to say for how long, so
    the log has to carry enough to recognise it on screen and to know
    which button a person would have to press.
    """
    key = id(widget)
    if key in _reported:
        return
    _reported.add(key)
    labels = []
    if buttons:
        for button in buttons:
            try:
                labels.append(button.text())
            except Exception:
                pass
    logger.warning(
        "Blocked on a modal dialog raised during an engine command; it needs a "
        "person, the bridge will not answer it: %s buttons=%s",
        _describe(widget), labels,
    )


def _tick():
    # type: () -> None
    """One poll. Cheap and silent unless the bridge is mid-command."""
    if _depth <= 0 or _qt_widgets is None:
        return
    try:
        widget = _qt_widgets.QApplication.activeModalWidget()
    except Exception:
        return
    if widget is None:
        if _reported:
            _reported.clear()
        return

    try:
        if not widget.inherits("QMessageBox"):
            _report_once(widget, None)
            return
        buttons = widget.findChildren(_qt_widgets.QAbstractButton)
        if len(buttons) != 1:
            _report_once(widget, buttons)
            return
        title = widget.windowTitle()
        message = _first_line(widget.property("text"))
        buttons[0].click()
    except Exception as exc:
        # Never let a dialog the binding cannot describe take the GUI's
        # event loop down with it.
        logger.debug("Modal guard could not act on a dialog: %s", exc)
        return

    logger.info("Dismissed a blocking engine dialog: %s -- %s", title, message)


def install(interval_ms=DEFAULT_POLL_INTERVAL_MS):
    # type: (int) -> bool
    """Arm the guard on the Qt event loop. Returns False if it cannot be.

    Never raises: a bridge that starts without the guard still works
    everywhere the dialog is switched off, which is every product from
    7.0 on.
    """
    global _timer, _qt_widgets

    try:
        core, widgets = _import_qt()
    except Exception as exc:
        logger.info("Modal guard unavailable: %s", exc)
        return False
    if core is None or widgets is None:
        logger.info("Modal guard unavailable: no Qt widgets binding")
        return False

    try:
        if _timer is not None:
            try:
                _timer.stop()
            except Exception:
                pass
        timer = core.QTimer()
        timer.setInterval(int(interval_ms))
        timer.timeout.connect(_tick)
        timer.start()
    except Exception as exc:
        logger.info("Modal guard could not start: %s", exc)
        return False

    _qt_widgets = widgets
    _timer = timer
    logger.info("Modal guard armed (poll=%sms)", interval_ms)
    return True
