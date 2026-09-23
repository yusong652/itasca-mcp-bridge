# -*- coding: utf-8 -*-
"""Hooks that record what the person at the keyboard types into the product.

A product GUI has two places to type: the IPython pane and the native
command prompt. Nothing typed there passes through the bridge, so without
these hooks the client only ever sees its own requests. Both hooks record
into one ``ConsoleHistory``; the client reads it through ``console_history``.

IPython pane
    The pane is a qtconsole with an in-process kernel, and ``get_ipython()``
    returns its shell directly. ``pre_run_cell`` / ``post_run_cell`` fire for
    every cell the person runs and for nothing the bridge runs (the bridge's
    own snippets never go through IPython). The cell's stdout is the kernel's
    stream for the duration of the cell; it is wrapped so the output is kept
    and still reaches the pane.

    The hook signature changed across the IPython generations the products
    ship: 6.2.1 (6.0/7.0 products) calls the hooks with no arguments, 8.x
    (9.x products) passes ``ExecutionInfo`` / ``ExecutionResult``. The hooks
    take ``*args`` and, when nothing is passed, read the shell's history and
    ``last_execution_result`` instead, which both generations keep.

Native command prompt
    The prompt widget (``itasca3d::PromptLineEdit``) emits its own signal,
    ``myReturnPressed(QString)``, carrying the line as typed. The command
    itself runs later, queued on the event loop after the slot returns, and
    its output lands in the console's output pane (``itascaxd::TextOutput``)
    as an echo line ``<prompt><command>`` followed by whatever it printed,
    up to the next echo line. So the entry is opened on the signal and
    closed once the pane has gone quiet or the next echo line has arrived.

    PySide2 as shipped with the 6.0/7.0 products has no shiboken, so the
    widgets it hands back are generic ``QWidget`` wrappers with none of the
    subclass API: text is read through ``property("text")`` and signals are
    connected old-style through ``QObject.connect(obj, SIGNAL(...), fn)``.
    Where that connect is unavailable the hook falls back to an event
    filter on the Return key, which needs nothing from the binding.

Both hooks are GUI-only: a console build has no pane and no prompt widget,
and the person there types into the same terminal the bridge is logging to.

Python 3.6 compatible implementation.
"""

import logging
import re
import sys
from io import StringIO

from .history import SOURCE_COMMAND, SOURCE_PYTHON

logger = logging.getLogger("itasca-mcp-bridge")

# Native prompt widget and the pane its output lands in, by C++ class name
# without its namespace: the 3D products put the prompt in `itasca3d::`,
# the 2D products (PFC2D, FLAC2D, MPoint2D) in `itasca2d::`, and the output
# pane sits in the shared `itascaxd::` for both.
PROMPT_WIDGET_CLASS = "PromptLineEdit"
OUTPUT_WIDGET_CLASS = "TextOutput"
PROMPT_SIGNAL = "myReturnPressed(QString)"

# A command's output is closed once the output pane has been quiet this
# long after the last change, unless its echo line has not appeared yet.
OUTPUT_SETTLE_MS = 300
# How long to keep waiting for the echo line before giving up on it: the
# engine may be busy with something else and run the line later.
OUTPUT_ECHO_WAIT_MS = 15000

# The prompt widgets may not exist yet when start() runs from a launch
# script; look again a few times before giving up.
FIND_RETRIES = 20
FIND_RETRY_MS = 500

# Default prompt line prefix pattern when the prompt label cannot be read:
# "pfc3d>", "flac3d>", "3dec>", ...
_PROMPT_LINE_RE = re.compile(r"^[A-Za-z0-9_]+>")

# Engine error lines in the console output start with these.
_ERROR_MARKERS = ("*** ", "Error", "ERROR")

# Module-level references keep timers, filters and hook objects alive
# (a Qt timer with no owning reference is collected and stops firing).
_installed = None  # type: object


# ---------------------------------------------------------------------------
# IPython pane
# ---------------------------------------------------------------------------


