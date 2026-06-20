"""
ITASCA HTTP + SSE bridge server - runs inside the ITASCA product GUI.

Transport is stdlib only (``http.server`` + Server-Sent Events): no
third-party dependency and no asyncio. Each request is served on its own
thread (``ThreadingMixIn``), so a long-blocking ``execute_code`` never stalls
a concurrent status query. Request/response is plain ``POST /<command>``;
the one server->client doorbell (``task_status_changed``) is pushed over a
single long-lived ``GET /events`` SSE stream.

Script-only workflow: ITASCA operations are executed through Python scripts
using ``itasca.command()``. This module only owns transport; the main-thread
execution semantics (queue vs cycle-gap callback, two-layer termination) live
in ``execution`` / ``handlers.exec_strategy`` and are untouched by the wire.

Python 3.6 compatible implementation (PFC 6/7 embedded interpreter).
"""

import http.server
import json
import logging
import queue
import socketserver
import threading
import time

from .execution import ScriptRunner
from .tasks import TaskManager
from .handlers import (
    ServerContext,
    handle_execute_task,
    handle_check_task_status,
    handle_list_tasks,
    handle_execute_code,
    handle_interrupt_task,
)

# Module logger
logger = logging.getLogger("itasca-mcp-bridge")

# SSE keepalive: how long an idle /events stream waits before emitting a
# comment line. Keeps the connection (and any intermediary proxy) alive.
_SSE_KEEPALIVE_S = 15.0

# Per-client doorbell queue depth. Doorbells are payload-free signals and the
# client always re-polls status, so a bounded queue that drops on overflow is
# safe: it can never block the ITASCA main thread or a script thread.
_SSE_QUEUE_MAXSIZE = 256

# Response-size safety net. Handlers already paginate/truncate task output and
# snippet output well before this; this is only a last-resort guard against a
# pathological single response.
_MAX_RESPONSE_BYTES = 50 * 2 ** 20  # 50 MiB
_TRUNCATED_TAIL_CHARS = 10000


def _json_bytes(obj):
    # type: (object) -> bytes
    return json.dumps(obj).encode("utf-8")


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Thread-per-request HTTP server.

    ``ThreadingMixIn`` is required: the long-lived ``GET /events`` SSE stream
    parks a thread for the connection's lifetime, so a single-threaded server
    would block every other request behind it. This is a 3.6-safe shim for
    ``http.server.ThreadingHTTPServer`` (which only exists on 3.7+).
    """

    daemon_threads = True
    allow_reuse_address = True


class _BridgeRequestHandler(http.server.BaseHTTPRequestHandler):
    """Maps HTTP requests onto the transport-agnostic handler dict.

    ``POST /<command>`` dispatches to the request/response handler;
    ``GET /events`` serves the SSE doorbell stream; ``GET /health`` is a
    liveness probe. The owning ``ItascaHttpServer`` is reachable via
    ``self.server.bridge``.
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Silence BaseHTTPRequestHandler's default stderr access log; the
        # bridge logs request/response summaries through its own logger.
        pass

    @property
    def _bridge(self):
        return self.server.bridge

    def _write_json(self, status, payload_bytes):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload_bytes)))
        self.end_headers()
        try:
            self.wfile.write(payload_bytes)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0
        return self.rfile.read(length) if length > 0 else b""

    def do_POST(self):
        command = self.path.split("?", 1)[0].strip("/")
        raw = self._read_body()

        handler = self._bridge.handlers.get(command)
        if handler is None:
            self._write_json(
                404,
                _json_bytes({
                    "type": "error",
                    "status": "error",
                    "message": "Unknown command: {}".format(command),
                    "error": {
                        "code": "unknown_command",
                        "message": "Unknown command: {}".format(command),
                        "details": {"available_commands": self._bridge.public_commands},
                    },
                }),
            )
            return

        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError) as exc:
            self._write_json(
                400,
                _json_bytes({
                    "type": "error",
                    "status": "error",
                    "message": "Invalid JSON format",
                    "error": {
                        "code": "invalid_json",
                        "message": "Invalid JSON format",
                        "details": {"error": str(exc)},
                    },
                }),
            )
            return

        request_id = data.get("request_id", "unknown")
        summary = self._bridge.summarize_request(command, data)
        logger.info("[%s] >> %s %s", str(request_id)[:8], command, summary)

        t0 = time.time()
        try:
            response = handler(self._bridge.context, data)
        except Exception as exc:  # last-resort net; handlers build their own error envelopes
            logger.error("[%s] handler error: %s", str(request_id)[:8], exc)
            self._write_json(
                500,
                _json_bytes({
                    "type": "error",
                    "request_id": request_id,
                    "status": "error",
                    "message": "Internal server error",
                    "error": {
                        "code": "internal_error",
                        "message": "Internal server error",
                        "details": {"error": str(exc)},
                    },
                }),
            )
            return
        elapsed_ms = (time.time() - t0) * 1000

        status = response.get("status", "unknown")
        logger.info("[%s] << %s status=%s (%.0fms)", str(request_id)[:8], command, status, elapsed_ms)

        self._write_json(200, self._bridge.serialize_response(response, request_id))

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/events":
            self._serve_sse()
        elif path == "/health":
            self._serve_health()
        else:
            self._write_json(
                404,
                _json_bytes({"status": "error", "error": {"code": "not_found", "message": "Not found"}}),
            )

    def _serve_health(self):
        """Liveness probe for curl / pre-flight checks."""
        from . import __version__  # lazy: package __init__ imports this module

        payload = {
            "status": "success",
            "version": __version__,
            "runtime_mode": self._bridge.context.runtime_mode,
        }
        self._write_json(200, _json_bytes(payload))

    def _serve_sse(self):
        q = self._bridge.register_sse_client()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=_SSE_KEEPALIVE_S)
                except queue.Empty:
                    # Keepalive comment line (ignored by the client parser).
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(b"data: " + msg.encode("utf-8") + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ValueError):
            # Client went away; fall through to deregister.
            pass
        finally:
            self._bridge.unregister_sse_client(q)


