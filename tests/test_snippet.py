"""Tests for execution.snippet.run_snippet — the shared compile/exec path
behind both pfc_execute_code routes.

Requires the `itasca_stub` fixture because `capture_pfc_console` imports
itasca and pokes its `command` attribute.
"""

from __future__ import annotations

from io import StringIO

import pytest
from pfc_mcp_bridge.execution.snippet import run_snippet
from pfc_mcp_bridge.execution.termination import BridgeTimeout
from pfc_mcp_bridge.signals.interrupt import (
    check_interrupt,
    get_exec_thread,
    peek_current_task,
    request_interrupt,
    set_current_task,
)


@pytest.fixture(autouse=True)
def _isolate_cwd(monkeypatch, tmp_path):
    # capture_pfc_console creates `.pfc-mcp/logs/` in CWD; redirect so
    # tests don't litter the repo.
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def _clean_main_namespace():
    # run_snippet uses __main__ as its exec globals. Leaks across tests
    # would let one test see another's `result` or planted helper.
    import __main__

    snapshot = dict(__main__.__dict__)
    yield
    for key in list(__main__.__dict__):
        if key not in snapshot:
            del __main__.__dict__[key]


class TestSuccessPath:
    def test_simple_expression(self, itasca_stub):
        result = run_snippet("1 + 1", StringIO())
        assert result["status"] == "success"
        assert result["result"] == 2
        assert result["output"] == ""

    def test_result_assignment_via_exec(self, itasca_stub):
        result = run_snippet("result = [1, 2, 3]", StringIO())
        assert result["status"] == "success"
        assert result["result"] == [1, 2, 3]

    def test_stale_result_does_not_leak(self, itasca_stub):
        # A prior snippet's `result` must not contaminate the next one.
        run_snippet("result = 'first'", StringIO())
        out = run_snippet("x = 1", StringIO())
        assert out["status"] == "success"
        assert out["result"] is None

    def test_stdout_captured(self, itasca_stub):
        buf = StringIO()
        result = run_snippet("print('hello')", buf)
        assert result["status"] == "success"
        assert "hello" in result["output"]


class TestErrorPath:
    def test_runtime_error_traceback_filtered_to_user_frames(self, itasca_stub):
        result = run_snippet("raise RuntimeError('boom')", StringIO())
        assert result["status"] == "error"
        # User frame must show up, internal bridge frames must not.
        assert "<execute_code>" in result["message"]
        assert "RuntimeError: boom" in result["message"]
        assert "snippet.py" not in result["message"]

    def test_syntax_error_reported_as_error(self, itasca_stub):
        result = run_snippet("def foo(:", StringIO())
        assert result["status"] == "error"
        assert "SyntaxError" in result["message"]


class TestTerminationPaths:
    def test_bridge_timeout_returns_terminated(self, itasca_stub):
        import __main__

        __main__.BridgeTimeout = BridgeTimeout
        result = run_snippet("raise BridgeTimeout()", StringIO())
        assert result["status"] == "terminated"
        assert result["result"] is None

    def test_interrupted_error_returns_interrupted(self, itasca_stub):
        result = run_snippet("raise InterruptedError('user')", StringIO())
        assert result["status"] == "interrupted"
        assert "user" in result["message"]

    def test_pfc_callback_value_error_recovers_interrupt(self, itasca_stub):
        # PFC wraps callback-raised exceptions in ValueError; run_snippet
        # sniffs the message and recovers the InterruptedError path so
        # the user sees the right status.
        code = 'raise ValueError("Exception in _pfc_interrupt_check: InterruptedError: stopped")'
        result = run_snippet(code, StringIO())
        assert result["status"] == "interrupted"


class TestTaskIdSaveRestore:
    """The execute_code-inside-busy-task interleaving fix. run_snippet
    must save the outer task's `_current_task_id` on entry and restore
    it on exit — otherwise the still-running outer task silently loses
    pfc_interrupt_task support."""

    def test_outer_task_id_preserved_across_snippet(self, itasca_stub):
        set_current_task("outer-task")
        result = run_snippet("1 + 1", StringIO(), request_id="inner-req")
        assert result["status"] == "success"
        assert peek_current_task() == "outer-task"

    def test_no_outer_task_means_cleared_after(self, itasca_stub):
        assert peek_current_task() is None
        run_snippet("1 + 1", StringIO(), request_id="solo-req")
        assert peek_current_task() is None

    def test_exec_thread_unregistered_after(self, itasca_stub):
        run_snippet("1 + 1", StringIO(), request_id="req-1")
        assert get_exec_thread("req-1") is None

    def test_inner_interrupt_flag_cleared_after(self, itasca_stub):
        # Plant a stale inner flag; run_snippet's finally must clear it
        # so a later request reusing the id doesn't see a phantom flag.
        request_interrupt("inner-req")
        assert check_interrupt("inner-req")
        run_snippet("1 + 1", StringIO(), request_id="inner-req")
        assert not check_interrupt("inner-req")

    def test_outer_interrupt_flag_preserved(self, itasca_stub):
        # Outer task's interrupt flag must survive a nested snippet —
        # the outer task may still need to be interrupted later.
        set_current_task("outer-task")
        request_interrupt("outer-task")
        run_snippet("1 + 1", StringIO(), request_id="inner-req")
        assert check_interrupt("outer-task")
        assert peek_current_task() == "outer-task"