class _TeeStream(object):
    """A stdout that keeps a copy of what passes through it."""

    def __init__(self, terminal):
        self._terminal = terminal
        self._buffer = StringIO()

    def write(self, s):
        self._buffer.write(s)
        if self._terminal is not None:
            try:
                self._terminal.write(s)
            except (ValueError, OSError):
                pass
        return len(s)

    def flush(self):
        if self._terminal is not None:
            try:
                self._terminal.flush()
            except (ValueError, OSError):
                pass

    def getvalue(self):
        return self._buffer.getvalue()

    def isatty(self):
        return False

    def readable(self):
        return False

    def writable(self):
        return True

    def seekable(self):
        return False

    @property
    def encoding(self):
        return getattr(self._terminal, "encoding", "utf-8")


class PythonConsoleCapture(object):
    """Records cells run in the IPython pane."""

    def __init__(self, history):
        self._history = history
        self._shell = None
        self._tee = None
        self._saved_stdout = None

    def install(self):
        # type: () -> bool
        shell = _find_ipython_shell()
        if shell is None:
            logger.info("No IPython shell in this process; Python console capture disabled")
            return False
        try:
            shell.events.register("pre_run_cell", self._pre_run_cell)
            shell.events.register("post_run_cell", self._post_run_cell)
        except Exception as e:
            logger.warning("IPython hooks not installed: %s", e)
            return False
        self._shell = shell
        logger.info("Python console capture installed on %s", type(shell).__name__)
        return True

    def uninstall(self):
        shell = self._shell
        if shell is None:
            return
        for name, fn in (("pre_run_cell", self._pre_run_cell), ("post_run_cell", self._post_run_cell)):
            try:
                shell.events.unregister(name, fn)
            except Exception:
                pass
        self._shell = None

    def _pre_run_cell(self, *args):
        self._saved_stdout = sys.stdout
        self._tee = _TeeStream(self._saved_stdout)
        sys.stdout = self._tee

    def _post_run_cell(self, *args):
        output = ""
        tee = self._tee
        if tee is not None:
            output = tee.getvalue()
            # Only put back what this hook took away; if something else
            # swapped stdout during the cell, leave its choice alone.
            if sys.stdout is tee:
                sys.stdout = self._saved_stdout
        self._tee = None
        self._saved_stdout = None

        try:
            raw_cell, result, success = self._describe_cell(args)
        except Exception as e:
            logger.error("Console capture could not read the cell: %s", e)
            return
        if not raw_cell or not raw_cell.strip():
            return
        try:
            self._history.add(SOURCE_PYTHON, raw_cell, output=output, result=result, success=success)
        except Exception as e:
            logger.error("Failed to record console entry: %s", e)

    def _describe_cell(self, args):
        """(raw_cell, result, success) for the cell that just ran.

        IPython 8 hands the ``ExecutionResult`` to the hook; IPython 6 hands
        nothing and keeps the same facts on the shell.
        """
        shell = self._shell
        execution = args[0] if args else None
        if execution is None or not hasattr(execution, "error_in_exec"):
            execution = getattr(shell, "last_execution_result", None)

        raw_cell = None
        info = getattr(execution, "info", None)
        if info is not None:
            raw_cell = getattr(info, "raw_cell", None)
        if raw_cell is None:
            history = getattr(shell, "history_manager", None)
            raw = getattr(history, "input_hist_raw", None)
            if raw:
                raw_cell = raw[-1]

        result = getattr(execution, "result", None)
        success = True
        if execution is not None:
            if getattr(execution, "error_before_exec", None) is not None:
                success = False
            if getattr(execution, "error_in_exec", None) is not None:
                success = False
        return raw_cell, result, success


def _find_ipython_shell():
    try:
        from IPython import get_ipython
    except ImportError:
        return None
    try:
        return get_ipython()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Native command prompt
# ---------------------------------------------------------------------------


