"""Tests for signals.positions.register_cycle_callback — the idempotent
remove-before-register primitive shared by the interrupt and executor
callback registration.

Regression coverage for the PFC 6.0 collision: ``itasca.set_callback`` is
strict there (re-registering an already-registered name at the same
position raises ``ValueError: Function <name> is already registered as a
callback at position <p> in the cycle sequence``), and ``model restore``
does not clear the registry, so the bridge's re-registration aborted the
restore. PFC 7.0's ``set_callback`` is lenient, which is why this never
surfaced on 7.0.
"""

from __future__ import annotations

from itasca_mcp_bridge.signals.positions import register_cycle_callback


class _StrictItasca:
    """Mimics PFC 6.0: set_callback raises on a duplicate (name, position);
    remove_callback is idempotent."""

    def __init__(self):
        self._registry = set()
        self.set_calls = 0
        self.remove_calls = 0

    def set_callback(self, name, position):
        self.set_calls += 1
        key = (name, position)
        if key in self._registry:
            raise ValueError(
                "Function {} is already registered as a callback at position {:g} in the cycle sequence.".format(
                    name, position
                )
            )
        self._registry.add(key)

    def remove_callback(self, name, position):
        self.remove_calls += 1
        self._registry.discard((name, position))


class TestRegisterCycleCallback:
    def test_first_registration_calls_remove_then_set(self):
        it = _StrictItasca()
        register_cycle_callback(it, "_pfc_interrupt_check", 50.0)
        assert ("_pfc_interrupt_check", 50.0) in it._registry
        assert it.remove_calls == 1
        assert it.set_calls == 1

    def test_re_registration_does_not_collide_on_strict_product(self):
        """The PFC 6.0 regression: registering an already-registered
        callback must succeed via remove-before-register, not raise."""
        it = _StrictItasca()
        register_cycle_callback(it, "_pfc_interrupt_check", 50.0)
        register_cycle_callback(it, "_pfc_interrupt_check", 50.0)  # would raise without remove-first
        register_cycle_callback(it, "_pfc_interrupt_check", 50.0)
        assert it._registry == {("_pfc_interrupt_check", 50.0)}

    def test_does_not_disturb_other_callbacks_at_same_position(self):
        """remove is keyed by (name, position); a user callback sharing the
        position but with a different name is left untouched."""
        it = _StrictItasca()
        it._registry.add(("user_callback", 50.0))
        register_cycle_callback(it, "_pfc_interrupt_check", 50.0)
        register_cycle_callback(it, "_pfc_interrupt_check", 50.0)
        assert ("user_callback", 50.0) in it._registry
        assert ("_pfc_interrupt_check", 50.0) in it._registry

    def test_tolerates_product_without_remove_callback(self):
        """Older products / stubs may lack remove_callback; the helper must
        still call set_callback rather than blow up."""

        class _NoRemove:
            def __init__(self):
                self.set_calls = 0

            def set_callback(self, name, position):
                self.set_calls += 1

        it = _NoRemove()
        register_cycle_callback(it, "_pfc_executor_callback", 51.0)
        assert it.set_calls == 1

    def test_set_callback_failure_still_propagates(self):
        """A genuine set_callback failure (not a duplicate) must not be
        swallowed — only the remove is best-effort."""

        class _BrokenSet:
            def remove_callback(self, name, position):
                pass

            def set_callback(self, name, position):
                raise RuntimeError("boom")

        import pytest

        with pytest.raises(RuntimeError, match="boom"):
            register_cycle_callback(_BrokenSet(), "_pfc_interrupt_check", 50.0)
