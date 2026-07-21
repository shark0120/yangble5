#!/usr/bin/env python3
"""Claude-wire compatibility shim: rewrites mid-conversation ``role: "system"`` messages.

THE BUG THIS WORKS AROUND
-------------------------
CLIProxyAPI 7.1.23's antigravity STREAMING translator passes ``messages[].role``
through verbatim (it only rewrites ``assistant`` -> ``model``). Claude Code 2.1.x
and later, with the mid-conversation-system beta enabled, injects a message with
``role: "system"`` in the MIDDLE of the conversation -- the Agent-tool agent list.
Gemini's ``streamGenerateContent`` rejects that role outright with::

    400  Request contains an invalid argument

The non-streaming ``generateContent`` path happens to tolerate it, which is why
the failure looked intermittent and untraceable: the same conversation worked
when a tool call went non-streaming and 400'd the moment it streamed.

Upstream fixed this in v7.2.93 by mapping ``system`` -> ``user`` in
``internal/translator/antigravity/claude/antigravity_claude_request.go``. This
shim applies that EXACT mapping in front of an older engine and forwards
everything else untouched, including SSE streams.

HOW TO RETIRE THIS FILE
-----------------------
1. Upgrade the engine to >= 7.2.93.
2. Point ``ANTHROPIC_BASE_URL`` back at the engine's own port (default 8318)
   instead of this shim's port (default 8320).
3. Delete this file. Nothing else depends on it -- it holds no state.

Verified 2026-07-21 against CLIProxyAPI 7.1.23. Requests whose body contains no
``role: "system"`` message are forwarded byte-for-byte, so the shim cannot change
the cache key of a conversation that did not need fixing. That property is what
makes it safe to leave in the path, and it is covered by the test suite.

CONFIGURATION (flag beats environment beats default)
----------------------------------------------------
    YANGBLE5_SHIM_HOST   listen address       (default 127.0.0.1)
    YANGBLE5_SHIM_PORT   listen port          (default 8320)
    YANGBLE5_BASE_URL    upstream engine URL  (default http://127.0.0.1:8318)

Runs on Linux, macOS and Windows; standard library only.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

SHIM_HOST_ENV = "YANGBLE5_SHIM_HOST"
SHIM_PORT_ENV = "YANGBLE5_SHIM_PORT"
BASE_URL_ENV = "YANGBLE5_BASE_URL"

DEFAULT_SHIM_HOST = "127.0.0.1"
DEFAULT_SHIM_PORT = 8320
DEFAULT_BASE_URL = "http://127.0.0.1:8318"
DEFAULT_TIMEOUT = 900.0

MESSAGES_PATH = "/v1/messages"

# Hop-by-hop headers plus the three we must recompute ourselves. Forwarding
# Content-Length after rewriting the body would truncate it; forwarding
# Accept-Encoding would let the upstream gzip a stream we then have to re-chunk.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
        "accept-encoding",
    }
)

UPSTREAM_UNREACHABLE = (
    b'{"type":"error","error":{"type":"api_error",'
    b'"message":"yangble5 engine unreachable through claude_shim"}}'
)

BAD_FRAMING = (
    b'{"type":"error","error":{"type":"invalid_request_error",'
    b'"message":"claude_shim could not frame the request body"}}'
)

CHUNKED_UNSUPPORTED = (
    b'{"type":"error","error":{"type":"invalid_request_error",'
    b'"message":"claude_shim requires Content-Length; chunked requests are not supported"}}'
)

BODY_TOO_LARGE = (
    b'{"type":"error","error":{"type":"invalid_request_error",'
    b'"message":"request body exceeds claude_shim limit"}}'
)

# Hard ceiling on a request body. A 1M-token prompt is a few MB of JSON; 64 MiB
# is generous for that and still bounds the memory one client can pin, since the
# shim must buffer the body to rewrite roles.
MAX_BODY_BYTES = int(os.environ.get("YANGBLE5_SHIM_MAX_BODY_BYTES") or 64 * 1024 * 1024)


# --------------------------------------------------------------------------
# Pure request rewriting. Unit-testable without a socket.
# --------------------------------------------------------------------------


def fix_system_roles(body: bytes) -> bytes:
    """Map ``messages[].role == "system"`` to ``"user"`` (the v7.2.93 mapping).

    Returns the ORIGINAL ``body`` object unchanged whenever no rewrite is needed:
    no system-role message, unparseable JSON, or ``messages`` that is not a list.
    Byte-identical passthrough matters -- re-serialising a body we did not need to
    touch would reorder nothing but would still change whitespace, and the
    upstream prompt cache keys on the exact bytes.

    Note the top-level Anthropic ``system`` parameter is NOT a message and is left
    alone; only entries inside ``messages`` are rewritten.
    """
    # Fast path: the substring must appear for any message to carry that role.
    # Skipping the JSON round-trip here is what keeps untouched bodies identical.
    if b'"system"' not in body:
        return body
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        # Malformed or non-JSON payload: not ours to fix. Let the upstream
        # produce its own error rather than inventing one here.
        return body
    if not isinstance(data, dict):
        return body
    messages = data.get("messages")
    if not isinstance(messages, list):
        return body

    changed = False
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "system":
            message["role"] = "user"
            changed = True
    if not changed:
        return body
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def should_rewrite(method: str, path: str) -> bool:
    """Only POSTs to the Anthropic messages endpoint carry a ``messages`` array."""
    return method.upper() == "POST" and path.split("?", 1)[0] == MESSAGES_PATH


def maybe_fix_body(method: str, path: str, body: bytes | None) -> bytes | None:
    """Apply :func:`fix_system_roles` only where it can possibly apply."""
    if not body or not should_rewrite(method, path):
        return body
    return fix_system_roles(body)


def parse_upstream(url: str) -> tuple[str, str, int]:
    """Split an upstream URL into ``(scheme, host, port)``.

    Accepts a bare ``host:port`` (assumed http) as well as a full URL, because
    operators reach for both.
    """
    candidate = url.strip()
    if "://" not in candidate:
        candidate = "http://" + candidate
    parts = urlsplit(candidate)
    scheme = parts.scheme or "http"
    if scheme not in ("http", "https"):
        raise ValueError(f"unsupported upstream scheme: {scheme!r}")
    host = parts.hostname
    if not host:
        raise ValueError(f"upstream URL has no host: {url!r}")
    port = parts.port or (443 if scheme == "https" else 80)
    return scheme, host, port


@dataclass(frozen=True)
class ShimConfig:
    """Everything the handler needs; passed on the server, never through globals."""

    listen_host: str = DEFAULT_SHIM_HOST
    listen_port: int = DEFAULT_SHIM_PORT
    upstream_scheme: str = "http"
    upstream_host: str = "127.0.0.1"
    upstream_port: int = 8318
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def from_parts(
        cls,
        listen_host: str,
        listen_port: int,
        upstream_url: str,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> ShimConfig:
        scheme, host, port = parse_upstream(upstream_url)
        return cls(listen_host, listen_port, scheme, host, port, timeout)

    def connect(self) -> http.client.HTTPConnection:
        if self.upstream_scheme == "https":
            return http.client.HTTPSConnection(
                self.upstream_host, self.upstream_port, timeout=self.timeout
            )
        return http.client.HTTPConnection(
            self.upstream_host, self.upstream_port, timeout=self.timeout
        )

    def describe(self) -> str:
        return (
            f"{self.listen_host}:{self.listen_port} -> "
            f"{self.upstream_scheme}://{self.upstream_host}:{self.upstream_port}"
        )


# --------------------------------------------------------------------------
# Proxy server
# --------------------------------------------------------------------------


class ShimHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # Quiet by default: the engine already logs every request, and a second log
    # of the same traffic just makes the real errors harder to find.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    @property
    def config(self) -> ShimConfig:
        return self.server.config  # type: ignore[attr-defined]

    def _read_body(self) -> bytes | None | bool:
        """Return the body, ``None`` when there is none, or ``False`` on refusal.

        Framing is security-critical here. The shim must buffer the body to
        rewrite roles, so it needs an exact length. Getting this wrong is a
        request-smuggling primitive: if we forward ``Content-Length: 0`` while
        leaving unread bytes on a keep-alive connection, the server parses those
        leftover bytes as the *next* request on that connection.
        """
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            # Refuse rather than half-handle it, and close the connection so no
            # undrained body can be re-parsed as a follow-up request.
            self.close_connection = True
            self._send_bytes(411, "application/json", CHUNKED_UNSUPPORTED)
            return False

        raw = self.headers.get("Content-Length")
        if raw is None:
            return None
        try:
            length = int(raw.strip())
        except (TypeError, ValueError):
            self.close_connection = True
            self._send_bytes(400, "application/json", BAD_FRAMING)
            return False
        if length < 0:
            self.close_connection = True
            self._send_bytes(400, "application/json", BAD_FRAMING)
            return False
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            self._send_bytes(413, "application/json", BODY_TOO_LARGE)
            return False
        if length == 0:
            return None

        body = self.rfile.read(length)
        if len(body) != length:
            # Client disconnected mid-body: the stream is no longer trustworthy.
            self.close_connection = True
            return False
        return body

    def _forward(self) -> None:
        config = self.config
        body = self._read_body()
        if body is False:
            return
        body = maybe_fix_body(self.command, self.path, body)

        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        # Identity encoding: we re-frame the response ourselves, so a compressed
        # upstream body would have to be decompressed and recompressed for nothing.
        headers["Accept-Encoding"] = "identity"
        if body is not None:
            headers["Content-Length"] = str(len(body))

        connection = config.connect()
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
        except OSError:
            connection.close()
            self._send_bytes(502, "application/json", UPSTREAM_UNREACHABLE)
            return

        try:
            self._relay(response)
        finally:
            connection.close()

    def _relay(self, response: http.client.HTTPResponse) -> None:
        streaming = "text/event-stream" in (response.getheader("Content-Type") or "")
        self.send_response(response.status)
        for key, value in response.getheaders():
            if key.lower() in HOP_BY_HOP:
                continue
            self.send_header(key, value)

        if not streaming:
            payload = response.read()
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
            return

        # SSE: forward chunk by chunk and flush each one. Buffering here would
        # turn a streaming agent UI into a long pause followed by a wall of text.
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        while True:
            chunk = response.read1(65536)
            if not chunk:
                break
            self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _send_bytes(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = _forward


class ShimServer(ThreadingHTTPServer):
    daemon_threads = True

    # WHY False: HTTPServer sets allow_reuse_address = 1, and on Windows
    # SO_REUSEADDR lets a second process bind a port another process is actively
    # listening on. That would silently start a second shim that receives some of
    # the traffic. Exclusive binding turns a double start into a clean error.
    allow_reuse_address = False

    def __init__(self, config: ShimConfig, handler: type[BaseHTTPRequestHandler] = ShimHandler):
        self.config = config
        super().__init__((config.listen_host, config.listen_port), handler)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser(env: dict[str, str] | None = None) -> argparse.ArgumentParser:
    env = os.environ if env is None else env
    try:
        default_port = int(env.get(SHIM_PORT_ENV, DEFAULT_SHIM_PORT))
    except (TypeError, ValueError):
        default_port = DEFAULT_SHIM_PORT
    parser = argparse.ArgumentParser(
        prog="claude_shim",
        description=(
            "Rewrite mid-conversation system-role messages for CLIProxyAPI < 7.2.93. "
            "Retire this once the engine is upgraded."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--listen-host", default=env.get(SHIM_HOST_ENV, DEFAULT_SHIM_HOST))
    parser.add_argument("--listen-port", type=int, default=default_port)
    parser.add_argument("--upstream", default=env.get(BASE_URL_ENV, DEFAULT_BASE_URL))
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = ShimConfig.from_parts(
            args.listen_host, args.listen_port, args.upstream, args.timeout
        )
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc

    try:
        server = ShimServer(config)
    except OSError as exc:
        print(
            f"claude_shim: cannot bind {config.listen_host}:{config.listen_port} ({exc}). "
            "Another shim is probably already running.",
            file=sys.stderr,
        )
        return 0
    print(
        f"claude_shim: {config.describe()} "
        "(system-role fix for antigravity streaming; retire at engine >= 7.2.93)",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