def slice_command_output(text, offset, prompt, command):
    # type: (str, int, str, str) -> tuple
    """What ``command`` printed, cut out of the output pane's text.

    ``text`` is the pane's full text; ``offset`` is its length when the
    command was entered; ``prompt`` is the prefix the pane echoes before a
    command (``"pfc3d>"``), or None to accept any ``word>`` prefix.

    Returns ``(output, found, ended)``: ``found`` is False while the echo
    line has not appeared yet (then ``output`` is everything after the
    offset, for a caller that gives up waiting); ``ended`` is True when a
    later echo line closed the region.
    """
    output, found, ended, _end = locate_command_output(text, offset, prompt, command)
    return output, found, ended


def locate_command_output(text, offset, prompt, command):
    # type: (str, int, str, str) -> tuple
    """``slice_command_output`` plus where the region ends in ``text``.

    The fourth value is the index just past the region: the start of the
    echo line that closed it, or ``len(text)`` for an open-ended region,
    or ``offset`` when the echo has not appeared. A command entered after
    this one cannot have echoed before that point.
    """
    region = text[offset:]
    lines = region.split("\n")
    wanted = command.strip()

    def is_prompt_line(line):
        if prompt:
            return line.startswith(prompt)
        return _PROMPT_LINE_RE.match(line) is not None

    def echoes_command(line):
        if not is_prompt_line(line):
            return False
        body = line[len(prompt):] if prompt else _PROMPT_LINE_RE.sub("", line, count=1)
        return body.strip() == wanted

    start = None
    for i, line in enumerate(lines):
        if echoes_command(line):
            start = i
            break
    if start is None:
        return region.strip("\n"), False, False, offset

    collected = []
    ended = False
    position = offset + sum(len(line) + 1 for line in lines[:start + 1])
    for line in lines[start + 1:]:
        if is_prompt_line(line):
            ended = True
            break
        collected.append(line)
        position += len(line) + 1
    if not ended:
        position = len(text)
    return "\n".join(collected).strip("\n"), True, ended, position


def output_reports_error(output):
    # type: (str) -> bool
    """Whether the engine flagged an error in this output."""
    for line in output.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith(_ERROR_MARKERS):
            return True
    return False


