# -*- coding: utf-8 -*-
"""What is open in the product GUI: plots, data files, plot items.

Typing is not the only thing a person does in the GUI. Which plot they have
in front of them, which data file they are editing, and which items they
just added to a plot say as much about what they are after as the command
they type next. This module reports those as entries in the same
``ConsoleHistory`` the typed input goes to: ``"view"`` entries when a plot
or a data file is opened, closed, renamed or brought to the front, or a
data file is executed from its editor, and
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

Executing a data file (measured on PFC 7.0)
    Not everyone runs a script from a console. The editor's Execute button
    is a tool button whose action, one for the whole session, is named
    "Execute"; Ctrl+E and Ctrl+M (the project's master files) are separate
    ``QShortcut`` objects that do not go through that action, and a
    Ctrl-click on the button runs the master files too. All three are
    listened to and recorded as an ``executed`` entry naming the file in
    front. That the person ran it is all that is recorded: what it printed
    is not, and what it does the client reads from the file.

Editing a data file (measured on PFC 7.0)
    Every ``EditorPage`` announces what happens to its buffer, with the
    file's full path: ``modifiedNotice`` on the first keystroke that makes
    the buffer differ from disk, then ``noLongerModifiedNotice`` and
    ``saveNotice`` back to back on a save -- or ``noLongerModifiedNotice``
    alone when the edits are discarded or the file reloaded from disk
    (the reload sends modified and clean within one event-loop pass).
    Only the save is recorded, as a ``saved`` entry: the file on disk
    changed, and the client would not know otherwise. Typing, undoing and
    reloading are the buffer's business; what the client needs of them is
    the state below, not a history of keystrokes. The GUI shows a modified
    buffer by a ``*`` after the dock's title; that is stripped from names,
    not reported as a rename.

    A ``saved`` entry carries the diff of what the person changed, and is
    written when the client asks (``report_saves``), not when the person
    saves: the file is read when the buffer turns modified (what they
    started from) and again at the client's read, and the two are
    compared, so several saves of one file between two reads are one entry
    with their combined diff (PFC 6.0 saves the project, open buffers
    included, every few minutes on its own). After the read the baseline
    is what the client saw, so the next entry's diff starts there. Capped
    in size -- the client reads the file for the rest -- and left out when
    the file was not readable at either end, or when the editing began
    before the bridge was watching.

What the person is doing right now
    Not a history but a state, read when the client asks (``state``, on
    the GUI thread): which data files have unsaved edits -- the ``*`` on
    the dock's title -- and where the selection is in the editor in
    front, as the file's path and a line range. The client reads the
    lines itself, so being told again costs it nothing, and a selection
    is reported as long as it stands, not when it is made (dragging one
    out fires the editor's signals once per character).

    The editor's text widget is a Scintilla ``ScintillaEditBase`` that
    the binding wraps as a plain scroll area, without Scintilla's
    methods; what it still answers are the ``QWidget`` input-method
    queries: the selected text, the paragraph around the cursor and the
    cursor's offset in it (measured on PFC 9.8 and 6.0). Neither the
    file's path nor a line number is to be had from the editor, so the
    path is the project's Data Files entry with the view's name
    (``project_files``), and the lines are found by locating the
    paragraph in the file on disk and the selection at the cursor's end
    of it. A selection that cannot be placed -- the file not listed, the
    paragraph not on disk as the buffer has it, or there twice -- is not
    reported.

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

    Signals are connected to an object once, and the object -- not the
    wrapper -- is what remembers it: a Qt dynamic property
    (``HOOKED_PROPERTY``) set on the C++ object, read back through
    whatever wrapper the next look gets. Holding the connected wrappers
    and asking ``is`` was not enough: an editor page whose wrapper had
    been declared dead was connected again through its new wrapper, the
    old connections staying alive in Qt, and every notice then arrived
    once per wrapper there had been (PFC 6.0: one save reported 3, 7,
    then 12 times as the session went on).

Python 3.6 compatible implementation.
"""

import difflib
import logging
import os
import pathlib
import threading

