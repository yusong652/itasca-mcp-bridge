"""Pump-mode auto-detection.

The bridge picks its main-thread task pump from what the host process
offers: a QTimer on the Qt event loop in a product GUI, a blocking poll
loop in a console build. Both misreadings hurt, asymmetrically:

- Qt chosen without an event loop (product console): the timer attaches
  and never ticks, so the bridge serves HTTP while no task is ever
  executed and every request hangs until it times out.
- blocking chosen in a GUI: the pump seizes the main thread forever and
  the product window freezes, with no way back except killing it.

Hence two independent witnesses, and the blocking pump only when both say
there is no event loop. The fixtures below model the three real hosts
measured on this machine; `_HOSTS` is the table those measurements came
out of.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from itasca_mcp_bridge import runtime


class _FakeTimer:
    def __init__(self):
        self.interval = None
        self.started = False
        self.slots = []
        self.timeout = types.SimpleNamespace(connect=self.slots.append)

    def setInterval(self, ms):  # noqa: N802 - Qt spelling
        self.interval = ms

    def start(self):
        self.started = True

    def stop(self):
        self.started = False


class _FakeMetaObject:
    """One link of a C++ QMetaObject superclass chain."""

    def __init__(self, name, parent=None):
        self._name = name
        self._parent = parent

    def className(self):  # noqa: N802 - Qt spelling
        return self._name

    def superClass(self):  # noqa: N802 - Qt spelling
        return self._parent


def _meta_chain(names):
    """Build a metaobject chain from most-derived to `QObject`."""
    meta = None
    for name in reversed(names):
        meta = _FakeMetaObject(name, meta)
    return meta


class _FakeApp:
    """A host application object.

    `python_type_name` is what the PySide wrapper calls itself, which is a
    fact about the binding rather than about the host: PySide2 will not
    downcast an application it did not create, so on PFC 7.0 GUI it says
    `QCoreApplication` for a real `QApplication`.
    """

    def __init__(self, meta_names, python_type_name):
        self._meta = _meta_chain(meta_names)
        self.__class__ = type(python_type_name, (_FakeApp,), {})

    def metaObject(self):  # noqa: N802 - Qt spelling
        return self._meta


_QOBJECT_TAIL = ["QApplication", "QGuiApplication", "QCoreApplication", "QObject"]

# Measured on this machine, 2026-09-14.
_HOSTS = {
    # PySide2 hands back a generic QCoreApplication wrapper for the host's
    # own C++ QApplication; only the metaobject chain tells the truth.
    "pfc70_gui": (
        ["itasca3d::ItascaApplication", "itasca3d::Application"] + _QOBJECT_TAIL,
        "QCoreApplication",
        1,
    ),
    # PySide6 does downcast, so here the Python type happens to agree.
    "pfc97_gui": (
        ["itasca3d::ItascaApplication", "itasca3d::Application"] + _QOBJECT_TAIL,
        "QApplication",
        1,
    ),
    # Console build: a bare QCoreApplication for Qt infrastructure, and
    # exec() is never called.
    "pfc97_console": (["QCoreApplication", "QObject"], "QCoreApplication", 0),
}


def _install_binding(monkeypatch, binding, app, loop_level=0):
    """Register a fake PySide binding exposing `app` and a thread loop level."""

    class _QCoreApplication:
        @staticmethod
        def instance():
            return app

    class _QThread:
        @staticmethod
        def currentThread():  # noqa: N802 - Qt spelling
            return types.SimpleNamespace(loopLevel=lambda: loop_level)

    qtcore = types.ModuleType(binding + ".QtCore")
    qtcore.QCoreApplication = _QCoreApplication
    qtcore.QThread = _QThread
    qtcore.QTimer = _FakeTimer

    package = types.ModuleType(binding)
    package.QtCore = qtcore

    monkeypatch.setitem(sys.modules, binding, package)
    monkeypatch.setitem(sys.modules, binding + ".QtCore", qtcore)
    return qtcore


def _install_host(monkeypatch, binding, host):
    meta_names, python_type_name, loop_level = _HOSTS[host]
    app = _FakeApp(meta_names, python_type_name)
    _install_binding(monkeypatch, binding, app, loop_level)
    return app


@pytest.fixture(autouse=True)
def _clear_timer_ref(monkeypatch):
    """Drop the module-global timer reference between tests."""
    monkeypatch.setattr(runtime, "_qt_task_timer", None, raising=False)


@pytest.fixture
def _no_real_bindings(monkeypatch):
    """Hide any Qt binding that happens to be installed in the test env."""
    for binding in runtime._QT_BINDINGS:
        monkeypatch.setitem(sys.modules, binding, None)
        monkeypatch.setitem(sys.modules, binding + ".QtCore", None)


def _start(logger=None):
    return runtime._start_qt_pump(
        MagicMock(name="main_executor"),
        20,
        1,
        logger if logger is not None else MagicMock(name="logger"),
    )


@pytest.mark.usefixtures("_no_real_bindings")
def test_console_does_not_win_qt_pump(monkeypatch):
    """PFC 9.7 console: a QCoreApplication for Qt infrastructure, no event loop."""
    _install_host(monkeypatch, "PySide6", "pfc97_console")

    assert _start() is False
    assert runtime._qt_task_timer is None


@pytest.mark.usefixtures("_no_real_bindings")
def test_pyside6_gui_wins_qt_pump(monkeypatch):
    _install_host(monkeypatch, "PySide6", "pfc97_gui")

    assert _start() is True
    timer = runtime._qt_task_timer
    assert timer.started is True
    assert timer.interval == 20
    assert len(timer.slots) == 1


@pytest.mark.usefixtures("_no_real_bindings")
def test_pyside2_gui_wins_despite_undowncast_wrapper(monkeypatch):
    """PFC 7.0 GUI regression guard.

    PySide2 reports the host application as a plain `QCoreApplication` --
    byte for byte what a console reports -- so any gate reading the Python
    type picks the blocking pump here and freezes the GUI.
    """
    app = _install_host(monkeypatch, "PySide2", "pfc70_gui")
    assert type(app).__name__ == "QCoreApplication", "fixture must reproduce the wrapper"

    assert _start() is True
    assert runtime._qt_task_timer.started is True


@pytest.mark.usefixtures("_no_real_bindings")
def test_running_event_loop_wins_without_a_gui_application(monkeypatch):
    """Second witness alone is enough: a live loop can carry the timer.

    Also the `start()`-before-`exec()` insurance in reverse -- a host that
    is not a GUI application but does run a loop is a working Qt pump.
    """
    app = _FakeApp(["QCoreApplication", "QObject"], "QCoreApplication")
    _install_binding(monkeypatch, "PySide6", app, loop_level=1)

    assert _start() is True


@pytest.mark.usefixtures("_no_real_bindings")
def test_gui_application_wins_with_loop_level_zero(monkeypatch):
    """First witness alone is enough: `start()` called before `exec()` begins."""
    meta_names, _, _ = _HOSTS["pfc97_gui"]
    app = _FakeApp(meta_names, "QApplication")
    _install_binding(monkeypatch, "PySide6", app, loop_level=0)

    assert _start() is True


@pytest.mark.usefixtures("_no_real_bindings")
def test_no_application_at_all_falls_back(monkeypatch):
    _install_binding(monkeypatch, "PySide6", None, loop_level=0)

    assert _start() is False
    assert runtime._qt_task_timer is None


@pytest.mark.usefixtures("_no_real_bindings")
def test_no_qt_binding_falls_back():
    assert _start() is False
    assert runtime._qt_task_timer is None


@pytest.mark.usefixtures("_no_real_bindings")
def test_broken_metaobject_still_consults_the_event_loop(monkeypatch):
    """A witness that raises must not be read as a negative answer."""

    class _Hostile(_FakeApp):
        def metaObject(self):  # noqa: N802 - Qt spelling
            raise RuntimeError("no metaobject for you")

    app = _Hostile(["QApplication", "QObject"], "QCoreApplication")
    _install_binding(monkeypatch, "PySide6", app, loop_level=1)

    assert _start() is True


@pytest.mark.usefixtures("_no_real_bindings")
def test_gate_reads_only_qtcore(monkeypatch):
    """The gate must not import a second Qt module to classify the host.

    Importing e.g. `QtGui` for an isinstance check is a step that can fail
    on its own, and a failure inside detection reads as "no Qt", which under
    `mode="auto"` picks the blocking pump and freezes the GUI. Any import
    beyond QtCore fails this test.
    """
    _install_host(monkeypatch, "PySide2", "pfc70_gui")

    reached = []
    real_import = __import__

    def _tripwire(name, *args, **kwargs):
        if name.startswith("PySide") and not name.endswith(".QtCore"):
            reached.append(name)
            raise ImportError("binding submodule unavailable: {}".format(name))
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _tripwire)

    assert _start() is True, "detection broke on a binding whose extras fail to import"
    assert reached == []