class CommandLineCapture(object):
    """Records lines entered at the product's native command prompt."""

    def __init__(self, history, qt_core, qt_widgets):
        self._history = history
        self._core = qt_core
        self._widgets = qt_widgets
        self._prompt_widget = None
        self._output_widget = None
        # The prompt label as last read while idle ("pfc3d>"). It is read
        # again at every Return, because the label changes with the engine's
        # state: it reads "BUSY>" while a data file or a solve is running.
        self._prompt = None  # type: str
        # Lines entered but not yet recorded, oldest first. More than one
        # only when lines are entered faster than the engine runs them.
        self._pending = []  # type: list
        self._settle_timer = None
        self._find_timer = None
        self._find_attempts = 0
        self._event_filter = None
        self._connected = []  # (obj, signal, fn) pairs for uninstall

    # -- install -------------------------------------------------------------

    def install(self):
        # type: () -> bool
        """Hook the prompt now, or keep looking for it for a little while.

        Returns True when the hook is in place already; False means it is
        not (yet) -- the retry timer may still install it later.
        """
        if self._try_install():
            return True
        timer = self._core.QTimer()
        timer.setInterval(FIND_RETRY_MS)
        timer.timeout.connect(self._retry_install)
        timer.start()
        self._find_timer = timer
        logger.info("Native prompt widget not found yet; will look again")
        return False

    def _retry_install(self):
        self._find_attempts += 1
        if self._try_install() or self._find_attempts >= FIND_RETRIES:
            timer = self._find_timer
            self._find_timer = None
            if timer is not None:
                try:
                    timer.stop()
                except Exception:
                    pass
            if self._prompt_widget is None:
                logger.info("Native prompt widget not found; command capture disabled")

    def _try_install(self):
        # type: () -> bool
        prompt_widget = _find_widget(self._widgets, PROMPT_WIDGET_CLASS)
        if prompt_widget is None:
            return False
        output_widget = _find_widget(self._widgets, OUTPUT_WIDGET_CLASS)
        self._prompt_widget = prompt_widget
        self._output_widget = output_widget

        if not self._connect(prompt_widget, PROMPT_SIGNAL, self._on_return_pressed):
            self._event_filter = _ReturnKeyFilter(self._core, self._on_return_pressed)
            prompt_widget.installEventFilter(self._event_filter)
            how = "event filter"
        else:
            how = PROMPT_SIGNAL
        if output_widget is not None:
            self._connect(output_widget, "textChanged()", self._on_output_changed)
        else:
            logger.info("Console output pane not found; command entries carry no output")

        timer = self._core.QTimer()
        timer.setSingleShot(True)
        timer.setInterval(OUTPUT_SETTLE_MS)
        timer.timeout.connect(self._on_settled)
        self._settle_timer = timer

        logger.info(
            "Command line capture installed via %s (output pane=%s)",
            how, "yes" if output_widget is not None else "no",
        )
        return True

    def _connect(self, obj, signature, fn):
        # type: (object, str, object) -> bool
        try:
            ok = self._core.QObject.connect(obj, self._core.SIGNAL(signature), fn)
        except Exception as e:
            logger.info("Old-style connect to %s unavailable: %s", signature, e)
            return False
        if not ok:
            return False
        self._connected.append((obj, signature, fn))
        return True

    def uninstall(self):
        for obj, signature, fn in self._connected:
            try:
                self._core.QObject.disconnect(obj, self._core.SIGNAL(signature), fn)
            except Exception:
                pass
        self._connected = []
        if self._event_filter is not None and self._prompt_widget is not None:
            try:
                self._prompt_widget.removeEventFilter(self._event_filter)
            except Exception:
                pass
        self._event_filter = None
        for timer in (self._settle_timer, self._find_timer):
            if timer is not None:
                try:
                    timer.stop()
                except Exception:
                    pass
        self._settle_timer = None
        self._find_timer = None
        self._prompt_widget = None
        self._output_widget = None

    # -- recording -----------------------------------------------------------

    def _output_text(self):
        # type: () -> str
        widget = self._output_widget
        if widget is None:
            return ""
        try:
            text = widget.property("plainText")
        except Exception:
            return ""
        return text if isinstance(text, str) else ""

    def _on_return_pressed(self, text):
        try:
            command = str(text).strip()
        except Exception:
            return
        if not command:
            return
        prompt = _read_prompt_label(self._prompt_widget)
        if prompt is not None:
            self._prompt = prompt
        self._pending.append({
            "command": command,
            "prompt": self._prompt,
            "offset": len(self._output_text()),
            "waited_ms": 0,
        })
        self._restart_settle_timer()

    def _on_output_changed(self):
        if self._pending:
            self._restart_settle_timer()

    def _restart_settle_timer(self):
        timer = self._settle_timer
        if timer is None:
            return
        try:
            timer.start()
        except Exception:
            pass

    def _on_settled(self):
        """The pane has been quiet for a while: record what can be recorded.

        Pending lines are closed oldest first. A line whose echo has not
        appeared is waited for (the engine may still be busy) and closes
        the ones behind it too, since they cannot have run before it. Once
        the wait runs out it is recorded without output and the queue moves
        on.
        """
        while self._pending:
            head = self._pending[0]
            text = self._output_text()
            output, found, _ended, end = locate_command_output(
                text, head["offset"], head["prompt"], head["command"]
            )
            if not found:
                head["waited_ms"] += OUTPUT_SETTLE_MS
                if head["waited_ms"] < OUTPUT_ECHO_WAIT_MS:
                    self._restart_settle_timer()
                    return
                output = ""
            self._pending.pop(0)
            if self._pending:
                # The next line's echo comes after this one's output; a
                # repeat of the same command must not claim this echo again.
                following = self._pending[0]
                if following["offset"] < end:
                    following["offset"] = end
            self._record(head["command"], output)

    def _record(self, command, output):
        try:
            self._history.add(
                SOURCE_COMMAND,
                command,
                output=output,
                success=not output_reports_error(output),
            )
        except Exception as e:
            logger.error("Failed to record command entry: %s", e)


