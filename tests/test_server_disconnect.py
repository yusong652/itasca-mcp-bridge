"""A client dropping its connection must not print a traceback into the product console.

Seen on PFC 7.0: a short-lived client process exiting with a keep-alive
connection open made socketserver print ConnectionAbortedError (WinError 10053)
from ``handle_one_request`` to stderr, which is the person's console.
"""

import socket
import struct
import threading

from itasca_mcp_bridge.server import _BridgeRequestHandler, _ThreadingHTTPServer


def _serve():
    server = _ThreadingHTTPServer(("127.0.0.1", 0), _BridgeRequestHandler)
    handled = threading.Event()
    original = server.handle_error

    def handle_error(request, client_address):
        try:
            original(request, client_address)
        finally:
            handled.set()

    server.handle_error = handle_error
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, handled


def test_reset_connection_prints_nothing(capsys):
    server, handled = _serve()
    try:
        client = socket.create_connection(server.server_address)
        # SO_LINGER (on, 0): close() sends RST instead of FIN, so the server's
        # readline fails the way an aborted client does, rather than seeing EOF.
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        client.send(b"GET ")  # a partial request line keeps the handler reading
        client.close()
        assert handled.wait(5), "server never saw the dropped connection"
    finally:
        server.shutdown()
        server.server_close()
    assert "Traceback" not in capsys.readouterr().err


def test_other_errors_go_to_the_log_not_stderr(capsys, caplog):
    server = _ThreadingHTTPServer(("127.0.0.1", 0), _BridgeRequestHandler)
    try:
        try:
            raise ValueError("broken handler")
        except ValueError:
            server.handle_error(None, ("127.0.0.1", 1))
    finally:
        server.server_close()
    assert "Traceback" not in capsys.readouterr().err
    assert any("broken handler" in (r.exc_text or "") or r.exc_info for r in caplog.records)
