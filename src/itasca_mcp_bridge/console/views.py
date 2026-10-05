# -*- coding: utf-8 -*-
"""What is open in the product GUI: plots, data files, plot items.

Typing is not the only thing a person does in the GUI. Which plot they have
in front of them, which data file they are editing, and which items they
just added to a plot say as much about what they are after as the command
they type next. This module reports those as entries in the same
``ConsoleHistory`` the typed input goes to: ``"view"`` entries when a plot
or a data file is opened, closed, renamed or brought to the front, and
``"plot_item"`` entries when items are added to, removed from or changed
in a plot.

Whoever made the change. The bridge's own code opens plots too, and the
engine runs the GUI's event loop while it executes a command, so a change
cannot be told apart by when it was seen. It need not be: the client
receives these as what changed in the GUI since its last call, and knows
what its own code did.

Names only, never contents. A data file is read from the working directory
and a plot is seen by exporting it; both are the client's to do, with the
name this module hands it.

Where things are (measured on the 9.x products)
    Every plot and every data file editor lives in an
    ``itascaxd::ItascaDockWidget`` whose window title is the plot's name or
    the file's name, with an ``itasca3d::PlotPage`` or
    ``itasca3d::EditorPage`` inside it. A view that another one replaced in
    its tile is parked, alive, in a hidden splitter the viewer names "'DOOM'
    hidden container for deleted widgets" -- despite the name, the person
    brings it back from there by switching to it. The x on a view's tab
    parks it there too: the plot still exists and can still be exported.
    So the set of open views is the set of docks holding one of those
    pages, wherever they sit, and a view is closed when its dock is gone.

    Each plot gets its own item list, an ``itasca3d::PlotItemWidget`` (a
    ``QTreeWidget``) created the first time the plot is brought up. All of
    them live in the Control Panel and only the one for the current plot is
    visible. Nothing in the list names its plot, so a list is attached to
    a plot when it is the one visible while that plot is the current view
    and neither has a partner yet; once attached it stays.

Where things are on 6.0/7.0 (measured on PFC 7.0, PySide2 5.11)
    Same docks and pages, but the x on a view's tab destroys its dock: the
    view is reported closed. Each plot's dock also holds an item list of its
    own, never shown, that mirrors the one in the Control Panel; a list
    inside a plot's dock is that plot's, with nothing to guess. It is put
    there late -- a plot open when the product starts has it outside every
    dock until the person has worked with the plot -- so the Control Panel's
    copy may be paired first, as on 9.x; a plot keeps one list. The binding
    wraps the lists as plain ``QWidget``, without the tree's methods, so
    the items are read from the tree's model, which is a child object of
    the list.

Which view is current
    The view holding the focus widget, when there is one. Otherwise the
    plot whose item list is the visible one: that keeps being true when the
    person clicks into the Control Panel, and when the product is not the
    foreground application at all (no focus widget then).

When to look
    No timer. ``QApplication.focusChanged`` fires when the person opens,
    switches to or closes a view with the mouse, each item list's model
    reports rows coming and going, and every line the person enters in a
    console is a look too (``plot create`` typed at the prompt moves no
    focus). Every such signal schedules one look a single event-loop pass
    later -- by then the product has finished rearranging (a closing view,
    a Control Panel switching lists) -- and signals that arrive before
    that look share it.

    A plot the bridge's code creates while the product is in the
    background moves no focus and has no item list yet, so nothing above
    fires. ``execute_code`` and task scripts therefore end with a look --
    at once, so the client's very next read has it, and again one pass
    later, as the viewer builds some views from the event loop. Not from
    inside a cycle callback: the GUI is not read from there.

Identity
    A view or list is recognised again by holding its wrapper: the binding
    hands back the same wrapper for a C++ object as long as one is alive,
    so ``is`` works. ``id()`` of a wrapper that was let go does not.

    PySide2 5.11 breaks that in one case: ``child.parentWidget()`` ties the
    child's wrapper to the parent's, and when the parent's wrapper is
    collected the child's is declared dead ("Internal C++ object already
    deleted") though the widget is alive; the next look gets a new wrapper
    for it. So nothing here walks up the parent chain -- containment is
    asked with ``isAncestorOf`` -- and when someone else's code does, a
    view whose dock went missing while one of the same kind and name
    turned up is the same view, not one closed and one opened.

Python 3.6 compatible implementation.
"""