class ItascaHttpServer:
    """HTTP + SSE bridge for executing code and running script tasks inside ITASCA.

    Owns transport only. The main-thread execution model (``MainThreadExecutor``
    queue, ``ScriptRunner``, cycle-gap snippet callback, two-layer termination)
    is reached through ``self.context`` and is independent of the wire protocol.
    """

    def __init__(
        self,
        main_executor,  # type: object
        host="localhost",  # type: str
        port=9001,  # type: int
        runtime_mode="unknown",  # type: str
    ):
        # type: (...) -> None
        self.main_executor = main_executor
        self.host = host
        self.port = port

        # Registry of connected SSE clients: one bounded queue per client.
        # Mutated from SSE request threads (connect/disconnect) and snapshotted
        # by the broadcast path, so all access is guarded by ``_conn_lock``.
        self.active_connections = set()
        self._conn_lock = threading.Lock()

        task_manager = TaskManager(on_task_terminal=self._broadcast_task_status)
        self.script_runner = ScriptRunner(main_executor, task_manager)

        # Single home for handler dependencies; handlers reach them via
        # ``self.context``, never as server attributes.
        self.context = ServerContext(
            task_manager=task_manager,
            script_runner=self.script_runner,
            main_executor=self.main_executor,
            runtime_mode=runtime_mode,
        )

        self.handlers = {
            "execute_task": handle_execute_task,
            "check_task_status": handle_check_task_status,
            "list_tasks": handle_list_tasks,
            "interrupt_task": handle_interrupt_task,
            "execute_code": handle_execute_code,
        }
        # Canonical command names advertised to callers (unknown_command errors).
        self.public_commands = sorted(self.handlers)

        # Bind eagerly so a port conflict surfaces on the calling (main)
        # thread, before the serving thread starts.
        self._httpd = _ThreadingHTTPServer((host, port), _BridgeRequestHandler)
        self._httpd.bridge = self

    # -- SSE client registry -------------------------------------------------

    def register_sse_client(self):
        q = queue.Queue(maxsize=_SSE_QUEUE_MAXSIZE)
        with self._conn_lock:
            self.active_connections.add(q)
            total = len(self.active_connections)
        logger.info("SSE client connected (total=%d)", total)
        return q

    def unregister_sse_client(self, q):
        with self._conn_lock:
            self.active_connections.discard(q)
            total = len(self.active_connections)
        logger.info("SSE client disconnected (total=%d)", total)

    # -- Server->client doorbell --------------------------------------------

    def _broadcast_task_status(self, task_id, status):
        # type: (str, str) -> None
        """Push a payload-free doorbell to every connected SSE client.

        Snapshot the queue set under the lock, then ``put_nowait`` outside it
        (never hold the lock across a put). Called from the ITASCA main thread
        (via TaskManager's terminal callback). ``queue.Queue`` is thread-safe;
        on overflow the doorbell is dropped because the client always re-polls
        status.
        """
        with self._conn_lock:
            if not self.active_connections:
                return
            queues = list(self.active_connections)
        msg = json.dumps({
            "type": "task_status_changed",
            "task_id": task_id,
            "status": status,
        })
        for q in queues:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass

    # -- Response serialization ---------------------------------------------

    def serialize_response(self, response, request_id="unknown"):
        payload = json.dumps(response)
        if len(payload) > _MAX_RESPONSE_BYTES:
            logger.warning(
                "[%s] Response too large (%d bytes), truncating output",
                str(request_id)[:8], len(payload),
            )
            response = self._truncate_response(response)
            payload = json.dumps(response)
        return payload.encode("utf-8")

    @staticmethod
    def _truncate_response(response):
        """Last-resort truncation when a response blows past the byte cap.

        Keeps the TAIL of ``data["output"]`` (most recent content), since
        monitoring cares about current progress, not the start of a long log.
        Bridge-side pagination already limits output before this path.
        """
        data = response.get("data", {})
        if isinstance(data, dict) and "output" in data:
            output = data["output"]
            if isinstance(output, str) and len(output) > _TRUNCATED_TAIL_CHARS:
                tail = output[-_TRUNCATED_TAIL_CHARS:]
                nl = tail.find("\n")
                if nl >= 0:
                    tail = tail[nl + 1:]
                omitted = len(output) - len(tail)
                data["output"] = (
                    "... [TRUNCATED: {} earlier chars omitted, showing most "
                    "recent {} chars. Consider writing output to file instead "
                    "of printing.]\n".format(omitted, len(tail))
                ) + tail
                response["data"] = data
        return response

    def summarize_request(self, command, data):
        """Build a short log summary for an incoming request."""
        if command == "execute_code":
            code = data.get("code", "")
            preview = code[:80].replace("\n", "\\n")
            if len(code) > 80:
                preview += "..."
            return 'code="{}"'.format(preview)
        if command == "execute_task":
            return 'script="{}" desc="{}"'.format(
                data.get("script_path", "?"), data.get("description", "")[:60]
            )
        if command in ("check_task_status", "interrupt_task"):
            return "task_id={}".format(data.get("task_id", "?"))
        if command == "list_tasks":
            return "offset={} limit={}".format(data.get("offset", 0), data.get("limit", "all"))
        return ""

    # -- Lifecycle ----------------------------------------------------------

    def serve_forever(self):
        logger.info("HTTP bridge listening on http://%s:%d", self.host, self.port)
        self._httpd.serve_forever()

    def shutdown(self):
        """Graceful shutdown: stop the HTTP server.

        ``HTTPServer.shutdown()`` must be called from a thread other than the
        one running ``serve_forever`` - here the main thread, while serving
        runs on a daemon thread. Task state is already persisted on every
        status change (TaskManager._save_tasks), so there is nothing to flush.
        """
        try:
            self._httpd.shutdown()
        except Exception:
            pass
        try:
            self._httpd.server_close()
        except Exception:
            pass
        logger.info("Server shutdown complete")

    def set_runtime_mode(self, runtime_mode):
        # type: (str) -> None
        """Update active runtime mode exposed to handlers."""
        self.context.runtime_mode = runtime_mode


def create_server(
    main_executor,  # type: object
    host="localhost",  # type: str
    port=9001,  # type: int
    runtime_mode="unknown",  # type: str
):
    # type: (...) -> ItascaHttpServer
    """Create an ITASCA HTTP + SSE bridge server instance.

    Args:
        main_executor: Main thread executor for queue-based execution
        host: Server host address (default: "localhost")
        port: Server port number (default: 9001)
        runtime_mode: Active bridge runtime mode ("gui" / "console" / ...)

    Returns:
        ItascaHttpServer: Server instance ready to be started via serve_forever().
    """
    return ItascaHttpServer(
        main_executor,
        host=host,
        port=port,
        runtime_mode=runtime_mode,
    )
