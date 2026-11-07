# -*- coding: utf-8 -*-
"""Put the files the bridge runs into the GUI project's Data Files list.

A data file the person calls from the product's own prompt turns up in the
Project pane: the engine tells the GUI when it opens a file to process it.
A file the bridge runs never does -- a ``program call`` is expanded here
into its commands, so the engine never opens the file, and a task's Python
script is run by the bridge, not the engine. The person then has nothing
to click on to see or rerun what the agent wrote. This module lists those
files the way the GUI would have.

How (measured on PFC 6.0 and 7.0 with PySide2 5.11, PFC 9.8 with PySide6)
    The GUI's ``EditorModule`` (a direct child of the main window) has the
    slots behind File > Open Data File: ``openDataFile(QString, bool)``
    adds the file to the project and opens it in an editor;
    ``closeDataFile(QString)`` closes the editor and leaves the file
    listed. Both in one go is the registration: no tab remains, the file
    shows as "Closed" in the list. Nothing in the engine does this without
    also executing the file (``program call`` with a bad label gets the
    file listed, and an error in the console with it).

    On 9.x the editor takes the tile of whatever view was in front and
    the close leaves that tile empty, the previous view parked; the GUI's
    own ``activate(bool)`` slot on the parked dock brings it back, so
    every dock the open parked is reactivated. On 6.0/7.0 the close
    destroys the editor's dock and the previous view returns by itself.
    The parked views are found between the open and the close, when
    every dock is alive. After the close no widget is touched: the
    editor's dock is then being destroyed, and wrapping a widget in that
    state took the 9.x product down three times running -- and waiting
    half a second is not enough either, once the session has seen heavy
    dock churn: the settle-time dock enumeration then corrupts the heap
    and the GUI's next project save dies on it, the project file
    truncated to zero bytes (issue #227, four crashes with one
    signature; isolated 2026-11-07 by suppressing each half in turn --
    the enumerations alone and the open/close alone both run clean).

    Whether a file is already listed is read from the Project pane, never
    guessed: the GUI adds nothing twice, and a file the person has open in
    an editor must not be closed under them. Two pane layouts: 6.0/7.0
    keep three ``FileListTreeView`` widgets whose ``FileListModel_DataFiles``
    rows carry "<path> -- <state>" as tool tip (the view is wrapped as a
    plain QWidget, so the model is asked of its header); 9.x has one
    ``ProjectTreeModel`` tree with a "Data Files" node under the project
    whose children carry the path as tool tip. A pane this module cannot
    read disables it: opening and closing without knowing would displace
    the person's view on every call.

Calling slots: queued, never from the Python stack (measured on PFC 9.8)
    ``openDataFile`` on 9.8 starts a worker thread of the engine that
    imports Python modules without taking the interpreter lock. From a
    person's click that is harmless: the GUI thread is idle in its event
    loop. Called from Python it is not: five crash dumps in a row show the
    worker faulting in the import machinery while the GUI thread is still
    inside ``openDataFile`` with the bridge's Python frames underneath.
    Nothing done after the call can help, and sleeping in it did not.

    So no slot is called directly. A helper QObject with signals matching
    the slots is connected to each receiver once, old-style
    (``QObject.connect(sender, SIGNAL(..), receiver, SLOT(..), type)``,
    which both bindings accept) with a *queued* connection, and its
    signals are emitted: that posts an event, and the slot runs when the
    event loop gets to it, with no Python on the stack -- the same path a
    click takes. The check for a displaced view runs one loop pass after
    the open, before the close is emitted, and the ``activate`` it may
    need is queued too, through ``invokeMethod``, ahead of the close.
    Connections are never taken down again: disconnecting
    from a live widget has crashed both bindings' products.

    No wrapper of a GUI object is held between calls. A held wrapper of
    an object the GUI later destroys is not inert: it stays registered
    with the binding, and the next time the binding meets that object --
    a signal argument, a child list -- it raises outside any Python frame,
    and the error surfaces as a ``SystemError`` in whatever Python the
    event loop runs next (the bridge's pump tick). And the binding keeps a
    child's wrapper alive as long as its parent's: holding the main window
    held every dock under it, destroyed ones included. So the editor
    module is found, connected to, and let go within one call (the
    connection lives in Qt); the docks visible before a registration are
    remembered by title and looked up again when the view is restored.

    While a task is executing the event loop runs from inside the engine's
    cycling, with the bridge's cycle callbacks on the Python side; files
    registered then are held back and queued when the task ends.

Never from a cycle callback, never raising: this touches the GUI, on the
same terms as ``console.views``.

Python 3.6 compatible implementation.
"""

