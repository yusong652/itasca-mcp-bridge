"""View tracking, driven with a fake GUI shaped like the 9.x products.

Plots and data files live in ``ItascaDockWidget`` docks holding a
``PlotPage`` / ``EditorPage``; a view replaced in its tile stays alive but
hidden. Each plot has its own ``PlotItemWidget`` (a ``QTreeWidget``) in the
Control Panel, and only the current plot's one is visible. The scheduler's
one-pass deferral is driven by hand through ``_Timer.flush()``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from itasca_mcp_bridge.console import views as views_module
from itasca_mcp_bridge.console.history import ConsoleHistory
from itasca_mcp_bridge.console.views import ViewTracker, diff_items


@pytest.fixture
def history(tmp_path):
    return ConsoleHistory(directory=str(tmp_path))


# ---------------------------------------------------------------------------
# Pure
# ---------------------------------------------------------------------------


def test_diff_items_reports_added_and_removed():
    assert diff_items(["Ball", "Legend"], ["Ball", "Wall", "Legend"]) == (["Wall"], [], [])
    assert diff_items(["Ball", "Wall", "Legend"], ["Ball", "Legend"]) == ([], ["Wall"], [])


def test_diff_items_counts_repeats_and_ignores_order():
    assert diff_items(["Ball", "Legend"], ["Ball", "Ball", "Legend"]) == (["Ball"], [], [])
    assert diff_items(["Ball", "Wall"], ["Wall", "Ball"]) == ([], [], [])


def test_diff_items_reports_a_name_changed_in_place():
    # Coloring balls by density renames the item where it stands.
    assert diff_items(["Ball", "Legend"], ["Ball density", "Legend"]) == ([], [], [["Ball", "Ball density"]])


def test_diff_items_does_not_pair_across_a_length_change():
    assert diff_items(["Ball", "Legend"], ["Wall", "Contact", "Legend"]) == (["Wall", "Contact"], ["Ball"], [])


def test_history_keeps_data_field(history):
    history.add("view", "Plot01", data={"kind": "plot", "event": "open"})
    history.add("python", "x = 1")
    entries = history.consume()["entries"]
    assert entries[0]["data"] == {"kind": "plot", "event": "open"}
    assert "data" not in entries[1]


# ---------------------------------------------------------------------------
# Fake GUI
# ---------------------------------------------------------------------------


class _Meta:
    def __init__(self, name):
        self._name = name

    def className(self):
        return self._name


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, fn):
        self.slots.append(fn)

    def emit(self, *args):
        for fn in list(self.slots):
            fn(*args)


class _Widget:
    def __init__(self, cls, parent=None, title="", visible=True):
        self._cls = cls
        self._parent = parent
        self.title = title
        self.visible = visible

    def metaObject(self):
        return _Meta(self._cls)

    def parentWidget(self):
        return self._parent

    def windowTitle(self):
        return self.title

    def isVisible(self):
        return self.visible


class _Item:
    def __init__(self, text):
        self._text = text

    def text(self, column):
        return self._text


class _Model:
    def __init__(self):
        self.rowsInserted = _Signal()
        self.rowsRemoved = _Signal()
        self.dataChanged = _Signal()
        self.modelReset = _Signal()
        self.layoutChanged = _Signal()


class _ItemList(_Widget):
    def __init__(self, items, visible=False):
        super().__init__("itasca3d::PlotItemWidget", visible=visible)
        self.items = list(items)
        self._model = _Model()

    def model(self):
        return self._model

    def topLevelItemCount(self):
        return len(self.items)

    def topLevelItem(self, i):
        return _Item(self.items[i] + " ")  # the product pads some names

    def add(self, name):
        self.items.insert(-1, name)
        self._model.rowsInserted.emit()


class _Timer:
    queued = []

    @staticmethod
    def singleShot(ms, fn):
        _Timer.queued.append(fn)

    @staticmethod
    def flush():
        while _Timer.queued:
            _Timer.queued.pop(0)()


class _Gui:
    """The product: docks, pages, item lists, focus."""

    def __init__(self):
        _Timer.queued.clear()
        self.widgets = []
        self.focus = None
        self.app = SimpleNamespace(focusChanged=_Signal())
        gui = self
        self.core = SimpleNamespace(
            QTimer=_Timer,
            QCoreApplication=SimpleNamespace(instance=lambda: gui.app),
        )
        self.qt_widgets = SimpleNamespace(
            QApplication=SimpleNamespace(
                allWidgets=lambda: list(gui.widgets),
                focusWidget=lambda: gui.focus,
            )
        )

    def view(self, kind, title, visible=True):
        dock = _Widget("itascaxd::ItascaDockWidget", title=title, visible=visible)
        page = _Widget("itasca3d::PlotPage" if kind == "plot" else "itasca3d::EditorPage", parent=dock)
        body = _Widget("itasca3d::PlotDisplay" if kind == "plot" else "itasca3d::DataTextEdit", parent=page)
        self.widgets += [dock, page, body]
        return SimpleNamespace(dock=dock, page=page, body=body)

    def item_list(self, items, visible=False):
        item_list = _ItemList(items, visible=visible)
        self.widgets.append(item_list)
        return item_list

    def focus_on(self, widget):
        old, self.focus = self.focus, widget
        self.app.focusChanged.emit(old, widget)
        _Timer.flush()

    def remove(self, view):
        for w in (view.dock, view.page, view.body):
            self.widgets.remove(w)


@pytest.fixture
def gui():
    return _Gui()


def _install(gui, history):
    tracker = ViewTracker(history, gui.core, gui.qt_widgets)
    assert tracker.install() is True
    return tracker


def _entries(history):
    return [(e["source"], e["input"], e.get("data")) for e in history.consume(limit=100)["entries"]]


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def test_install_reports_open_views_and_the_plot_on_screen(gui, history):
    gui.view("data_file", "script", visible=False)
    gui.view("plot", "Plot01")
    gui.item_list(["Ball", "Legend"], visible=True)

    _install(gui, history)

    assert _entries(history) == [
        ("view", "script", {"kind": "data_file", "event": "open"}),
        ("view", "Plot01", {"kind": "plot", "event": "open", "active": True, "items": ["Ball", "Legend"]}),
    ]


def test_switching_views_reports_active_without_closing_the_hidden_one(gui, history):
    editor = gui.view("data_file", "script", visible=False)
    plot = gui.view("plot", "Plot01")
    item_list = gui.item_list(["Ball", "Legend"], visible=True)
    _install(gui, history)
    _entries(history)

    # The editor replaces the plot in its tile; the plot's dock stays alive.
    editor.dock.visible, plot.dock.visible, item_list.visible = True, False, False
    gui.focus_on(editor.body)
    plot.dock.visible, editor.dock.visible, item_list.visible = True, False, True
    gui.focus_on(plot.body)

    assert _entries(history) == [
        ("view", "script", {"kind": "data_file", "event": "active"}),
        ("view", "Plot01", {"kind": "plot", "event": "active", "items": ["Ball", "Legend"]}),
    ]


def test_new_plot_gets_its_own_item_list(gui, history):
    plot1 = gui.view("plot", "Plot01")
    list1 = gui.item_list(["Ball", "Legend"], visible=True)
    _install(gui, history)
    _entries(history)

    plot1.dock.visible, list1.visible = False, False
    plot2 = gui.view("plot", "Plot02")
    list2 = gui.item_list(["Legend"], visible=True)
    gui.focus_on(plot2.body)
    list2.add("DFN Contour Plot")
    _Timer.flush()

    assert _entries(history) == [
        ("view", "Plot02", {"kind": "plot", "event": "open", "active": True, "items": ["Legend"]}),
        ("plot_item", "Plot02", {"items": ["DFN Contour Plot", "Legend"], "added": ["DFN Contour Plot"]}),
    ]
    # Plot01's list was not mistaken for Plot02's.
    assert list1.items == ["Ball", "Legend"]


def test_item_removed(gui, history):
    gui.view("plot", "Plot01")
    item_list = gui.item_list(["Ball", "Wall", "Legend"], visible=True)
    _install(gui, history)
    _entries(history)

    item_list.items.remove("Ball")
    item_list.model().rowsRemoved.emit()
    _Timer.flush()

    assert _entries(history) == [
        ("plot_item", "Plot01", {"items": ["Wall", "Legend"], "removed": ["Ball"]}),
    ]


def test_focus_outside_every_view_changes_nothing(gui, history):
    editor = gui.view("data_file", "script")
    gui.view("plot", "Plot01")
    gui.item_list(["Legend"], visible=False)
    _install(gui, history)
    gui.focus_on(editor.body)
    _entries(history)

    console = _Widget("itascaxd::TextOutput")
    gui.widgets.append(console)
    gui.focus_on(console)
    gui.focus_on(None)

    assert _entries(history) == []


def test_visible_item_list_names_the_current_plot_without_focus(gui, history):
    editor = gui.view("data_file", "script")
    plot1 = gui.view("plot", "Plot01")
    list1 = gui.item_list(["Ball", "Legend"], visible=True)
    _install(gui, history)  # Plot01 is the only plot on screen: its list is paired
    gui.focus_on(editor.body)
    _entries(history)

    # The product is in the background (no focus widget) when the person
    # brings Plot01 back through its tab: the Control Panel follows.
    list1.visible = False
    gui.focus_on(None)
    list1.visible = True
    gui.app.focusChanged.emit(None, None)
    _Timer.flush()

    assert _entries(history) == [
        ("view", "Plot01", {"kind": "plot", "event": "active", "items": ["Ball", "Legend"]}),
    ]
    assert plot1.dock.visible


def test_item_changed_in_place(gui, history):
    gui.view("plot", "Plot01")
    item_list = gui.item_list(["Ball", "Legend"], visible=True)
    _install(gui, history)
    _entries(history)

    item_list.items[0] = "Ball density"
    item_list.model().dataChanged.emit()
    _Timer.flush()

    assert _entries(history) == [
        ("plot_item", "Plot01", {"items": ["Ball density", "Legend"], "changed": [["Ball", "Ball density"]]}),
    ]


def test_dock_gone_reports_closed(gui, history):
    gui.view("data_file", "script")
    plot = gui.view("plot", "Plot01")
    _install(gui, history)
    _entries(history)

    gui.remove(plot)
    gui.focus_on(None)

    assert _entries(history) == [("view", "Plot01", {"kind": "plot", "event": "closed"})]


def test_rename_is_reported_with_the_previous_name(gui, history):
    editor = gui.view("data_file", "script")
    _install(gui, history)
    _entries(history)

    editor.dock.title = "consolidate.dat"
    gui.focus_on(editor.body)

    entries = _entries(history)
    assert entries[0] == (
        "view", "consolidate.dat", {"kind": "data_file", "event": "renamed", "previous": "script"},
    )


def test_signals_arriving_together_share_one_look(gui, history, monkeypatch):
    gui.view("plot", "Plot01")
    tracker = _install(gui, history)
    looks = []
    monkeypatch.setattr(tracker, "look", lambda: looks.append(1))

    for _ in range(5):
        gui.app.focusChanged.emit(None, None)
    _Timer.flush()

    assert looks == [1]


def test_lists_without_model_access_leave_views_working(gui, history):
    gui.view("plot", "Plot01")
    item_list = gui.item_list(["Legend"], visible=True)
    item_list.model = None  # a binding that hands back a generic QWidget

    plot = gui.view("plot", "Plot02", visible=False)
    _install(gui, history)
    gui.focus_on(plot.body)

    # Views and focus still work; no item names are reported.
    assert _entries(history) == [
        ("view", "Plot01", {"kind": "plot", "event": "open"}),
        ("view", "Plot02", {"kind": "plot", "event": "open"}),
        ("view", "Plot02", {"kind": "plot", "event": "active"}),
    ]


def test_uninstall_stops_recording(gui, history):
    editor = gui.view("data_file", "script")
    tracker = _install(gui, history)
    _entries(history)

    tracker.uninstall()
    gui.remove(editor)
    gui.focus_on(None)

    assert _entries(history) == []


def test_install_without_application_is_unavailable(gui, history):
    gui.core.QCoreApplication = SimpleNamespace(instance=lambda: None)
    assert ViewTracker(history, gui.core, gui.qt_widgets).install() is False


def test_module_install_replaces_previous_tracker(gui, history, monkeypatch):
    gui.view("plot", "Plot01")
    monkeypatch.setattr(
        "itasca_mcp_bridge.utils.modal_guard._import_qt", lambda: (gui.core, gui.qt_widgets)
    )
    assert views_module.install(history) is True
    first = views_module._tracker
    assert views_module.install(history) is True
    assert first._active is False and views_module._tracker is not first
    views_module.uninstall()


# ---------------------------------------------------------------------------
# The bridge's own code, and the console
# ---------------------------------------------------------------------------


@pytest.fixture
def installed(gui, history, monkeypatch):
    """The module-level tracker, as start() leaves it."""
    monkeypatch.setattr(
        "itasca_mcp_bridge.utils.modal_guard._import_qt", lambda: (gui.core, gui.qt_widgets)
    )
    assert views_module.install(history) is True
    yield views_module._tracker
    views_module.uninstall()


def test_what_bridge_code_opened_is_in_the_next_read(gui, history, installed):
    _entries(history)
    gui.view("plot", "AgentPlot")  # `plot create` from execute_code, product in the background

    views_module.after_bridge_code()

    assert _entries(history) == [("view", "AgentPlot", {"kind": "plot", "event": "open"})]


def test_views_built_after_the_command_returns_are_seen_one_pass_later(gui, history, installed):
    _entries(history)
    views_module.after_bridge_code()
    gui.view("plot", "AgentPlot")  # the viewer builds it from the event loop
    _Timer.flush()

    assert _entries(history) == [("view", "AgentPlot", {"kind": "plot", "event": "open"})]


def test_inside_a_cycle_callback_the_gui_is_not_read(gui, history, installed, monkeypatch):
    monkeypatch.setattr("itasca_mcp_bridge.signals.cycle_executor.in_cycle_callback", lambda: True)
    looks = []
    monkeypatch.setattr(installed, "look", lambda: looks.append(1))

    views_module.after_bridge_code()
    _Timer.flush()

    assert looks == []


def test_a_line_typed_at_the_prompt_triggers_a_look(gui, history, installed):
    _entries(history)
    gui.view("plot", "Plot02")  # `plot create` typed at the prompt: no focus change

    history.add("command", "plot create")
    _Timer.flush()

    assert _entries(history) == [
        ("command", "plot create", None),
        ("view", "Plot02", {"kind": "plot", "event": "open"}),
    ]


def test_console_hook_keeps_the_servers_doorbell(gui, tmp_path, monkeypatch):
    rung = []
    history = ConsoleHistory(directory=str(tmp_path))
    history.on_new_entry = rung.append
    monkeypatch.setattr(
        "itasca_mcp_bridge.utils.modal_guard._import_qt", lambda: (gui.core, gui.qt_widgets)
    )
    views_module.install(history)
    try:
        history.add("python", "x = 1")
    finally:
        views_module.uninstall()

    assert [e["input"] for e in rung] == ["x = 1"]
