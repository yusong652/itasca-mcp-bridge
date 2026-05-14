"""Test configuration for pfc-mcp-bridge.

The bridge package isn't installed in this repo's virtualenv (it's
deployed inside PFC GUI's embedded Python), so we put `src/` on sys.path
to make `import pfc_mcp_bridge.*` work from these tests.

Tests that exercise code paths reaching `import itasca` (run_snippet via
capture_pfc_console) request the `itasca_stub` fixture, which installs a
MagicMock under that name for the duration of the test.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_BRIDGE_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_BRIDGE_SRC) not in sys.path:
    sys.path.insert(0, str(_BRIDGE_SRC))


@pytest.fixture
def itasca_stub():
    """Install a MagicMock for the `itasca` module.

    `capture_pfc_console` does `import itasca` and immediately calls
    `itasca.command(...)` to set the log-file path; a MagicMock accepts
    arbitrary attribute access and calls, so the snippet path can run
    end-to-end without a real PFC GUI behind it.
    """
    stub = MagicMock(name="itasca")
    sys.modules["itasca"] = stub
    try:
        yield stub
    finally:
        sys.modules.pop("itasca", None)


@pytest.fixture(autouse=True)
def reset_interrupt_state():
    """Clear module-level state in signals.interrupt between tests.

    The interrupt module keeps three pieces of process-wide state:
    a set of interrupt flags, a single current_task_id, and a thread
    registry. Tests that assert on these can't tolerate leaks from
    earlier tests.
    """
    yield
    try:
        from pfc_mcp_bridge.signals import interrupt as _interrupt
    except ImportError:
        return
    _interrupt._interrupt_flags.clear()
    _interrupt._current_task_id = None
    _interrupt._exec_thread_ids.clear()
