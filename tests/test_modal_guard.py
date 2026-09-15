"""Tests for utils.modal_guard — dismissing the engine's blocking error box.

The engine can raise a modal QMessageBox on every command error ("Raise
Dialog on Error", on by default in the 6.0 generation of the products).
It holds the C-level itasca.command, and with it the GIL and every bridge
thread, until a person clicks OK — the bridge simply disappears from its
client. The guard clicks it, but only from inside the narrow window where
the bridge itself raised it, and only when the box offers no choice.
"""

from __future__ import annotations

import logging

import pytest
from itasca_mcp_bridge.signals.interrupt import register_interrupt_callback
from itasca_mcp_bridge.utils import modal_guard

from test_interrupt import _FakeItasca


class _FakeButton:
    def __init__(self, label):
        self._label = label
        self.clicks = 0

    def text(self):
        return self._label

    def click(self):
        self.clicks += 1


class _FakeMetaObject:
    def __init__(self, name):
        self._name = name

    def className(self):
        return self._name


class _FakeWidget:
    """Stand-in for whatever `activeModalWidget()` hands back.

    PySide2 does not downcast a widget it did not create, so the real
    code never relies on the Python type: it asks the C++ object with
    `inherits()` and reads the message through the metaobject property.
    The fake answers the same three questions.
    """

    def __init__(self, class_name="QMessageBox", title="", text="", buttons=()):
        self._class_name = class_name
        self._title = title
        self._text = text
        self._buttons = [_FakeButton(b) for b in buttons]

    @property
    def buttons(self):
        return self._buttons

    def inherits(self, name):
        return name == self._class_name

    def metaObject(self):
        return _FakeMetaObject(self._class_name)

    def windowTitle(self):
        return self._title

    def property(self, name):
        return self._text if name == "text" else None

    def findChildren(self, cls):
        return list(self._buttons)


class _FakeQApplication:
    modal = None

    @classmethod
    def activeModalWidget(cls):
        return cls.modal


class _FakeQtWidgets:
    QApplication = _FakeQApplication

    class QAbstractButton:
        pass


def _error_box(buttons=("OK",)):
    return _FakeWidget(
        class_name="QMessageBox",
        title="PFC3D 6.00 Command Processing Error",
        text="Bad conversion of parameter number 2 (foobarbaz).\nExpected tokens:",
        buttons=buttons,
    )


@pytest.fixture(autouse=True)
def guard_state(monkeypatch):
    """Point the guard at the fake binding and reset its module state."""
    monkeypatch.setattr(modal_guard, "_qt_widgets", _FakeQtWidgets)
    monkeypatch.setattr(modal_guard, "_depth", 0)
    monkeypatch.setattr(modal_guard, "_reported", set())
    _FakeQApplication.modal = None
    yield
    _FakeQApplication.modal = None


class TestCommandWindow:
    def test_idle_bridge_never_touches_a_dialog(self):
        """A box raised while the bridge is idle belongs to the person at
        the keyboard, even when it is exactly the box the guard exists for."""
        box = _error_box()
        _FakeQApplication.modal = box
        modal_guard._tick()
        assert box.buttons[0].clicks == 0

    def test_dismissed_while_the_bridge_holds_a_command(self):
        box = _error_box()
        _FakeQApplication.modal = box
        modal_guard.entered()
        modal_guard._tick()
        modal_guard.left()
        assert box.buttons[0].clicks == 1

    def test_window_closes_again_after_the_command_returns(self):
        modal_guard.entered()
        modal_guard.left()
        box = _error_box()
        _FakeQApplication.modal = box
        modal_guard._tick()
        assert box.buttons[0].clicks == 0

    def test_nested_commands_keep_the_window_open(self):
        """`program call` expansion and cycle-callback snippets nest, so the
        inner command returning must not close the window on the outer one."""
        modal_guard.entered()
        modal_guard.entered()
        modal_guard.left()
        box = _error_box()
        _FakeQApplication.modal = box
        modal_guard._tick()
        assert box.buttons[0].clicks == 1

    def test_left_without_entered_does_not_go_negative(self):
        modal_guard.left()
        modal_guard.left()
        modal_guard.entered()
        box = _error_box()
        _FakeQApplication.modal = box
        modal_guard._tick()
        assert box.buttons[0].clicks == 1