import logging

from .history import SOURCE_COMMAND, SOURCE_PLOT_ITEM, SOURCE_PYTHON, SOURCE_VIEW

logger = logging.getLogger("itasca-mcp-bridge")

# C++ class names, namespace stripped: the 3D products use `itasca3d::`, the
# 2D ones `itasca2d::`, the shared widgets `itascaxd::`.
DOCK_CLASS = "ItascaDockWidget"
PAGE_KINDS = {"PlotPage": "plot", "EditorPage": "data_file"}
ITEM_LIST_CLASS = "PlotItemWidget"

FOCUS_SIGNAL = "focusChanged(QWidget*,QWidget*)"

KIND_PLOT = "plot"
KIND_DATA_FILE = "data_file"

EVENT_OPEN = "open"
EVENT_ACTIVE = "active"
EVENT_CLOSED = "closed"
EVENT_RENAMED = "renamed"

# Module-level reference: a tracker nothing points at is collected and its
# slots stop firing.
_tracker = None  # type: object


def diff_items(old, new):
    # type: (list, list) -> tuple
    """Items added, removed and changed between two item lists.

    A plot can hold two items of the same kind ("Ball" twice), so added and
    removed are a multiset difference, in the order the names appear. An
    item's name carries some of its attributes -- coloring balls by density
    turns "Ball" into "Ball density" in place -- so a name that differs at
    the same position, with the list otherwise the same length, is one item
    changed (``[old, new]``) rather than one removed and one added.
    """
    removed = list(old)
    added = []
    for name in new:
        if name in removed:
            removed.remove(name)
        else:
            added.append(name)

    changed = []
    if len(old) == len(new):
        for before, after in zip(old, new):
            if before != after and before in removed and after in added:
                removed.remove(before)
                added.remove(after)
                changed.append([before, after])
    return added, removed, changed


def class_name(obj):
    # type: (object) -> str
    """The object's own C++ class, namespace stripped, or None.

    Asked of the metaobject: PySide2 does not downcast objects it did not
    create, so the Python type can be a base class of what is really there.
    """
    try:
        name = obj.metaObject().className()
    except Exception:
        return None
    return str(name).split("::")[-1]


def view_name(dock):
    # type: (object) -> str
    """The dock's title, as the plot or file is named in the GUI."""
    try:
        title = dock.windowTitle()
    except Exception:
        return None
    return str(title).strip() if title is not None else None


class _View(object):
    __slots__ = ("dock", "kind", "name")

    def __init__(self, dock, kind, name):
        self.dock = dock
        self.kind = kind
        self.name = name


class _ItemList(object):
    __slots__ = ("widget", "model", "view", "items", "embedded")

    def __init__(self, widget, model):
        self.widget = widget
        self.model = model
        self.view = None  # the _View it belongs to, once known
        self.items = None  # the names last reported for it
        self.embedded = False  # inside its plot's own dock (6.0/7.0)


