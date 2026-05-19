"""Cycle-point positions for ITASCA ``itasca.set_callback`` registration.

Single source of truth for where the bridge's two cycle callbacks attach
in the product solve loop. They must stay ordered::

    INTERRUPT_CALLBACK_POSITION < EXECUTOR_CALLBACK_POSITION

so the interrupt check runs before the cycle-gap snippet executor within
each cycle.

These are valid Itasca cycle points, verified working in PFC. The
``itasca.set_callback(name, position)`` API is identical across ITASCA
products (PFC and FLAC3D confirmed). Whether these exact slot numbers map
to the same point in every product's solve loop has only been confirmed
for PFC. A product-neutral once-per-cycle scheme (position ``-1.0``) is a
candidate simplification, but changing these values alters verified
runtime cycle-gap interleaving, so it is deliberately left as a follow-up
to be validated on PFC + FLAC3D rather than changed blindly here.

Python 3.6 compatible.
"""

INTERRUPT_CALLBACK_POSITION = 50.0
EXECUTOR_CALLBACK_POSITION = 51.0
