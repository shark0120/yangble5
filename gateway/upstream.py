"""Transport to the internal engine.

The gateway is a *reverse proxy for opaque payloads*: it does not understand,
rewrite or inspect the user's messages. It only swaps the credential, forwards
the bytes, and counts what comes back.

Two rules encoded here:

1. **The engine key is added on this side of the wire, never passed through.**
   `build_upstream_headers()` drops every client-supplied credential header
   before adding the server-side one, so a user cannot smuggle their own
   upstream key in, and cannot read the operator's out.

2. **`Accept-Encoding: identity`.** The engine is local, so compression buys
   nothing, and asking for identity means the bytes we forward are the bytes we
   can scan for a usage block — and any `Content-Length` we pass through still
   matches the body. Anything else risks a corrupted response.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Protocol, runtime_checkable

import httpx

__all__ = ["HttpxUpstream", "Upstream", "UpstreamError", "build_upstream_headers"]


class UpstreamError(RuntimeError):
    """The engine could not be reached or died mid-response."""


# Headers that must never travel from client to engine.
_STRIPPED_REQUEST_HEADERS = frozenset(
    {
        # credentials: the caller's yangble5 key is meaningless upstream, and
        # letting a caller set these would let them supply their own provider key.
        "authorization",
        "x-api-key",
        "api-key",
        "x-goog-api-key",
        "proxy-authorization",
        # hop-by-hop / connection framing (RFC 9110 s7.6.1)
        "connection",
        "keep-alive",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
        # rebuilt by the client
        "host",
        "content-length",
        "accept-encoding",
        # never let a caller forge their own forwarding chain
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-real-ip",
        # gateway-internal namespace: these are set BY the gateway, so a client
        # that sends them must not be able to have them believed downstream.
        "x-yangble5-key-id",
        "x-yangble5-byok",
    }
)

# Headers that must never travel back from engine to client.
_STRIPPED_RESPONSE_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "content-length",  # recomputed by our ASGI server
        "server",          # do not advertise the internal engine build
        "x-powered-by",
    }
)


def build_upstream_headers(
    incoming: Mapping[str, str], engine_api_key: str, *, extra: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Client headers -> engine headers, with the server-side credential added."""
    headers = {
        key: value
        for key, value in incoming.items()
        if key.lower() not in _STRIPPED_REQUEST_HEADERS
    }
    headers["Authorization"] = f"Bearer {engine_api_key}"
    # The engine speaks several wire formats; the Anthropic one authenticates
    # with x-api-key rather than Authorization.
    headers["x-api-key"] = engine_api_key
    headers["Accept-Encoding"] = "identity"
    if extra:
        headers.update(extra)
    return headers


def filter_response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _STRIPPED_RESPONSE_HEADERS}


@runtime_checkable
class UpstreamResponse(Protocol):
    status_code: int
    headers: Mapping[str, str]

    def aiter_raw(self) -> AsyncIterator[bytes]: ...


class Upstream(Protocol):
    """Minimal surface so tests can substitute a fake without a socket."""

    def stream(
        self, method: str, path: str, *, headers: Mapping[str, str], content: bytes | None = None
    ): ...

    async def aclose(self) -> None: ...


class HttpxUpstream:
    """Real transport. One pooled AsyncClient for the process lifetime."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 900.0,
        connect_timeout: float = 10.0,
        pool_timeout: float = 15.0,
        max_connections: int = 32,
    ):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            # Long read timeout: a 748K-token prompt legitimately takes minutes
            # before the first token arrives. A short connect timeout keeps a
            # dead engine from parking every worker.
            #
            # `pool` is set EXPLICITLY and short. httpx applies the default
            # timeout to the pool-acquire wait as well, so `Timeout(900,
            # connect=10)` meant a request that found the connection pool full
            # waited up to fifteen minutes before it was even sent — a queue
            # indistinguishable, from the caller's side, from a hung engine.
            # Failing fast is the only answer a client can act on.
            timeout=httpx.Timeout(timeout, connect=connect_timeout, pool=pool_timeout),
            follow_redirects=False,  # a redirect off-host would leak the engine key
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=min(40, max_connections),
            ),
        )

    @asynccontextmanager
    async def stream(
        self, method: str, path: str, *, headers: Mapping[str, str], content: bytes | None = None
    ) -> AsyncIterator[UpstreamResponse]:
        try:
            async with self._client.stream(
                method, path, headers=dict(headers), content=content
            ) as response:
                yield response
        except httpx.HTTPError as exc:
            # Deliberately generic: the message reaches the public client, and
            # httpx exception text can contain the internal URL.
            raise UpstreamError(f"{type(exc).__name__}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()
