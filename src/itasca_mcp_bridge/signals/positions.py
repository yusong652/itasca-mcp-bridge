"""Cycle-point positions for ITASCA ``itasca.set_callback`` registration.

Single source of truth for where the bridge's two cycle callbacks attach
in the product solve loop. They must stay ordered::

    INTERRUPT_CALLBACK_POSITION < EXECUTOR_CALLBACK_POSITION

so the interrupt check runs before the cycle-gap snippet executor within
each cycle.

These are valid Itasca cycle points; the
``itasca.set_callback(name, position)`` API is identical across the ITASCA
products and the bridge's cycle-gap interleaving is verified on them (see
the README's Requirements). A once-per-cycle scheme (position ``-1.0``) is
a candidate simplification, but changing these values alters verified
runtime cycle-gap interleaving, so it is deliberately left as a follow-up
to be re-validated on every product rather than changed blindly here.

Python 3.6 compatible.
"""

from typing import Any

INTERRUPT_CALLBACK_POSITION = 50.0
EXECUTOR_CALLBACK_POSITION = 51.0


def register_cycle_callback(itasca_module, name, position):
    # type: (Any, str, float) -> None
    """Register a cycle callback idempotently across ITASCA versions.

    ``itasca.set_callback`` is not idempotent on every product version.
    The 7.0 engine silently accepts re-registering an already-registered
    ``(name, position)``; the 6.0 engine is strict and raises
    ``ValueError: Function <name> is already registered as a callback at
    position <p> in the cycle sequence``. Compounding this, ``model new``
    clears the cycle-callback registry but ``model restore`` does **not**
    on the 6.0 engine, so the bridge's post-restore re-registration hits the
    strict path and aborts the whole ``model restore``. The same strict
    path also breaks a second ``start()`` (e.g. re-running ``addon.py``).

    Removing first makes (re)registration safe on every version and on
    every path (start / model-new / model-restore). ``remove_callback``
    is idempotent (no error when the name is absent); the ``try`` also
    tolerates products/mocks that lack ``remove_callback``.

    Only ever called with the bridge's own reserved callback names at
    their fixed positions, so the remove never touches user callbacks
    (different names coexist at the same position).

    The function name says "cycle" because both reserved callbacks attach
    in the cycling sequence (see the positions above); it is the low-level
    register primitive shared by the interrupt and executor registration.
    """
    try:
        itasca_module.remove_callback(name, position)
    except Exception:
        pass
    itasca_module.set_callback(name, position)
