"""Tests for signals.interrupt — process-wide state used by L1 cancellation."""

from __future__ import annotations

import threading

import pytest
from itasca_mcp_bridge.signals import interrupt as interrupt_mod
from itasca_mcp_bridge.signals.interrupt import (
    _pfc_interrupt_check,
    check_interrupt,
    clear_current_task,
    clear_interrupt,
    get_exec_thread,
    peek_current_task,
    register_exec_thread,
    request_interrupt,
    set_current_task,
    unregister_exec_thread,
)


class TestInterruptFlags:
    def test_request_then_check_then_clear(self):
        assert request_interrupt("task-1") is True
        assert check_interrupt("task-1") is True
        clear_interrupt("task-1")
        assert check_interrupt("task-1") is False

    def test_empty_task_id_rejected(self):
        assert request_interrupt("") is False
        assert request_interrupt(None) is False  # type: ignore[arg-type]

    def test_clear_unknown_is_idempotent(self):
        clear_interrupt("never-existed")  # must not raise

    def test_independent_tasks(self):
        request_interrupt("a")
        request_interrupt("b")
        assert check_interrupt("a") and check_interrupt("b")
        clear_interrupt("a")
        assert not check_interrupt("a")
        assert check_interrupt("b")


class TestCurrentTask:
    def test_set_peek_clear_cycle(self):
        assert peek_current_task() is None
        set_current_task("task-1")
        assert peek_current_task() == "task-1"
        clear_current_task()
        assert peek_current_task() is None

    def test_set_overwrites_previous(self):
        set_current_task("outer")
        set_current_task("inner")
        assert peek_current_task() == "inner"


class TestPfcInterruptCheck:
    def test_no_current_task_is_noop(self):
        _pfc_interrupt_check()  # must not raise

    def test_current_task_without_flag_is_noop(self):
        set_current_task("task-1")
        _pfc_interrupt_check()  # no flag set, must not raise

    def test_raises_when_current_task_has_flag(self):
        set_current_task("task-1")
        request_interrupt("task-1")
        with pytest.raises(InterruptedError, match="task-1"):
            _pfc_interrupt_check()

    def test_ignores_flag_for_unrelated_task(self):
        # Common scenario: snippet runs inside the cycle gap of a busy
        # task. peek/restore semantics in run_snippet keep _current_task_id
        # pointing at the *outer* task, so an interrupt request against
        # the *inner* snippet must not trip the outer task's check.
        set_current_task("outer")
        request_interrupt("inner")
        _pfc_interrupt_check()  # outer is current, only inner is flagged


class TestExecThreadRegistry:
    def test_register_get_unregister_cycle(self):
        register_exec_thread("req-1", 12345)
        assert get_exec_thread("req-1") == 12345
        unregister_exec_thread("req-1")
        assert get_exec_thread("req-1") is None

    def test_get_unknown_returns_none(self):
        assert get_exec_thread("never-registered") is None

    def test_unregister_unknown_is_idempotent(self):
        unregister_exec_thread("never-registered")  # must not raise

    def test_register_overwrites_same_request(self):
        register_exec_thread("req-1", 111)
        register_exec_thread("req-1", 222)
        assert get_exec_thread("req-1") == 222

    def test_register_scrubs_dead_threads(self):
        """If a prior run_snippet skipped its `finally` (vanishingly
        rare), the registry would grow. The next register_exec_thread
        call scans for dead-thread entries and drops them."""
        # Start a real thread, capture its tid, let it die.
        captured: dict[str, int] = {}
        ready = threading.Event()

        def _worker():
            captured["tid"] = threading.get_ident()
            ready.set()

        t = threading.Thread(target=_worker, name="will-die")
        t.start()
        ready.wait(timeout=2.0)
        t.join(timeout=2.0)
        dead_tid = captured["tid"]
        assert not t.is_alive()

        # Manually plant a stale entry (simulating a leaked register).
        interrupt_mod._exec_thread_ids["stale-req"] = dead_tid

        # A fresh register should scrub the stale entry.
        register_exec_thread("live-req", threading.get_ident())
        assert get_exec_thread("stale-req") is None
        assert get_exec_thread("live-req") == threading.get_ident()


class _FakeItasca:
    """Minimal itasca module stand-in for _wrapped_command tests."""

    def __init__(self):
        self.set_calls: list[tuple[str, float]] = []
        self.commands: list[str] = []

    def set_callback(self, name, position):
        self.set_calls.append((name, position))

    def remove_callback(self, name, position):
        pass

    def command(self, cmd):
        self.commands.append(cmd)


