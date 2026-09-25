"""Console capture hooks, driven with stand-ins for IPython and Qt.

The IPython hook is checked against both calling conventions the products
ship (IPython 6: no arguments; IPython 8: ExecutionInfo/ExecutionResult).
The command-line hook is checked against a fake binding that behaves like
the PySide2 the 6.0/7.0 products ship: generic QWidget wrappers, text only
through ``property()``, signals only through old-style ``connect``.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from itasca_mcp_bridge.console import capture as capture_module
from itasca_mcp_bridge.console.capture import (
    CommandLineCapture,
    PythonConsoleCapture,
    output_reports_error,
    slice_command_output,
)
from itasca_mcp_bridge.console.history import ConsoleHistory


@pytest.fixture
def history(tmp_path):
    return ConsoleHistory(directory=str(tmp_path))


# ---------------------------------------------------------------------------
# Output slicing (pure)
# ---------------------------------------------------------------------------

PANE = (
    "pfc3d>program log-file 'x.log'\n"
    "pfc3d>fish list\n"
    "Name          Type Value\n"
    "---- -------- ---- -----\n"
    "pfc3d>program log-file 'y.log'\n"
)


def test_slice_takes_echo_to_next_prompt():
    output, found, ended = slice_command_output(PANE, 0, "pfc3d>", "fish list")
    assert found and ended
    assert output == "Name          Type Value\n---- -------- ---- -----"


def test_slice_ignores_text_before_offset():
    offset = PANE.index("pfc3d>fish list")
    output, found, ended = slice_command_output(PANE, offset, "pfc3d>", "fish list")
    assert found and output.startswith("Name")
    # Same command echoed earlier is not confused with this one.
    _, found_late, _ = slice_command_output(PANE, len(PANE), "pfc3d>", "fish list")
    assert found_late is False


def test_slice_open_ended_when_no_later_prompt():
    text = "pfc3d>fish list\nName Type Value\n"
    output, found, ended = slice_command_output(text, 0, "pfc3d>", "fish list")
    assert found and not ended
    assert output == "Name Type Value"


def test_slice_reports_missing_echo_with_raw_region():
    output, found, ended = slice_command_output("junk\nmore", 0, "pfc3d>", "fish list")
    assert not found and not ended
    assert output == "junk\nmore"


def test_slice_without_prompt_label_accepts_any_word_prompt():
    text = "flac3d>zone list\nzone 1\n3dec>next\n"
    output, found, ended = slice_command_output(text, 0, None, "zone list")
    assert found and ended
    assert output == "zone 1"


def test_slice_matches_command_ignoring_surrounding_whitespace():
    text = "pfc3d>  fish list \nout\n"
    output, found, _ = slice_command_output(text, 0, "pfc3d>", "fish list")
    assert found and output == "out"


def test_output_reports_error():
    assert output_reports_error("*** Command not recognized")
    assert output_reports_error("  Error: bad range")
    assert not output_reports_error("Name Type Value\n---- ---- -----")
    assert not output_reports_error("")


# ---------------------------------------------------------------------------
# IPython pane
# ---------------------------------------------------------------------------


class _Events:
    def __init__(self):
        self.callbacks = {}

    def register(self, name, fn):
        self.callbacks.setdefault(name, []).append(fn)

    def unregister(self, name, fn):
        self.callbacks[name].remove(fn)

    def fire(self, name, *args):
        for fn in list(self.callbacks.get(name, [])):
            fn(*args)


class _Shell:
    """Enough of an InteractiveShell for the hook: events, history, last result."""

    def __init__(self):
        self.events = _Events()
        self.history_manager = SimpleNamespace(input_hist_raw=[""])
        self.last_execution_result = None

    def run_cell(self, raw_cell, result=None, error=None, ipython8=False, print_text=None):
        self.history_manager.input_hist_raw.append(raw_cell)
        execution = SimpleNamespace(
            result=result,
            error_before_exec=None,
            error_in_exec=error,
            execution_count=len(self.history_manager.input_hist_raw),
        )
        if ipython8:
            execution.info = SimpleNamespace(raw_cell=raw_cell)
        self.last_execution_result = execution
        if ipython8:
            self.events.fire("pre_run_cell", SimpleNamespace(raw_cell=raw_cell))
        else:
            self.events.fire("pre_run_cell")
        if print_text is not None:
            sys.stdout.write(print_text)
        if ipython8:
            self.events.fire("post_run_cell", execution)
        else:
            self.events.fire("post_run_cell")


@pytest.fixture
def shell(monkeypatch):
    shell = _Shell()
    monkeypatch.setattr(capture_module, "_find_ipython_shell", lambda: shell)
    return shell


@pytest.mark.parametrize("ipython8", [False, True])
def test_python_capture_records_cell_output_and_result(history, shell, ipython8, capsys):
    hook = PythonConsoleCapture(history)
    assert hook.install() is True

    shell.run_cell("x = 6 * 7\nprint('hi')\nx", result=42, ipython8=ipython8, print_text="hi\n")

    entry = history.consume()["entries"][0]
    assert entry["source"] == "python"
    assert entry["input"] == "x = 6 * 7\nprint('hi')\nx"
    assert entry["output"] == "hi\n"
    assert entry["result"] == 42
    assert entry["success"] is True
    # The print still reached the real stdout.
    assert capsys.readouterr().out == "hi\n"


@pytest.mark.parametrize("ipython8", [False, True])
def test_python_capture_marks_failed_cell(history, shell, ipython8):
    PythonConsoleCapture(history).install()
    shell.run_cell("1/0", error=ZeroDivisionError("division by zero"), ipython8=ipython8)
    entry = history.consume()["entries"][0]
    assert entry["success"] is False
    assert entry["result"] is None


def test_python_capture_skips_blank_cells(history, shell):
    PythonConsoleCapture(history).install()
    shell.run_cell("   ")
    assert history.consume()["entries"] == []


def test_python_capture_restores_stdout_even_when_cell_swapped_it(history, shell):
    PythonConsoleCapture(history).install()
    original = sys.stdout
    shell.run_cell("print(1)", print_text="1\n")
    assert sys.stdout is original

    # A cell that replaces stdout keeps its replacement.
    class Sink:
        def write(self, s):
            return len(s)

        def flush(self):
            pass

    sink = Sink()

    def swap_then_post(*_):
        sys.stdout = sink

    shell.events.register("post_run_cell", swap_then_post)
    # Our post hook was registered first, so it runs before the swap; make
    # the swap happen before by firing it as a pre hook instead.
    shell.events.callbacks["post_run_cell"].remove(swap_then_post)
    shell.events.register("pre_run_cell", swap_then_post)
    shell.run_cell("sys.stdout = sink")
    assert sys.stdout is sink
    sys.stdout = original


def test_python_capture_without_shell_is_disabled(history, monkeypatch):
    monkeypatch.setattr(capture_module, "_find_ipython_shell", lambda: None)
    assert PythonConsoleCapture(history).install() is False


def test_python_capture_uninstall_removes_hooks(history, shell):
    hook = PythonConsoleCapture(history)
    hook.install()
    hook.uninstall()
    assert all(not fns for fns in shell.events.callbacks.values())


# ---------------------------------------------------------------------------
# Native command prompt, against a PySide2-5.11-shaped fake binding
# ---------------------------------------------------------------------------


class _Meta:
    def __init__(self, chain):
        self._chain = chain

    def className(self):
        return self._chain[0]

    def superClass(self):
        return _Meta(self._chain[1:]) if len(self._chain) > 1 else None


class _Widget:
    """A generic QWidget wrapper: metaobject, properties, signals, no subclass API."""

    def __init__(self, chain, parent=None, **props):
        self._chain = chain
        self._parent = parent
        self._props = dict(props)
        self._children = []
        self.signals = {}
        self.filters = []
        if parent is not None:
            parent._children.append(self)

    def metaObject(self):
        return _Meta(self._chain)

    def property(self, name):
        return self._props.get(name)

    def setProperty(self, name, value):
        self._props[name] = value

    def parent(self):
        return self._parent

    def children(self):
        return list(self._children)

    def installEventFilter(self, f):
        self.filters.append(f)

    def removeEventFilter(self, f):
        self.filters.remove(f)

    def emit(self, signature, *args):
        for fn in list(self.signals.get(signature, [])):
            fn(*args)


class _Timer:
    """A QTimer that fires only when the test says so."""

    instances = []

    def __init__(self):
        self.interval = None
        self.single = False
        self.running = False
        self.timeout = SimpleNamespace(connect=lambda fn: setattr(self, "_fn", fn))
        _Timer.instances.append(self)

    def setInterval(self, ms):
        self.interval = ms

    def setSingleShot(self, flag):
        self.single = flag

    def start(self):
        self.running = True

    def stop(self):
        self.running = False

    def fire(self):
        if self.single:
            self.running = False
        self._fn()


class _Core:
    QTimer = _Timer

    class QObject:
        @staticmethod
        def connect(obj, signature, fn):
            obj.signals.setdefault(signature, []).append(fn)
            return True

        @staticmethod
        def disconnect(obj, signature, fn):
            obj.signals[signature].remove(fn)
            return True

    @staticmethod
    def SIGNAL(signature):
        return signature


class _Widgets:
    def __init__(self, widgets):
        self._widgets = widgets
        self.QApplication = SimpleNamespace(allWidgets=lambda: list(self._widgets))


@pytest.fixture
def gui():
    _Timer.instances.clear()
    prompt_box = _Widget(["itasca3d::Prompt", "QWidget", "QObject"])
    label = _Widget(["QLabel", "QFrame", "QWidget", "QObject"], parent=prompt_box, text="pfc3d>")
    prompt = _Widget(["itasca3d::PromptLineEdit", "QLineEdit", "QWidget", "QObject"], parent=prompt_box, text="")
    output = _Widget(["itascaxd::TextOutput", "QPlainTextEdit", "QAbstractScrollArea"], plainText="")
    other = _Widget(["QLineEdit", "QWidget", "QObject"], text="decoy")
    widgets = _Widgets([other, label, output, prompt_box, prompt])
    return SimpleNamespace(prompt=prompt, output=output, label=label, widgets=widgets)


def _settle_timer():
    return [t for t in _Timer.instances if t.single][0]


def _append_output(gui, text):
    gui.output.setProperty("plainText", (gui.output.property("plainText") or "") + text)
    gui.output.emit("textChanged()")


def test_command_capture_installs_on_the_prompt_signal(history, gui):
    hook = CommandLineCapture(history, _Core, gui.widgets)
    assert hook.install() is True
    assert "myReturnPressed(QString)" in gui.prompt.signals
    assert "textChanged()" in gui.output.signals
    assert gui.prompt.filters == []
    gui.prompt.emit("myReturnPressed(QString)", "fish list")
    assert hook._pending[0]["prompt"] == "pfc3d>"


def test_busy_prompt_label_falls_back_to_generic_prompt_match(history, gui):
    """While a data file runs the label reads BUSY>, but the echo still says pfc3d>."""
    gui.label.setProperty("text", "BUSY>")
    CommandLineCapture(history, _Core, gui.widgets).install()
    gui.prompt.emit("myReturnPressed(QString)", "fish list")
    _append_output(gui, "pfc3d>fish list\nlisted\n")
    _settle_timer().fire()
    entry = history.consume()["entries"][0]
    assert entry["output"] == "listed"


def test_prompt_label_is_read_at_each_return(history, gui):
    hook = CommandLineCapture(history, _Core, gui.widgets)
    hook.install()
    gui.label.setProperty("text", "BUSY>")
    gui.prompt.emit("myReturnPressed(QString)", "a")
    assert hook._pending[0]["prompt"] is None
    gui.label.setProperty("text", "flac3d>")
    gui.prompt.emit("myReturnPressed(QString)", "b")
    assert hook._pending[1]["prompt"] == "flac3d>"
    # A busy label after a good read keeps the last good prompt.
    gui.label.setProperty("text", "BUSY>")
    gui.prompt.emit("myReturnPressed(QString)", "c")
    assert hook._pending[2]["prompt"] == "flac3d>"


def test_prompt_label_is_read_through_the_parent_taken_at_install(history, gui):
    """PySide2 5.11 marks the prompt wrapper deleted once a parent() wrapper
    it handed out is collected; the hook never asks for the parent again."""
    hook = CommandLineCapture(history, _Core, gui.widgets)
    hook.install()
    assert hook._prompt_parent is gui.prompt.parent()

    def dead_parent():
        raise RuntimeError("Internal C++ object (PySide2.QtWidgets.QWidget) already deleted.")

    gui.prompt.parent = dead_parent
    gui.label.setProperty("text", "flac3d>")
    gui.prompt.emit("myReturnPressed(QString)", "a")
    assert hook._pending[0]["prompt"] == "flac3d>"
    assert capture_module._read_prompt_label(gui.prompt) is None  # what asking again would give


def test_command_capture_records_command_with_its_output(history, gui):
    CommandLineCapture(history, _Core, gui.widgets).install()
    _append_output(gui, "pfc3d>program log-file 'x.log'\n")

    gui.prompt.emit("myReturnPressed(QString)", "fish list")
    # The command runs later: nothing recorded yet.
    assert history.consume()["entries"] == []

    _append_output(gui, "pfc3d>fish list\nName Type Value\n---- ---- -----\n")
    _settle_timer().fire()

    entry = history.consume()["entries"][0]
    assert entry["source"] == "command"
    assert entry["input"] == "fish list"
    assert entry["output"] == "Name Type Value\n---- ---- -----"
    assert entry["success"] is True


def test_line_the_engine_has_not_run_is_recorded_queued_then_ran(history, gui):
    """Typed while a script's `model cycle` runs: the engine runs it when that ends."""
    hook = CommandLineCapture(history, _Core, gui.widgets)
    hook.install()
    gui.prompt.emit("myReturnPressed(QString)", "ball list")
    _settle_timer().fire()

    # Recorded at once, as queued, with no output claimed; still pending.
    entries = history.consume()["entries"]
    assert [(e["input"], e["output"], e["status"]) for e in entries] == [("ball list", "", "queued")]
    assert len(hook._pending) == 1
    # The timer keeps looking while a line is pending, whether or not the
    # pane's change signal arrives.
    assert _settle_timer().running

    _append_output(gui, "--- Cycling ended at: 21:43:54\n")
    _settle_timer().fire()
    assert history.consume()["entries"] == []  # queued once, not again
    assert _settle_timer().running

    _append_output(gui, "pfc3d>ball list\n  Ball  Radius\n")
    _settle_timer().fire()
    entries = history.consume()["entries"]
    assert [(e["input"], e["output"], e["status"]) for e in entries] == [("ball list", "  Ball  Radius", "ran")]
    assert hook._pending == []


