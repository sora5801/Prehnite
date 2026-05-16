"""HTTP CONNECT proxy that enforces an allowlist and emits egress events.

Lives in the prehnite-mcp host process; the sandboxed container reaches it
via `host.docker.internal` set as `HTTP_PROXY`/`HTTPS_PROXY` env vars in
the container. Both pip and most other client libraries honour these.

Logs every connection attempt — allowed and denied — through a callback
the caller wires to the trajectory writer. The point of `restricted` mode
is the trajectory; the proxy is just the mechanism.

Scope (v0):
- Handles the HTTP CONNECT method (used for HTTPS, which is what pip /
  GitHub / most modern services use). Other methods get 405.
- Suffix-matches the host against the allowlist: `pythonhosted.org`
  matches both itself and `files.pythonhosted.org`.
- No HTTPS interception — we see the host but not the URL/path. That's
  by design; deeper visibility is a separate (mitmproxy) decision.
"""

from __future__ import annotations

import socket
import socketserver
import threading
import time
from collections.abc import Callable, Iterable

EgressCallback = Callable[[dict[str, object]], None]


# Default allowlist for `restricted` mode. Conservative; per-task
# `extra_allow` is intended for one-off additions.
DEFAULT_ALLOWLIST: frozenset[str] = frozenset(
    {
        "pypi.org",
        "pythonhosted.org",  # covers files.pythonhosted.org via suffix
        "github.com",
        "githubusercontent.com",  # covers raw.githubusercontent.com
        "registry.npmjs.org",
        "crates.io",
        "static.crates.io",
        "httpbin.org",
        "example.com",
    }
)


def matches_allowlist(host: str, allow: Iterable[str]) -> bool:
    """Suffix-match `host` against `allow`. Exposed for testing."""
    allow_set = set(allow)
    if host in allow_set:
        return True
    return any(host.endswith("." + a) for a in allow_set)


class _Handler(socketserver.BaseRequestHandler):
    server: "_ProxyServer"

    def handle(self) -> None:
        client = self.request
        client.settimeout(30)

        # Read up to the first \r\n to get the request line.
        buf = b""
        while b"\r\n" not in buf:
            try:
                chunk = client.recv(4096)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            if len(buf) > 16384:
                return  # malformed / abuse

        request_line = buf.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
        parts = request_line.split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return

        target = parts[1]
        try:
            host, port_s = target.rsplit(":", 1)
            port = int(port_s)
        except (ValueError, IndexError):
            client.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return

        allowed = matches_allowlist(host, self.server.allowlist)

        # Drain the rest of the CONNECT request (headers + blank line) before
        # responding, so the client doesn't see a half-read socket.
        while b"\r\n\r\n" not in buf:
            try:
                chunk = client.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk

        if not allowed:
            self.server.on_attempt(
                {
                    "host": host,
                    "port": port,
                    "allowed": False,
                    "reason": "not in allowlist",
                    "duration_ms": 0,
                }
            )
            client.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return

        start = time.monotonic()
        try:
            upstream = socket.create_connection((host, port), timeout=10)
        except OSError as e:
            duration_ms = int((time.monotonic() - start) * 1000)
            self.server.on_attempt(
                {
                    "host": host,
                    "port": port,
                    "allowed": True,
                    "reason": f"upstream connect failed: {e}",
                    "duration_ms": duration_ms,
                }
            )
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return

        duration_ms = int((time.monotonic() - start) * 1000)
        self.server.on_attempt(
            {
                "host": host,
                "port": port,
                "allowed": True,
                "reason": "matched allowlist",
                "duration_ms": duration_ms,
            }
        )
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        _pump(client, upstream)


def _pump(a: socket.socket, b: socket.socket) -> None:
    """Bidirectionally forward bytes until either side closes."""

    def forward(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                try:
                    data = src.recv(8192)
                except OSError:
                    break
                if not data:
                    break
                try:
                    dst.sendall(data)
                except OSError:
                    break
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    t1 = threading.Thread(target=forward, args=(a, b), daemon=True)
    t2 = threading.Thread(target=forward, args=(b, a), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    for s in (a, b):
        try:
            s.close()
        except OSError:
            pass


class _ProxyServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, allowlist: set[str], on_attempt: EgressCallback) -> None:
        # Bind to all interfaces on a random free port. The container reaches
        # us via host.docker.internal, which resolves to the host's default
        # interface; binding to 0.0.0.0 covers both Docker Desktop and the
        # Linux `host-gateway` extra_hosts trick.
        super().__init__(("0.0.0.0", 0), _Handler)
        self.allowlist = allowlist
        self.on_attempt = on_attempt


class EgressProxy:
    """Lifecycle wrapper around an HTTP CONNECT proxy on a random port."""

    def __init__(
        self,
        allowlist: Iterable[str],
        on_attempt: EgressCallback,
    ) -> None:
        self._server = _ProxyServer(set(allowlist), on_attempt)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> int:
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()
        return self.port

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