class _ReturnKeyFilter(object):
    """Built on demand: a QObject subclass needs the binding's QObject."""

    def __new__(cls, qt_core, callback):
        QObject = qt_core.QObject
        QEvent = qt_core.QEvent
        Qt = qt_core.Qt

        class ReturnKeyFilter(QObject):
            def eventFilter(self, obj, event):
                try:
                    if event.type() == QEvent.KeyPress and event.key() in (Qt.Key_Return, Qt.Key_Enter):
                        callback(obj.property("text"))
                except Exception as e:
                    logger.error("Return key filter failed: %s", e)
                return False

        return ReturnKeyFilter()


def _class_chain(obj):
    # type: (object) -> list
    """C++ class names from the object's own class up to QObject.

    The Python type is not asked because PySide2 does not downcast objects
    it did not create; the metaobject reports what the C++ object is.
    """
    names = []
    try:
        meta = obj.metaObject()
    except Exception:
        return names
    while meta is not None:
        try:
            names.append(meta.className())
            meta = meta.superClass()
        except Exception:
            break
    return names


def _find_widget(qt_widgets, class_name):
    """The first widget whose own C++ class is ``class_name``, namespace aside."""
    try:
        widgets = qt_widgets.QApplication.allWidgets()
    except Exception:
        return None
    for widget in widgets:
        chain = _class_chain(widget)
        if chain and chain[0].split("::")[-1] == class_name:
            return widget
    return None


def _read_prompt_label(prompt_widget):
    # type: (object) -> str
    """The ``pfc3d>`` label next to the prompt line, or None if not readable.

    None also while the engine is busy: the label then reads ``BUSY>``,
    which is not what the echo line will carry.
    """
    if prompt_widget is None:
        return None
    try:
        parent = prompt_widget.parent()
        siblings = parent.children() if parent is not None else []
    except Exception:
        return None
    for sibling in siblings:
        chain = _class_chain(sibling)
        if not chain or chain[0] != "QLabel":
            continue
        try:
            text = sibling.property("text")
        except Exception:
            continue
        if not isinstance(text, str):
            continue
        text = text.strip()
        if text.endswith(">") and text.upper() != "BUSY>":
            return text
    return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


class _Installed(object):
    def __init__(self, python_capture, command_capture):
        self.python = python_capture
        self.command = command_capture

    def uninstall(self):
        for capture in (self.python, self.command):
            if capture is not None:
                try:
                    capture.uninstall()
                except Exception:
                    pass


def install(history):
    # type: (object) -> dict
    """Install both hooks; returns which of them is in place.

    Only meaningful in a GUI with a running event loop, which is why
    ``start()`` calls it only after the Qt pump has won. A second
    ``start()`` in the same session replaces the hooks of the first.
    """
    global _installed
    if _installed is not None:
        _installed.uninstall()
        _installed = None

    python_capture = PythonConsoleCapture(history)
    python_ok = python_capture.install()

    command_capture = None
    command_ok = False
    from ..utils.modal_guard import _import_qt

    core, widgets = _import_qt()
    if core is None or widgets is None:
        logger.info("No Qt widgets binding; command line capture disabled")
    else:
        command_capture = CommandLineCapture(history, core, widgets)
        try:
            command_ok = command_capture.install()
        except Exception as e:
            logger.warning("Command line capture not installed: %s", e)
            command_capture = None

    _installed = _Installed(python_capture if python_ok else None, command_capture)
    return {"python": python_ok, "command": command_ok}


def uninstall():
    global _installed
    if _installed is not None:
        _installed.uninstall()
        _installed = None