def test_line_that_ran_at_once_carries_no_status(history, gui):
    CommandLineCapture(history, _Core, gui.widgets).install()
    gui.prompt.emit("myReturnPressed(QString)", "fish list")
    _append_output(gui, "pfc3d>fish list\nlisted\n")
    _settle_timer().fire()
    entry = history.consume()["entries"][0]
    assert entry["output"] == "listed"
    assert "status" not in entry


def test_queued_line_the_engine_dropped_is_let_go(history, gui):
    """An interrupt flushes the engine's input queue: the line never echoes.

    A later line that does echo proves it, and must not be held up.
    """
    hook = CommandLineCapture(history, _Core, gui.widgets)
    hook.install()
    gui.prompt.emit("myReturnPressed(QString)", "ball list")
    _settle_timer().fire()
    assert [e["status"] for e in history.consume()["entries"]] == ["queued"]

    # ... interrupt; the person types again once the engine is idle.
    gui.prompt.emit("myReturnPressed(QString)", "wall list")
    _append_output(gui, "pfc3d>wall list\nwalls\n")
    _settle_timer().fire()

    entries = history.consume()["entries"]
    assert [(e["input"], e["output"]) for e in entries] == [("wall list", "walls")]
    assert "status" not in entries[0]
    assert hook._pending == []


