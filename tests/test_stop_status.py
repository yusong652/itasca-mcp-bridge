"""Tests for itasca_mcp_bridge.status() / stop() and the unregister primitives."""

from __future__ import annotations

import logging
import queue
import threading
from concurrent.futures import Future

import pytest
from itasca_mcp_bridge import runtime
from itasca_mcp_bridge.signals import cycle_executor
from itasca_mcp_bridge.signals import interrupt as interrupt_mod
from itasca_mcp_bridge.signals.interrupt import (
    clear_current_task,
    register_interrupt_callback,
    set_current_task,
    unregister_interrupt_callback,
)
from itasca_mcp_bridge.signals.positions import (
    EXECUTOR_CALLBACK_POSITION,
    INTERRUPT_CALLBACK_POSITION,
)

BRIDGE_NAMES = {"_mcp_bridge_interrupt_check", "_mcp_bridge_executor_callback"}


class _RegistryFakeItasca:
    def __init__(self):
        self.registry: set[tuple[str, float]] = set()
        self.commands: list[str] = []
        self.command = self._engine_command  # the engine's own, rebound by the bridge

    def _engine_command(self, cmd):
        self.commands.append(cmd)

    def set_callback(self, name, position):
        self.registry.add((name, position))

    def remove_callback(self, name, position):
        self.registry.discard((name, position))

    def names(self) -> set[str]:
        return {name for name, _ in self.registry}


@pytest.fixture(autouse=True)
def _fresh_process_state(monkeypatch):
    monkeypatch.setattr(cycle_executor, "_callback_registered", False)
    monkeypatch.setattr(cycle_executor, "_callback_depth", [0])
    monkeypatch.setattr(runtime, "_runtime", None)
    monkeypatch.setattr(runtime, "_qt_task_timer", None)
    clear_current_task()
    yield
    clear_current_task()


def _register_both(fake: _RegistryFakeItasca):
    assert register_interrupt_callback(fake) is True
    assert cycle_executor.register_executor_callback(fake) is True
    assert BRIDGE_NAMES <= fake.names()


class TestUnregister:
    def test_interrupt_unregister_removes_callback_and_unwraps_command(self):
        fake = _RegistryFakeItasca()
        engine_command = fake.command
        _register_both(fake)
        assert fake.command is not engine_command  # wrapped

        unregister_interrupt_callback(fake)

        assert fake.command is engine_command
        assert ("_mcp_bridge_interrupt_check", INTERRUPT_CALLBACK_POSITION) not in fake.registry
        assert interrupt_mod._itasca_module is None
        fake.command("model cycle 1")  # the engine's own, no bridge in the path
        assert fake.commands == ["model cycle 1"]

    def test_second_register_wraps_the_engine_not_the_first_wrapper(self):
        fake = _RegistryFakeItasca()
        engine_command = fake.command
        assert register_interrupt_callback(fake) is True
        assert register_interrupt_callback(fake) is True
        assert fake.command._bridge_original is engine_command
        unregister_interrupt_callback(fake)
        assert fake.command is engine_command

    def test_executor_unregister_resets_flag_and_fails_waiting_snippets(self):
        fake = _RegistryFakeItasca()
        _register_both(fake)
        future: Future = Future()
        cycle_executor._pending_queue.put(("print(1)", "req-1", future))

        cycle_executor.unregister_executor_callback(fake)

        assert ("_mcp_bridge_executor_callback", EXECUTOR_CALLBACK_POSITION) not in fake.registry
        assert cycle_executor.is_executor_callback_registered() is False
        with pytest.raises(RuntimeError, match="bridge stopped"):
            future.result(timeout=0)
        # Registering again works: the flag is clear.
        assert cycle_executor.register_executor_callback(fake) is True

    def test_unregister_refused_inside_cycle_callback(self, monkeypatch):
        fake = _RegistryFakeItasca()
        _register_both(fake)
        monkeypatch.setattr(cycle_executor, "_callback_depth", [1])
        with pytest.raises(RuntimeError):
            unregister_interrupt_callback(fake)
        assert BRIDGE_NAMES <= fake.names()  # untouched


class _FakeTaskManager:
    def __init__(self):
        self.tasks: dict[str, object] = {}


class _FakeContext:
    def __init__(self):
        self.task_manager = _FakeTaskManager()


class _FakeServer:
    def __init__(self):
        self.calls: list[str] = []
        self.context = _FakeContext()

    def close_sse_clients(self):
        self.calls.append("close_sse_clients")

    def shutdown(self):
        self.calls.append("shutdown")


class _FakeThread:
    def __init__(self):
        self.joined = False

    def join(self, timeout=None):
        self.joined = True


class _FakeExecutor:
    def __init__(self, main_thread_id=None):
        self.task_queue: queue.Queue = queue.Queue()
        self.main_thread_id = main_thread_id or threading.current_thread().ident