class TestModelResetReRegistration:
    """The _wrapped_command hook must re-register the cycle callbacks
    after any command call that contains `model new`/`model restore` —
    those clear the engine's callback registry, killing the bridge's
    busy-time reachability and interrupt support."""

    @staticmethod
    def _interrupt_registrations(fake: _FakeItasca) -> int:
        return sum(1 for name, _ in fake.set_calls if name == "_pfc_interrupt_check")

    def _registered_fake(self) -> _FakeItasca:
        from itasca_mcp_bridge.signals.interrupt import register_interrupt_callback

        fake = _FakeItasca()
        assert register_interrupt_callback(fake) is True
        return fake

    def test_non_reset_command_does_not_re_register(self):
        fake = self._registered_fake()
        base = self._interrupt_registrations(fake)
        fake.command("model cycle 100")
        assert self._interrupt_registrations(fake) == base

    def test_single_line_reset_re_registers(self):
        fake = self._registered_fake()
        base = self._interrupt_registrations(fake)
        fake.command("model new")
        assert self._interrupt_registrations(fake) == base + 1

    def test_mid_string_reset_re_registers(self):
        # The execute_code path is not split by the command splitter, so
        # a reset command can sit mid-string in a multi-line batch. A
        # whole-string startswith check used to miss it, leaving the
        # registry dead until some later first-line match.
        fake = self._registered_fake()
        base = self._interrupt_registrations(fake)
        fake.command("ball delete\nmodel new\nball generate radius 0.1 number 5")
        assert self._interrupt_registrations(fake) == base + 1

    def test_model_restore_also_re_registers(self):
        fake = self._registered_fake()
        base = self._interrupt_registrations(fake)
        fake.command("plot clear\nmodel restore 'sample.p3sav'")
        assert self._interrupt_registrations(fake) == base + 1


class TestCommandBoundaryInterrupt:
    """_wrapped_command honors a pending interrupt at every command
    boundary. The engine does not always propagate the cycle callback's
    InterruptedError: PFC 6 swallows it when cycling was started from a
    FISH `command` block and carries on with the next command. The
    task's flag is still set, so the wrapper raises before the next
    command starts and after the current one returns."""

    def _registered_fake(self) -> _FakeItasca:
        from itasca_mcp_bridge.signals.interrupt import register_interrupt_callback

        fake = _FakeItasca()
        assert register_interrupt_callback(fake) is True
        return fake

    def test_no_pending_interrupt_runs_command(self):
        fake = self._registered_fake()
        set_current_task("t1")
        try:
            fake.command("ball list")
            assert fake.commands == ["ball list"]
        finally:
            clear_current_task()

    def test_pending_interrupt_raises_before_running_next_command(self):
        fake = self._registered_fake()
        set_current_task("t1")
        request_interrupt("t1")
        try:
            with pytest.raises(InterruptedError):
                fake.command("ball list")
            assert fake.commands == []  # never reached the engine
        finally:
            clear_interrupt("t1")
            clear_current_task()

    def test_interrupt_swallowed_by_engine_is_raised_after_command_returns(self):
        # Emulate PFC 6: the flag is set (callback raised) during the
        # command, but the engine swallows the exception and returns.
        fake = self._registered_fake()
        real_command = fake.command  # the wrapper

        def swallowing_engine(cmd):
            _FakeItasca.command(fake, cmd)
            request_interrupt("t1")

        # Rebuild the wrapper over the swallowing engine.
        fake.command = swallowing_engine
        from itasca_mcp_bridge.signals.interrupt import register_interrupt_callback

        assert register_interrupt_callback(fake) is True
        set_current_task("t1")
        try:
            with pytest.raises(InterruptedError):
                fake.command("@settle(20000)")
            assert fake.commands == ["@settle(20000)"]
        finally:
            clear_interrupt("t1")
            clear_current_task()
        del real_command

    def test_capture_control_commands_are_not_interrupt_points(self):
        # utils.command_log wraps every user command in `program log
        # on`/`off` and its log-off runs in a finally while the task's
        # interrupt flag is still set. Raising there would skip the
        # log-off, leaving the engine's log session open and dropping
        # the interrupted command's captured output.
        fake = self._registered_fake()
        set_current_task("t1")
        request_interrupt("t1")
        try:
            for control in (
                "program log off",
                "program log on truncate show-message off",
                "program log-file 'task.log'",
            ):
                fake.command(control)  # must not raise
            assert fake.commands == [
                "program log off",
                "program log on truncate show-message off",
                "program log-file 'task.log'",
            ]
            # A real command still aborts.
            with pytest.raises(InterruptedError):
                fake.command("model cycle 100")
        finally:
            clear_interrupt("t1")
            clear_current_task()

    def test_lookalike_commands_are_still_interrupt_points(self):
        fake = self._registered_fake()
        set_current_task("t1")
        request_interrupt("t1")
        try:
            for cmd in ("program logic", "fish define log", "ball list"):
                with pytest.raises(InterruptedError):
                    fake.command(cmd)
            assert fake.commands == []
        finally:
            clear_interrupt("t1")
            clear_current_task()

    def test_unrelated_task_flag_is_ignored(self):
        fake = self._registered_fake()
        set_current_task("t1")
        request_interrupt("other")
        try:
            fake.command("ball list")
            assert fake.commands == ["ball list"]
        finally:
            clear_interrupt("other")
            clear_current_task()


