"""Listing the files the bridge runs in the GUI project's Data Files.

Driven with fake GUI objects shaped like the two Project pane layouts
(6.0/7.0 file lists, 9.x project tree) and a fake EditorModule whose slots
record what they were asked, reached the way the module reaches them on
both bindings: helper signals connected old-style to the slots.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from itasca_mcp_bridge.console import project_files
from itasca_mcp_bridge.console.project_files import _Registrar, normalize


class _Meta:
    def __init__(self, name):
        self._name = name

    def className(self):
        return self._name


class _Obj:
    def __init__(self, cls, visible=True):
        self._cls = cls
        self.visible = visible

    def metaObject(self):
        return _Meta(self._cls)

    def isVisible(self):
        return self.visible

    def windowTitle(self):
        return getattr(self, "title", "")


class _Index:
    def __init__(self, text, tip, children=()):
        self.text = text
        self.tip = tip
        self.children = list(children)


class _TreeModel(_Obj):
    """A nested model: ``index(row, col, parent)`` hands back node objects."""

    def __init__(self, cls, roots):
        super().__init__(cls)
        self.roots = roots

    def index(self, row, col, parent=None):
        nodes = self.roots if parent is None else parent.children
        return nodes[row]

    def rowCount(self, parent=None):
        return len(self.roots if parent is None else parent.children)

    def data(self, index, role=0):
        return index.tip if role == "tip" else index.text


class _Header:
    def __init__(self, model):
        self._model = model

    def model(self):
        return self._model


class _FileListView(_Obj):
    """6.0/7.0: wrapped as a plain widget; the model is on its header."""

    def __init__(self, model):
        super().__init__("itascaxd::FileListTreeView")
        self._header = _Header(model)

    def findChildren(self, cls):
        return [self._header]


class _Tree:
    def __init__(self, model):
        self._model = model

    def model(self):
        return self._model


class _ProjectPage(_Obj):
    def __init__(self, tree_model):
        super().__init__("itasca3d::ProjectPage")
        self._tree = _Tree(tree_model)

    def findChildren(self, cls):
        return [self._tree]


class _Editor(_Obj):
    """The EditorModule: opening lists the file and takes the front tile."""

    def __init__(self, gui):
        super().__init__("itasca3d::EditorModule")
        self.gui = gui
        self.calls = []

    def openDataFile(self, path, restore):
        self.calls.append(("open", path, restore))
        self.gui.listed_paths.append(path)
        dock = _Obj("itascaxd::ItascaDockWidget")
        dock.title = path
        self.gui.widgets.append(dock)
        for other in self.gui.docks():
            if other is not dock:
                other.visible = False  # the new editor took the tile
        self.gui.editor_dock = dock

    def closeDataFile(self, path):
        self.calls.append(("close", path))
        self.gui.widgets.remove(self.gui.editor_dock)
        if self.gui.restores_previous_view:
            for dock in self.gui.docks():
                dock.visible = True


class _Window:
    def __init__(self, editor):
        self._editor = editor

    def metaObject(self):
        return _Meta("pfcengine::MainWindowPFC")

    def findChildren(self, cls, name="", options=None):
        return [_Obj("itasca3d::PlotModule"), self._editor]


class _Gui:
    def __init__(self, layout, restores_previous_view):
        self.listed_paths = []
        self.restores_previous_view = restores_previous_view
        self.widgets = []
        self.activated = []
        self.editor = _Editor(self)
        self.layout = layout
        plot = _Obj("itascaxd::ItascaDockWidget")
        plot.title = "Plot01"
        self.widgets.append(plot)
        self.plot = plot
        gui = self
        self.qt_widgets = SimpleNamespace(
            QApplication=SimpleNamespace(
                allWidgets=lambda: list(gui.widgets) + gui.pane(),
                topLevelWidgets=lambda: [_Window(gui.editor)],
            ),
            QHeaderView=object,
            QTreeView=object,
        )

    def docks(self):
        return [w for w in self.widgets if w.metaObject().className().endswith("ItascaDockWidget")]

    def pane(self):
        if self.layout == "9.x":
            data_files = _Index("Data Files", None, [_Index(p.rsplit("/", 1)[-1], p) for p in self.listed_paths])
            root = _Index("Project : x.prj", "G:/x/x.prj", [data_files, _Index("Plots", None)])
            return [_ProjectPage(_TreeModel("itasca3d::ProjectTreeModel", [root]))]
        if self.layout == "6.0/7.0":
            rows = [_Index(p.rsplit("/", 1)[-1], p + " -- Closed") for p in self.listed_paths]
            saved = _TreeModel("itascaxd::FileListModel_SavedStates", [])
            data = _TreeModel("itascaxd::FileListModel_DataFiles", rows)
            return [_FileListView(saved), _FileListView(data)]
        return [_Obj("itasca3d::ProjectPage")]  # unknown layout: no readable list


class _Timers:
    """``QTimer.singleShot`` fakes: callbacks run when the test flushes them."""

    def __init__(self):
        self.queued = []

    def singleShot(self, ms, fn):
        self.queued.append((ms, fn))

    def flush(self):
        while self.queued:
            self.queued.pop(0)[1]()


def _core(gui):
    """A helper QObject's signals connected old-style to the slots, as on both bindings.

    Queued connections are delivered at once here: the order of effects is
    what the tests check, the event loop is not modelled.
    """
    connections = []
    gui.connection_types = []

    class _Signal:
        def __init__(self, *types):
            self.name = None

        def __set_name__(self, owner, name):
            self.name = name

        def __get__(self, instance, owner):
            name = self.name

            def emit(*args):
                for helper, sig, obj, slot in connections:
                    if helper is instance and sig.startswith(name + "("):
                        getattr(obj, slot.split("(")[0])(*args)

            return SimpleNamespace(emit=emit)

    class _QObject:
        pass

    def connect(helper, sig, obj, slot, kind=None):
        connections.append((helper, sig, obj, slot))
        gui.connection_types.append(kind)
        return True

    gui.timers = _Timers()

    def invoke(obj, method, kind, *packed):
        assert kind == 2 and method == "activate"
        gui.activated.append(obj)
        obj.visible = True
        return True

    return SimpleNamespace(
        Qt=SimpleNamespace(ToolTipRole="tip", DirectConnection=0, QueuedConnection=2, FindDirectChildrenOnly=1),
        QObject=SimpleNamespace(connect=connect),
        QTimer=gui.timers,
        Q_ARG=lambda typ, value: SimpleNamespace(value=value),
        QMetaObject=SimpleNamespace(invokeMethod=invoke),
        SIGNAL=lambda s: s,
        SLOT=lambda s: s,
        Signal=_Signal,
        _QObjectBase=_QObject,
    )


@pytest.fixture(autouse=True)
def _fresh_module(monkeypatch):
    project_files.reset()
    monkeypatch.setattr(project_files, "_task_executing", lambda: False)
    yield
    project_files.reset()


def _registrar(layout="9.x", restores_previous_view=False):
    gui = _Gui(layout, restores_previous_view)
    core = _core(gui)
    # The helper class derives from core.QObject; give the fake one a base.
    core.QObject = type("QObjectNS", (), {"connect": staticmethod(core.QObject.connect)})
    return gui, _Registrar(core, gui.qt_widgets)


def test_reads_the_9x_project_tree(tmp_path):
    gui, registrar = _registrar("9.x")
    gui.listed_paths.append("G:/run/a.dat")
    assert registrar.listed() == {normalize("G:/run/a.dat")}


def test_reads_the_60_70_file_list(tmp_path):
    gui, registrar = _registrar("6.0/7.0")
    gui.listed_paths.append("G:/run/a.dat")
    assert registrar.listed() == {normalize("G:/run/a.dat")}


def test_registers_by_opening_and_closing_through_queued_slots(tmp_path):
    script = tmp_path / "script.dat"
    script.write_text("model new\n")
    gui, registrar = _registrar("9.x")

    assert registrar.register(str(script)) is True
    target = str(script).replace("\\", "/")
    assert gui.editor.calls == [("open", target, True)]  # the close waits one loop pass
    gui.timers.flush()

    assert gui.editor.calls == [("open", target, True), ("close", target)]
    assert set(gui.connection_types) == {2}  # Qt.QueuedConnection, never direct
    assert normalize(str(script)) in registrar.listed()
    assert not any(d.title == target for d in gui.docks())  # no editor tab left


def test_the_same_file_is_not_queued_twice_before_it_settles(tmp_path):
    script = tmp_path / "script.dat"
    script.write_text("model new\n")
    gui, registrar = _registrar("9.x")

    assert registrar.register(str(script)) is True
    gui.listed_paths.clear()  # the GUI has not processed the queue yet
    assert registrar.register(str(script)) is False
    gui.timers.flush()
    assert len(gui.editor.calls) == 2


def test_while_a_task_executes_the_file_is_held_back(tmp_path, monkeypatch):
    script = tmp_path / "script.dat"
    script.write_text("model new\n")
    gui, registrar = _registrar("9.x")
    monkeypatch.setattr(project_files, "_task_executing", lambda: True)

    assert registrar.register(str(script)) is False
    assert gui.editor.calls == []

    monkeypatch.setattr(project_files, "_task_executing", lambda: False)
    registrar.flush_deferred()
    gui.timers.flush()
    assert [c[0] for c in gui.editor.calls] == ["open", "close"]


def test_a_listed_file_is_left_alone(tmp_path):
    script = tmp_path / "script.dat"
    script.write_text("model new\n")
    gui, registrar = _registrar("9.x")
    gui.listed_paths.append(str(script).replace("\\", "/"))

    assert registrar.register(str(script)) is False
    assert gui.editor.calls == []


def test_a_view_the_editor_displaced_is_brought_back(tmp_path):
    # 9.x: the close leaves the tile empty; the parked view is reactivated.
    script = tmp_path / "script.dat"
    script.write_text("model new\n")
    gui, registrar = _registrar("9.x", restores_previous_view=False)

    registrar.register(str(script))
    assert gui.activated == []  # not before the GUI has had its half second
    gui.timers.flush()

    assert gui.activated == [gui.plot]
    assert gui.plot.visible is True


def test_no_reactivation_when_the_gui_restores_the_view_itself(tmp_path):
    # 6.0/7.0: destroying the editor's dock brings the previous view back.
    script = tmp_path / "script.dat"
    script.write_text("model new\n")
    gui, registrar = _registrar("6.0/7.0", restores_previous_view=True)

    assert registrar.register(str(script)) is True
    gui.timers.flush()

    assert gui.activated == []
    assert gui.editor.calls[0][0] == "open" and gui.editor.calls[1][0] == "close"


def test_an_unreadable_pane_disables_registration(tmp_path):
    script = tmp_path / "script.dat"
    script.write_text("model new\n")
    gui, registrar = _registrar("unknown")

    assert registrar.register(str(script)) is False
    assert registrar.register(str(script)) is False
    assert gui.editor.calls == []


def test_a_missing_file_is_not_registered(tmp_path):
    gui, registrar = _registrar("9.x")
    assert registrar.register(str(tmp_path / "nope.dat")) is False
    assert gui.editor.calls == []


def test_refresh_editors_queues_the_foreground_check():
    gui, registrar = _registrar("9.x")
    invoked = []
    gui_core = registrar._core
    gui_core.QMetaObject.invokeMethod = lambda obj, method, kind, *a: invoked.append((obj, method, kind)) or True

    assert registrar.refresh_editors() is True
    assert invoked == [(gui.editor, "onApplicationActivated", 2)]  # queued, on the EditorModule


def test_refresh_editors_waits_while_a_task_executes(monkeypatch):
    gui, registrar = _registrar("9.x")
    monkeypatch.setattr(project_files, "_get_registrar", lambda: registrar)
    monkeypatch.setattr(project_files, "_task_executing", lambda: True)
    registrar.refresh_editors = lambda: (_ for _ in ()).throw(AssertionError("must not be called"))

    project_files.refresh_editors()  # returns quietly


def test_module_register_never_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(project_files, "_get_registrar", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert project_files.register(str(tmp_path / "x.dat")) is False
