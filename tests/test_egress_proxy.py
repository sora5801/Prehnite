"""Egress-proxy unit tests.

Allowlist-matching is pure-Python and tested directly. The full
CONNECT-and-pump flow is covered by an end-to-end test that runs an
upstream loopback server, points the proxy at it, and round-trips a
byte payload — no Docker involvement.
"""

from __future__ import annotations

import socket
import socketserver
import threading
from collections.abc import Callable, Iterator

import pytest

from prehnite.egress_proxy import EgressProxy, matches_allowlist


# --- pure unit tests -----------------------------------------------------


def test_matches_allowlist_exact() -> None:
    assert matches_allowlist("pypi.org", {"pypi.org"})


def test_matches_allowlist_suffix() -> None:
    # `pythonhosted.org` allows `files.pythonhosted.org` via suffix match.
    assert matches_allowlist("files.pythonhosted.org", {"pythonhosted.org"})


def test_matches_allowlist_does_not_match_partial_substring() -> None:
    # `evilpypi.org` is NOT a subdomain of `pypi.org` and must not match.
    assert not matches_allowlist("evilpypi.org", {"pypi.org"})


def test_matches_allowlist_empty_set() -> None:
    assert not matches_allowlist("pypi.org", set())


# --- end-to-end against a loopback upstream ------------------------------


class _EchoHandler(socketserver.BaseRequestHandler):
    """Reads one chunk, echoes it back, closes."""

    def handle(self) -> None:
        data = self.request.recv(64)
        self.request.sendall(b"ECHO:" + data)


@pytest.fixture
def echo_server() -> Iterator[int]:
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _EchoHandler)
    server.daemon_threads = True
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


def _record_attempts() -> tuple[list[dict[str, object]], Callable[[dict[str, object]], None]]:
    seen: list[dict[str, object]] = []
    return seen, seen.append


def _send_connect(port: int, target: str) -> tuple[socket.socket, bytes]:
    """Send a CONNECT request via raw socket and read the proxy's response
    headers (up to and including the blank line). Returns the socket (still
    open) and the response bytes consumed so far."""
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(
        f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode("ascii")
    )
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    return s, buf


def test_proxy_forwards_to_allowed_upstream(echo_server: int) -> None:
    seen, cb = _record_attempts()
    proxy = EgressProxy({"localhost"}, cb)
    port = proxy.start()
    try:
        s, headers = _send_connect(port, f"localhost:{echo_server}")
        assert b" 200 " in headers.split(b"\r\n", 1)[0]
        # Socket is now a raw tunnel to the upstream echo server.
        s.sendall(b"hi")
        got = s.recv(64)
        assert got == b"ECHO:hi"
        s.close()
    finally:
        proxy.stop()

    assert len(seen) == 1
    e = seen[0]
    assert e["host"] == "localhost"
    assert e["port"] == echo_server
    assert e["allowed"] is True


def test_proxy_denies_unallowed_host_and_logs_attempt() -> None:
    seen, cb = _record_attempts()
    proxy = EgressProxy(set(), cb)  # empty allowlist denies everything
    port = proxy.start()
    try:
        s, headers = _send_connect(port, "blocked.example:443")
        assert b" 403 " in headers.split(b"\r\n", 1)[0]
        s.close()
    finally:
        proxy.stop()

    assert len(seen) == 1
    e = seen[0]
    assert e["host"] == "blocked.example"
    assert e["port"] == 443
    assert e["allowed"] is False
    assert "not in allowlist" in str(e["reason"])


def test_proxy_rejects_non_connect_method() -> None:
    seen, cb = _record_attempts()
    proxy = EgressProxy({"any"}, cb)
    port = proxy.start()
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        head = s.recv(64).decode("ascii", errors="replace")
        assert "405" in head
        s.close()
    finally:
        proxy.stop()
    # No attempt logged for malformed/non-CONNECT requests — those never got
    # far enough to identify a target host.
    assert seen == []