import logging
import os

logger = logging.getLogger("itasca-mcp-bridge")

EDITOR_MODULE_CLASS = "EditorModule"
MAIN_WINDOW_MARK = "MainWindow"
DOCK_CLASS = "ItascaDockWidget"
# 6.0/7.0 pane
FILE_LIST_VIEW_CLASS = "FileListTreeView"
DATA_FILES_MODEL_CLASS = "FileListModel_DataFiles"
# 9.x pane
PROJECT_TREE_MODEL_CLASS = "ProjectTreeModel"
DATA_FILES_NODE = "Data Files"

OPEN_SLOT = "openDataFile(QString,bool)"
CLOSE_SLOT = "closeDataFile(QString)"

# The open and close have run long before this and the GUI has listed
# the file: the pending guard against re-queuing the path can be let go.
SETTLE_DELAY_MS = 500

_registrar = None  # type: object


def register(path, force=False):
    # type: (str, bool) -> bool
    """List ``path`` in the GUI project's Data Files; True when it was queued.

    False when it was listed already, held back for a running task, when
    there is no GUI, when the pane cannot be read, or when anything along
    the way fails. Never raises.
    """
    try:
        registrar = _get_registrar()
        if registrar is None:
            return False
        return registrar.register(path, force=force)
    except Exception as e:
        logger.debug("Data file registration failed for %s: %s", path, e)
        return False


def _get_registrar():
    global _registrar
    if _registrar is None:
        from ..utils.modal_guard import _import_qt

        core, widgets = _import_qt()
        if core is None or widgets is None:
            return None
        _registrar = _Registrar(core, widgets)
    return _registrar


def reset():
    """Forget the GUI objects found so far (tests; a stopped bridge)."""
    global _registrar
    _registrar = None


def class_name(obj):
    # type: (object) -> str
    try:
        return str(obj.metaObject().className()).split("::")[-1]
    except Exception:
        return ""


def _is_visible(widget):
    # type: (object) -> bool
    try:
        return bool(widget.isVisible())
    except Exception:
        return True  # a dead wrapper is not a parked view to bring back


def view_title(dock):
    # type: (object) -> str
    try:
        return str(dock.windowTitle()).strip()
    except Exception:
        return ""


def normalize(path):
    # type: (str) -> str
    """One spelling of a path for comparisons: absolute, OS case rules."""
    return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))


def gui_path(path):
    # type: (str) -> str
    """The path as the GUI spells it: absolute, forward slashes."""
    return os.path.abspath(str(path)).replace("\\", "/")


