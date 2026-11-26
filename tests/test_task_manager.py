"""Tests for tasks.manager.TaskManager restoring tasks after a restart.

A task restored from tasks.json has no Future, so nothing can ever move
it to a terminal state. One that the previous process left unfinished
must therefore come back as failed; otherwise check_task_status reports
it as still in progress for good and an agent waits on it indefinitely.

The on-disk status of a task that died mid-run is normally "pending",
not "running": the runner flips the in-memory status when the script
starts, and tasks.json is only rewritten on the next save.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import Future

import pytest

from itasca_mcp_bridge.tasks.manager import UNFINISHED_TASK_ERROR, TaskManager
from itasca_mcp_bridge.utils import FileBuffer, path_utils


@pytest.fixture(autouse=True)
def bridge_root(tmp_path):
    """Anchor bridge state to a temp dir; restore the process-wide root after."""
    saved_root = path_utils._bridge_root
    path_utils.set_bridge_root(str(tmp_path))
    try:
        yield
    finally:
        path_utils._bridge_root = saved_root


def _submit(manager, task_id):
    """Register a task the way ScriptRunner.submit does."""
    buffer = FileBuffer(path_utils.task_log_path(task_id))
    future = Future()
    manager.create_script_task(future, "s.py", "/x/s.py", buffer, "probe", task_id)
    return future, buffer


def test_task_killed_mid_run_is_restored_as_failed():
    manager = TaskManager()
    future, buffer = _submit(manager, "abc123")
    # What ScriptRunner._execute does when the script starts.
    future.set_running_or_notify_cancel()
    manager.tasks["abc123"].status = "running"
    buffer.write("cycle 1000\n")
    buffer.flush()

    restored = TaskManager().get_task_status("abc123")

    assert restored["status"] == "failed"
    assert restored["data"]["error"] == UNFINISHED_TASK_ERROR
    assert restored["data"]["output"] == "cycle 1000"


@pytest.mark.parametrize("status", ["pending", "running"])
def test_unfinished_status_on_disk_is_restored_as_failed(status):
    os.makedirs(path_utils.data_dir())
    record = {
        "task_id": "abc123",
        "description": "probe",
        "status": status,
        "start_time": 1.0,
    }
    with open(path_utils.tasks_file(), "w") as f:
        json.dump([record], f)

    assert TaskManager().get_task_status("abc123")["status"] == "failed"


def test_finished_task_keeps_its_status_and_output():
    manager = TaskManager()
    future, buffer = _submit(manager, "abc123")
    buffer.write("done\n")
    future.set_result({"status": "success"})

    restored = TaskManager().get_task_status("abc123")

    assert restored["status"] == "completed"
    assert restored["data"]["output"] == "done"