def test_queued_line_is_let_go_once_the_engine_is_idle_without_it(history, gui):
    """After an interrupt the engine is idle and the line never echoed: gone."""
    busy = [True]
    hook = CommandLineCapture(history, _Core, gui.widgets, engine_busy=lambda: busy[0])
    hook.install()
    gui.prompt.emit("myReturnPressed(QString)", "ball list")
    _settle_timer().fire()
    assert [e["status"] for e in history.consume()["entries"]] == ["queued"]

    # Still cycling: the pane moves, the line stays pending.
    _append_output(gui, "   1000  cycles\n")
    _settle_timer().fire()
    assert len(hook._pending) == 1

    busy[0] = False
    _append_output(gui, "--- Cycling ended at: 22:14:18\n")
    _settle_timer().fire()
    # One idle look is not proof: the pane is written asynchronously.
    assert len(hook._pending) == 1
    _settle_timer().fire()
    assert hook._pending == []
    assert history.consume()["entries"] == []


def test_one_idle_look_before_the_echo_lands_does_not_drop_the_line(history, gui):
    """The line ran at the command boundary; its echo reaches the pane a
    moment after the engine reports idle."""
    busy = [True]
    hook = CommandLineCapture(history, _Core, gui.widgets, engine_busy=lambda: busy[0])
    hook.install()
    gui.prompt.emit("myReturnPressed(QString)", "ball list")
    _settle_timer().fire()
    assert [e["status"] for e in history.consume()["entries"]] == ["queued"]

    busy[0] = False
    _settle_timer().fire()  # idle, echo not there yet
    assert len(hook._pending) == 1
    _append_output(gui, "pfc3d>ball list\n  Ball  Radius\n")
    _settle_timer().fire()
    entries = history.consume()["entries"]
    assert [(e["output"], e["status"]) for e in entries] == [("  Ball  Radius", "ran")]
    assert hook._pending == []


