"""Tests for signals.interrupt — process-wide state used by L1 cancellation."""

from __future__ import annotations

import threading

import pytest
from pfc_mcp_bridge.signals import interrupt as interrupt_mod
from pfc_mcp_bridge.signals.interrupt import (
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
