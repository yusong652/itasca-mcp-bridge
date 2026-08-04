"""Tests for execution.script.ScriptRunner interrupt classification.

The engine wraps an InterruptedError raised inside the cycle callback
differently per version: PFC 7 raises a ValueError whose text still
contains "InterruptedError" and the callback name; PFC 6 raises an
opaque RuntimeError ("Error in execution - See the Itasca Console...")
with no trace of the original exception. Classification therefore has
two legs: string matching for the PFC 7 shape, and the still-pending
interrupt flag as the engine-agnostic fallback.
"""

from __future__ import annotations

from io import StringIO

import pytest
from itasca_mcp_bridge.execution.script import ScriptRunner
from itasca_mcp_bridge.signals.interrupt import request_interrupt


@pytest.fixture(autouse=True)
def _isolate_cwd(monkeypatch, tmp_path):
    # capture_engine_console creates a logs dir in CWD; keep the repo clean.
    monkeypatch.chdir(tmp_path)


class _FakeTaskManager:
    def __init__(self):
        self.tasks = {}


def _run(script_content: str, task_id: str) -> dict:
    runner = ScriptRunner(main_executor=None, task_manager=_FakeTaskManager())
    return runner._execute("fake_script.py", script_content, StringIO(), task_id)


PFC6_OPAQUE_WRAP = (
    'raise RuntimeError("Error in execution - '
    'See the Itasca Console for further details.")'
)


def test_opaque_wrap_with_pending_interrupt_is_interrupted(itasca_stub):
    # PFC 6 shape: the interrupt flag (still set when classification
    # runs; cleared in the finally) identifies the interruption.
    request_interrupt("task-6")
    result = _run(PFC6_OPAQUE_WRAP, "task-6")
    assert result["status"] == "interrupted"


def test_opaque_wrap_without_interrupt_is_error(itasca_stub):
    # Same engine error with no pending interrupt request must remain a
    # genuine failure.
    result = _run(PFC6_OPAQUE_WRAP, "task-noflag")
    assert result["status"] == "error"


def test_pfc7_valueerror_wrap_is_interrupted(itasca_stub):
    code = (
        'raise ValueError("InterruptedError: interrupted '
        'while processing _pfc_interrupt_check")'
    )
    result = _run(code, "task-7")
    assert result["status"] == "interrupted"


def test_direct_interruptederror_is_interrupted(itasca_stub):
    result = _run('raise InterruptedError("Task task-x interrupted")', "task-x")
    assert result["status"] == "interrupted"


def test_success_path_unaffected(itasca_stub):
    result = _run("result = 41 + 1", "task-ok")
    assert result["status"] == "success"
    assert result["result"] == 42