class ViewTracker(object):
    """Watches the GUI's plots and data files and records what changes."""

    def __init__(self, history, qt_core, qt_widgets):
        self._history = history
        self._core = qt_core
        self._widgets = qt_widgets
        self._views = []  # type: list
        self._lists = []  # type: list
        self._current = None  # type: object
        self._pending = False
        self._active = False
        self._lists_readable = True
        self._connected = []  # type: list

    # -- install -------------------------------------------------------------

    def install(self):
        # type: () -> bool
        """Hook the focus signal and record what is open right now."""
        app = self._core.QCoreApplication.instance()
        if app is None:
            return False
        if not self._connect(app, "focusChanged", FOCUS_SIGNAL):
            logger.info("View tracking unavailable: cannot connect to focusChanged")
            return False
        self._follow_console_input()
        self._active = True
        # What is open when the bridge starts is worth knowing too.
        self.look()
        logger.info(
            "View tracking installed (%d open: %s)",
            len(self._views), ", ".join(v.name or "?" for v in self._views),
        )
        return True

    def uninstall(self):
        """Stop recording. The connections stay; their slot does nothing.

        Same reason as the console hooks: disconnecting from a live widget
        has taken a PySide2 product down.
        """
        self._active = False

    def _connect(self, obj, attribute, signature):
        # type: (object, str, str) -> bool
        """Connect ``obj``'s signal to the scheduler, new-style or old-style.

        PySide2 as the 6.0/7.0 products ship it hands back generic wrappers
        without the subclass's signal attributes; there the old-style
        string connect is the one that works.
        """
        try:
            getattr(obj, attribute).connect(self._schedule)
        except Exception:
            try:
                ok = self._core.QObject.connect(obj, self._core.SIGNAL(signature), self._schedule)
            except Exception:
                return False
            if not ok:
                return False
        self._connected.append(obj)
        return True

    def _follow_console_input(self):
        """Look again after every line the person enters in a console.

        Chained onto the history's new-entry callback, which the server set
        when it built the history (its SSE doorbell); that one still runs
        first.
        """
        history = self._history
        previous = getattr(history, "on_new_entry", None)
        tracker = self

        def on_new_entry(entry):
            if previous is not None:
                previous(entry)
            if entry.get("source") in (SOURCE_PYTHON, SOURCE_COMMAND):
                tracker._schedule()

        try:
            history.on_new_entry = on_new_entry
        except Exception:
            pass

    # -- scheduling ----------------------------------------------------------

    def _schedule(self, *args):
        """Ask for one look at the GUI after the current event is done."""
        if not self._active or self._pending:
            return
        self._pending = True
        self._core.QTimer.singleShot(0, self._run)

    def _run(self):
        self._pending = False
        self.look()

    # -- looking -------------------------------------------------------------

    def look(self):
        # type: () -> None
        """Look at the GUI now and record what changed.

        Never raises: a widget the binding cannot describe must not break
        the product's event loop or the bridge's request.
        """
        if not self._active:
            return
        try:
            self._look()
        except Exception as e:
            logger.debug("View tracking could not look at the GUI: %s", e)

    def _look(self):
        try:
            widgets = self._widgets.QApplication.allWidgets()
        except Exception:
            return

        docks = []
        pages = []
        lists = []
        for widget in widgets:
            name = class_name(widget)
            if name == DOCK_CLASS:
                docks.append(widget)
            elif name in PAGE_KINDS:
                pages.append((widget, PAGE_KINDS[name]))
            elif name == ITEM_LIST_CLASS:
                lists.append(widget)

        entries = []  # (name, data, source), recorded in this order
        opened = self._update_views(docks, pages, entries)
        self._update_lists(lists)
        self._update_current(opened, entries)
        self._update_items(entries)

        for name, data, source in entries:
            self._history.add(source, name, data=data)

    def _update_views(self, all_docks, pages, entries):
        # type: (list, list, list) -> list
        """Track the docks holding a plot or editor page; record opens and closes."""
        docks = []
        for page, kind in pages:
            dock = _containing(all_docks, page)
            if dock is None:
                continue
            if not any(dock is d for d, _ in docks):
                docks.append((dock, kind))

        for view in list(self._views):
            if any(view.dock is d for d, _ in docks):
                continue
            # Its wrapper may have been replaced under us (see Identity).
            for dock, kind in docks:
                if kind == view.kind and view_name(dock) == view.name and self._find_view(dock) is None:
                    view.dock = dock
                    break
            else:
                self._views.remove(view)
                for item_list in self._lists:
                    if item_list.view is view:
                        item_list.view = None
                if self._current is view:
                    self._current = None
                entries.append((view.name, {"kind": view.kind, "event": EVENT_CLOSED}, SOURCE_VIEW))

        opened = []
        for dock, kind in docks:
            view = self._find_view(dock)
            name = view_name(dock)
            if view is None:
                view = _View(dock, kind, name)
                self._views.append(view)
                opened.append(view)
                entries.append((name, {"kind": kind, "event": EVENT_OPEN}, SOURCE_VIEW))
            elif name and name != view.name:
                entries.append((name, {"kind": kind, "event": EVENT_RENAMED, "previous": view.name}, SOURCE_VIEW))
                view.name = name
        return opened

    def _update_lists(self, lists):
        # type: (list) -> None
        """Track every plot item list; hook its model so changes are seen."""
        self._lists = [l for l in self._lists if any(l.widget is w for w in lists)]
        if not self._lists_readable:
            return
        for widget in lists:
            if any(l.widget is widget for l in self._lists):
                continue
            model = self._model_of(widget)
            if model is None:
                self._lists_readable = False
                logger.info("Plot item lists are not readable with this Qt binding")
                return
            for signal in ("rowsInserted", "rowsRemoved", "dataChanged", "modelReset", "layoutChanged"):
                try:
                    getattr(model, signal).connect(self._schedule)
                except Exception:
                    pass
            self._connected.append(model)
            self._lists.append(_ItemList(widget, model))

        # A list inside a plot's own dock is that plot's.
        plot_docks = [v.dock for v in self._views if v.kind == KIND_PLOT]
        for item_list in self._lists:
            if item_list.view is None:
                dock = _containing(plot_docks, item_list.widget)
                if dock is None:
                    continue
                item_list.embedded = True
                # The product builds it late (it can turn up in the dock
                # after the Control Panel's copy was paired): one list a plot.
                view = self._find_view(dock)
                if not any(l.view is view for l in self._lists):
                    item_list.view = view

    def _model_of(self, widget):
        # type: (object) -> object
        """The item list's model, or None when the binding cannot reach it."""
        try:
            return widget.model()
        except Exception:
            pass
        # A wrapper without the tree's methods: the model is a child object.
        try:
            for model in widget.findChildren(self._core.QAbstractItemModel):
                return model
        except Exception:
            pass
        return None

    def _update_current(self, opened, entries):
        # type: (list, list) -> None
        """Work out the current view, attach item lists, record a change."""
        visible = [l for l in self._lists if not l.embedded and _is_visible(l.widget)]
        candidate = self._view_with_focus()

        # Attaching a list needs to know which plot is up. The focused view
        # says so; failing that, a plot that is the only one on screen does
        # -- good enough to pair a list with, not to call it current: the
        # person may have been in a data file next to it.
        owner = candidate
        if owner is None:
            visible_plots = [v for v in self._views if v.kind == KIND_PLOT and _is_visible(v.dock)]
            if len(visible_plots) == 1:
                owner = visible_plots[0]
        if owner is not None and owner.kind == KIND_PLOT and len(visible) == 1:
            item_list = visible[0]
            owned = any(l.view is owner for l in self._lists)
            if item_list.view is None and not owned:
                item_list.view = owner

        if candidate is None:
            # Focus is outside every view (the Control Panel, the console,
            # another application): the visible item list still names the
            # plot the person last brought up.
            if len(visible) == 1 and visible[0].view is not None:
                candidate = visible[0].view

        if candidate is None or candidate is self._current:
            return
        self._current = candidate

        items = self._items_of(candidate)
        if candidate in opened:
            for name, data, source in entries:
                if source == SOURCE_VIEW and name == candidate.name and data.get("event") == EVENT_OPEN:
                    data["active"] = True
                    if items is not None:
                        data["items"] = items
                    return
        data = {"kind": candidate.kind, "event": EVENT_ACTIVE}
        if items is not None:
            data["items"] = items
        entries.append((candidate.name, data, SOURCE_VIEW))

    def _update_items(self, entries):
        # type: (list) -> None
        """Record items added to or removed from each plot with a known list."""
        for item_list in self._lists:
            if item_list.view is None:
                continue
            items = _read_items(item_list)
            if items is None:
                continue
            if item_list.items is None:
                item_list.items = items
                continue
            if items == item_list.items:
                continue
            added, removed, changed = diff_items(item_list.items, items)
            item_list.items = items
            if not added and not removed and not changed:
                continue  # reordered only
            data = {"items": items}
            if added:
                data["added"] = added
            if removed:
                data["removed"] = removed
            if changed:
                data["changed"] = changed
            entries.append((item_list.view.name, data, SOURCE_PLOT_ITEM))

    # -- helpers -------------------------------------------------------------

    def _find_view(self, dock):
        for view in self._views:
            if view.dock is dock:
                return view
        return None

    def _view_with_focus(self):
        try:
            focus = self._widgets.QApplication.focusWidget()
        except Exception:
            return None
        if focus is None:
            return None
        dock = _containing([v.dock for v in self._views], focus)
        return self._find_view(dock) if dock is not None else None

    def _items_of(self, view):
        # type: (object) -> list
        """The plot's item names, when its list is known and readable."""
        if view.kind != KIND_PLOT:
            return None
        for item_list in self._lists:
            if item_list.view is view:
                items = _read_items(item_list)
                if items is not None:
                    item_list.items = items
                return items
        return None


