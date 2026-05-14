"""
Async exception injection for terminating stuck execute_code snippets.

Used by the execute_code timeout handler: when a snippet blows its
timeout we inject ``BridgeTimeout`` into the thread that is running
user code so the code unwinds at the next Python bytecode edge,
freeing PFC's main thread.

This is the L2 mechanism. L1 (``signals.request_interrupt`` + the
PFC interrupt callback) handles cycle-gap cancellation - it pauses
``it.cycle(...)``-style loops cleanly but cannot interrupt pure Python
loops (``while True``, ``time.sleep``, busy arithmetic). L2 fills that
gap.

Safety: ``Dummy-N`` threads MUST NOT receive an injected exception -
the exception would propagate back into ``boost::python`` and trigger
ITASCA's C++ FATAL handler. MainThread is intentionally allowed: in
PFC GUI mode the bridge task pump runs on MainThread via a Qt timer,
so a stuck snippet is always sitting on MainThread's Python stack.
The caller looks up the target thread via ``get_exec_thread``, which
only returns a value while ``run_snippet`` is inside its try-block,
so MainThread is demonstrably running user code (not idling in the Qt
event loop). ``BridgeTimeout`` is caught explicitly in ``run_snippet``
and never propagates out.

Python 3.6 compatible implementation.
"""

import ctypes
import threading


class BridgeTimeout(BaseException):
    """Sentinel exception injected by the bridge to abort a stuck snippet.

    Inherits from ``BaseException`` (not ``Exception``) so well-meaning
    user code that does ``except Exception`` cannot swallow it.
    """


def _find_thread(thread_id):
    # type: (int) -> object
    """Return the ``Thread`` object for the given ident, or None.

    ``threading.enumerate()`` is a snapshot; a thread that exited
    between enumeration and our inspection is reported as None.
    """
    for t in threading.enumerate():
        if t.ident == thread_id:
            return t
    return None


def is_safe_to_async_raise(thread_id):
    # type: (int) -> tuple
    """Check whether it is safe to inject an exception into ``thread_id``.

    Returns ``(True, "ok")`` when injection is allowed, otherwise
    ``(False, reason)`` with a short machine-readable reason code.

    Rejected cases:

    * ``thread_not_alive``: thread already exited or never existed.
    * ``nested_boost_python_callback``: name starts with ``Dummy-`` -
      injecting would escape via ``boost::python`` and trigger ITASCA's
      C++ FATAL handler.

    MainThread is accepted; see the module docstring for the rationale.
    """
    target = _find_thread(thread_id)
    if target is None or not target.is_alive():
        return False, "thread_not_alive"
    if target.name.startswith("Dummy-"):
        return False, "nested_boost_python_callback"
    return True, "ok"


def fire_async_exception(thread_id, exc_type):
    # type: (int, type) -> int
    """Inject ``exc_type`` into the target thread via CPython's async
    exception API. Returns the number of threads affected.

    Semantics per ``PyThreadState_SetAsyncExc`` (CPython docs):

    * Return 0  -> the thread_id was invalid / no matching thread.
    * Return 1  -> success; exception will fire at the next bytecode
      edge in the target thread.
    * Return >1 -> API misuse; docs mandate immediately undoing by
      calling again with NULL. We do that and return -1 to signal the
      caller something went wrong.

    The exception fires only at Python bytecode edges. Threads stuck
    in C code (numpy, scipy, GIL-releasing I/O, PFC C-extension cycle)
    receive the exception queued but it does not fire until control
    returns to Python.
    """
    exc = ctypes.py_object(exc_type)
    tid = ctypes.c_ulong(thread_id)
    affected = ctypes.pythonapi.PyThreadState_SetAsyncExc(tid, exc)
    if affected > 1:
        ctypes.pythonapi.PyThreadState_SetAsyncExc(tid, ctypes.c_void_p())
        return -1
    return int(affected)