class TestNoRegistryMutationInCycleCallback:
    """Mutating the engine's cycle-callback registry from inside a cycle
    callback hard-crashes the process (PFC3D 6.00.030 exits on the spot).
    A snippet delivered through the executor callback can issue a
    `model new`, whose repair hook would otherwise do exactly that."""

    def _registered_fake(self) -> _FakeItasca:
        from itasca_mcp_bridge.signals.interrupt import register_interrupt_callback

        fake = _FakeItasca()
        assert register_interrupt_callback(fake) is True
        return fake

    @staticmethod
    def _interrupt_registrations(fake: _FakeItasca) -> int:
        return sum(1 for name, _ in fake.set_calls if name == "_pfc_interrupt_check")

    def test_reset_inside_callback_defers_re_registration(self, monkeypatch):
        from itasca_mcp_bridge.signals import cycle_executor

        fake = self._registered_fake()
        base = self._interrupt_registrations(fake)
        monkeypatch.setattr(cycle_executor, "_callback_depth", [1])
        fake.command("model new")
        assert fake.commands == ["model new"]  # the command still runs
        assert self._interrupt_registrations(fake) == base  # registry untouched

    def test_reset_outside_callback_still_re_registers(self, monkeypatch):
        from itasca_mcp_bridge.signals import cycle_executor

        fake = self._registered_fake()
        base = self._interrupt_registrations(fake)
        monkeypatch.setattr(cycle_executor, "_callback_depth", [0])
        fake.command("model new")
        assert self._interrupt_registrations(fake) == base + 1

    def test_ensure_cycle_callbacks_is_a_noop_inside_callback(self, monkeypatch):
        from itasca_mcp_bridge.signals import cycle_executor
        from itasca_mcp_bridge.signals.interrupt import ensure_cycle_callbacks

        fake = self._registered_fake()
        base = self._interrupt_registrations(fake)
        monkeypatch.setattr(cycle_executor, "_callback_depth", [1])
        ensure_cycle_callbacks()
        assert self._interrupt_registrations(fake) == base


class _RegistryFakeItasca:
    """itasca stand-in with an actual cycle-callback registry, so a test
    can simulate a `model new` issued OUTSIDE the wrapped Python
    itasca.command (GUI console, File menu, `program call` script):
    the engine wipes the registry and no repair hook ever sees it."""

    def __init__(self, strict=False):
        self.registry: set[tuple[str, float]] = set()
        self.strict = strict

    def set_callback(self, name, position):
        key = (name, position)
        if self.strict and key in self.registry:
            raise ValueError("already registered")
        self.registry.add(key)

    def remove_callback(self, name, position):
        self.registry.discard((name, position))

    def command(self, cmd):
        pass

    def wipe(self):
        # What the engine does on model new / model restore.
        self.registry.clear()


class TestEnsureCycleCallbacks:
    """ensure_cycle_callbacks() is the entry-point bedrock repair: it
    must bring the registry back regardless of who emptied it, and must
    be a no-op-with-False when the bridge was never started."""

    def test_returns_false_before_registration(self, monkeypatch):
        from itasca_mcp_bridge.signals.interrupt import ensure_cycle_callbacks

        monkeypatch.setattr(interrupt_mod, "_itasca_module", None)
        assert ensure_cycle_callbacks() is False

    def test_repairs_registry_wiped_outside_python(self):
        from itasca_mcp_bridge.signals.interrupt import (
            ensure_cycle_callbacks,
            register_interrupt_callback,
        )
        from itasca_mcp_bridge.signals.positions import INTERRUPT_CALLBACK_POSITION

        fake = _RegistryFakeItasca()
        assert register_interrupt_callback(fake) is True
        key = ("_pfc_interrupt_check", INTERRUPT_CALLBACK_POSITION)
        assert key in fake.registry

        fake.wipe()  # GUI-console `model new`: no wrapped command, no repair
        assert key not in fake.registry

        assert ensure_cycle_callbacks() is True
        assert key in fake.registry

    def test_idempotent_on_strict_engine(self):
        # PFC 6 raises on a duplicate set_callback; remove-first keeps
        # the unconditional re-registration safe when nothing was wiped.
        from itasca_mcp_bridge.signals.interrupt import (
            ensure_cycle_callbacks,
            register_interrupt_callback,
        )

        fake = _RegistryFakeItasca(strict=True)
        assert register_interrupt_callback(fake) is True
        assert ensure_cycle_callbacks() is True
        assert ensure_cycle_callbacks() is True

    def test_engine_rejection_is_swallowed(self):
        from itasca_mcp_bridge.signals.interrupt import (
            ensure_cycle_callbacks,
            register_interrupt_callback,
        )

        fake = _RegistryFakeItasca()
        assert register_interrupt_callback(fake) is True

        def boom(name, position):
            raise RuntimeError("engine busy")

        fake.set_callback = boom  # type: ignore[method-assign]
        assert ensure_cycle_callbacks() is False  # logged, never raised