class _Registrar(object):
    def __init__(self, core, widgets):
        self._core = core
        self._widgets = widgets
        self._helper = None  # QObject whose signals drive the slots
        self._editor_connected = False  # helper signals reach the EditorModule
        self._unreadable = False  # pane layout not recognised: stay out
        self._pending = set()  # queued, not yet settled (normalized paths)
        self._deferred = []  # held back while a task was executing

    # -- public -------------------------------------------------------------

    def register(self, path, force=False):
        # type: (str, bool) -> bool
        """Queue the registration of ``path``; True when it was queued.

        While a task is executing the path is held back instead (see
        ``flush_deferred``): the event loop then runs from inside the
        engine's cycling, with the bridge's cycle callbacks on the Python
        side, which is not the idle GUI the slots must run in. ``force``
        is the flush at the task's end, when its status may still read
        as running.
        """
        if self._unreadable:
            return False
        if not os.path.isfile(path):
            return False
        key = normalize(path)
        if key in self._pending:
            return False
        if not force and _task_executing():
            if key not in self._deferred:
                self._deferred.append(key)
            return False
        listed = self.listed()
        if listed is None:
            self._unreadable = True
            logger.info("Project pane not readable with this GUI; data files are not registered")
            return False
        if key in listed:
            return False
        if not self._connect_editor():
            return False

        before = self._visible_dock_titles()
        target = gui_path(path)
        if not self._emit("open_file", target, True):
            return False
        self._pending.add(key)
        registrar = self

        def restore_and_close():
            # One event-loop pass after the open ran, not in the same one:
            # the view tracker's look that the open triggers must not find
            # the editor's widgets already on their way out. The views the
            # open parked are brought back NOW, while every dock is alive;
            # after the close no widget is touched (see the module
            # docstring: the half-second settle enumeration crashed 9.8
            # once the session had seen heavy dock churn, issue #227).
            try:
                registrar._restore_views(before)
            finally:
                registrar._emit("close_file", target)

        def settle():
            registrar._pending.discard(key)

        try:
            self._core.QTimer.singleShot(0, restore_and_close)
            self._core.QTimer.singleShot(SETTLE_DELAY_MS, settle)
        except Exception:
            self._pending.discard(key)
        logger.info("Data file queued for the GUI project: %s", target)
        return True

    def _restore_views(self, before):
        # type: (set) -> None
        """Queue ``activate`` on the views the open parked.

        Runs between the open and the close: the editor holds the front
        tile, the views it displaced are parked, nothing is being
        destroyed. The activations are queued ahead of the close, so
        their slots still run before it. The docks are looked up again
        by title: no wrapper of one was held across the loop pass (see
        the module docstring). 6.0/7.0 need no restoring and
        ``_activate`` does nothing there (no ``Q_ARG``).
        """
        try:
            for dock in self._docks():
                if view_title(dock) in before and not _is_visible(dock):
                    self._activate(dock)
        except Exception as e:
            logger.debug("Could not restore the view after registering: %s", e)

    def _activate(self, dock):
        # type: (object) -> None
        """Queue the GUI's ``activate(true)`` on a parked dock.

        ``invokeMethod`` with ``Q_ARG`` (PySide6): queued like the rest,
        and no connection to a dock is recorded anywhere. The 6.0/7.0
        products neither have ``Q_ARG`` nor need this: there the close
        destroys the editor's dock and the previous view comes back.
        """
        core = self._core
        q_arg = getattr(core, "Q_ARG", None)
        if q_arg is None:
            return
        try:
            core.QMetaObject.invokeMethod(dock, "activate", core.Qt.QueuedConnection, q_arg(bool, True))
        except Exception as e:
            logger.debug("activate could not be queued: %s", e)

    def flush_deferred(self):
        """Register what was held back while a task was executing."""
        deferred, self._deferred = self._deferred, []
        for key in deferred:
            self.register(key, force=True)

    def listed(self):
        # type: () -> set
        """Normalized paths of the project's data files, or None if unreadable."""
        try:
            widgets = self._widgets.QApplication.allWidgets()
        except Exception:
            return None
        paths = None
        for widget in widgets:
            name = class_name(widget)
            if name == FILE_LIST_VIEW_CLASS:
                found = self._paths_from_file_list(widget)
            elif name == "ProjectPage":
                found = self._paths_from_project_tree(widget)
            else:
                continue
            if found is not None:
                paths = found if paths is None else paths | found
        return paths

    # -- the two pane layouts -------------------------------------------------

    def _paths_from_file_list(self, view):
        # type: (object) -> set
        """6.0/7.0: the Data Files list's rows, tool tip "<path> -- <state>"."""
        try:
            headers = view.findChildren(self._widgets.QHeaderView)
            if not headers:
                return None
            model = headers[0].model()
            if model is None or class_name(model) != DATA_FILES_MODEL_CLASS:
                return None
            tip_role = self._core.Qt.ToolTipRole
            paths = set()
            for row in range(model.rowCount()):
                tip = model.data(model.index(row, 0), tip_role)
                if tip is None:
                    continue
                path = str(tip).rpartition(" -- ")[0] or str(tip)
                paths.add(normalize(path))
            return paths
        except Exception:
            return None

    def _paths_from_project_tree(self, page):
        # type: (object) -> set
        """9.x: the "Data Files" node under the project, children's tool tip = path."""
        try:
            for tree in page.findChildren(self._widgets.QTreeView):
                model = tree.model()
                if model is None or class_name(model) != PROJECT_TREE_MODEL_CLASS:
                    continue
                tip_role = self._core.Qt.ToolTipRole
                root = model.index(0, 0)
                for row in range(model.rowCount(root)):
                    node = model.index(row, 0, root)
                    if str(model.data(node) or "").strip() != DATA_FILES_NODE:
                        continue
                    paths = set()
                    for child_row in range(model.rowCount(node)):
                        tip = model.data(model.index(child_row, 0, node), tip_role)
                        if tip:
                            paths.add(normalize(str(tip)))
                    return paths
        except Exception:
            return None
        return None

    # -- GUI objects ------------------------------------------------------------

    def _connect_editor(self):
        # type: () -> bool
        """Connect the helper's signals to the EditorModule's slots, once.

        The module is found and let go within this call: the connection
        lives in Qt, not in the wrapper. Nothing found through the main
        window is kept (see the module docstring).
        """
        if self._editor_connected:
            return True
        core = self._core
        helper = self._helper
        if helper is None:
            helper = self._helper = _make_helper(core)

        def connect(editor):
            for name, slot in (("open_file", OPEN_SLOT), ("close_file", CLOSE_SLOT)):
                try:
                    ok = core.QObject.connect(
                        helper, core.SIGNAL(_HELPER_SIGNALS[name]), editor, core.SLOT(slot),
                        core.Qt.QueuedConnection,
                    )
                except Exception as e:
                    logger.debug("connect to %s failed: %s", slot, e)
                    return False
                if not ok:
                    return False
            return True

        if self._with_editor_module(connect):
            self._editor_connected = True
            return True
        return False

    def refresh_editors(self):
        # type: () -> bool
        """Queue the editor module's foreground check (see ``refresh_editors``)."""
        core = self._core

        def queue_check(editor):
            try:
                return bool(core.QMetaObject.invokeMethod(editor, "onApplicationActivated", core.Qt.QueuedConnection))
            except Exception as e:
                logger.debug("onApplicationActivated could not be queued: %s", e)
                return False

        return self._with_editor_module(queue_check)

    def _with_editor_module(self, fn):
        # type: (object) -> bool
        """Call ``fn(editor_module)`` and return its result; False when not found.

        The module is found through the main window and used before the
        call returns: PySide2 declares a child's wrapper dead as soon as
        the parent's wrapper it was found through is collected, so the
        editor cannot be handed out of the scope that holds the window.
        Nothing is kept (see the module docstring).
        """
        core = self._core
        try:
            windows = self._widgets.QApplication.topLevelWidgets()
        except Exception:
            return False
        for window in windows:
            if MAIN_WINDOW_MARK not in class_name(window):
                continue
            # Direct children only: enumerating every QObject of the
            # application has crashed the 9.x product.
            try:
                children = window.findChildren(core.QObject, "", core.Qt.FindDirectChildrenOnly)
            except Exception:
                try:
                    children = window.findChildren(core.QObject, options=core.Qt.FindDirectChildrenOnly)
                except Exception:
                    continue
            for child in children:
                if class_name(child) == EDITOR_MODULE_CLASS:
                    return bool(fn(child))
        return False

    def _emit(self, name, *args):
        # type: (str, *object) -> bool
        """Emit the helper's signal ``name``: the slot runs from the event loop."""
        try:
            getattr(self._helper, name).emit(*args)
            return True
        except Exception as e:
            logger.debug("emit %s failed: %s", name, e)
            return False

    def _docks(self):
        # type: () -> list
        try:
            return [w for w in self._widgets.QApplication.allWidgets() if class_name(w) == DOCK_CLASS]
        except Exception:
            return []

    def _visible_dock_titles(self):
        # type: () -> set
        return set(view_title(d) for d in self._docks() if _is_visible(d))

    # -- calling a slot -----------------------------------------------------------

