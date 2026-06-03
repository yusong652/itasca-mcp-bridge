"""
Path utilities for the bridge.

Two concerns live here:

1. LLM-friendly path formatting (forward slashes) for cross-platform display.
2. Stable bridge-root resolution: all bridge state lives under
   ``<bridge_root>/.itasca-mcp-bridge/`` (logs, tasks.json, temporary command
   logs). The bridge root is captured ONCE — at startup — from the process
   working directory and then frozen for the life of the process.

   Why freeze instead of resolving against ``os.getcwd()`` each time: a user
   task script may call ``os.chdir()``, which moves the cwd of the entire
   embedded Python interpreter. Any bridge path resolved as a relative string
   against the live cwd after such a call then points at the wrong directory —
   writes land in one place and later reads look in another. The most visible
   symptom is ``check_task_status`` returning ``(no output)`` for a task whose
   log was in fact written correctly: the FileBuffer kept writing through its
   already-open handle, but the relative ``log_path`` re-resolved against the
   post-chdir cwd on read. Anchoring every bridge path to the frozen root keeps
   logs, tasks.json, and command logs referencing one stable on-disk location
   regardless of later chdir.

Python 3.6 compatible.
"""

import os
from typing import Optional

DATA_DIRNAME = ".itasca-mcp-bridge"
LOGS_DIRNAME = "logs"
TASKS_FILENAME = "tasks.json"

# Frozen absolute path to the directory that contains .itasca-mcp-bridge/.
# Set once via set_bridge_root() at startup, or lazily captured from the cwd
# on first access (which still happens during startup, before any task runs).
_bridge_root = None  # type: Optional[str]


def set_bridge_root(path):
    # type: (str) -> str
    """Freeze the bridge root to an absolute form of ``path``.

    Called once at bridge startup, before any task can chdir.
    """
    global _bridge_root
    _bridge_root = os.path.abspath(path)
    return _bridge_root


def bridge_root():
    # type: () -> str
    """Return the frozen bridge root, capturing it from the cwd on first use."""
    global _bridge_root
    if _bridge_root is None:
        _bridge_root = os.path.abspath(os.getcwd())
    return _bridge_root


def data_dir():
    # type: () -> str
    """Absolute path to ``<root>/.itasca-mcp-bridge``."""
    return os.path.join(bridge_root(), DATA_DIRNAME)


def logs_dir():
    # type: () -> str
    """Absolute path to ``<root>/.itasca-mcp-bridge/logs``."""
    return os.path.join(data_dir(), LOGS_DIRNAME)


def tasks_file():
    # type: () -> str
    """Absolute path to ``<root>/.itasca-mcp-bridge/tasks.json``."""
    return os.path.join(data_dir(), TASKS_FILENAME)


def task_log_path(task_id):
    # type: (str) -> str
    """Absolute path to a task's log file under ``logs/``."""
    return os.path.join(logs_dir(), "task_{}.log".format(task_id))


def path_to_llm_format(path):
    # type: (str) -> str
    """
    Convert a path to LLM-friendly format (forward slashes).

    This ensures all paths shown to the LLM use consistent forward slash format,
    regardless of the underlying platform.

    Args:
        path: Path string to format

    Returns:
        Path string with forward slashes

    Example:
        >>> path_to_llm_format("C:\\Users\\test\\file.txt")
        'C:/Users/test/file.txt'
    """
    # Normalize to forward slashes for LLM consistency
    return path.replace('\\', '/')