from .history import SOURCE_COMMAND, SOURCE_PLOT_ITEM, SOURCE_PYTHON, SOURCE_VIEW

logger = logging.getLogger("itasca-mcp-bridge")

# C++ class names, namespace stripped: the 3D products use `itasca3d::`, the
# 2D ones `itasca2d::`, the shared widgets `itascaxd::`.
DOCK_CLASS = "ItascaDockWidget"
PAGE_KINDS = {"PlotPage": "plot", "EditorPage": "data_file"}
ITEM_LIST_CLASS = "PlotItemWidget"

FOCUS_SIGNAL = "focusChanged(QWidget*,QWidget*)"

TOOL_BUTTON_CLASS = "QToolButton"
EXECUTE_ACTION_TEXT = "Execute"
EXECUTE_ACTION_SIGNAL = "triggered(bool)"
SHORTCUT_SIGNAL = "activated()"
EXECUTE_SHORTCUT = "Ctrl+E"
EXECUTE_MASTER_SHORTCUT = "Ctrl+M"

# An editor page's notices about its buffer: (attribute, signature).
NOTICE_MODIFIED = ("modifiedNotice", "modifiedNotice(QString,QString)")
NOTICE_CLEAN = ("noLongerModifiedNotice", "noLongerModifiedNotice(QString,QString)")
NOTICE_SAVED = ("saveNotice", "saveNotice(QString,QString)")
NOTICE_CLOSED = ("closeNotice", "closeNotice(QString,QString)")

# Set on a C++ object whose signals this module has connected; on the
# object, not its Python wrapper (see Identity).
HOOKED_PROPERTY = "_itasca_mcp_bridge_hooked"

# The editor's text widget, found by its Scintilla base class (the leaf
# class is the product's own: ``DataTextEdit`` on 9.8).
TEXT_EDIT_BASE_CLASS = "ScintillaEditBase"

# What the editor is asked about its selection, in this order: the
# selected text, the paragraph around the cursor, the cursor's offset in it.
SELECTION_QUERIES = ("ImCurrentSelection", "ImSurroundingText", "ImCursorPosition")

# A file read for a diff, at most; a diff reported, at most.
MAX_SNAPSHOT_BYTES = 1024 * 1024
MAX_DIFF_LINES = 80
MAX_DIFF_CHARS = 4000
DIFF_CONTEXT_LINES = 2

# How long the client's read waits for the GUI thread to answer ``state``:
# at once when the product is idle or cycling, never while one long
# engine command runs.
STATE_TIMEOUT_S = 1.0

KIND_PLOT = "plot"
KIND_DATA_FILE = "data_file"

EVENT_OPEN = "open"
EVENT_ACTIVE = "active"
EVENT_CLOSED = "closed"
EVENT_RENAMED = "renamed"
EVENT_EXECUTED = "executed"
EVENT_SAVED = "saved"

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
    """The dock's title, as the plot or file is named in the GUI.

    Without the ``*`` the editor appends while its buffer has unsaved
    changes: that is state, reported through the editor's notices.
    """
    try:
        title = dock.windowTitle()
    except Exception:
        return None
    if title is None:
        return None
    return str(title).strip().rstrip("*").strip()


def file_view_name(path):
    # type: (str) -> str
    """The name the GUI gives a data file's view: its name without extension."""
    return os.path.splitext(os.path.basename(path.replace("\\", "/")))[0]


def read_text(path):
    # type: (str) -> str
    """The file's text, or None when it is missing, too big or unreadable.

    UTF-8 first; failing that the bytes are read as Latin-1, which maps
    every byte, so a file in a legacy code page still diffs line by line.
    """
    try:
        if os.path.getsize(path) > MAX_SNAPSHOT_BYTES:
            return None
        with open(path, "rb") as f:
            raw = f.read()
    except (OSError, ValueError):
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def selection_lines(text, selection, paragraph, cursor):
    # type: (str, str, str, int) -> tuple
    """``(first, last)``, the 1-based lines ``selection`` covers in ``text``, or None.

    ``paragraph`` is the text around the cursor as the editor gives it and
    ``cursor`` the cursor's offset in it; the selection begins or ends at
    the cursor. None when the paragraph is not in ``text`` exactly once,
    or the selection is not at the cursor there -- the buffer and the file
    differ, or the paragraph is not the one.
    """
    if not selection or not paragraph:
        return None
    start = text.find(paragraph)
    if start < 0 or text.find(paragraph, start + 1) >= 0:
        return None
    at = start + cursor
    n = len(selection)
    if text[at:at + n] == selection:
        begin = at
    elif at >= n and text[at - n:at] == selection:
        begin = at - n
    else:
        return None
    end = begin + n
    if selection.endswith("\n"):
        end -= 1  # a selection ending with a line's break does not take in the next line
    return text.count("\n", 0, begin) + 1, text.count("\n", 0, end) + 1