def test_repeat_of_a_dropped_line_is_not_matched_to_the_dropped_ones_echo(history, gui):
    """The dropped 'ball list' must not claim the echo of a fresh 'ball list'."""
    busy = [True]
    hook = CommandLineCapture(history, _Core, gui.widgets, engine_busy=lambda: busy[0])
    hook.install()
    gui.prompt.emit("myReturnPressed(QString)", "ball list")
    gui.prompt.emit("myReturnPressed(QString)", "foo bar")
    _settle_timer().fire()
    assert [e["status"] for e in history.consume()["entries"]] == ["queued", "queued"]

    busy[0] = False  # interrupted; both lines flushed by the engine
    _append_output(gui, "--- Cycling ended at: 22:14:18\npfc3d>program log off\n")
    _settle_timer().fire()
    _settle_timer().fire()
    assert hook._pending == []

    gui.prompt.emit("myReturnPressed(QString)", "ball list")
    _append_output(gui, "pfc3d>ball list\n  Ball  Radius\n")
    _settle_timer().fire()
    entries = history.consume()["entries"]
    assert [(e["input"], e["output"], e.get("status")) for e in entries] == [("ball list", "  Ball  Radius", None)]


def test_a_recorded_echo_moves_every_later_line_past_it(history, gui):
    """x, y, x entered in a row: the third must not be matched to the first echo."""
    hook = CommandLineCapture(history, _Core, gui.widgets, engine_busy=lambda: True)
    hook.install()
    for line in ("x", "y", "x"):
        gui.prompt.emit("myReturnPressed(QString)", line)
    _append_output(gui, "pfc3d>x\nfirst\n")
    _settle_timer().fire()
    entries = history.consume()["entries"]
    assert [(e["input"], e["output"], e.get("status")) for e in entries] == [
        ("x", "first", None), ("y", "", "queued"), ("x", "", "queued"),
    ]
    assert [p["command"] for p in hook._pending] == ["y", "x"]

    _append_output(gui, "pfc3d>y\nsecond\npfc3d>x\nthird\n")
    _settle_timer().fire()
    entries = history.consume()["entries"]
    assert [(e["input"], e["output"], e["status"]) for e in entries] == [("y", "second", "ran"), ("x", "third", "ran")]