_HELPER_SIGNALS = {
    "open_file": "open_file(QString,bool)",
    "close_file": "close_file(QString)",
}


def _task_executing():
    # type: () -> bool
    """True while a task script runs, or inside a cycle callback.

    Then the event loop next runs from inside the engine's cycling. A
    snippet on its own (``execute_code`` with no task running) returns to
    an idle loop and is fine. Not ``interrupt._current_task_id``: snippets
    set that too.
    """
    try:
        from ..signals.cycle_executor import in_cycle_callback

        if in_cycle_callback():
            return True
    except Exception:
        pass
    try:
        from .. import runtime

        tasks = runtime._runtime.server.context.task_manager.tasks
        return any(getattr(t, "status", None) == "running" for t in tasks.values())
    except Exception:
        return False


def flush_deferred():
    """Register the files held back while a task was executing. Never raises."""
    try:
        registrar = _registrar
        if registrar is not None:
            registrar.flush_deferred()
    except Exception as e:
        logger.debug("Deferred data file registration failed: %s", e)


def refresh_editors():
    """Have the GUI's editors take in files changed on disk. Never raises.

    The editor checks its files against disk when the application comes to
    the foreground: a clean buffer is reloaded, one with unsaved edits
    gets the Load / Save / Do Nothing dialog. The bridge's code changes
    files while the person may be looking at them in the GUI, with no
    window switch to trigger that check, so the check is asked for after
    the bridge's code has run: ``EditorModule.onApplicationActivated()``,
    queued like everything else here (measured on PFC 7.0). Not while a
    task is executing, for the same reason as ``register``.
    """
    try:
        if _task_executing():
            return
        registrar = _get_registrar()
        if registrar is not None:
            registrar.refresh_editors()
    except Exception as e:
        logger.debug("Editor refresh skipped: %s", e)


def _make_helper(core):
    """A QObject whose signals match the slots this module calls."""

    class _Helper(core.QObject):
        open_file = core.Signal(str, bool)
        close_file = core.Signal(str)

    return _Helper()