def unified_diff(before, after):
    # type: (str, str) -> tuple
    """``(diff, truncated)``: the hunks between two texts, cut to size.

    No file headers -- the entry names the file -- and no diff at all when
    the texts are the same (a save that changed nothing).
    """
    lines = list(difflib.unified_diff(
        before.splitlines(), after.splitlines(), n=DIFF_CONTEXT_LINES, lineterm="",
    ))[2:]
    if not lines:
        return "", False
    truncated = False
    if len(lines) > MAX_DIFF_LINES:
        omitted = len(lines) - MAX_DIFF_LINES
        lines = lines[:MAX_DIFF_LINES] + ["... [{} more lines]".format(omitted)]
        truncated = True
    text = "\n".join(lines)
    if len(text) > MAX_DIFF_CHARS:
        text = text[:MAX_DIFF_CHARS] + "\n... [truncated]"
        truncated = True
    return text, truncated


class _View(object):
    __slots__ = ("dock", "kind", "name", "editor", "path")

    def __init__(self, dock, kind, name):
        self.dock = dock
        self.kind = kind
        self.name = name
        self.editor = None  # a data file's text widget, once looked for
        self.path = None  # a data file's path, once found in the project


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

    def __init__(self, history, qt_core, qt_widgets, listed=None):
        self._history = history
        self._core = qt_core
        self._widgets = qt_widgets
        self._listed = listed  # () -> the project's data file paths, or None
        self._views = []  # type: list
        self._lists = []  # type: list
        self._current = None  # type: object
        self._pending = False
        self._active = False
        self._lists_readable = True
        self._connected = []  # type: list
        self._execute_action = None  # type: object
        self._shortcuts_hooked = False
        self._editor_pages = []  # type: list  # pages whose notices are hooked
        self._baselines = {}  # type: dict  # path -> the file's text as the client last saw it
        self._saves = {}  # type: dict  # path -> saves since the client's last read
        self._clean_pending = set()  # type: set  # paths clean again, save not yet seen
        # The GUI thread fills _baselines and _saves; the client's read,
        # on the server's thread, takes them.
        self._lock = threading.Lock()

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

    def _connect(self, obj, attribute, signature, slot=None):
        # type: (object, str, str, object) -> bool
        """Connect ``obj``'s signal to ``slot`` (the scheduler unless given).

        New-style, or old-style when that fails.

        PySide2 as the 6.0/7.0 products ship it hands back generic wrappers
        without the subclass's signal attributes; there the old-style
        string connect is the one that works.
        """
        if slot is None:
            slot = self._schedule
        try:
            getattr(obj, attribute).connect(slot)
        except Exception:
            try:
                ok = self._core.QObject.connect(obj, self._core.SIGNAL(signature), slot)
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
            if name == TOOL_BUTTON_CLASS:
                if self._execute_action is None:
                    self._hook_execute_action(widget)
            elif name == DOCK_CLASS:
                docks.append(widget)
            elif name in PAGE_KINDS:
                pages.append((widget, PAGE_KINDS[name]))
                if PAGE_KINDS[name] == KIND_DATA_FILE:
                    self._hook_editor_page(widget)
            elif name == ITEM_LIST_CLASS:
                lists.append(widget)

        if not self._shortcuts_hooked:
            self._hook_execute_shortcuts()

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
            if _first_hook(model, self._connected):
                for signal in ("rowsInserted", "rowsRemoved", "dataChanged", "modelReset", "layoutChanged"):
                    try:
                        getattr(model, signal).connect(self._schedule)
                    except Exception:
                        pass
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

    # -- executing a data file -----------------------------------------------

    def _hook_execute_action(self, button):
        # type: (object) -> None
        """Listen to the Execute action if ``button`` is the one carrying it.

        Asked through ``actions()``, which every widget has: the binding
        may have wrapped the button without ``defaultAction()``.
        """
        try:
            actions = button.actions()
        except Exception:
            return
        for action in actions:
            try:
                if str(action.text()) != EXECUTE_ACTION_TEXT:
                    continue
            except Exception:
                continue
            if self._connect(action, "triggered", EXECUTE_ACTION_SIGNAL, self._on_execute):
                self._execute_action = action
            return

    def _hook_execute_shortcuts(self):
        """Listen to Ctrl+E and Ctrl+M, which bypass the Execute action."""
        shortcut_class = getattr(self._widgets, "QShortcut", None)
        if shortcut_class is None:  # Qt 6 moved it to QtGui
            try:
                binding = self._widgets.__name__.split(".")[0]
                gui = __import__(binding + ".QtGui", fromlist=["QtGui"])
                shortcut_class = gui.QShortcut
            except Exception:
                self._shortcuts_hooked = True  # nothing to find; stop looking
                return
        try:
            windows = self._widgets.QApplication.topLevelWidgets()
        except Exception:
            return
        slots = {EXECUTE_SHORTCUT: self._on_execute_shortcut, EXECUTE_MASTER_SHORTCUT: self._on_execute_master}
        for window in windows:
            try:
                shortcuts = window.findChildren(shortcut_class)
            except Exception:
                continue
            for shortcut in shortcuts:
                try:
                    key = str(shortcut.key().toString())
                except Exception:
                    continue
                if key in slots and self._connect(shortcut, "activated", SHORTCUT_SIGNAL, slots[key]):
                    self._shortcuts_hooked = True

    def _on_execute(self, *args):
        # The button: a Ctrl-click runs the master files.
        self._executed(self._control_held())

    def _on_execute_shortcut(self, *args):
        # Ctrl+E: Ctrl is held by definition and means nothing more.
        self._executed(False)

    def _on_execute_master(self, *args):
        self._executed(True)

    def _control_held(self):
        # type: () -> bool
        try:
            modifiers = self._widgets.QApplication.keyboardModifiers()
            return bool(modifiers & self._core.Qt.ControlModifier)
        except Exception:
            return False

    def _executed(self, master):
        # type: (bool) -> None
        """Record that the person ran the data file in front, or the master files.

        No look from here: this runs inside the click, and the file in
        front is already known from the focus change that put it there.
        """
        if not self._active:
            return
        try:
            if master:
                self._history.add(
                    SOURCE_VIEW, "", data={"kind": KIND_DATA_FILE, "event": EVENT_EXECUTED, "master": True}
                )
                return
            view = self._view_with_focus() or self._current
            if view is None or view.kind != KIND_DATA_FILE:
                return
            self._history.add(SOURCE_VIEW, view.name, data={"kind": view.kind, "event": EVENT_EXECUTED})
        except Exception as e:
            logger.debug("View tracking could not record an execute: %s", e)

    # -- editing a data file -------------------------------------------------

    def _hook_editor_page(self, page):
        # type: (object) -> None
        """Listen to the page's buffer notices, once per page (not per wrapper)."""
        if not _first_hook(page, self._editor_pages):
            return
        for (attribute, signature), slot in (
            (NOTICE_MODIFIED, self._on_modified),
            (NOTICE_CLEAN, self._on_clean),
            (NOTICE_SAVED, self._on_saved),
            (NOTICE_CLOSED, self._on_editor_closed),
        ):
            self._connect(page, attribute, signature, slot)

    def _on_modified(self, path, *args):
        """The buffer differs from disk: remember what the person started from."""
        if not self._active:
            return
        try:
            path = str(path)
            self._clean_pending.discard(path)
            with self._lock:
                if path in self._baselines:
                    # Announced again mid-edit (seen on 9.8): the person is
                    # still editing from the same starting point.
                    return
            text = read_text(path)
            with self._lock:
                self._baselines.setdefault(path, text)
        except Exception as e:
            logger.debug("View tracking could not follow an edit: %s", e)

    def _on_clean(self, path, *args):
        """The buffer matches disk again: a save (its notice follows at once), a revert or a reload."""
        if not self._active:
            return
        try:
            path = str(path)
            self._clean_pending.add(path)
            self._core.QTimer.singleShot(0, lambda: self._settle_clean(path))
        except Exception as e:
            logger.debug("View tracking could not follow a buffer: %s", e)

    def _settle_clean(self, path):
        """One pass later, no save claimed the clean: the edits are gone, and
        with them what they started from -- unless a save is still to be
        reported, whose diff needs it."""
        if path not in self._clean_pending:
            return
        self._clean_pending.discard(path)
        self._forget(path)

    def _on_saved(self, path, *args):
        """The file on disk is now what the person wrote; reported at the client's next read."""
        if not self._active:
            return
        try:
            path = str(path)
            self._clean_pending.discard(path)
            with self._lock:
                self._saves[path] = self._saves.get(path, 0) + 1
        except Exception as e:
            logger.debug("View tracking could not follow a save: %s", e)

    def _on_editor_closed(self, path, *args):
        try:
            path = str(path)
        except Exception:
            return
        self._clean_pending.discard(path)
        self._forget(path)

    def _forget(self, path):
        with self._lock:
            if path not in self._saves:
                self._baselines.pop(path, None)

    def report_saves(self):
        # type: () -> None
        """Record one ``saved`` entry per file saved since the last call.

        Any thread: files and the history are read, not the GUI. The diff
        is from the text the client last saw to the file now; afterwards
        the file now is what it last saw.
        """
        with self._lock:
            saves, self._saves = self._saves, {}
        for path, count in saves.items():
            with self._lock:
                before = self._baselines.get(path)
            after = read_text(path)
            data = {"kind": KIND_DATA_FILE, "event": EVENT_SAVED}
            if count > 1:
                data["saves"] = count
            if before is not None and after is not None:
                diff, truncated = unified_diff(before, after)
                if diff:
                    data["diff"] = diff
                if truncated:
                    data["truncated"] = True
            with self._lock:
                if after is None:
                    self._baselines.pop(path, None)
                else:
                    self._baselines[path] = after
            self._record_file(path, data)

    # -- what the person is doing right now ----------------------------------

    def state(self):
        # type: () -> dict
        """The editors' state: ``unsaved`` files and the ``selection``; empty when nothing.

        GUI thread only. The selection is the one in the data file in
        front, or failing that in another open one, as ``path`` and
        ``lines`` (first and last, 1-based).
        """
        if not self._active:
            return {}
        state = {}
        front = self._view_with_focus() or self._current
        editors = [v for v in self._views if v.kind == KIND_DATA_FILE]
        editors.sort(key=lambda v: v is not front)
        unsaved = [v.name for v in editors if _has_unsaved_edits(v.dock)]
        if unsaved:
            state["unsaved"] = unsaved
        for view in editors:
            selection = self._selection_in(view)
            if selection is not None:
                state["selection"] = selection
                break
        return state

    def _selection_in(self, view):
        # type: (object) -> dict
        """Where the view's editor has its selection, or None when it has none or it cannot be placed."""
        answers = self._ask_editor(view, SELECTION_QUERIES)
        if answers is None:
            return None
        selection, paragraph, cursor = answers
        if not selection:
            return None
        path = self._path_of(view)
        if path is None:
            return None
        text = read_text(path)
        if text is None:
            return None
        try:
            cursor = int(cursor)
        except (TypeError, ValueError):
            return None
        lines = selection_lines(text, str(selection), str(paragraph or ""), cursor)
        if lines is None:
            return None
        return {"name": view.name, "path": path, "lines": list(lines)}

    def _ask_editor(self, view, queries):
        # type: (object, tuple) -> list
        """The editor's answers to the input-method ``queries``, or None without an editor."""
        for _attempt in (1, 2):
            if view.editor is None:
                view.editor = self._find_editor(view.dock)
                if view.editor is None:
                    return None
            try:
                return [view.editor.inputMethodQuery(_qt_enum(self._core.Qt, q)) for q in queries]
            except Exception:
                view.editor = None  # its wrapper was declared dead (see Identity)
        return None

    def _path_of(self, view):
        # type: (object) -> str
        """The project's data file with the view's name, once; None when there is not exactly one."""
        if view.path is not None:
            return view.path
        if self._listed is None:
            return None
        try:
            paths = self._listed()
        except Exception:
            return None
        if not paths:
            return None
        # The project's paths are normalized (lower case on Windows); the
        # view's name is the file's own spelling, and so is the path given
        # back, as the file system has it.
        wanted = str(view.name or "").lower()
        matches = [p for p in paths if file_view_name(p).lower() == wanted]
        if len(matches) != 1:
            return None
        path = matches[0]
        try:
            path = str(pathlib.Path(path).resolve())
        except Exception:
            pass
        view.path = path.replace("\\", "/")
        return view.path

    def _find_editor(self, dock):
        # type: (object) -> object
        try:
            for widget in dock.findChildren(self._widgets.QWidget):
                if widget.inherits(TEXT_EDIT_BASE_CLASS):
                    return widget
        except Exception:
            pass
        return None

    def _record_file(self, path, data):
        # type: (str, dict) -> None
        data["path"] = path
        self._history.add(SOURCE_VIEW, file_view_name(path), data=data)

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