class _FakeTimer:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def _running_runtime(monkeypatch, mode="gui", executor=None) -> tuple[_RegistryFakeItasca, runtime._Runtime]:
    fake = _RegistryFakeItasca()
    _register_both(fake)
    handler = logging.NullHandler()
    logging.getLogger().addHandler(handler)
    rt = runtime._Runtime(
        server=_FakeServer(),
        server_thread=_FakeThread(),
        main_executor=executor or _FakeExecutor(),
        itasca_module=fake,
        host="localhost",
        port=9001,
        mode=mode,
        log_handlers=[handler],
    )
    monkeypatch.setattr(runtime, "_runtime", rt)
    return fake, rt


class TestStatus:
    def test_not_running(self):
        from itasca_mcp_bridge import __version__

        assert runtime.status() == {"running": False, "version": __version__}

    def test_idle(self, monkeypatch):
        _running_runtime(monkeypatch)
        state = runtime.status()
        assert state["running"] is True
        assert (state["host"], state["port"], state["mode"]) == ("localhost", 9001, "gui")
        assert state["busy"] is False
        assert state["task_id"] is None

    def test_busy_with_task(self, monkeypatch):
        _, rt = _running_runtime(monkeypatch)
        rt.server.context.task_manager.tasks["abc123"] = object()
        set_current_task("abc123")
        state = runtime.status()
        assert state["busy"] is True
        assert state["task_id"] == "abc123"

    def test_busy_with_snippet_names_no_task(self, monkeypatch):
        # Snippets are tracked under their request id, which is not a task.
        _running_runtime(monkeypatch)
        set_current_task("f9477ecd-2402-410c-bda6-14b60d01befb")
        state = runtime.status()
        assert state["busy"] is True
        assert state["task_id"] is None

    def test_busy_inside_cycle_callback(self, monkeypatch):
        _running_runtime(monkeypatch)
        monkeypatch.setattr(cycle_executor, "_callback_depth", [1])
        assert runtime.status()["busy"] is True


class TestStop:
    def test_not_running_returns_false(self, capsys):
        assert runtime.stop() is False
        assert "not running" in capsys.readouterr().out

    def test_full_teardown(self, monkeypatch, capsys):
        timer = _FakeTimer()
        monkeypatch.setattr(runtime, "_qt_task_timer", timer)
        uninstalled: list[str] = []
        from itasca_mcp_bridge.console import capture, views
        from itasca_mcp_bridge.utils import modal_guard

        monkeypatch.setattr(capture, "uninstall", lambda: uninstalled.append("capture"))
        monkeypatch.setattr(views, "uninstall", lambda: uninstalled.append("views"))
        monkeypatch.setattr(modal_guard, "uninstall", lambda: uninstalled.append("modal_guard"))

        fake, rt = _running_runtime(monkeypatch)
        engine_command = fake.command._bridge_original
        queued: Future = Future()
        rt.main_executor.task_queue.put((lambda: None, (), {}, queued))

        assert runtime.stop() is True

        assert timer.stopped and runtime._qt_task_timer is None
        assert rt.server.calls == ["close_sse_clients", "shutdown"]
        assert rt.server_thread.joined
        with pytest.raises(RuntimeError, match="bridge stopped"):
            queued.result(timeout=0)
        assert uninstalled == ["capture", "views", "modal_guard"]
        assert not (BRIDGE_NAMES & fake.names())
        assert fake.command is engine_command
        assert cycle_executor.is_executor_callback_registered() is False
        assert rt.log_handlers[0] not in logging.getLogger().handlers
        assert runtime._runtime is None
        assert runtime.status()["running"] is False
        assert "stopped (port 9001)" in capsys.readouterr().out
        # Idempotent.
        assert runtime.stop() is False

    def test_console_mode_skips_gui_hooks(self, monkeypatch):
        from itasca_mcp_bridge.console import capture

        def _boom():
            raise AssertionError("GUI hook touched in console mode")

        monkeypatch.setattr(capture, "uninstall", _boom)
        _running_runtime(monkeypatch, mode="console")
        assert runtime.stop() is True

    def test_refused_while_busy(self, monkeypatch):
        fake, rt = _running_runtime(monkeypatch)
        rt.server.context.task_manager.tasks["abc123"] = object()
        set_current_task("abc123")
        with pytest.raises(RuntimeError, match="task abc123"):
            runtime.stop()
        assert runtime._runtime is not None
        assert BRIDGE_NAMES <= fake.names()  # nothing torn down

    def test_refused_while_a_snippet_runs(self, monkeypatch):
        fake, _ = _running_runtime(monkeypatch)
        set_current_task("f9477ecd-2402-410c-bda6-14b60d01befb")
        with pytest.raises(RuntimeError, match="current request"):
            runtime.stop()
        assert BRIDGE_NAMES <= fake.names()

    def test_refused_off_main_thread(self, monkeypatch):
        _running_runtime(monkeypatch, executor=_FakeExecutor(main_thread_id=-1))
        with pytest.raises(RuntimeError, match="main thread"):
            runtime.stop()
        assert runtime._runtime is not None

    def test_start_refuses_while_running(self, monkeypatch):
        _running_runtime(monkeypatch)
        with pytest.raises(RuntimeError, match="stop"):
            runtime.start()