def test_line_without_an_output_pane_is_recorded_at_once(history):
    prompt_box = _Widget(["itasca3d::Prompt", "QWidget", "QObject"])
    _Widget(["QLabel", "QFrame", "QWidget", "QObject"], parent=prompt_box, text="pfc3d>")
    prompt = _Widget(["itasca3d::PromptLineEdit", "QLineEdit", "QWidget", "QObject"], parent=prompt_box, text="")
    hook = CommandLineCapture(history, _Core, _Widgets([prompt_box, prompt]))
    hook.install()
    prompt.emit("myReturnPressed(QString)", "fish list")
    entry = history.consume()["entries"][0]
    assert (entry["input"], entry["output"]) == ("fish list", "")
    assert "status" not in entry
    assert hook._pending == []


def test_engine_busy_is_assumed_when_nothing_can_be_read(history, gui):
    """No itasca module and a plain label: a queued line waits for proof."""
    hook = CommandLineCapture(history, _Core, gui.widgets)
    hook.install()
    assert hook._engine_busy() is True
    gui.label.setProperty("text", "BUSY>")
    assert hook._engine_busy() is True


def test_next_command_closes_the_previous_one(history, gui):
    CommandLineCapture(history, _Core, gui.widgets).install()
    gui.prompt.emit("myReturnPressed(QString)", "fish list")
    _append_output(gui, "pfc3d>fish list\nfirst\n")
    gui.prompt.emit("myReturnPressed(QString)", "ball list")
    _append_output(gui, "pfc3d>ball list\nsecond\n")
    _settle_timer().fire()

    entries = history.consume()["entries"]
    assert [(e["input"], e["output"]) for e in entries] == [("fish list", "first"), ("ball list", "second")]