def _first_hook(obj, seen):
    # type: (object, list) -> bool
    """True the first time ``obj`` comes by; mark it so its next wrapper says no.

    The mark is a Qt dynamic property on the C++ object (see Identity):
    a wrapper the binding declared dead while the object lives on would
    otherwise have the object connected once more, and every signal then
    arrives once per wrapper. ``seen`` keeps the wrappers connected so far
    and is the identity fallback for an object that has no properties.
    """
    try:
        if obj.property(HOOKED_PROPERTY):
            return False
        obj.setProperty(HOOKED_PROPERTY, True)
    except Exception:
        if any(obj is o for o in seen):
            return False
    seen.append(obj)
    return True


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


def _has_unsaved_edits(dock):
    # type: (object) -> bool
    """The editor's ``*`` after the dock's title."""
    try:
        return str(dock.windowTitle()).strip().endswith("*")
    except Exception:
        return False


def _qt_enum(qt, name):
    """``Qt.<name>`` as the binding spells it (PySide6 keeps the enum's own namespace too)."""
    value = getattr(qt, name, None)
    if value is None:
        value = getattr(qt.InputMethodQuery, name)
    return value


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
    from . import project_files

    core, widgets = _import_qt()
    if core is None or widgets is None:
        logger.info("No Qt widgets binding; view tracking disabled")
        return False

    tracker = ViewTracker(history, core, widgets, listed=project_files.listed_paths)
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


def report_saves():
    """Record the files saved since the client's last read; never raises.

    Any thread: called by the ``console_history`` handler before it hands
    the entries over.
    """
    tracker = _tracker
    if tracker is None:
        return
    try:
        tracker.report_saves()
    except Exception as e:
        logger.debug("View tracking could not report the saves: %s", e)


def state(main_executor, timeout=STATE_TIMEOUT_S):
    # type: (object, float) -> dict
    """The editors' state, read on the GUI thread; ``{}`` when there is
    nothing to say or the GUI thread did not answer in time (one long
    engine command holds it). Never raises.
    """
    tracker = _tracker
    if tracker is None or main_executor is None:
        return {}
    try:
        future = main_executor.submit(tracker.state)
    except Exception as e:
        logger.debug("View tracking could not ask for the GUI's state: %s", e)
        return {}
    try:
        return future.result(timeout) or {}
    except Exception as e:
        future.cancel()
        logger.debug("View tracking did not get the GUI's state: %s", e)
        return {}


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