class TestWhatItWillAnswer:
    def test_two_button_box_is_left_alone(self):
        """Two buttons mean the engine asked something. Answering it would
        decide on the person's behalf, so the bridge waits instead."""
        box = _error_box(buttons=("Yes", "No"))
        _FakeQApplication.modal = box
        modal_guard.entered()
        modal_guard._tick()
        assert [b.clicks for b in box.buttons] == [0, 0]

    def test_non_messagebox_modal_is_left_alone(self):
        box = _FakeWidget(class_name="QFileDialog", title="Open", buttons=("Open",))
        _FakeQApplication.modal = box
        modal_guard.entered()
        modal_guard._tick()
        assert box.buttons[0].clicks == 0

    def test_declined_dialog_is_reported_once_not_every_tick(self, caplog):
        box = _error_box(buttons=("Abort", "Retry"))
        _FakeQApplication.modal = box
        modal_guard.entered()
        with caplog.at_level(logging.WARNING, logger="itasca-mcp-bridge"):
            for _ in range(5):
                modal_guard._tick()
        blocked = [r for r in caplog.records if "Blocked on a modal dialog" in r.message]
        assert len(blocked) == 1
        assert "Abort" in blocked[0].getMessage()

    def test_a_new_dialog_is_reported_again_after_the_first_clears(self, caplog):
        modal_guard.entered()
        with caplog.at_level(logging.WARNING, logger="itasca-mcp-bridge"):
            _FakeQApplication.modal = _error_box(buttons=("Abort", "Retry"))
            modal_guard._tick()
            _FakeQApplication.modal = None
            modal_guard._tick()
            _FakeQApplication.modal = _error_box(buttons=("Abort", "Retry"))
            modal_guard._tick()
        blocked = [r for r in caplog.records if "Blocked on a modal dialog" in r.message]
        assert len(blocked) == 2

    def test_dismissal_is_logged_with_title_and_message(self, caplog):
        _FakeQApplication.modal = _error_box()
        modal_guard.entered()
        with caplog.at_level(logging.INFO, logger="itasca-mcp-bridge"):
            modal_guard._tick()
        line = [r.getMessage() for r in caplog.records if "Dismissed" in r.message]
        assert len(line) == 1
        assert "Command Processing Error" in line[0]
        assert "Bad conversion" in line[0]
        # One line only: the console and the task log already carry the rest.
        assert "Expected tokens" not in line[0]


class TestNeverBreaksTheHost:
    def test_binding_errors_are_swallowed(self, monkeypatch):
        """This runs on the GUI's event loop; an exception escaping the
        tick would surface in the product, not in the bridge."""

        class _Exploding:
            class QApplication:
                @staticmethod
                def activeModalWidget():
                    raise RuntimeError("binding gone")

        monkeypatch.setattr(modal_guard, "_qt_widgets", _Exploding)
        modal_guard.entered()
        modal_guard._tick()  # must not raise

    def test_widget_errors_are_swallowed(self):
        class _Hostile(_FakeWidget):
            def findChildren(self, cls):
                raise RuntimeError("deleted underneath us")

        _FakeQApplication.modal = _Hostile()
        modal_guard.entered()
        modal_guard._tick()  # must not raise

    def test_tick_is_inert_without_a_binding(self, monkeypatch):
        monkeypatch.setattr(modal_guard, "_qt_widgets", None)
        modal_guard.entered()
        modal_guard._tick()  # must not raise

    def test_install_without_a_qt_binding_returns_false(self, monkeypatch):
        monkeypatch.setattr(modal_guard, "_import_qt", lambda: (None, None))
        assert modal_guard.install() is False

    def test_install_survives_a_binding_that_raises(self, monkeypatch):
        def _boom():
            raise ImportError("no Qt here")

        monkeypatch.setattr(modal_guard, "_import_qt", _boom)
        assert modal_guard.install() is False


class TestWrappedCommandBracket:
    """The window is opened by signals.interrupt's command wrapper, which is
    the single place every engine command the bridge issues goes through."""

    def test_window_is_open_during_the_engine_call(self):
        fake = _FakeItasca()
        seen = []
        raw = fake.command

        def _recording(cmd):
            seen.append(modal_guard._depth)
            return raw(cmd)

        fake.command = _recording
        assert register_interrupt_callback(fake) is True
        fake.command("ball create radius 1")
        assert seen and all(d > 0 for d in seen)
        assert modal_guard._depth == 0

    def test_window_closes_when_the_command_raises(self):
        class _Failing(_FakeItasca):
            def command(self, cmd):
                raise RuntimeError("Error in execution")

        fake = _Failing()
        assert register_interrupt_callback(fake) is True
        with pytest.raises(RuntimeError):
            fake.command("ball foobarbaz 123")
        assert modal_guard._depth == 0
