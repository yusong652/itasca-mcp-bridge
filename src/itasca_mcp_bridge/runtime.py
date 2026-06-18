# -*- coding: utf-8 -*-
"""Bridge runtime: server startup and the main-thread task pump.

Holds the implementation behind `itasca_mcp_bridge.start()` so the package
`__init__` stays a thin entry point. Must stay compatible with Python 3.6
(PFC 6/7 embedded interpreter).
"""

# Keep global references to avoid Qt timer/callback garbage collection.
_qt_task_timer = None

DEFAULT_TIMER_INTERVAL_MS = 20
DEFAULT_MAX_TASKS_PER_TICK = 1
VALID_RUNTIME_MODES = ("auto", "gui", "console")


# Qt binding shipped with the host product varies by version:
#   PFC 6/7 and early PFC 9 -> PySide2 (Qt5)
#   PFC 9.7+                 -> PySide6 (Qt6)
# Probe newest first so the same bridge build works across all of them.
_QT_BINDINGS = ("PySide6", "PySide2")


def _import_qtcore(logger=None):
    # type: (...) -> object
    """Return QtCore from the first available Qt binding, or None."""
    for binding in _QT_BINDINGS:
        try:
            module = __import__(binding + ".QtCore", fromlist=["QtCore"])
        except Exception:
            continue
        if logger is not None:
            logger.info("Qt binding detected: {}".format(binding))
        return module
    return None


def _start_qt_pump(main_executor, interval_ms, max_tasks_per_tick, logger):
    # type: (...) -> bool
    """Try to attach task processing to Qt event loop. Returns True on success."""
    global _qt_task_timer

    QtCore = _import_qtcore(logger)
    if QtCore is None:
        return False

    app = QtCore.QCoreApplication.instance()
    if app is None:
        return False

    # Stop previous timer if start() is called multiple times.
    if _qt_task_timer is not None:
        try:
            _qt_task_timer.stop()
        except Exception:
            pass

    per_tick = None
    if max_tasks_per_tick is not None:
        try:
            value = int(max_tasks_per_tick)
            if value > 0:
                per_tick = value
        except Exception:
            per_tick = 1

    def _process_tick():
        try:
            main_executor.process_tasks(max_tasks=per_tick)
        except Exception as e:
            logger.error("Task pump tick failed: {}".format(e))

    timer = QtCore.QTimer()
    timer.setInterval(interval_ms)
    timer.timeout.connect(_process_tick)
    timer.start()

    _qt_task_timer = timer
    return True


def _run_blocking_pump(main_executor, interval_ms, max_tasks_per_tick, logger):
    # type: (...) -> None
    """Block the main thread and poll task queue. Used in console mode."""
    import time

    per_tick = None
    if max_tasks_per_tick is not None:
        try:
            value = int(max_tasks_per_tick)
            if value > 0:
                per_tick = value
        except Exception:
            per_tick = 1

    sleep_s = interval_ms / 1000.0
    try:
        while True:
            try:
                main_executor.process_tasks(max_tasks=per_tick)
            except Exception as e:
                logger.error("Task pump tick failed: {}".format(e))
            time.sleep(sleep_s)
    except KeyboardInterrupt:
        logger.info("Bridge stopped by user")