def test_lines_entered_before_either_ran_are_both_recorded(history, gui):
    """Two lines in quick succession: the engine runs them later, in order."""
    CommandLineCapture(history, _Core, gui.widgets).install()
    gui.prompt.emit("myReturnPressed(QString)", "fish list")
    gui.prompt.emit("myReturnPressed(QString)", "ball list")
    _settle_timer().fire()
    # Neither has echoed: both are reported as queued, in order.
    entries = history.consume()["entries"]
    assert [(e["input"], e["status"]) for e in entries] == [("fish list", "queued"), ("ball list", "queued")]

    _append_output(gui, "pfc3d>fish list\nfirst\n")
    _settle_timer().fire()
    entries = history.consume()["entries"]
    assert [(e["input"], e["output"], e["status"]) for e in entries] == [("fish list", "first", "ran")]

    _append_output(gui, "pfc3d>ball list\nsecond\n")
    _settle_timer().fire()
    entries = history.consume()["entries"]
    assert [(e["input"], e["output"], e["status"]) for e in entries] == [("ball list", "second", "ran")]


def test_repeated_command_does_not_claim_the_earlier_echo(history, gui):
    CommandLineCapture(history, _Core, gui.widgets).install()
    gui.prompt.emit("myReturnPressed(QString)", "fish list")
    gui.prompt.emit("myReturnPressed(QString)", "fish list")
    _append_output(gui, "pfc3d>fish list\nfirst\n")
    _settle_timer().fire()
    entries = history.consume()["entries"]
    assert [(e["output"], e.get("status")) for e in entries] == [("first", None), ("", "queued")]

    _append_output(gui, "pfc3d>fish list\nsecond\n")
    _settle_timer().fire()
    entries = history.consume()["entries"]
    assert [(e["output"], e.get("status")) for e in entries] == [("second", "ran")]


def test_command_capture_flags_engine_error(history, gui):
    CommandLineCapture(history, _Core, gui.widgets).install()
    gui.prompt.emit("myReturnPressed(QString)", "foo bar")
    _append_output(gui, "pfc3d>foo bar\n*** Command not recognized\n")
    _settle_timer().fire()
    assert history.consume()["entries"][0]["success"] is False


def test_command_capture_ignores_blank_lines(history, gui):
    CommandLineCapture(history, _Core, gui.widgets).install()
    gui.prompt.emit("myReturnPressed(QString)", "   ")
    assert history.consume()["entries"] == []


