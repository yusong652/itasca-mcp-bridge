"""Tests for execution.termination — the L2 async-exc cancellation layer.

These exercise pure-Python machinery (ctypes + threading); no itasca
stub is required.
"""

from __future__ import annotations

import threading
import time

from pfc_mcp_bridge.execution.termination import (
    BridgeTimeout,
    fire_async_exception,
    is_safe_to_async_raise,
)


class TestBridgeTimeout:
    def test_inherits_baseexception_not_exception(self):
        # Load-bearing: well-meaning user code that does `except Exception`
        # must NOT swallow BridgeTimeout, otherwise the bridge can't
        # actually unwind a runaway snippet.
        assert issubclass(BridgeTimeout, BaseException)
        assert not issubclass(BridgeTimeout, Exception)


class TestIsSafeToAsyncRaise:
    def test_main_thread_allowed(self):
        # MainThread is intentionally accepted: in GUI mode the bridge
        # task pump runs on MainThread via a Qt timer, so a stuck snippet
        # is always sitting on MainThread's Python stack.
        ok, reason = is_safe_to_async_raise(threading.main_thread().ident)
        assert ok is True
        assert reason == "ok"

    def test_unknown_tid_rejected(self):
        ok, reason = is_safe_to_async_raise(0x7FFFFFFF)
        assert ok is False
        assert reason == "thread_not_alive"

    def test_live_named_thread_allowed(self):
        ready = threading.Event()
        stop = threading.Event()

        def _worker():
            ready.set()
            stop.wait()

        t = threading.Thread(target=_worker, name="bridge-test-worker")
        t.start()
        try:
            ready.wait(timeout=2.0)
            ok, reason = is_safe_to_async_raise(t.ident)
            assert ok is True
            assert reason == "ok"
        finally:
            stop.set()
            t.join(timeout=2.0)

    def test_dummy_thread_rejected(self):
        # The Dummy-N prefix is how CPython names threads it didn't
        # create itself (boost::python callbacks land here). Injecting
        # into one would propagate back into PFC's C++ FATAL handler.
        fake = threading.Thread(name="Dummy-99")
        fake.start()
        fake.join()
        # The dead Dummy thread will trip thread_not_alive first; use a
        # live thread with that name to exercise the actual branch.
        ready = threading.Event()
        stop = threading.Event()

        def _worker():
            ready.set()
            stop.wait()

        t = threading.Thread(target=_worker, name="Dummy-42")
        t.start()
        try:
            ready.wait(timeout=2.0)
            ok, reason = is_safe_to_async_raise(t.ident)
            assert ok is False
            assert reason == "nested_boost_python_callback"
        finally:
            stop.set()
            t.join(timeout=2.0)


class TestFireAsyncException:
    def test_terminates_pure_python_loop(self):
        """The whole point of L2: a `while not stop` loop that never
        yields to the PFC callback still unwinds when we async-raise."""
        captured: dict[str, BaseException | None] = {"exc": None}
        running = threading.Event()

        def _worker():
            running.set()
            try:
                # Busy loop with no GIL release; only an injected
                # async exception can break us out.
                while True:
                    pass
            except BaseException as e:
                captured["exc"] = e

        t = threading.Thread(target=_worker, name="async-exc-target")
        t.start()
        try:
            assert running.wait(timeout=2.0)
            # Give the thread a beat to be deep in the loop.
            time.sleep(0.05)
            affected = fire_async_exception(t.ident, BridgeTimeout)
            assert affected == 1
            t.join(timeout=2.0)
            assert not t.is_alive(), "worker did not unwind after async-exc"
            assert isinstance(captured["exc"], BridgeTimeout)
        finally:
            if t.is_alive():
                # Best-effort cleanup so a failing test doesn't leak a thread.
                fire_async_exception(t.ident, SystemExit)
                t.join(timeout=1.0)

    def test_unknown_tid_returns_zero(self):
        # `PyThreadState_SetAsyncExc` documents 0 as "no matching thread".
        assert fire_async_exception(0x7FFFFFFF, BridgeTimeout) == 0
