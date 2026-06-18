"""Gap-free logging handlers for the L2 async-termination boundary.

The L2 timeout mechanism (``execution.termination``) aborts a runaway
``execute_code`` snippet by injecting ``BridgeTimeout`` into the thread
running user code via ``PyThreadState_SetAsyncExc``. The exception fires
at the next bytecode edge in the target thread.

CPython's stdlib ``logging.Handler.handle`` (Python <= 3.10) guards
``emit`` with the explicit acquire / try / finally pattern::

    self.acquire()      # CALL_METHOD -> lock now held
    try:                # SETUP_FINALLY -> cleanup registered HERE
        self.emit(record)
    finally:
        self.release()

There are bytecode instructions between ``acquire()`` returning (lock
held) and ``SETUP_FINALLY`` registering the ``finally``. If the injected
``BridgeTimeout`` fires on one of those edges, the stack unwinds with no
``finally`` registered and the handler's ``RLock`` is leaked -- held
forever by a thread that has already returned to the pump loop. The
background WebSocket thread then blocks the first time it logs anything,
freezing the whole bridge until PFC is restarted.

CPython closed this in 3.11 by switching ``handle`` to ``with
self.lock:``, which compiles to a single ``SETUP_WITH`` opcode that
acquires the lock and registers ``__exit__`` atomically -- no exploitable
edge between the two. We backport that form here so the bridge is safe on
the Python versions PFC actually ships (3.6 for PFC 6/7, 3.10 for PFC 9).

Measured on the live PFC3D 9 / Python 3.10.5 runtime: the stdlib
acquire/try/finally form leaked the lock in ~1/3 of injections that
landed inside ``handle``; the ``with self.lock`` form leaked 0/150. See
``pfc-mcp/docs/development/deadlock-analysis.md`` section 1 for the full
analysis.

Python 3.6 compatible implementation.
"""

import logging


class _GapFreeHandleMixin(object):
    """Override ``Handler.handle`` with the ``with self.lock`` form.

    Semantically identical to ``logging.Handler.handle`` but with no
    bytecode gap between lock acquisition and cleanup registration, so an
    async-injected ``BridgeTimeout`` (or any async exception) can never
    orphan ``self.lock``. Mixed in ahead of the concrete handler class so
    this ``handle`` wins via MRO.
    """

    def handle(self, record):
        rv = self.filter(record)
        if rv:
            with self.lock:
                self.emit(record)
        return rv


class GapFreeFileHandler(_GapFreeHandleMixin, logging.FileHandler):
    """``logging.FileHandler`` with the gap-free ``handle`` (see module doc)."""


class GapFreeStreamHandler(_GapFreeHandleMixin, logging.StreamHandler):
    """``logging.StreamHandler`` with the gap-free ``handle`` (see module doc)."""