def _containing(docks, widget):
    # type: (list, object) -> object
    """The dock among ``docks`` that is ``widget`` or holds it, or None.

    Asked of each dock rather than by walking up from the widget: see
    Identity in the module docstring.
    """
    for dock in docks:
        try:
            if dock is widget or dock.isAncestorOf(widget):
                return dock
        except Exception:
            continue
    return None


def _is_visible(widget):
    # type: (object) -> bool
    try:
        return bool(widget.isVisible())
    except Exception:
        return False


def _read_items(item_list):
    # type: (object) -> list
    """Top-level item names of a plot item list, or None if unreadable.

    The sub-entries every item carries (cutting tool, clip box, range) say
    nothing about what the person is after and are left out.
    """
    widget = item_list.widget
    try:
        count = widget.topLevelItemCount()
        return [str(widget.topLevelItem(i).text(0)).strip() for i in range(count)]
    except Exception:
        pass
    # A wrapper without the tree's methods: the model's top-level rows.
    model = item_list.model
    try:
        return [str(model.data(model.index(row, 0)) or "").strip() for row in range(model.rowCount())]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def install(history):
    # type: (object) -> bool
    """Start tracking views; True when the tracker is in place.

    Like the console hooks, only meaningful in a GUI with a running event
    loop. A second ``start()`` in the same session replaces the tracker of
    the first, which reports what is open again.
    """
    global _tracker
    if _tracker is not None:
        _tracker.uninstall()
        _tracker = None

    from ..utils.modal_guard import _import_qt

    core, widgets = _import_qt()
    if core is None or widgets is None:
        logger.info("No Qt widgets binding; view tracking disabled")
        return False

    tracker = ViewTracker(history, core, widgets)
    try:
        ok = tracker.install()
    except Exception as e:
        logger.warning("View tracking not installed: %s", e)
        return False
    if ok:
        _tracker = tracker
    return ok


def uninstall():
    global _tracker
    if _tracker is not None:
        _tracker.uninstall()
        _tracker = None


def after_bridge_code():
    """Look at the GUI once the bridge's own code has run.

    What that code opened is then in the client's very next read, whether
    or not it moved any focus. Some views are built from the event loop
    after the command that asked for them has returned, so a second look
    follows one pass later. Skipped inside a cycle callback, where the GUI
    is not read. Never raises.
    """
    tracker = _tracker
    if tracker is None:
        return
    try:
        from ..signals.cycle_executor import in_cycle_callback

        if in_cycle_callback():
            return
    except Exception:
        return
    tracker.look()
    tracker._schedule()
