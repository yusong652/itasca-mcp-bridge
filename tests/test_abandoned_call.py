"""Tests for calls the engine abandons — settled by the next pump tick.

Some engine errors never become Python exceptions. On PFC3D 7.00 GUI,
``itasca.facetarray.set_conveyor`` with a velocity normal to the facets
unwinds the engine's C++ error straight through the interpreter to the
product's event loop, which shows it in a message box and carries on. The
call's Python frames are never resumed, ``finally`` blocks included, so the
request waited out its timeout and left the bridge's stdout tee, its
console capture and the request's interrupt flag behind; that flag then
interrupted every cycle typed in the GUI console.

A test cannot unwind native frames, so it parks the call instead: the tick
that started it runs on a thread blocked inside the call, which leaves the
executor in exactly the state an unwind leaves it in, and the next tick
runs on the test's own thread.
"""

from __future__ import annotations

import sys
import threading
from io import StringIO

import pytest
from itasca_mcp_bridge.execution import main_thread
from itasca_mcp_bridge.execution.main_thread import (
    EngineAbandonedCall,
    MainThreadExecutor,
    on_abandon,
)
from itasca_mcp_bridge.execution.script import ScriptRunner
from itasca_mcp_bridge.execution.snippet import run_snippet
from itasca_mcp_bridge.signals import cycle_executor, interrupt
from itasca_mcp_bridge.signals.interrupt import register_interrupt_callback
from itasca_mcp_bridge.utils import command_log, modal_guard

from test_interrupt import _FakeItasca
from test_modal_guard import _FakeQApplication, _FakeQtWidgets, _FakeWidget

CONVEYOR_ERROR = "The conveyor velocity must be parallel to the facet."


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    # capture_engine_console creates a logs dir in CWD; keep the repo clean.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(modal_guard, "_qt_widgets", _FakeQtWidgets)
    monkeypatch.setattr(modal_guard, "_depth", 0)
    monkeypatch.setattr(modal_guard, "_dismissed", [])
    monkeypatch.setattr(modal_guard, "_dismissed_total", 0)
    _FakeQApplication.modal = None
    yield
    _FakeQApplication.modal = None
    main_thread._running[:] = []


@pytest.fixture
def engine_call():
    """A function in ``__main__`` that never returns until released."""
    import __main__

    entered = threading.Event()
    release = threading.Event()

    def _engine_call():
        entered.set()
        release.wait(5)

    __main__._engine_call = _engine_call
    yield entered, release
    release.set()
    del __main__._engine_call


def _abandon(executor, entered):
    """Run one tick that the engine never returns from."""
    tick = threading.Thread(target=executor.process_tasks, daemon=True)
    tick.start()
    assert entered.wait(5), "the call never started"
    return tick


def _engine_shows_its_error_box():
    """The guard's poll while the engine's box is up."""
    _FakeQApplication.modal = _FakeWidget(title="PFC3D 7.00", text=CONVEYOR_ERROR, buttons=("OK",))
    modal_guard._tick()
    _FakeQApplication.modal = None


class TestExecutor:
    def test_next_tick_settles_the_abandoned_call(self, engine_call):
        entered, release = engine_call
        executor = MainThreadExecutor()
        future = executor.submit(lambda: __import__("__main__")._engine_call())
        tick = _abandon(executor, entered)

        executor.process_tasks()

        assert isinstance(future.exception(timeout=0), EngineAbandonedCall)
        release.set()
        tick.join(5)

    def test_guard_window_covers_the_whole_call(self):
        """The box can come from any engine call, not only itasca.command."""
        seen = []
        executor = MainThreadExecutor()
        executor.submit(lambda: seen.append(modal_guard.depth()))
        executor.process_tasks()
        assert seen == [1]
        assert modal_guard.depth() == 0

    def test_settling_closes_the_guard_window_the_call_left_open(self, engine_call):
        entered, release = engine_call

        def inside_a_command():
            modal_guard.entered()  # as _wrapped_command does; its left() never runs
            __import__("__main__")._engine_call()

        executor = MainThreadExecutor()
        executor.submit(inside_a_command)
        tick = _abandon(executor, entered)
        assert modal_guard.depth() == 2

        executor.process_tasks()

        assert modal_guard.depth() == 0
        release.set()
        tick.join(5)

    def test_a_finished_call_leaves_nothing_running(self):
        executor = MainThreadExecutor()
        future = executor.submit(lambda: 42)
        executor.process_tasks()
        assert future.result(timeout=0) == 42
        assert main_thread._running == []

    def test_only_the_running_call_can_register_a_handler(self):
        """A snippet run from a cycle callback inside a task must not take
        over the task's handler."""
        registered = []

        def task():
            on_abandon(task, lambda message: "task")
            on_abandon(run_snippet, lambda message: "snippet")
            registered.append(main_thread._running[-1].on_abandon(""))

        executor = MainThreadExecutor()
        executor.submit(task)
        executor.process_tasks()
        assert registered == ["task"]


