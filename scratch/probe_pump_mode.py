# -*- coding: utf-8 -*-
"""Report which task pump `start()` would pick on this host, without starting.

Read-only on purpose. Picking the blocking pump inside a product GUI seizes
the main thread and freezes the window, so on a version whose Qt bindings
have not been checked yet, run this first and only start the bridge where it
says `Qt timer`.

Run it from the product GUI's IPython console:

    %run C:/path/to/itasca-mcp-bridge/scratch/probe_pump_mode.py

or from a product console CLI -- one statement per line, never joined with
`;`, which the Itasca command language reads as a comment and would use to
silently truncate the line:

    python import os
    python os.environ["ITASCA_MCP_BRIDGE_SRC"] = r"C:\\path\\to\\itasca-mcp-bridge\\src"
    python exec(open(r"C:\\path\\to\\scratch\\probe_pump_mode.py").read())

`%run` supplies `__file__`, so there the source checkout next to this script
is found on its own; `exec()` does not, hence the environment variable. With
neither, the installed package is probed. Whichever it resolves to is
printed first, because "which copy am I actually testing" is the easiest
thing to get wrong here.

Must stay compatible with Python 3.6 (PFC 6/7 embedded interpreter).
"""

import os
import sys


def _bridge_src():
    """Directory to prepend to sys.path, or "" to use the installed package."""
    from_env = os.environ.get("ITASCA_MCP_BRIDGE_SRC", "")
    if from_env:
        return from_env
    try:
        here = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        # exec(open(...).read()) leaves no __file__; fall back to whatever
        # is importable.
        return ""
    candidate = os.path.join(os.path.dirname(here), "src")
    return candidate if os.path.isdir(candidate) else ""


def _load_runtime():
    src = _bridge_src()
    if src and src not in sys.path:
        sys.path.insert(0, src)
    # A stale itasca_mcp_bridge already in sys.modules wins over any path
    # change, which is how a probe ends up reporting on the wrong copy.
    for name in [m for m in sys.modules if m.startswith("itasca_mcp_bridge")]:
        del sys.modules[name]
    from itasca_mcp_bridge import runtime

    return runtime


def _meta_chain(app):
    """C++ class chain of `app`, or a one-item explanation of why not."""
    try:
        meta = app.metaObject()
    except Exception as e:
        return ["<metaObject() failed: {}>".format(e)]
    chain = []
    while meta is not None:
        try:
            chain.append(meta.className())
        except Exception as e:
            chain.append("<className() failed: {}>".format(e))
            break
        try:
            meta = meta.superClass()
        except Exception as e:
            chain.append("<superClass() failed: {}>".format(e))
            break
    return chain


def _loop_level(QtCore):
    try:
        return QtCore.QThread.currentThread().loopLevel()
    except Exception as e:
        return "<loopLevel() failed: {}>".format(e)


def main():
    runtime = _load_runtime()

    print("")
    print("=" * 60)
    print("Bridge pump-mode probe")
    print("=" * 60)
    print("  engine    : {}".format(sys.executable))
    print("  python    : {}".format(sys.version.split()[0]))
    print("  runtime   : {}".format(runtime.__file__))

    QtCore = runtime._import_qtcore()
    if QtCore is None:
        print("  qt binding: none importable")
        print("  verdict   : blocking poll (no Qt at all)")
        print("=" * 60)
        return

    print("  qt binding: {}".format(getattr(QtCore, "__file__", QtCore)))

    app = QtCore.QCoreApplication.instance()
    if app is None:
        print("  app       : absent")
    else:
        print("  app type  : {}   <- the Python wrapper; PySide2 does not".format(
            type(app).__name__
        ))
        print("              downcast, so this lies about a GUI host")
        print("  metaobject: {}".format(" -> ".join(_meta_chain(app))))

    print("  loopLevel : {}".format(_loop_level(QtCore)))
    print("")

    # The two witnesses exactly as _start_qt_pump consults them. Printed
    # separately: if both read False the bridge takes the main thread, so it
    # matters which one abstained and whether it abstained because the API
    # was missing rather than because the answer was really no.
    gui = runtime._is_qt_gui_app(app)
    loop = runtime._qt_event_loop_running(QtCore)
    print("  is_gui_app         : {}".format(gui))
    print("  event_loop_running : {}".format(loop))
    print("  verdict            : {}".format("Qt timer" if (gui or loop) else "blocking poll"))
    print("")
    print("  Expected: 'Qt timer' in a product GUI, 'blocking poll' in a")
    print("  console build. A GUI reading 'blocking poll' would freeze on")
    print("  start() -- report it instead of starting the bridge.")
    print("=" * 60)


main()
