"""Tests for what importing the command-line entry point does.

The console script's entry point is `itasca_mcp_bridge.__main__:main`, so
the wrapper pip generates imports this module to reach `main`:

    from itasca_mcp_bridge.__main__ import main
    if __name__ == '__main__':
        main()

With `main()` called at module level, that import *is* the start: the
bridge binds its port while the wrapper is still importing, and the
wrapper's own call then runs `main()` a second time on the same port.

`start` is stubbed throughout rather than the socket layer being mocked,
because the question is only whether `main` runs and with what -- a stub
answers it without a port ever being touched.
"""

from __future__ import annotations

import importlib
import runpy
import sys

import itasca_mcp_bridge

DEFAULT_CALL = {"host": "localhost", "port": 9001, "mode": "auto", "auto_upgrade": True}


def _reimport_main(monkeypatch, argv):
    """Import `itasca_mcp_bridge.__main__` fresh, with `start` stubbed.

    Returns the module and the list its `start` calls append to. The stub
    is installed on the package because both routes read it from there:
    `__main__.py`'s own `from itasca_mcp_bridge import start`, and the
    wrapper's `from itasca_mcp_bridge.__main__ import main`.
    """
    calls = []
    monkeypatch.setattr(itasca_mcp_bridge, "start", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.delitem(sys.modules, "itasca_mcp_bridge.__main__", raising=False)
    return importlib.import_module("itasca_mcp_bridge.__main__"), calls


class TestImportIsInert:
    def test_importing_the_module_starts_nothing(self, monkeypatch):
        _, calls = _reimport_main(monkeypatch, ["itasca-mcp-bridge"])

        assert calls == []

    def test_console_script_shape_starts_the_bridge_exactly_once(self, monkeypatch):
        """Import, then call -- the wrapper's two steps, in its order."""
        module, calls = _reimport_main(monkeypatch, ["itasca-mcp-bridge"])

        module.main()

        assert calls == [DEFAULT_CALL]

    def test_options_reach_start(self, monkeypatch):
        module, calls = _reimport_main(
            monkeypatch,
            [
                "itasca-mcp-bridge",
                "--host", "0.0.0.0",
                "--port", "9002",
                "--mode", "gui",
                "--no-upgrade",
            ],
        )

        module.main()

        assert calls == [
            {"host": "0.0.0.0", "port": 9002, "mode": "gui", "auto_upgrade": False}
        ]


class TestDirectInvocationStillStarts:
    def test_python_m_starts_the_bridge(self, monkeypatch):
        """The guard must gate the call, not remove it."""
        calls = []
        monkeypatch.setattr(itasca_mcp_bridge, "start", lambda **kwargs: calls.append(kwargs))
        monkeypatch.setattr(sys, "argv", ["itasca_mcp_bridge"])
        monkeypatch.delitem(sys.modules, "itasca_mcp_bridge.__main__", raising=False)

        runpy.run_module("itasca_mcp_bridge", run_name="__main__")

        assert calls == [DEFAULT_CALL]
