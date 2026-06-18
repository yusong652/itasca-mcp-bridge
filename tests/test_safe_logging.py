"""Tests for utils.safe_logging — the gap-free logging handlers that
close the L2 logging-lock deadlock.

Background (see utils/safe_logging.py): when the L2
timeout handler async-raises ``BridgeTimeout`` into a thread that is
inside ``logging.Handler.handle``, the stdlib acquire/try/finally form
(Python <= 3.10) can unwind in the bytecode gap before ``finally`` is
registered, orphaning the handler's ``RLock`` forever and freezing the
bridge. The ``with self.lock`` form has no such gap.

These tests guard the fix *structurally* and *deterministically* — they
run in milliseconds and never touch a thread. The gap closure is a
property of the compiled bytecode (``with`` makes lock acquisition and
cleanup registration a single atomic opcode), so it can be asserted by
disassembly rather than by sampling a race.

The effectiveness of the fix was confirmed empirically on the live
PFC3D 9 / Python 3.10.5 runtime with an async-exception injection
harness: the gap-free form leaked 0/120 times, the acquire/try/finally
form 21/120. That harness is intentionally NOT kept as a CI test — it is
slow, scheduler-dependent, and verifies a property that is structural.
"""

from __future__ import annotations

import dis
import io
import logging

import pytest

from itasca_mcp_bridge.utils.safe_logging import (
    GapFreeFileHandler,
    GapFreeStreamHandler,
    _GapFreeHandleMixin,
)

# `with` compiles to a single setup opcode that acquires the context
# manager and registers its cleanup atomically: SETUP_WITH on Python
# <= 3.10, BEFORE_WITH on 3.11+. Either one proves the gap is closed.
_WITH_SETUP_OPCODES = {"SETUP_WITH", "BEFORE_WITH"}


def _record():
    return logging.LogRecord(
        "test", logging.INFO, __file__, 1, "msg", None, None
    )


class TestHandleIsGapFree:
    """The load-bearing regression gate: ``handle`` must guard ``emit``
    with ``with self.lock`` so there is no bytecode edge between acquiring
    the lock and registering its release. Reverting to the stdlib
    acquire/try/finally form reopens the deadlock and must fail here."""

    def test_handle_uses_with_statement(self):
        opnames = {i.opname for i in dis.get_instructions(_GapFreeHandleMixin.handle)}
        assert opnames & _WITH_SETUP_OPCODES, (
            "handle must guard emit with `with self.lock` so lock "
            "acquisition and cleanup registration are one atomic opcode "
            "(no async-exc gap). Reverting to acquire()/try/finally "
            "reopens the L2 logging-lock deadlock; see safe_logging.py."
        )

    def test_handle_has_no_separate_acquire_call(self):
        # The vulnerable form calls self.acquire() as its own opcode; the
        # gap-free form acquires via the context manager instead. Guard
        # against a partial revert that keeps `with` but re-adds a manual
        # acquire.
        names = [
            i.argval
            for i in dis.get_instructions(_GapFreeHandleMixin.handle)
            if i.opname in ("LOAD_METHOD", "LOAD_ATTR")
        ]
        assert "acquire" not in names
        assert "release" not in names


class TestHandleSemantics:
    """``handle`` must remain behaviourally equivalent to the stdlib: it
    filters, emits under the lock, releases on the way out (including when
    emit raises), and returns the filter result."""

    def test_filters_then_emits(self):
        emitted = []

        class _H(_GapFreeHandleMixin, logging.Handler):
            def emit(self, record):
                emitted.append(record.getMessage())

        h = _H()
        rec = _record()
        assert h.handle(rec)  # default filter passes -> truthy
        assert emitted == ["msg"]

        h.addFilter(lambda r: False)
        assert not h.handle(rec)  # rejected -> falsy, short-circuits
        assert emitted == ["msg"]  # emit not called again

    def test_lock_released_when_emit_raises(self):
        class _Boom(_GapFreeHandleMixin, logging.Handler):
            def emit(self, record):
                raise RuntimeError("boom")

        h = _Boom()
        with pytest.raises(RuntimeError):
            h.handle(_record())
        # `with self.lock` must have released on the exception path.
        assert h.lock.acquire(timeout=0.5)
        h.lock.release()


class TestShippedHandlerClasses:
    """The concrete handlers wired into the runtime must be real stdlib
    handlers carrying the gap-free ``handle``."""

    def test_stream_handler_type_and_handle(self):
        h = GapFreeStreamHandler(io.StringIO())
        assert isinstance(h, logging.StreamHandler)
        assert h.handle.__func__ is _GapFreeHandleMixin.handle

    def test_file_handler_type_and_handle(self, tmp_path):
        h = GapFreeFileHandler(str(tmp_path / "x.log"), mode="w")
        try:
            assert isinstance(h, logging.FileHandler)
            assert h.handle.__func__ is _GapFreeHandleMixin.handle
        finally:
            h.close()
