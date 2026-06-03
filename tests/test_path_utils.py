"""Tests for utils.path_utils bridge-root anchoring.

A user task script may call os.chdir(), which moves the cwd of the whole
embedded Python interpreter. Before this fix, bridge paths (task log,
tasks.json, command logs) were relative strings re-resolved against the live
cwd on every access, so a post-chdir read of a task log looked in the wrong
directory and check_task_status returned "(no output)" for a task whose log
had in fact been written correctly.

These tests pin the invariant: once the bridge root is frozen, every derived
path stays absolute and anchored to that root no matter where the cwd moves.
"""

from __future__ import annotations

import os

import pytest

from itasca_mcp_bridge.utils import path_utils


@pytest.fixture(autouse=True)
def reset_bridge_root():
    """Isolate the process-wide frozen root and cwd between tests."""
    saved_root = path_utils._bridge_root
    saved_cwd = os.getcwd()
    path_utils._bridge_root = None
    try:
        yield
    finally:
        path_utils._bridge_root = saved_root
        os.chdir(saved_cwd)


def test_set_bridge_root_freezes_absolute(tmp_path):
    root = path_utils.set_bridge_root(str(tmp_path))
    assert os.path.isabs(root)
    assert root == os.path.abspath(str(tmp_path))


def test_derived_paths_compose_under_root(tmp_path):
    path_utils.set_bridge_root(str(tmp_path))
    data = path_utils.data_dir()
    assert data == os.path.join(str(tmp_path), ".itasca-mcp-bridge")
    assert path_utils.logs_dir() == os.path.join(data, "logs")
    assert path_utils.tasks_file() == os.path.join(data, "tasks.json")
    assert path_utils.task_log_path("abc123") == os.path.join(
        data, "logs", "task_abc123.log"
    )


def test_paths_survive_chdir(tmp_path):
    """The core regression: chdir after freezing must not move bridge paths."""
    launch = tmp_path / "launch"
    elsewhere = tmp_path / "elsewhere"
    launch.mkdir()
    elsewhere.mkdir()

    path_utils.set_bridge_root(str(launch))
    before_log = path_utils.task_log_path("t1")
    before_tasks = path_utils.tasks_file()

    os.chdir(str(elsewhere))

    assert path_utils.task_log_path("t1") == before_log
    assert path_utils.tasks_file() == before_tasks
    assert os.path.isabs(before_log)
    assert before_log.startswith(os.path.abspath(str(launch)))


def test_lazy_capture_from_cwd_when_unset(tmp_path):
    """With no explicit root set, the first access freezes the current cwd."""
    os.chdir(str(tmp_path))
    assert path_utils._bridge_root is None
    root = path_utils.bridge_root()
    assert root == os.path.abspath(str(tmp_path))

    # A later chdir must not change the now-frozen root.
    nested = tmp_path / "nested"
    nested.mkdir()
    os.chdir(str(nested))
    assert path_utils.bridge_root() == root