def start(
    host="localhost",
    port=9001,
    ping_interval=120,
    ping_timeout=300,
    timer_interval_ms=DEFAULT_TIMER_INTERVAL_MS,
    max_tasks_per_tick=DEFAULT_MAX_TASKS_PER_TICK,
    mode="auto",
):
    """Start the bridge server and task pump. See `itasca_mcp_bridge.start`."""
    import sys
    import os
    import asyncio
    import logging

    from . import __version__
    from .upgrade import ENV_UPGRADED_FROM

    if mode not in VALID_RUNTIME_MODES:
        raise ValueError(
            "Invalid mode '{}'. Expected one of: {}".format(mode, ", ".join(VALID_RUNTIME_MODES))
        )

    def _to_positive_int(value, default):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    interval_ms = _to_positive_int(timer_interval_ms, DEFAULT_TIMER_INTERVAL_MS)

    # ── Logging ───────────────────────────────────────────────
    # Freeze the bridge root to the launch directory before any task can run.
    # A user task script may later os.chdir() the whole interpreter; all bridge
    # state (logs, tasks.json, command logs) must stay anchored here regardless.
    from .utils import path_utils
    path_utils.set_bridge_root(os.getcwd())

    bridge_dir = path_utils.data_dir()
    if not os.path.exists(bridge_dir):
        os.makedirs(bridge_dir)
    log_file = os.path.join(bridge_dir, "bridge.log")

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()

    formatter = logging.Formatter('[%(asctime)s] %(levelname)s - %(message)s')
    # Full INFO trail in bridge.log; stdout only surfaces warnings/errors so the
    # PFC IPython console stays clean for non-developer users. Bookkeeping like
    # callback registration and TaskManager init goes to file only.
    #
    # Gap-free handlers: L2 termination async-raises BridgeTimeout into the
    # snippet thread, which may be mid-handle() while logging. The stdlib
    # acquire/try/finally form (Python <= 3.10) can leak the handler lock on
    # that race and freeze the bridge; these subclasses use the gap-free
    # `with self.lock` form. See utils/safe_logging.py.
    from .utils.safe_logging import GapFreeFileHandler, GapFreeStreamHandler
    file_handler = GapFreeFileHandler(log_file, mode='w', encoding='utf-8')
    file_handler.setFormatter(formatter)
    stream_handler = GapFreeStreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(logging.WARNING)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)
    logger = logging.getLogger("itasca-mcp-bridge")

    # ── Server components ─────────────────────────────────────
    from .execution import MainThreadExecutor
    from .server import create_server

    main_executor = MainThreadExecutor()

    # ── ITASCA configuration (required) ───────────────────────
    try:
        import itasca as it  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "itasca module not available; run bridge inside an ITASCA product "
            "GUI (PFC, FLAC3D, ...)"
        ) from e

    it.command("python-reset-state false")

    from .signals import (
        register_interrupt_callback,
        register_executor_callback,
        is_executor_callback_registered,
    )

    interrupt_ok = register_interrupt_callback(it)
    executor_ok = register_executor_callback(it)
    executor_registered = bool(executor_ok or is_executor_callback_registered())

    if not interrupt_ok:
        raise RuntimeError("Failed to register interrupt callback")
    if not executor_registered:
        raise RuntimeError("Failed to register executor callback")

    # ── Port availability check ──────────────────────────────
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
    except OSError:
        raise RuntimeError(
            "Port {} is already in use. "
            "Another bridge may be running, or another process is using this port.\n"
            "Try: itasca_mcp_bridge.start(port={})".format(port, port + 1)
        )
    finally:
        sock.close()

    # ── Start WebSocket server ────────────────────────────────
    pfc_server = create_server(
        main_executor=main_executor, host=host, port=port,
        ping_interval=ping_interval, ping_timeout=ping_timeout,
        runtime_mode=mode,
    )

    def run_server_background():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(pfc_server.start())
            loop.run_forever()
        except Exception as e:
            logger.error("Server error: {}".format(e))
            import traceback
            traceback.print_exc()
        finally:
            loop.close()

    import threading
    server_thread = threading.Thread(target=run_server_background, daemon=True)
    server_thread.start()

    if not server_thread.is_alive():
        raise RuntimeError("Bridge server thread failed to start")

    # ── Status display ────────────────────────────────────────
    upgraded_from = os.environ.pop(ENV_UPGRADED_FROM, None)

    print("\n" + "=" * 60)
    print("Itasca MCP Bridge Server")
    print("=" * 60)
    print("  Version:  {}".format(__version__))
    if upgraded_from:
        print("  Upgraded: {} -> {}".format(upgraded_from, __version__))
    print("  URL:      ws://{}:{}".format(host, port))
    print("  Log:      {}".format(log_file))
    print("=" * 60 + "\n")

    if upgraded_from:
        # Surface what the self-upgrade just delivered. Never let the
        # announcement block a successful start.
        try:
            from .announce import whats_new
            whats_new(since=upgraded_from, until=__version__)
        except Exception:
            pass

    # ── Main-thread task pump ─────────────────────────────────
    use_qt = mode in ("auto", "gui")
    use_blocking = mode in ("auto", "console")

    if use_qt and _start_qt_pump(main_executor, interval_ms, max_tasks_per_tick, logger):
        pfc_server.set_runtime_mode("gui")
        print("Task loop running via Qt timer (interval={}ms, max_tasks_per_tick={})".format(
            interval_ms, max_tasks_per_tick))
        return

    if mode == "gui":
        raise RuntimeError("Qt is not available; cannot start in gui mode")

    if use_blocking:
        pfc_server.set_runtime_mode("console")
        print("Task loop running via blocking poll (interval={}ms)".format(interval_ms))
        print("Bridge started in blocking mode (console). Press Ctrl+C to stop.")
        _run_blocking_pump(main_executor, interval_ms, max_tasks_per_tick, logger)