class TestSnippet:
    def test_reports_the_engine_error_and_puts_everything_back(self, itasca_stub, engine_call):
        entered, release = engine_call
        command = itasca_stub.command
        stdout = sys.stdout
        executor = MainThreadExecutor()
        future = executor.submit(
            run_snippet, "print('before')\n_engine_call()\nprint('after')", StringIO(), "req-1"
        )
        tick = _abandon(executor, entered)
        _engine_shows_its_error_box()

        executor.process_tasks()

        result = future.result(timeout=0)
        assert result["status"] == "error"
        assert result["_error_code"] == "engine_abandoned"
        assert CONVEYOR_ERROR in result["message"]
        assert "before" in result["output"]
        assert "after" not in result["output"]
        assert sys.stdout is stdout
        assert command_log._stack == []
        assert itasca_stub.command is command
        assert interrupt.peek_current_task() is None
        assert interrupt.get_exec_thread("req-1") is None
        release.set()
        tick.join(5)

    def test_gui_cycling_is_not_interrupted_afterwards(self, itasca_stub, engine_call):
        """The timed-out request's interrupt flag used to stay current,
        stopping every later cycle typed in the GUI console."""
        entered, release = engine_call
        executor = MainThreadExecutor()
        executor.submit(run_snippet, "_engine_call()", StringIO(), "req-2")
        tick = _abandon(executor, entered)
        interrupt.request_interrupt("req-2")

        executor.process_tasks()

        interrupt._mcp_bridge_interrupt_check()  # does not raise
        release.set()
        tick.join(5)


class TestCallbackSnippet:
    def test_cycling_command_settles_a_snippet_abandoned_inside_it(self, itasca_stub, engine_call, monkeypatch):
        """From a cycle callback the engine unwinds to its cycle loop and
        fails the task's cycling command with the same message. The snippet
        is settled there, and no longer counts as inside the callback."""
        entered, release = engine_call
        # The parked callback thread decrements this again once released.
        monkeypatch.setattr(cycle_executor, "_callback_depth", [0])
        callback = []

        class _Engine(_FakeItasca):
            def command(self, cmd):
                if cmd.startswith("model cycle"):
                    callback.append(threading.Thread(target=cycle_executor._mcp_bridge_executor_callback, daemon=True))
                    callback[0].start()
                    assert entered.wait(5)
                    # No box on this path: the message comes with the error.
                    raise ValueError(CONVEYOR_ERROR + "\n    While processing line 0 of source Python interpreter.")

        engine = _Engine()
        assert register_interrupt_callback(engine) is True
        future = cycle_executor.submit_snippet("_engine_call()", "req-cb")

        with pytest.raises(ValueError):
            engine.command("model cycle 100")

        result = future.result(timeout=0)
        assert result["_error_code"] == "engine_abandoned"
        assert CONVEYOR_ERROR in result["message"]
        assert cycle_executor._callback_depth == [0]
        assert main_thread._running == []
        assert modal_guard.depth() == 0
        release.set()
        callback[0].join(5)


class _FakeTaskManager:
    def __init__(self):
        self.tasks = {}


class TestTask:
    def test_task_fails_with_the_engine_error(self, itasca_stub, engine_call):
        entered, release = engine_call
        runner = ScriptRunner(main_executor=None, task_manager=_FakeTaskManager())
        executor = MainThreadExecutor()
        future = executor.submit(runner._execute, "task.py", "_engine_call()", StringIO(), "abc123")
        tick = _abandon(executor, entered)
        _engine_shows_its_error_box()

        executor.process_tasks()

        result = future.result(timeout=0)
        assert result["status"] == "error"
        assert CONVEYOR_ERROR in result["message"]
        assert command_log._stack == []
        assert interrupt.peek_current_task() is None
        release.set()
        tick.join(5)