def test_command_capture_falls_back_to_event_filter(history, gui):
    class NoConnectCore(_Core):
        class QObject:
            @staticmethod
            def connect(obj, signature, fn):
                raise AttributeError("no old-style connect")

            @staticmethod
            def disconnect(obj, signature, fn):
                return True

        class QEvent:
            KeyPress = "KeyPress"

        class Qt:
            Key_Return = "Return"
            Key_Enter = "Enter"

    hook = CommandLineCapture(history, NoConnectCore, gui.widgets)
    assert hook.install() is True
    assert len(gui.prompt.filters) == 1

    gui.prompt.setProperty("text", "fish list")
    event = SimpleNamespace(type=lambda: "KeyPress", key=lambda: "Return")
    assert gui.prompt.filters[0].eventFilter(gui.prompt, event) is False
    assert hook._pending[0]["command"] == "fish list"


def test_command_capture_finds_the_2d_products_prompt(history):
    """PFC2D / FLAC2D / MPoint2D put the prompt in itasca2d::, not itasca3d::."""
    prompt_box = _Widget(["itasca2d::Prompt", "QWidget", "QObject"])
    _Widget(["QLabel", "QFrame", "QWidget", "QObject"], parent=prompt_box, text="mpoint2d>")
    prompt = _Widget(["itasca2d::PromptLineEdit", "QLineEdit", "QWidget", "QObject"], parent=prompt_box, text="")
    output = _Widget(["itascaxd::TextOutput", "QPlainTextEdit", "QAbstractScrollArea"], plainText="")
    hook = CommandLineCapture(history, _Core, _Widgets([prompt_box, output, prompt]))
    assert hook.install() is True
    assert "myReturnPressed(QString)" in prompt.signals
    prompt.emit("myReturnPressed(QString)", "model list")
    assert hook._pending[0]["prompt"] == "mpoint2d>"


def test_command_capture_retries_until_widgets_appear(history):
    empty = _Widgets([])
    hook = CommandLineCapture(history, _Core, empty)
    assert hook.install() is False
    retry = [t for t in _Timer.instances if not t.single][-1]
    assert retry.running

    prompt = _Widget(["itasca3d::PromptLineEdit", "QLineEdit", "QWidget", "QObject"], text="")
    empty._widgets.append(prompt)
    retry.fire()
    assert not retry.running
    assert "myReturnPressed(QString)" in prompt.signals


def test_command_capture_gives_up_after_retries(history):
    hook = CommandLineCapture(history, _Core, _Widgets([]))
    hook.install()
    retry = [t for t in _Timer.instances if not t.single][-1]
    for _ in range(capture_module.FIND_RETRIES):
        retry.fire()
    assert not retry.running
    assert hook._prompt_widget is None


def test_command_capture_uninstall_switches_the_hook_off_without_disconnecting(history, gui):
    """QObject.disconnect on PySide2 5.11 crashed the 7.0 product; the slots
    stay connected and ignore what comes through."""
    hook = CommandLineCapture(history, _Core, gui.widgets)
    hook.install()
    hook.uninstall()
    assert len(gui.prompt.signals["myReturnPressed(QString)"]) == 1
    assert len(gui.output.signals["textChanged()"]) == 1
    assert hook._prompt_parent is None

    gui.prompt.emit("myReturnPressed(QString)", "fish list")
    assert hook._pending == []
    _append_output(gui, "pfc3d>fish list\nlisted\n")
    assert not _settle_timer().running
    assert len(history) == 0


def test_second_install_records_once(history, gui):
    """start() twice in one session: only the new hook records."""
    first = CommandLineCapture(history, _Core, gui.widgets)
    first.install()
    first.uninstall()
    second = CommandLineCapture(history, _Core, gui.widgets)
    second.install()
    gui.prompt.emit("myReturnPressed(QString)", "fish list")
    _append_output(gui, "pfc3d>fish list\nlisted\n")
    for timer in [t for t in _Timer.instances if t.single and t.running]:
        timer.fire()
    assert [e["input"] for e in history.consume()["entries"]] == ["fish list"]
