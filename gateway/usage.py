"""Token accounting and cost pricing.

Two jobs:

1. Pull a usage report out of an upstream response *without buffering the whole
   response*, so streaming stays incremental. `UsageScanner.feed()` is handed
   the same bytes that are being forwarded to the client, one chunk at a time.

2. Price it, charging cached input tokens at their own (much lower) rate.
   That separation is the whole reason this project exists: a session that
   re-reads a 748K-token prefix out of the upstream prompt cache must not be
   billed as if it had sent it fresh.

Nothing here ever retains message content. The scanner keeps a bounded byte
buffer while it looks for JSON, extracts integers, and drops the rest.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .config import ModelPrice

__all__ = ["TokenUsage", "UsageScanner", "compute_cost"]

_MAX_SSE_LINE = 1 * 1024 * 1024  # a single SSE line larger than this is not usage


@dataclass
class TokenUsage:
    """Normalized token counts for one request."""

    input_tokens: int = 0          # fresh (uncached) prompt tokens
    cached_input_tokens: int = 0   # prompt tokens served from the upstream cache
    cache_write_tokens: int = 0    # prompt tokens written into the cache
    output_tokens: int = 0
    parsed: bool = False           # False -> upstream reported nothing usable

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cached_input_tokens

    @property
    def total_tokens(self) -> int:
        """What per-key token budgets are charged against."""
        return self.prompt_tokens + self.output_tokens

    @property
    def cache_hit_ratio(self) -> float:
        return (self.cached_input_tokens / self.prompt_tokens) if self.prompt_tokens else 0.0


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    try:
        out = int(value)
    except (ValueError, OverflowError):
        return 0
    return out if out > 0 else 0


class UsageScanner:
    """Incrementally extracts usage from a response body.

    Handles both shapes the engine can emit:

    * ``text/event-stream`` — scans each ``data:`` line as it goes by. Anthropic
      reports input tokens in ``message_start`` and cumulative output tokens in
      ``message_delta``; OpenAI reports a single ``usage`` object in the final
      chunk. Taking the per-field maximum handles both without double counting.
    * plain JSON — accumulates up to ``max_body_bytes`` and parses once at the
      end. Larger bodies are forwarded untouched and simply reported as
      unparsed rather than being held in memory.
    """

    def __init__(self, *, streaming: bool, max_body_bytes: int = 2 * 1024 * 1024):
        self.streaming = streaming
        self.max_body_bytes = max_body_bytes
        self._buffer = bytearray()
        self._overflowed = False
        self._raw_input = 0        # as reported by upstream (may include cached)
        self._cached = 0
        self._cache_write = 0
        self._output = 0
        self._saw_usage = False

    # -- feeding ---------------------------------------------------------------
    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        if self.streaming:
            self._feed_sse(chunk)
        else:
            if self._overflowed:
                return
            if len(self._buffer) + len(chunk) > self.max_body_bytes:
                self._overflowed = True
                self._buffer.clear()
                return
            self._buffer.extend(chunk)

    def _feed_sse(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)
        while True:
            idx = self._buffer.find(b"\n")
            if idx == -1:
                # Guard against a pathological unterminated line eating memory.
                if len(self._buffer) > _MAX_SSE_LINE:
                    del self._buffer[:-64]
                return
            line = bytes(self._buffer[:idx])
            del self._buffer[: idx + 1]
            self._scan_sse_line(line)

    def _scan_sse_line(self, line: bytes) -> None:
        line = line.strip()
        if not line.startswith(b"data:") or len(line) > _MAX_SSE_LINE:
            return
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]" or b"usage" not in payload:
            return
        try:
            obj = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            return
        self._absorb(obj)

    def finish(self) -> TokenUsage:
        """Call once the body is fully forwarded."""
        if self.streaming:
            if self._buffer:
                self._scan_sse_line(bytes(self._buffer))
                self._buffer.clear()
        elif self._buffer and not self._overflowed:
            # An unparseable body just means no usage numbers for this request;
            # accounting is best-effort and must never break the response path.
            with contextlib.suppress(ValueError, UnicodeDecodeError):
                self._absorb(json.loads(bytes(self._buffer)))
            self._buffer.clear()
        return self.usage()

    # -- extraction ------------------------------------------------------------
    def _absorb(self, obj: Any) -> None:
        """Read usage from the handful of places the supported wire formats use."""
        if not isinstance(obj, Mapping):
            return
        for container in (obj, obj.get("message"), obj.get("response"), obj.get("body")):
            if isinstance(container, Mapping):
                usage = container.get("usage")
                if isinstance(usage, Mapping):
                    self._absorb_usage(usage)

    def _absorb_usage(self, usage: Mapping[str, Any]) -> None:
        self._saw_usage = True
        raw_input = max(_as_int(usage.get("input_tokens")), _as_int(usage.get("prompt_tokens")))
        cached = max(
            _as_int(usage.get("cache_read_input_tokens")),
            _as_int(usage.get("cached_tokens")),
        )
        details = usage.get("prompt_tokens_details")
        if isinstance(details, Mapping):
            cached = max(cached, _as_int(details.get("cached_tokens")))
        cache_write = _as_int(usage.get("cache_creation_input_tokens"))
        output = max(
            _as_int(usage.get("output_tokens")), _as_int(usage.get("completion_tokens"))
        )
        # max(), not +=: Anthropic streams cumulative counters across several
        # events, so summing them would bill the same tokens repeatedly.
        self._raw_input = max(self._raw_input, raw_input)
        self._cached = max(self._cached, cached)
        self._cache_write = max(self._cache_write, cache_write)
        self._output = max(self._output, output)

    def usage(self) -> TokenUsage:
        """Normalize the two possible meanings of `input_tokens`.

        Verified live on 2026-07-21 against CLIProxyAPI 7.1.23: it maps Gemini's
        `promptTokenCount` to `input_tokens` unchanged, so that number ALREADY
        INCLUDES the cached reads (input >= cached). Native Anthropic upstreams
        report input_tokens EXCLUDING cache reads (input can be < cached). The
        same normalization cache_bench.py uses: pick whichever reading keeps the
        prompt total consistent, then derive the fresh portion by subtraction —
        never bill a token twice, never bill a cached token at the fresh rate.
        """
        if self._raw_input >= self._cached:
            prompt_total = self._raw_input
        else:
            prompt_total = self._raw_input + self._cached
        fresh = max(0, prompt_total - self._cached)
        return TokenUsage(
            input_tokens=fresh,
            cached_input_tokens=self._cached,
            cache_write_tokens=self._cache_write,
            output_tokens=self._output,
            parsed=self._saw_usage,
        )


def compute_cost(usage: TokenUsage, price: ModelPrice) -> float:
    """USD for one request. Prices are per 1,000,000 tokens."""
    million = 1_000_000.0
    return (
        usage.input_tokens * price.input
        + usage.cached_input_tokens * price.cached_input
        + usage.cache_write_tokens * price.cache_write_price
        + usage.output_tokens * price.output
    ) / million
