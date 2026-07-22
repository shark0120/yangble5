"""The yangble5 public gateway — FastAPI application.

WHAT THIS IS
------------
A reverse proxy that sits between the public internet and the *internal*
yangble5 engine (CLIProxyAPI). It authenticates a yangble5-issued key, enforces
quota and rate limits, then forwards the caller's opaque request body to the
engine using a SERVER-side engine key the caller never sees.

It is deliberately boring. Every design choice below prefers "refuse the
request" over "guess and keep spending the operator's money".

LOGGING POLICY (enforced by construction, not by convention)
------------------------------------------------------------
This module NEVER logs:
  * a yangble5 key, in whole or in part, plaintext or hashed;
  * the engine key or the admin key;
  * a request body, a response body, a prompt, or a completion;
  * a raw client IP address (only the salted hash Storage produces).

It logs only metadata: key_id (a public, non-secret identifier), endpoint,
HTTP status, token counts, cost, latency, and abuse counters. `_log()` takes
keyword fields and serialises them to JSON; if you are tempted to add a field
here that carries user content, the answer is no.

THE FOUR THINGS THAT PROTECT THE OPERATOR
-----------------------------------------
1. Per-key daily budget (tokens and/or USD) — durable, in SQLite, shared by all
   workers. Checked before the request, charged after it.
2. Global monthly cap — the backstop. When it trips, every *spending* endpoint
   returns 402 and the service degrades to read-only. It never silently keeps
   spending.
3. Per-key concurrency — this is what bounds quota *overshoot*. A budget can
   only be checked before a request and charged after it, so N in-flight
   requests can each pass a check that the others are about to invalidate.
   Capping N caps the overshoot to N requests, not to infinity.
4. Per-IP limits and auth-failure backoff on the unauthenticated endpoints.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import re
import sys
import threading
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .byok import ByokCipher, SealedCredential, storage_notice
from .config import Settings
from .ratelimit import (
    AuthCache,
    ConcurrencyLimiter,
    FailureBackoff,
    SlidingWindowLimiter,
    TimedThrottle,
    UpstreamHealth,
)
from .storage import (
    MACHINE_ID_MAX_CHARS,
    MACHINE_ID_MIN_CHARS,
    EmailInUseError,
    InviteError,
    RegistrationCapError,
    Storage,
    day_key,
    month_key,
    normalize_machine_id,
    parse_key,
    pepper_fingerprint,
    utcnow,
    verify_secret,
)
from .upstream import (
    HttpxUpstream,
    UpstreamError,
    build_upstream_headers,
    filter_response_headers,
)
from .usage import TokenUsage, UsageScanner, compute_cost

__all__ = ["GatewayState", "create_app"]

logger = logging.getLogger("yangble5.gateway")

# The engine paths this gateway is willing to expose. An allowlist, not a
# catch-all `/{path:path}` route: the engine also serves a management API, and a
# prefix proxy would hand it to the internet the day someone adds a new route.
PROXY_ROUTES: tuple[tuple[str, str], ...] = (
    ("POST", "/v1/messages"),
    ("POST", "/v1/chat/completions"),
    ("POST", "/v1/responses"),
    ("GET", "/v1/models"),
)

# Endpoints that can cost the operator money. These are what the global cap
# switches off; everything else stays up so clients get a clear diagnosis
# instead of a dead host.
_SPENDING_METHODS = frozenset({"POST"})

# Upstream statuses that mean "the account behind the shared pool cannot serve
# this right now" — quota exhausted, or rate-limited by the provider. The
# gateway answers these itself rather than forwarding the upstream body: the
# provider's error text can name the account, and a user who just hit a wall
# needs the BYOK instructions far more than they need a provider stack trace.
_UPSTREAM_QUOTA_STATUSES = frozenset({402, 429})

# The ONLY non-2xx statuses forwarded verbatim on shared-pool traffic.
#
# WHY an allowlist and not a denylist: the failure modes of one personal OAuth
# credential are not confined to 402/429. An account that needs re-verification
# answers 403 and names the account in the body; an engine that cannot mint a
# token answers 5xx and can name the internal host; a 3xx carries a Location
# header pointing at infrastructure the public has no business seeing. Every one
# of those used to stream straight to the caller. These four, by contrast, are
# verdicts on the CALLER'S OWN request — "your JSON is malformed", "that is too
# big", "wrong media type", "that field is invalid" — and withholding them would
# leave a user unable to fix a request only they can fix.
#
# BYOK traffic is never intercepted at all: that is the caller's own account
# answering the caller, and it is theirs to read.
_UPSTREAM_PASSTHROUGH_STATUSES = frozenset({400, 413, 415, 422})

# Identifier for the single process-wide upstream concurrency bucket. A constant
# on purpose: ConcurrencyLimiter buckets per identifier, so one constant is
# exactly what turns a per-caller cap into an aggregate one.
_UPSTREAM_SLOT = "__shared_pool__"

# Used in user-facing text so a "month" window never renders as "monthly" via
# naive string concatenation ("dayly").
_WINDOW_ADJECTIVE = {"day": "daily", "month": "monthly"}

# Bounded scan for the model name, used only to select a price-table row.
_MODEL_RE = re.compile(rb'"model"\s*:\s*"([^"\\]{1,200})"')

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s.]{1,63}(\.[^@\s.]{1,63})+$")

# How often ONE machine may re-register in a day. Not an operator setting: the
# honest use is "my credentials file is gone", which happens once, and a knob
# here would only ever be turned up by someone debugging their own install.
# Five leaves room for a bad afternoon and still ends a replay quickly.
MAX_REISSUES_PER_MACHINE_PER_DAY = 5


# ---------------------------------------------------------------------------
# structured logging
# ---------------------------------------------------------------------------
class _JsonFormatter(logging.Formatter):
    """One JSON object per line. Fields come from `extra={"fields": {...}}`."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, Mapping):
            payload.update(fields)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _log(level: int, event: str, **fields: Any) -> None:
    """Emit one structured line. See the module docstring's logging policy:
    every caller passes metadata only."""
    logger.log(level, event, extra={"fields": fields})


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    logger.handlers[:] = [handler]
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _error(status: int, kind: str, message: str, **extra: Any) -> JSONResponse:
    """Error envelope shaped like the upstream APIs so SDKs surface `message`."""
    body: dict[str, Any] = {"error": {"type": kind, "message": message}}
    body["error"].update(extra)
    headers = {}
    retry_after = extra.get("retry_after_seconds")
    if isinstance(retry_after, (int, float)) and retry_after > 0:
        headers["Retry-After"] = str(int(retry_after))
    return JSONResponse(body, status_code=status, headers=headers)


def _next_utc_midnight():
    now = utcnow()
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def _next_utc_month_start():
    now = utcnow()
    if now.month == 12:
        return now.replace(
            year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    return now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)


def _seconds_until_utc_midnight() -> int:
    return max(1, int((_next_utc_midnight() - utcnow()).total_seconds()))


def _seconds_until_month_end() -> int:
    return max(1, int((_next_utc_month_start() - utcnow()).total_seconds()))


def _normalize_ip(raw: str | None) -> str | None:
    """Return `raw` as a canonical IP literal, or None if it is not one.

    Forwarded headers are attacker-influenced strings. Everything downstream
    (per-IP rate limits, the abuse fan-out counter, the registration cap) buckets
    on this value, so a caller that can put arbitrary text here can mint an
    unlimited number of distinct buckets. Rejecting non-addresses means a forged
    header falls through to the next source instead of becoming a bucket key.

    Ports are tolerated because some proxies append them: `1.2.3.4:5678` and
    `[2001:db8::1]:443` are both common in the wild and both mean the address.
    """
    value = (raw or "").strip()
    if not value:
        return None
    if value.startswith("[") and "]" in value:          # [2001:db8::1]:443
        value = value[1 : value.index("]")]
    elif value.count(":") == 1 and "." in value:        # 1.2.3.4:5678
        value = value.split(":", 1)[0]
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def client_ip(request: Request, settings: Settings) -> str:
    """Best-effort client address.

    ORDER MATTERS, and it is the opposite of the obvious one.

    X-Real-IP is consulted FIRST. Our edge (deploy/Caddyfile) sets it with
    `header_up X-Real-IP {client_ip}`, which REPLACES any value the client sent
    and carries Caddy's own verdict on who the client is — already unwound
    through `trusted_proxies` and `Cf-Connecting-Ip`. It is a single address with
    no list semantics to get wrong.

    X-Forwarded-For is the FALLBACK, for edges that do not set X-Real-IP. It is
    appended to by every hop, and Caddy appends *its own peer* — which behind
    Cloudflare is a Cloudflare edge node, not the user. Reading the last entry
    therefore attributes every request on the planet to a handful of Cloudflare
    addresses and collapses all per-IP limits into one shared bucket. Hence the
    hop arithmetic: with N appending proxies in front, the real client is the
    Nth entry from the end. Taking the FIRST entry instead would be worse still —
    that one is fully client-controlled.

    Neither header is looked at when TRUST_PROXY_HEADERS is off: without a proxy
    that overwrites them, both are just strings the caller chose.
    """
    if settings.trust_proxy_headers:
        real = _normalize_ip(request.headers.get("x-real-ip"))
        if real:
            return real
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            parts = [p.strip() for p in forwarded.split(",") if p.strip()]
            if parts:
                candidate = _normalize_ip(parts[max(0, len(parts) - settings.trusted_proxy_hops)])
                if candidate:
                    return candidate
    return request.client.host if request.client else "unknown"


def _bearer(request: Request) -> str | None:
    """Accept the three spellings the supported SDKs use."""
    header = request.headers.get("authorization")
    if header:
        prefix, _, rest = header.partition(" ")
        if prefix.lower() == "bearer" and rest.strip():
            return rest.strip()
    for name in ("x-api-key", "api-key"):
        value = request.headers.get(name)
        if value and value.strip():
            return value.strip()
    return None


def extract_model(body: bytes, max_parse_bytes: int) -> str | None:
    """Pull the requested model out of a request body, for PRICING only.

    Small bodies are parsed properly. Large ones (a 748K-token prompt is
    several MB of JSON) fall back to a regex, because parsing multiple megabytes
    of JSON on the hot path to read one short string is not a trade worth
    making. A wrong answer here costs pricing accuracy, not correctness: an
    unknown model falls through to the price table's mandatory 'default' row,
    and the regex can in principle match a "model" key nested in user content.
    """
    if not body:
        return None
    if len(body) <= max_parse_bytes:
        try:
            parsed = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            parsed = None
        if isinstance(parsed, Mapping):
            model = parsed.get("model")
            return model if isinstance(model, str) and model else None
    match = _MODEL_RE.search(body)
    if match:
        try:
            return match.group(1).decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None


async def read_body_capped(request: Request, limit: int) -> bytes | None:
    """Read the request body, or return None if it exceeds `limit`.

    Checked twice on purpose: Content-Length gives a cheap early reject, but it
    is client-supplied, so the streaming read is what actually enforces the cap.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        return None
    buffer = bytearray()
    async for chunk in request.stream():
        buffer.extend(chunk)
        if len(buffer) > limit:
            return None
    return bytes(buffer)


# ---------------------------------------------------------------------------
# global spend tracking
# ---------------------------------------------------------------------------
class GlobalSpendTracker:
    """The operator's monthly ceiling, cached so it is cheap to check per request.

    WHY a cache: the authoritative number is `SUM(cost_usd)` over the month's
    usage rows, and running that aggregate on every single request would make
    the cap itself the bottleneck. So the value is read from SQLite at most once
    per `ttl`, and every charge made in this process is added immediately.

    That makes the cached value *monotonically correct in the safe direction*:
    it can only ever be too high (this process's own charges are counted the
    moment they happen), never too low, so the cap trips early rather than late.
    Multi-process deployments converge within `ttl` seconds.
    """

    def __init__(self, storage: Storage, ttl_seconds: float = 10.0):
        self._storage = storage
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._month: str | None = None
        self._cost = 0.0
        self._tokens = 0
        # The DAY totals are tracked with exactly the same discipline: the
        # shared pool has a daily ceiling as well as a monthly one, and a cap
        # that is only checked once every restart is not a cap.
        self._day: str | None = None
        self._day_cost = 0.0
        self._day_tokens = 0
        self._fetched_at = float("-inf")

    def _refresh_locked(self, month: str, day: str, now: float) -> None:
        month_totals = self._storage.global_usage_for_month(month)
        day_totals = self._storage.global_usage_for_day(day)
        self._month = month
        self._cost = month_totals.cost_usd
        self._tokens = month_totals.total_tokens
        self._day = day
        self._day_cost = day_totals.cost_usd
        self._day_tokens = day_totals.total_tokens
        self._fetched_at = now

    def _ensure_fresh_locked(self) -> None:
        now = time.monotonic()
        month, day = month_key(), day_key()
        if month != self._month or day != self._day or now - self._fetched_at > self._ttl:
            self._refresh_locked(month, day, now)

    def current(self) -> tuple[float, int]:
        """(cost_usd, tokens) for the current UTC month."""
        with self._lock:
            self._ensure_fresh_locked()
            return self._cost, self._tokens

    def current_day(self) -> tuple[float, int]:
        """(cost_usd, tokens) for the current UTC day."""
        with self._lock:
            self._ensure_fresh_locked()
            return self._day_cost, self._day_tokens

    def add(self, cost_usd: float, tokens: int) -> None:
        with self._lock:
            if self._month == month_key():
                self._cost += cost_usd
                self._tokens += tokens
            if self._day == day_key():
                self._day_cost += cost_usd
                self._day_tokens += tokens

    def invalidate(self) -> None:
        with self._lock:
            self._fetched_at = float("-inf")


@dataclass(frozen=True)
class BudgetVerdict:
    allowed: bool
    status: int = 200
    kind: str = ""
    message: str = ""
    retry_after: int = 0


@dataclass(frozen=True)
class PoolState:
    """How much of the SHARED pool is left, as a fraction, plus when it refills.

    A fraction rather than a number of dollars or tokens on purpose: this value
    is served unauthenticated to a landing-page widget, and how much the
    operator spends is nobody else's business. A percentage tells a visitor the
    one thing they need ("is there room for me right now?") and tells a
    competitor, a scraper or an attacker nothing at all.
    """

    remaining_pct: float   # 0.0-1.0, the MINIMUM across every configured cap
    reset_at: str          # ISO-8601 UTC, when the binding cap refills
    window: str            # "day" | "month" | "none"
    capped: bool           # False when the operator configured no pool ceiling

    @property
    def exhausted(self) -> bool:
        return self.capped and self.remaining_pct <= 0.0


@dataclass(frozen=True)
class ServiceState:
    """The ONE answer to "can this service serve a shared-pool request now?".

    It exists because there used to be two. `/pool/status` computed
    `registration_open` from the monthly cap AND the daily pool, while
    `POST /auth/register` gated on the monthly cap alone — so during a
    daily-exhausted window the widget advertised "registration closed" while the
    endpoint kept returning 201 with a key that could not be used. Two surfaces
    describing the same service must not compute the answer twice.

    The three questions are kept SEPARATE on purpose, because they have
    genuinely different answers:

      * `registration_allowed` — will /auth/register mint a key? Yes unless the
        monthly cap is tripped. A pool that is dry *today* is not a reason to
        refuse a key: attaching a BYOK credential is the documented way out of
        exactly that situation, and it needs a key to attach to.
      * `accepting` — will a shared-pool request be served? Needs budget AND a
        working upstream.
      * `usable_now` — `accepting` and the caller is not locked out by the
        operator reserve. This is what the registration response reports, so an
        installer never stores a key while believing it will work.
    """

    pool: PoolState
    cap: BudgetVerdict
    daily: BudgetVerdict
    reserve: BudgetVerdict           # always evaluated for a NON-operator caller
    upstream_ok: bool
    upstream_failures: int

    @property
    def registration_allowed(self) -> bool:
        return self.cap.allowed

    @property
    def accepting(self) -> bool:
        return self.cap.allowed and self.daily.allowed and self.upstream_ok

    @property
    def reserve_engaged(self) -> bool:
        return not self.reserve.allowed

    @property
    def usable_now(self) -> bool:
        return self.accepting and self.reserve.allowed

    @property
    def remaining_pct(self) -> float:
        """Budget headroom, CAPPED AT ZERO while the upstream is failing.

        `pool.remaining_pct` is a pure budget ratio, and a budget ratio cannot
        move when the upstream refuses: the refusal path writes no usage row and
        adds no spend. Reporting 100% through an outage is the single most
        misleading thing this endpoint could do, because its entire job is to
        answer "is there room for me right now?".
        """
        return 0.0 if not self.upstream_ok else self.pool.remaining_pct

    def blocking_verdict(self) -> BudgetVerdict | None:
        """The reason a shared-pool request would be refused, or None."""
        for verdict in (self.cap, self.daily, self.reserve):
            if not verdict.allowed:
                return verdict
        if not self.upstream_ok:
            return BudgetVerdict(
                False,
                503,
                "upstream_unavailable",
                "The upstream account behind the shared pool is not serving "
                "requests at the moment. This is not a problem with your key, and "
                "no budget of yours has been spent. Attach your own upstream "
                "credential to keep working now.",
                60,
            )
        return None


def byok_instructions(settings: Settings) -> dict[str, Any]:
    """The "here is how to keep working" payload.

    Attached to every degradation response, because the moment a user is told
    "the pool is dry" is the only moment they will actually read how to stop
    depending on it. Contains no secrets and no per-user data — it is the same
    text for everybody.
    """
    if not settings.byok_enabled:
        return {
            "available": False,
            "summary": (
                "This instance does not accept user-supplied upstream credentials. "
                "To stop depending on the shared pool, run your own instance — the "
                "whole stack is open source."
            ),
            "self_host": "https://github.com/shark0120/yangble5",
        }
    payload: dict[str, Any] = {
        "available": True,
        "summary": (
            "Attach your own free upstream account and your requests stop drawing "
            "on the shared pool entirely — no daily ceiling, no operator reserve, "
            "no queue behind anyone else."
        ),
        "steps": [
            "Get a credential for an upstream account you own.",
            "POST /byok with {\"credential\": \"...\"} and your yangble5 key in the "
            "Authorization header.",
            "Send requests exactly as before. GET /byok confirms it is attached; "
            "DELETE /byok detaches it and puts you back on the shared pool.",
        ],
        "attach_endpoint": "POST /byok",
        "detach_endpoint": "DELETE /byok",
        "self_host": "https://github.com/shark0120/yangble5",
    }
    if settings.byok_docs_url:
        payload["docs_url"] = settings.byok_docs_url
    return payload


def _degraded(
    settings: Settings,
    pool: PoolState,
    *,
    status: int,
    reason: str,
    message: str,
    retry_after: int,
) -> JSONResponse:
    """The one shape every "you cannot spend right now" answer takes.

    The `error` envelope is kept so an OpenAI/Anthropic SDK still surfaces
    `message` to the user instead of a bare status code, and the same facts are
    repeated at the top level where a plain HTTP client (an installer script, a
    status widget) can read them without knowing the SDK convention.
    """
    body: dict[str, Any] = {
        "error": {"type": reason, "message": message},
        "reason": reason,
        "message": message,
        "reset_at": pool.reset_at,
        "remaining_pct": pool.remaining_pct,
        "byok_instructions": byok_instructions(settings),
    }
    headers = {"Retry-After": str(max(1, int(retry_after)))}
    return JSONResponse(body, status_code=status, headers=headers)


def _binding_throttled(state: GatewayState, held: float) -> JSONResponse:
    """The soft machine-binding throttle. Explicitly NOT a ban, and it says so.

    A user who sees this has done nothing wrong — they moved networks, or they
    are on a phone hopping between cells. The response tells them what happened,
    how long it lasts, and that nothing was taken away from them, because the
    alternative (a silent 403) generates support messages the operator has no
    time to answer.
    """
    settings = state.settings
    return _degraded(
        settings,
        state.pool_state(),
        status=429,
        reason="key_binding_throttled",
        message=(
            f"This key has been used from more than {settings.max_ips_per_key} "
            "network addresses recently, so it is being slowed down for about "
            f"{int(held) + 1}s. Your key is still active and nothing has been "
            "suspended — this clears on its own. If you share one key across "
            "several machines, register each machine separately instead."
        ),
        retry_after=int(held) + 1,
    )


# ---------------------------------------------------------------------------
# application state
# ---------------------------------------------------------------------------
class GatewayState:
    """Everything the handlers need, built once per app.

    Constructed explicitly (rather than read from module globals) so tests can
    hand in an in-memory database and a fake upstream without monkeypatching.
    """

    def __init__(self, settings: Settings, storage: Storage, upstream: Any):
        self.settings = settings
        self.storage = storage
        self.upstream = upstream
        self.started_at = time.monotonic()
        self.key_rpm = SlidingWindowLimiter(settings.rate_limit_rpm)
        self.key_concurrency = ConcurrencyLimiter(settings.rate_limit_concurrency)
        # AGGREGATE, not per-key. `key_concurrency` buckets on key_id, so N keys
        # get N x rate_limit_concurrency simultaneous requests; the single
        # account behind the shared pool sees that product, not the factor.
        self.upstream_concurrency = ConcurrencyLimiter(settings.upstream_max_concurrency)
        self.upstream_health = UpstreamHealth(
            settings.upstream_health_window_seconds,
            settings.upstream_health_failure_threshold,
        )
        self.auth_ip_rpm = SlidingWindowLimiter(settings.auth_rpm_per_ip)
        self.auth_backoff = FailureBackoff(
            settings.auth_fail_lockout_threshold, settings.auth_fail_lockout_seconds
        )
        self.auth_cache = AuthCache(settings.auth_cache_ttl_seconds)
        self.spend = GlobalSpendTracker(storage)
        self.binding_throttle = TimedThrottle()
        self.byok_cipher = ByokCipher(settings.byok_encryption_key)

    # -- shared pool -----------------------------------------------------------
    def pool_state(self) -> PoolState:
        """Remaining capacity as the MINIMUM across every configured ceiling.

        The minimum, not an average: if the monthly budget is 80% free but
        today's slice is spent, the honest answer to "is there room for me?" is
        no. Taking the tightest constraint is also what makes `reset_at`
        meaningful — it names the cap the user is actually waiting on.
        """
        settings = self.settings
        month_cost, month_tokens = self.spend.current()
        day_cost, day_tokens = self.spend.current_day()

        remaining: list[tuple[float, str]] = []
        if settings.global_monthly_usd_budget > 0:
            remaining.append(
                (1.0 - month_cost / settings.global_monthly_usd_budget, "month")
            )
        if settings.global_monthly_token_budget > 0:
            remaining.append(
                (1.0 - month_tokens / settings.global_monthly_token_budget, "month")
            )
        if settings.global_daily_usd_budget > 0:
            remaining.append((1.0 - day_cost / settings.global_daily_usd_budget, "day"))
        if settings.global_daily_token_budget > 0:
            remaining.append(
                (1.0 - day_tokens / settings.global_daily_token_budget, "day")
            )

        if not remaining:
            # No ceiling configured. Report "full" rather than "unknown": there
            # is nothing to ration, so there is nothing to reserve either.
            return PoolState(1.0, _next_utc_midnight().isoformat(), "none", False)

        fraction, window = min(remaining, key=lambda item: item[0])
        fraction = max(0.0, min(1.0, fraction))
        reset = _next_utc_midnight() if window == "day" else _next_utc_month_start()
        return PoolState(round(fraction, 4), reset.isoformat(), window, True)

    def service_state(self) -> ServiceState:
        """Every capacity question answered once, from one set of reads.

        `/pool/status`, `POST /auth/register` and `/admin/stats` all call this,
        so they cannot disagree about whether the service is usable.
        """
        pool = self.pool_state()
        healthy, _last_status, failures = self.upstream_health.snapshot()
        return ServiceState(
            pool=pool,
            cap=self.global_cap_state(),
            daily=self.daily_pool_verdict(),
            reserve=self.reserve_verdict(pool, is_operator=False),
            upstream_ok=healthy,
            upstream_failures=failures,
        )

    def reserve_verdict(self, pool: PoolState, is_operator: bool) -> BudgetVerdict:
        """Gate the bottom slice of the pool for the operator's own keys.

        The operator funds this pool out of their own accounts and also has to
        get their own work done with it. Without this, one enthusiastic
        afternoon of public traffic takes the operator's daily driver offline —
        and an operator whose own tools stop working turns the service off for
        everyone. Reserving a slice is what keeps the doors open at all.
        """
        fraction = self.settings.operator_reserve_fraction
        if fraction <= 0 or not pool.capped or is_operator:
            return BudgetVerdict(True)
        if pool.remaining_pct > fraction:
            return BudgetVerdict(True)
        # RETRY-AFTER MUST FOLLOW THE WINDOW THAT IS ACTUALLY BINDING.
        #
        # This used to hardcode "seconds until 00:00 UTC" no matter which cap
        # produced `pool.remaining_pct`. On this deployment the binding window is
        # the MONTH, and nothing refills a monthly counter before the 1st — so
        # the header said "try again in a few hours" while the `reset_at` field
        # in the very same JSON body said "2026-08-01". A client that trusts the
        # header retries every night for up to four weeks and is refused every
        # time; a client that trusts the body gives up. Both were told the truth
        # by one half of the response and a lie by the other.
        retry = (
            _seconds_until_month_end()
            if pool.window == "month"
            else _seconds_until_utc_midnight()
        )
        return BudgetVerdict(
            False,
            429,
            "operator_reserve_engaged",
            "The shared pool is down to its reserved slice, which is held for the "
            "operator's own account so this service can keep running at all. The "
            f"binding ceiling is the {_WINDOW_ADJECTIVE.get(pool.window, pool.window)} "
            f"one, so your requests resume at {pool.reset_at} — or immediately if "
            "you attach your own upstream credential.",
            retry,
        )

    def daily_pool_verdict(self) -> BudgetVerdict:
        """The shared pool's daily ceiling. Distinct from the monthly cap
        because it clears at 00:00 UTC, which is a much kinder thing to tell a
        user than 'come back on the 1st'."""
        settings = self.settings
        if not settings.has_daily_pool_cap:
            return BudgetVerdict(True)
        cost, tokens = self.spend.current_day()
        exhausted = (
            settings.global_daily_usd_budget > 0 and cost >= settings.global_daily_usd_budget
        ) or (
            settings.global_daily_token_budget > 0
            and tokens >= settings.global_daily_token_budget
        )
        if not exhausted:
            return BudgetVerdict(True)
        return BudgetVerdict(
            False,
            429,
            "pool_exhausted",
            "Today's shared pool is spent. It refills at 00:00 UTC. Nothing is "
            "being sent upstream until then — attach your own upstream credential "
            "to keep working now.",
            _seconds_until_utc_midnight(),
        )

    # -- global cap ------------------------------------------------------------
    def global_cap_state(self) -> BudgetVerdict:
        settings = self.settings
        if not settings.has_global_cap:
            return BudgetVerdict(True)
        cost, tokens = self.spend.current()
        if settings.global_monthly_usd_budget > 0 and cost >= settings.global_monthly_usd_budget:
            return BudgetVerdict(
                False,
                402,
                "operator_budget_exhausted",
                "This yangble5 instance has reached its monthly operator spend cap and "
                "is temporarily read-only. No requests are being sent upstream. "
                "Budgets reset at 00:00 UTC on the 1st.",
            )
        if (
            settings.global_monthly_token_budget > 0
            and tokens >= settings.global_monthly_token_budget
        ):
            return BudgetVerdict(
                False,
                402,
                "operator_budget_exhausted",
                "This yangble5 instance has reached its monthly operator token cap and "
                "is temporarily read-only. No requests are being sent upstream. "
                "Budgets reset at 00:00 UTC on the 1st.",
            )
        return BudgetVerdict(True)

    # -- per-key budget --------------------------------------------------------
    def key_budget_state(self, record: Any) -> BudgetVerdict:
        """Per-key daily ceiling. Per-key overrides beat the global default."""
        settings = self.settings
        token_budget = (
            record.daily_token_budget
            if record.daily_token_budget is not None
            else settings.daily_token_budget
        )
        cost_budget = (
            record.daily_cost_budget_usd
            if record.daily_cost_budget_usd is not None
            else settings.daily_cost_usd_budget
        )
        if token_budget <= 0 and cost_budget <= 0:
            return BudgetVerdict(True)
        # billable_only: the per-key daily budget is this user's slice of the
        # SHARED pool. Requests they paid for themselves (BYOK) never touched it,
        # so counting those here would charge them twice for the same tokens.
        used = self.storage.usage_for_day(record.key_id, billable_only=True)
        retry = _seconds_until_utc_midnight()
        if token_budget > 0 and used.total_tokens >= token_budget:
            return BudgetVerdict(
                False,
                429,
                "daily_quota_exhausted",
                f"Daily token allowance reached ({used.total_tokens:,} of {token_budget:,}). "
                "It resets at 00:00 UTC.",
                retry,
            )
        if cost_budget > 0 and used.cost_usd >= cost_budget:
            return BudgetVerdict(
                False,
                429,
                "daily_quota_exhausted",
                "Daily cost allowance reached. It resets at 00:00 UTC.",
                retry,
            )
        return BudgetVerdict(True)


@dataclass(frozen=True)
class AuthContext:
    record: Any
    key_id: str
    ip: str
    ip_hash: str


class _AuthFailure(Exception):
    """Carries the response to return; keeps handlers free of auth branching."""

    def __init__(self, response: JSONResponse):
        self.response = response


# ---------------------------------------------------------------------------
# authentication
# ---------------------------------------------------------------------------
async def authenticate(request: Request, state: GatewayState) -> AuthContext:
    settings = state.settings
    ip = client_ip(request, settings)
    ip_hash = state.storage.hash_ip(ip)

    locked = state.auth_backoff.locked_for(ip_hash)
    if locked > 0:
        raise _AuthFailure(
            _error(
                429,
                "too_many_auth_failures",
                "Too many failed authentication attempts. Try again later.",
                retry_after_seconds=int(locked) + 1,
            )
        )

    presented = _bearer(request)
    if not presented:
        raise _AuthFailure(
            _error(
                401,
                "authentication_error",
                "Missing credentials. Send your yangble5 key as "
                "'Authorization: Bearer yb5_...' or 'x-api-key: yb5_...'.",
            )
        )

    parsed = parse_key(presented)
    if parsed is None:
        # Malformed keys are rejected without a database round trip, but they
        # still count as a failure so key-format probing is throttled too.
        state.auth_backoff.record_failure(ip_hash)
        raise _AuthFailure(
            _error(401, "authentication_error", "Invalid yangble5 key.")
        )
    key_id, secret = parsed

    # Status and budgets are read fresh on EVERY request, never cached, so
    # suspending or revoking a key takes effect on the caller's next call.
    record = await run_in_threadpool(state.storage.get_key, key_id)
    if record is None:
        state.auth_backoff.record_failure(ip_hash)
        raise _AuthFailure(_error(401, "authentication_error", "Invalid yangble5 key."))

    if not state.auth_cache.check(key_id, secret):
        # Cache miss: pay the KDF. Off the event loop, because scrypt at these
        # parameters costs tens of milliseconds and ~16 MiB, and blocking the
        # loop on it would make the gateway trivially CPU-DoS-able.
        ok = await run_in_threadpool(
            verify_secret, secret, record.digest, record.salt, record.scheme, settings.key_pepper
        )
        if not ok:
            state.auth_backoff.record_failure(ip_hash)
            _log(
                logging.WARNING,
                "auth.failed",
                key_id=key_id,
                ip_hash=ip_hash[:12],
                reason="bad_secret",
                # If the operator rotated KEY_PEPPER, every key fails at once and
                # looks like a mass credential leak. Say which it is.
                pepper_mismatch=record.pepper_fp != pepper_fingerprint(settings.key_pepper),
            )
            raise _AuthFailure(_error(401, "authentication_error", "Invalid yangble5 key."))
        state.auth_cache.store(key_id, secret)

    if record.status != "active":
        # A suspended key is a *known* key, so this is not a brute-force signal.
        raise _AuthFailure(
            _error(
                403,
                "key_suspended",
                f"This key is {record.status}."
                + (f" Reason: {record.suspended_reason}" if record.suspended_reason else ""),
            )
        )

    state.auth_backoff.record_success(ip_hash)
    return AuthContext(record=record, key_id=key_id, ip=ip, ip_hash=ip_hash)


async def check_abuse(state: GatewayState, ctx: AuthContext) -> None:
    """Distinct-IP fan-out detection for one key.

    Two thresholds, deliberately different in kind:

    * MAX_IPS_PER_KEY (low, default 5) — a SOFT, self-clearing throttle. This is
      the "loose binding" the operator asked for. A key is not a licence tied to
      one machine; people tether, travel, and work from cafés, and none of that
      should cost them their access. Crossing it slows them down for a minute.
    * ABUSE_DISTINCT_IP_THRESHOLD (high, default 8) — the resale signal, which
      can suspend the key if the operator turned that on.

    The count only runs when the IP is new for this key, so the steady-state
    cost of both is one UPSERT.
    """
    settings = state.settings
    is_new = await run_in_threadpool(state.storage.observe_ip, ctx.key_id, ctx.ip_hash)
    if not is_new:
        return
    watching_softly = settings.max_ips_per_key > 0
    watching_abuse = settings.abuse_distinct_ip_threshold > 0
    if not watching_softly and not watching_abuse:
        return

    window = max(settings.ip_binding_window_hours, settings.abuse_ip_window_hours)
    distinct = await run_in_threadpool(
        state.storage.distinct_ip_count, ctx.key_id, window
    )

    if watching_softly and distinct > settings.max_ips_per_key:
        state.binding_throttle.throttle(ctx.key_id, settings.binding_throttle_seconds)
        _log(
            logging.INFO,
            "binding.throttled",
            key_id=ctx.key_id,
            distinct_ips=distinct,
            limit=settings.max_ips_per_key,
        )

    if not watching_abuse or distinct < settings.abuse_distinct_ip_threshold:
        return
    if settings.abuse_auto_suspend:
        await run_in_threadpool(
            state.storage.set_key_status,
            ctx.key_id,
            "suspended",
            f"auto: {distinct} distinct IPs in {settings.abuse_ip_window_hours}h",
        )
        state.auth_cache.invalidate(ctx.key_id)
        _log(
            logging.WARNING,
            "abuse.suspended",
            key_id=ctx.key_id,
            distinct_ips=distinct,
            window_hours=window,
        )
    else:
        _log(
            logging.WARNING,
            "abuse.flagged",
            key_id=ctx.key_id,
            distinct_ips=distinct,
            window_hours=window,
        )


async def resolve_byok(state: GatewayState, key_id: str) -> str | None:
    """The caller's own upstream credential, or None if they are on the pool.

    Returns None (rather than raising) when a stored row cannot be opened,
    which happens for exactly one real reason: the operator changed or removed
    BYOK_ENCRYPTION_KEY. Falling back to the shared pool keeps that user
    working while they re-attach, instead of turning an operator's key rotation
    into an outage for everyone who had ever used BYOK.
    """
    if not state.settings.byok_enabled:
        return None
    stored = await run_in_threadpool(state.storage.get_byok, key_id)
    if stored is None:
        return None
    credential = state.byok_cipher.open(
        SealedCredential(stored.scheme, stored.nonce, stored.ciphertext)
    )
    if credential is None:
        _log(logging.WARNING, "byok.unreadable", key_id=key_id, scheme=stored.scheme)
        return None
    return credential


# ---------------------------------------------------------------------------
# request models
# ---------------------------------------------------------------------------
class RegisterRequest(BaseModel):
    email: str | None = Field(default=None, max_length=254)
    invite_code: str | None = Field(default=None, max_length=200)
    label: str | None = Field(default=None, max_length=100)
    # An opaque sha256 hex digest from the installer. Bounded here as well as in
    # normalize_machine_id so an oversized value is rejected by the parser
    # before it ever reaches a hash function or a database.
    machine_id: str | None = Field(default=None, max_length=MACHINE_ID_MAX_CHARS)


class ByokRequest(BaseModel):
    credential: str = Field(min_length=8, max_length=4096)
    label: str | None = Field(default=None, max_length=100)


class OperatorFlagRequest(BaseModel):
    is_operator: bool


class InviteRequest(BaseModel):
    code: str | None = Field(default=None, max_length=200)
    label: str | None = Field(default=None, max_length=100)
    max_uses: int = Field(default=1, ge=1, le=10_000)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class KeyStatusRequest(BaseModel):
    status: str = Field(pattern="^(active|suspended|revoked)$")
    reason: str | None = Field(default=None, max_length=200)


# ---------------------------------------------------------------------------
# app factory
# ---------------------------------------------------------------------------
def create_app(
    settings: Settings | None = None,
    storage: Storage | None = None,
    upstream: Any | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)

    owns_storage = storage is None
    storage = storage or Storage(settings.db_path)
    owns_upstream = upstream is None
    upstream = upstream or HttpxUpstream(
        settings.engine_url,
        timeout=settings.upstream_timeout_seconds,
        connect_timeout=settings.upstream_connect_timeout_seconds,
        pool_timeout=settings.upstream_pool_timeout_seconds,
        max_connections=settings.upstream_max_connections,
    )
    state = GatewayState(settings, storage, upstream)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        for warning in settings.startup_warnings():
            _log(logging.WARNING, "startup.warning", detail=warning)
        _log(
            logging.INFO,
            "startup.ready",
            registration_mode=settings.registration_mode,
            global_cap_usd=settings.global_monthly_usd_budget or None,
            global_cap_tokens=settings.global_monthly_token_budget or None,
            daily_cap_usd=settings.global_daily_usd_budget or None,
            daily_cap_tokens=settings.global_daily_token_budget or None,
            operator_reserve_fraction=settings.operator_reserve_fraction,
            byok_enabled=settings.byok_enabled,
            byok_encrypted_at_rest=state.byok_cipher.encrypts,
            admin_enabled=settings.admin_enabled,
        )
        try:
            yield
        finally:
            if owns_upstream:
                await upstream.aclose()
            if owns_storage:
                storage.close()

    app = FastAPI(
        title="yangble5 gateway",
        version=_package_version(),
        docs_url=None,      # no interactive docs on a public credentialed surface
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.gateway = state

    _register_public_routes(app, state)
    _register_proxy_routes(app, state)
    _register_admin_routes(app, state)
    return app


def _daily_allowance(settings: Settings) -> dict[str, Any]:
    """The per-key daily ceilings, with the one that BINDS named explicitly.

    A registration response that lists a 2,000,000-token allowance and a $2.00
    allowance side by side invites the reader — especially an AI agent following
    an install script — to quote the looser one. At the shipped placeholder
    prices those two numbers are not two views of the same allowance: $2.00 is
    reached after 400,000 tokens, so four fifths of the advertised token budget
    is unreachable. Whoever reads this gets told which number stops them.
    """
    limits: list[dict[str, Any]] = []
    if settings.daily_token_budget > 0:
        limits.append({"unit": "tokens", "value": settings.daily_token_budget})
    if settings.daily_cost_usd_budget > 0:
        limits.append({"unit": "usd", "value": settings.daily_cost_usd_budget})
    if not limits:
        return {"limits": [], "binds": None, "note": "No per-key daily ceiling is configured."}

    binds = limits[0]["unit"] if len(limits) == 1 else None
    if binds is None:
        # Both configured. `usd_budget_token_ceiling` uses the cheapest price
        # column, so it OVER-states how far the dollar budget reaches; if even
        # that over-statement lands below the token budget, the dollar ceiling
        # binds and the conclusion cannot be an artefact of the estimate.
        ceiling = settings.usd_budget_token_ceiling(settings.daily_cost_usd_budget)
        if ceiling is not None and ceiling < settings.daily_token_budget:
            binds = "usd"
    note = (
        "Whichever ceiling is reached first stops requests until 00:00 UTC."
        if binds is None
        else f"The {binds} ceiling is the one that will stop you first."
    )
    return {"limits": limits, "binds": binds, "note": note}


def _issuance_status(state: GatewayState) -> dict[str, Any]:
    """Whether a key handed out RIGHT NOW would actually be served.

    A registration response used to describe only the allowance the key was
    granted. That is what the key is *permitted*, not what it will *get*: the
    daily pool can be spent, the operator reserve can be engaged, or the single
    upstream account can be refusing — and in every one of those cases the key
    is perfectly valid and every request it makes is refused. An installer that
    stores such a key and reports success is lying to its user on this gateway's
    behalf, so the facts and the way out ship with the key itself.
    """
    svc = state.service_state()
    payload: dict[str, Any] = {
        "usable_now": svc.usable_now,
        "pool_remaining_pct": svc.remaining_pct,
    }
    blocking = svc.blocking_verdict()
    if blocking is not None:
        payload["not_usable_reason"] = blocking.kind
        payload["not_usable_detail"] = blocking.message
        payload["retry_after_seconds"] = max(1, blocking.retry_after)
        payload["reset_at"] = svc.pool.reset_at
        payload["byok_instructions"] = byok_instructions(state.settings)
    return payload


def _package_version() -> str:
    from . import __version__

    return __version__


async def _reissue_for_machine(state: GatewayState, binding: Any, ip_hash: str) -> JSONResponse:
    """Answer a repeat registration from a known machine.

    Returns 200 (not 201): nothing was created. The caller gets the same
    `key_id` they had, carrying the same usage history and the same allowance.

    The SECRET half of the credential is new, because the old one genuinely no
    longer exists anywhere — only a salted, peppered KDF digest of it was ever
    stored, and keeping this project's "a stolen database yields no usable key"
    property is worth more than handing back a byte-identical string. The
    response says so plainly rather than letting the user discover it.
    """
    settings = state.settings
    record = await run_in_threadpool(state.storage.get_key, binding.key_id)
    if record is None:
        # The binding outlived its key. Treat it as unknown rather than
        # resurrecting anything; the operator deleted that key for a reason.
        _log(logging.WARNING, "register.orphan_binding", key_id=binding.key_id)
        return _error(
            409, "binding_orphaned",
            "This machine was registered before, but its key no longer exists. "
            "Ask the operator to clear the binding.",
        )
    if record.status != "active":
        # Re-registering must never launder a suspended or revoked key back into
        # service — that would make suspension a one-installer-rerun problem.
        return _error(
            403, "key_suspended",
            f"The key bound to this machine is {record.status}."
            + (f" Reason: {record.suspended_reason}" if record.suspended_reason else ""),
        )

    issued = await run_in_threadpool(
        lambda: state.storage.reissue_key_secret(
            binding.key_id, scheme=settings.key_hash_scheme, pepper=settings.key_pepper
        )
    )
    if issued is None:  # pragma: no cover - the row was read one statement ago
        return _error(500, "internal_error", "Could not re-issue this key.")
    await run_in_threadpool(state.storage.touch_machine_binding, binding.machine_hash)
    # The old secret is gone, so any cached verification of it must go too.
    state.auth_cache.invalidate(binding.key_id)
    _log(
        logging.INFO,
        "register.reissued",
        key_id=issued.key_id,
        ip_hash=ip_hash[:12],
        reissue_count=binding.reissue_count + 1,
    )
    return JSONResponse(
        {
            "api_key": issued.plaintext,
            "key_id": issued.key_id,
            "created_at": issued.created_at,
            "reused": True,
            "machine_bound": True,
            "warning": (
                "This machine already had a key, so no new one was created — you "
                "have the same key_id, the same usage history and the same daily "
                "allowance. The key STRING is freshly generated because the "
                "previous one is not recoverable from the server (it is only "
                "stored as a hash). Any copy of the old string has stopped working."
            ),
            "daily_token_budget": settings.daily_token_budget or None,
            "daily_cost_usd_budget": settings.daily_cost_usd_budget or None,
            "daily_allowance": _daily_allowance(settings),
            **_issuance_status(state),
        },
        status_code=200,
    )


# ---------------------------------------------------------------------------
# public routes
# ---------------------------------------------------------------------------
def _register_public_routes(app: FastAPI, state: GatewayState) -> None:
    settings = state.settings

    @app.get("/health")
    @app.get("/healthz")
    async def health() -> JSONResponse:
        """Liveness for the load balancer and a public status signal.

        Unauthenticated, so it deliberately exposes NOTHING an attacker could
        use: no engine URL, no database path, no key counts, no dollar amounts
        (the operator's spend is their business), no version of the internal
        engine. Only whether this process is up and whether it is still
        accepting paid work.

        TWO PATHS, ONE PAYLOAD. `/health` is canonical and is what the docs, the
        smoke test and both compose files probe. `/healthz` is the same handler
        under the spelling that container platforms, Kubernetes probes and half
        the reverse-proxy templates on the internet assume. Serving both HERE —
        rather than rewriting at the edge — is what makes the gateway work
        unchanged behind Caddy, behind someone else's nginx, or behind nothing at
        all. An edge rewrite only exists in the edge that has it; the operator
        whose panel-managed nginx does not have it gets a 404 at 3am instead.

        The one alias that is NOT served here is `/api/health`: `/api/*` is a
        prefix an edge owns, not a route this app should squat on. The Caddyfile
        rewrites it onto `/health`, and the landing page falls back to `/health`
        anyway when it 404s.
        """
        cap = state.global_cap_state()
        return JSONResponse(
            {
                "status": "ok" if cap.allowed else "degraded",
                "service": "yangble5-gateway",
                "version": _package_version(),
                "uptime_seconds": int(time.monotonic() - state.started_at),
                "accepting_requests": cap.allowed,
                "registration": settings.registration_mode,
            }
        )

    @app.get("/pool/status")
    async def pool_status() -> JSONResponse:
        """What the landing page's capacity widget reads. Unauthenticated.

        Everything here is a fraction, a boolean or a timestamp. There is
        deliberately no dollar figure, no token count, no key count, no upstream
        identifier and no engine detail: a visitor needs to know whether there is
        room for them, and nobody needs to know how much the operator spends.
        """
        svc = state.service_state()
        pool = svc.pool
        return JSONResponse(
            {
                # Capped at 0 while the upstream is refusing: a budget ratio
                # cannot move during an outage, so the raw ratio would report
                # "plenty of room" through a total failure to serve.
                "remaining_pct": svc.remaining_pct,
                "reset_at": pool.reset_at,
                "reset_window": pool.window,
                # Exactly the expression POST /auth/register uses. It is
                # deliberately NOT `accepting_requests`: a dry pool today is the
                # moment BYOK matters most, and attaching a credential needs a
                # key to attach it to.
                "registration_open": settings.registration_open and svc.registration_allowed,
                "accepting_requests": svc.accepting,
                "capped": pool.capped,
                "reserve_engaged": svc.reserve_engaged,
                "operator_reserve_fraction": settings.operator_reserve_fraction,
                "byok_available": settings.byok_enabled,
            }
        )

    @app.post("/auth/register")
    async def register(request: Request) -> JSONResponse:
        ip = client_ip(request, settings)
        ip_hash = state.storage.hash_ip(ip)

        # Per-IP throttle first: this endpoint mints credentials, so it is the
        # one an attacker scripts.
        allowed, retry = state.auth_ip_rpm.check(ip_hash)
        if not allowed:
            return _error(
                429, "rate_limit_error", "Too many registration attempts.",
                retry_after_seconds=int(retry) + 1,
            )
        locked = state.auth_backoff.locked_for(ip_hash)
        if locked > 0:
            return _error(
                429, "too_many_auth_failures",
                "Too many failed registration attempts. Try again later.",
                retry_after_seconds=int(locked) + 1,
            )

        if settings.registration_mode == "closed":
            return _error(
                403, "registration_closed",
                "Self-service registration is disabled on this instance.",
            )

        # ONE expression, shared with /pool/status's `registration_open`, so the
        # widget and the endpoint can never disagree about whether a key will be
        # issued. `svc` is also what fills in `usable_now` on the way out.
        svc = state.service_state()
        if not svc.registration_allowed:
            return _error(
                503, "registration_unavailable",
                "This instance is at its operator budget cap and is not issuing new "
                "keys right now.",
            )

        try:
            payload = RegisterRequest.model_validate(await request.json())
        except Exception:
            return _error(400, "invalid_request_error", "Body must be a JSON object.")

        # The fingerprint is validated before anything else touches it. An
        # invalid one is REJECTED, never quietly downgraded to "no fingerprint":
        # a validation you can skip by sending garbage is not a validation.
        machine_id: str | None = None
        if payload.machine_id is not None:
            machine_id = normalize_machine_id(payload.machine_id)
            if machine_id is None:
                return _error(
                    400, "invalid_machine_id",
                    "'machine_id' must be a hex fingerprint of "
                    f"{MACHINE_ID_MIN_CHARS}-{MACHINE_ID_MAX_CHARS} characters (the "
                    "installer sends a sha256 digest). Send nothing rather than "
                    "something else.",
                )
        machine_hash = state.storage.hash_machine_id(machine_id) if machine_id else None

        # ---- idempotent re-registration -------------------------------------
        # This is what makes "just re-run the installer" a safe instruction. The
        # same machine gets its EXISTING key back — same key_id, same usage
        # history, same daily allowance, same operator flag — instead of a
        # second key with a second allowance, which is the cheapest quota-farming
        # trick there is.
        #
        # DELIBERATELY BEFORE the per-IP reject below. That reject reads a counter
        # this path never increments, which made the ordering wrong in both
        # directions at once: an honest rerun from a shared office or CGNAT
        # address was refused once the neighbours had used the day's
        # registrations, while a replay of one captured machine id was refused
        # never, because it moved no counter at all.
        if machine_hash is not None:
            binding = await run_in_threadpool(state.storage.get_machine_binding, machine_hash)
            if binding is not None:
                # A reissue mints a fresh secret and invalidates the previous
                # one, so anyone holding this machine id can take the account
                # over — and the holder can take it straight back. Unbounded,
                # that is a key-rotation tug-of-war either side can run forever.
                # Bounded per machine, honest recovery still works and replay
                # stops after a handful of attempts.
                claimed, used = await run_in_threadpool(
                    state.storage.claim_machine_reissue,
                    machine_hash,
                    MAX_REISSUES_PER_MACHINE_PER_DAY,
                )
                if not claimed:
                    _log(
                        logging.WARNING,
                        "register.reissue_capped",
                        key_id=binding.key_id,
                        ip_hash=ip_hash[:12],
                        used=used,
                    )
                    return _error(
                        429, "rate_limit_error",
                        "This machine has re-registered too many times today. If "
                        "you did not do that, someone else has a copy of this "
                        "machine's id: ask the operator to revoke the key. The "
                        "limit clears at 00:00 UTC.",
                        retry_after_seconds=_seconds_until_utc_midnight(),
                    )
                return await _reissue_for_machine(state, binding, ip_hash)

        # CHEAP EARLY REJECT, not the decision. It saves a JSON parse and a
        # couple of hashes for an address that is already obviously over its
        # allowance. The binding decision is `claim_register_attempt` further
        # down; this read is allowed to be stale because nothing acts on it
        # except an immediate 429 that the atomic claim would also have issued.
        daily_count = await run_in_threadpool(state.storage.register_attempts_today, ip_hash)
        if settings.register_max_per_ip_per_day > 0 and (
            daily_count >= settings.register_max_per_ip_per_day
        ):
            return _error(
                429, "rate_limit_error",
                "This network has reached today's registration limit.",
                retry_after_seconds=_seconds_until_utc_midnight(),
            )

        email = (payload.email or "").strip() or None
        if email and not _EMAIL_RE.match(email):
            return _error(400, "invalid_request_error", "'email' is not a valid address.")
        if settings.registration_mode == "open" and not email and machine_id is None:
            # Open mode needs ONE stable identity, not a verified one. A machine
            # fingerprint is enough, and asking a fan to prove an email address
            # before they can try the thing is exactly the friction this mode
            # exists to remove. Nothing here sends mail or verifies anything.
            return _error(
                400, "invalid_request_error",
                "Send either a 'machine_id' (the installer does this automatically) "
                "or a valid 'email'. No verification step follows either way.",
            )

        # Count the attempt BEFORE the invite check, so guessing invite codes
        # burns the per-IP daily allowance instead of being free.
        #
        # ATOMIC: check-and-increment in one transaction. The stale read above
        # cannot be the ceiling, because between it and this line the request has
        # awaited a JSON parse, a fingerprint hash and a binding lookup — plenty
        # of room for a burst to pile through on one stale count. This call is
        # the first point at which the answer is binding, and it is placed AFTER
        # the machine-binding short-circuit on purpose: re-running the installer
        # is not an attempt to obtain a new key and must not consume allowance.
        claimed, _attempts = await run_in_threadpool(
            state.storage.claim_register_attempt, ip_hash, settings.register_max_per_ip_per_day
        )
        if not claimed:
            return _error(
                429, "rate_limit_error",
                "This network has reached today's registration limit.",
                retry_after_seconds=_seconds_until_utc_midnight(),
            )

        if settings.registration_mode == "invite":
            if not payload.invite_code:
                return _error(
                    400, "invite_required",
                    "This instance is invite-only. Supply 'invite_code'.",
                )
            try:
                await run_in_threadpool(state.storage.consume_invite, payload.invite_code)
            except InviteError:
                state.auth_backoff.record_failure(ip_hash)
                _log(logging.WARNING, "register.invite_rejected", ip_hash=ip_hash[:12])
                return _error(
                    403, "invite_invalid",
                    "That invite code is not valid, has expired, or has been used.",
                )

        # Both remaining ceilings are enforced INSIDE issue_key's transaction:
        #
        #   * keys ISSUED from this address today — deliberately a different
        #     counter from the attempts above. Mistyping an invite code five
        #     times farms nothing and should not be punished as if it had.
        #   * one active key per address.
        #
        # Checking either one here and issuing afterwards is the same read-then-
        # act race the attempt counter had: the count would be read, the request
        # would await the key derivation (scrypt — milliseconds, not
        # microseconds), and every concurrent sibling would have read the same
        # number. storage.issue_key raises instead, having counted under the
        # write lock that is about to insert the row.
        try:
            issued = await run_in_threadpool(
                lambda: state.storage.issue_key(
                    email=email,
                    label=payload.label,
                    scheme=settings.key_hash_scheme,
                    pepper=settings.key_pepper,
                    machine_hash=machine_hash,
                    registration_ip_hash=ip_hash,
                    max_keys_per_ip_per_day=settings.max_keys_per_ip,
                    enforce_unique_email=not settings.allow_multiple_keys_per_email,
                )
            )
        except RegistrationCapError as capped:
            _log(logging.INFO, "register.ip_key_cap", ip_hash=ip_hash[:12])
            return _error(
                429, "registration_throttled",
                f"This network already has {capped.count} key(s) from today "
                f"(limit {capped.limit}). This is a throttle, not a "
                "ban: it clears at 00:00 UTC. If you are re-installing, send the "
                "same 'machine_id' and you will get your existing key back "
                "instead of a new one.",
                retry_after_seconds=_seconds_until_utc_midnight(),
            )
        except EmailInUseError:
            return _error(
                409, "already_registered",
                "This address already has an active key. Ask the operator to "
                "revoke it if you need a replacement.",
            )
        _log(
            logging.INFO,
            "register.issued",
            key_id=issued.key_id,
            mode=settings.registration_mode,
            ip_hash=ip_hash[:12],
            machine_bound=machine_hash is not None,
        )
        # The ONLY moment the plaintext key exists outside the caller's memory.
        # It is not stored, not logged, and cannot be re-read from any endpoint.
        return JSONResponse(
            {
                "api_key": issued.plaintext,
                "key_id": issued.key_id,
                "created_at": issued.created_at,
                "warning": (
                    "Store this key now. It is hashed on the server and cannot be "
                    "shown again. If you lose it, you must register a new one."
                ),
                "daily_token_budget": settings.daily_token_budget or None,
                "daily_cost_usd_budget": settings.daily_cost_usd_budget or None,
                "daily_allowance": _daily_allowance(settings),
                "machine_bound": machine_hash is not None,
                "reused": False,
                # Re-read rather than reusing the `svc` from the top of the
                # handler: issuing a key involves a KDF and several awaits, and
                # the pool can be exhausted by another caller in that window.
                # The status shipped with the key describes the moment the key
                # is handed over, not the moment the request arrived.
                **_issuance_status(state),
            },
            status_code=201,
        )

    @app.get("/usage")
    async def usage_endpoint(request: Request) -> JSONResponse:
        """Own usage only. The key authenticates *and* selects the rows, so
        there is no parameter that could be tampered with to read someone else's."""
        try:
            ctx = await authenticate(request, state)
        except _AuthFailure as failure:
            return failure.response

        allowed, retry = state.key_rpm.check(ctx.key_id)
        if not allowed:
            return _error(
                429, "rate_limit_error", "Rate limit exceeded.",
                retry_after_seconds=int(retry) + 1,
            )

        day = await run_in_threadpool(state.storage.usage_for_day, ctx.key_id)
        month = await run_in_threadpool(state.storage.usage_for_month, ctx.key_id)
        record = ctx.record
        token_budget = (
            record.daily_token_budget
            if record.daily_token_budget is not None
            else settings.daily_token_budget
        )
        cost_budget = (
            record.daily_cost_budget_usd
            if record.daily_cost_budget_usd is not None
            else settings.daily_cost_usd_budget
        )
        return JSONResponse(
            {
                "key_id": ctx.key_id,
                "status": record.status,
                "today": {
                    "requests": day.requests,
                    "total_tokens": day.total_tokens,
                    "cost_usd": round(day.cost_usd, 6),
                    "token_budget": token_budget or None,
                    "cost_usd_budget": cost_budget or None,
                    "tokens_remaining": (
                        max(0, token_budget - day.total_tokens) if token_budget > 0 else None
                    ),
                },
                "this_month": {
                    "requests": month.requests,
                    "total_tokens": month.total_tokens,
                    "cost_usd": round(month.cost_usd, 6),
                },
                "resets_at": "00:00 UTC",
                "is_operator": bool(record.is_operator),
                "byok_attached": bool(
                    await run_in_threadpool(state.storage.get_byok, ctx.key_id)
                ),
            }
        )

    # -- BYOK ------------------------------------------------------------------
    @app.post("/byok")
    async def attach_byok(request: Request) -> JSONResponse:
        """Attach the caller's own upstream credential.

        From here on their requests are routed with it and stop drawing on the
        shared pool. The response states, in words, exactly how the credential
        is stored on THIS server, because that is the only question worth
        answering at the moment somebody hands one over.
        """
        try:
            ctx = await authenticate(request, state)
        except _AuthFailure as failure:
            return failure.response
        if not settings.byok_enabled:
            return _error(
                403, "byok_disabled",
                "This instance does not accept user-supplied upstream credentials. "
                "Run your own instance to use your own account.",
            )
        allowed, retry = state.key_rpm.check(ctx.key_id)
        if not allowed:
            return _error(
                429, "rate_limit_error", "Rate limit exceeded.",
                retry_after_seconds=int(retry) + 1,
            )
        try:
            payload = ByokRequest.model_validate(await request.json())
        except Exception:
            return _error(
                400, "invalid_request_error",
                "Body must be {\"credential\": \"<your upstream key>\"}.",
            )

        sealed = state.byok_cipher.seal(payload.credential.strip())
        await run_in_threadpool(
            lambda: state.storage.put_byok(
                ctx.key_id,
                scheme=sealed.scheme,
                nonce=sealed.nonce,
                ciphertext=sealed.ciphertext,
                label=payload.label,
            )
        )
        # Metadata only: the credential itself is not logged, not echoed back,
        # and not readable through any endpoint.
        _log(
            logging.INFO, "byok.attached",
            key_id=ctx.key_id, encrypted=state.byok_cipher.encrypts,
        )
        return JSONResponse(
            {
                "attached": True,
                "key_id": ctx.key_id,
                "encrypted_at_rest": state.byok_cipher.encrypts,
                "storage_notice": storage_notice(state.byok_cipher),
                "effect": (
                    "Your requests now use your own upstream account. They no longer "
                    "count against the shared pool, the operator reserve does not "
                    "apply to you, and your per-key daily allowance is not consumed."
                ),
                "detach": "DELETE /byok",
            },
            status_code=201,
        )

    @app.get("/byok")
    async def byok_status(request: Request) -> JSONResponse:
        try:
            ctx = await authenticate(request, state)
        except _AuthFailure as failure:
            return failure.response
        stored = await run_in_threadpool(state.storage.get_byok, ctx.key_id)
        # Never returns the credential — not even a prefix of it. There is no
        # endpoint on this service that can read one back out.
        return JSONResponse(
            {
                "attached": stored is not None,
                "encrypted_at_rest": bool(stored and stored.scheme != "plaintext"),
                "label": stored.label if stored else None,
                "updated_at": stored.updated_at if stored else None,
                "byok_available": settings.byok_enabled,
            }
        )

    @app.delete("/byok")
    async def detach_byok(request: Request) -> JSONResponse:
        try:
            ctx = await authenticate(request, state)
        except _AuthFailure as failure:
            return failure.response
        removed = await run_in_threadpool(state.storage.delete_byok, ctx.key_id)
        if removed:
            _log(logging.INFO, "byok.detached", key_id=ctx.key_id)
        return JSONResponse(
            {
                "attached": False,
                "removed": removed,
                "effect": (
                    "Your requests draw on the shared pool again, subject to its "
                    "daily allowance and the operator reserve."
                ),
            }
        )


# ---------------------------------------------------------------------------
# proxy routes
# ---------------------------------------------------------------------------
def _register_proxy_routes(app: FastAPI, state: GatewayState) -> None:
    for method, path in PROXY_ROUTES:
        app.add_api_route(
            path,
            _make_proxy_handler(state, path),
            methods=[method],
            include_in_schema=False,
        )


def _make_proxy_handler(state: GatewayState, path: str):
    settings = state.settings

    async def handler(request: Request):
        started = time.monotonic()
        try:
            ctx = await authenticate(request, state)
        except _AuthFailure as failure:
            return failure.response

        spending = request.method in _SPENDING_METHODS

        # Order matters: cheap in-memory checks before anything that touches the
        # database or the engine.
        allowed, retry = state.key_rpm.check(ctx.key_id)
        if not allowed:
            _log(logging.INFO, "request.rate_limited", key_id=ctx.key_id, endpoint=path)
            return _error(
                429, "rate_limit_error",
                f"Rate limit exceeded ({settings.rate_limit_rpm} requests/minute).",
                retry_after_seconds=int(retry) + 1,
            )

        # Still inside a machine-binding throttle from a previous request. Free
        # to check, so it happens before anything acquires a resource.
        held = state.binding_throttle.remaining(ctx.key_id)
        if held > 0:
            return _binding_throttled(state, held)

        # A BYOK caller pays their own upstream, so none of the pool gates below
        # apply to them — that is the entire point of attaching a credential.
        byok_credential = await resolve_byok(state, ctx.key_id) if spending else None
        billable = spending and byok_credential is None

        if billable:
            pool = state.pool_state()

            cap = state.global_cap_state()
            if not cap.allowed:
                _log(logging.WARNING, "request.global_cap", key_id=ctx.key_id, endpoint=path)
                return _degraded(
                    settings, pool, status=cap.status, reason=cap.kind,
                    message=cap.message, retry_after=_seconds_until_month_end(),
                )

            daily = state.daily_pool_verdict()
            if not daily.allowed:
                _log(logging.WARNING, "request.pool_exhausted", key_id=ctx.key_id, endpoint=path)
                return _degraded(
                    settings, pool, status=daily.status, reason=daily.kind,
                    message=daily.message, retry_after=daily.retry_after,
                )

            reserve = state.reserve_verdict(pool, bool(ctx.record.is_operator))
            if not reserve.allowed:
                _log(
                    logging.INFO, "request.reserve_engaged",
                    key_id=ctx.key_id, endpoint=path, remaining_pct=pool.remaining_pct,
                )
                return _degraded(
                    settings, pool, status=reserve.status, reason=reserve.kind,
                    message=reserve.message, retry_after=reserve.retry_after,
                )

            verdict = await run_in_threadpool(state.key_budget_state, ctx.record)
            if not verdict.allowed:
                _log(logging.INFO, "request.quota_exhausted", key_id=ctx.key_id, endpoint=path)
                # The per-key allowance, not the pool — but this is the exact
                # moment BYOK is worth knowing about, so it ships with the same
                # payload rather than a bare "quota exhausted".
                return _degraded(
                    settings, pool, status=verdict.status, reason=verdict.kind,
                    message=verdict.message, retry_after=verdict.retry_after,
                )

        # AGGREGATE FIRST, then per-key.
        #
        # `key_concurrency` buckets on key_id, so it caps ONE caller at
        # RATE_LIMIT_CONCURRENCY and says nothing about the sum. Fifty registered
        # keys at four in flight each is two hundred simultaneous requests landing
        # on the ONE upstream credential that serves the shared pool. This
        # limiter is the only thing in the request path that counts across keys.
        #
        # BYOK traffic is exempt on purpose: it is served by the caller's own
        # account, and `byok_instructions` promises attaching a credential means
        # "no queue behind anyone else". Charging BYOK callers for shared-pool
        # congestion would make that sentence false.
        if billable and not state.upstream_concurrency.acquire(_UPSTREAM_SLOT):
            _log(logging.INFO, "request.upstream_saturated", key_id=ctx.key_id, endpoint=path)
            return _error(
                429, "upstream_busy",
                "The shared pool is serving as many simultaneous requests as its "
                f"upstream account will take ({settings.upstream_max_concurrency}). "
                "Nothing is wrong with your key and no allowance was spent. Retry "
                "in a moment, or attach your own upstream credential to stop "
                "sharing this queue.",
                retry_after_seconds=2,
            )
        if not state.key_concurrency.acquire(ctx.key_id):
            if billable:
                state.upstream_concurrency.release(_UPSTREAM_SLOT)
            return _error(
                429, "concurrency_limit_error",
                f"Too many concurrent requests for this key "
                f"(limit {settings.rate_limit_concurrency}).",
                retry_after_seconds=1,
            )

        def release_slots() -> None:
            state.key_concurrency.release(ctx.key_id)
            if billable:
                state.upstream_concurrency.release(_UPSTREAM_SLOT)

        try:
            await check_abuse(state, ctx)
            # check_abuse may have just tripped the soft binding throttle on
            # this very request; honour it now rather than one request late.
            held = state.binding_throttle.remaining(ctx.key_id)
            if held > 0:
                release_slots()
                return _binding_throttled(state, held)

            body = await read_body_capped(request, settings.max_request_bytes)
            if body is None:
                release_slots()
                return _error(
                    413, "request_too_large",
                    f"Request body exceeds {settings.max_request_bytes} bytes.",
                )
            model = extract_model(body, settings.max_usage_parse_bytes) if spending else None
            return await _proxy(
                state, ctx, request, path, body, model, started, spending,
                billable=billable, credential=byok_credential or settings.engine_api_key,
                byok=byok_credential is not None,
            )
        except Exception:
            release_slots()
            raise

    handler.__name__ = f"proxy_{path.strip('/').replace('/', '_')}"
    return handler


async def _proxy(
    state: GatewayState,
    ctx: AuthContext,
    request: Request,
    path: str,
    body: bytes,
    model: str | None,
    started: float,
    spending: bool,
    *,
    billable: bool = True,
    credential: str | None = None,
    byok: bool = False,
):
    """Forward to the engine and stream the response back incrementally.

    The upstream context manager is kept open by an AsyncExitStack that the
    response generator owns, so headers are available immediately while the body
    still streams. Nothing is buffered: each chunk is fed to the usage scanner
    and yielded onward in the same step, which is what keeps SSE token-by-token.

    `credential` is what gets injected upstream: the server's engine key for
    pool traffic, or the caller's own credential for BYOK. It is chosen by the
    caller and never read from a client header — a user cannot smuggle one in.
    """
    settings = state.settings
    headers = build_upstream_headers(
        request.headers, credential or settings.engine_api_key,
        # A stable per-key tag so the engine's own logs can be correlated with
        # gateway logs. It is an opaque public id, never the secret.
        extra={
            "X-Yangble5-Key-Id": ctx.key_id,
            # Lets the engine pick a caller-credential route instead of its own
            # account pool. Set by the gateway only; stripped from client input.
            "X-Yangble5-Byok": "1" if byok else "0",
        },
    )
    released = False

    def release_once() -> None:
        nonlocal released
        if not released:
            released = True
            state.key_concurrency.release(ctx.key_id)
            # Mirrors the acquisition in the handler: the aggregate slot is only
            # taken for shared-pool traffic, so it is only given back for it.
            if billable:
                state.upstream_concurrency.release(_UPSTREAM_SLOT)

    stack = AsyncExitStack()
    try:
        response = await stack.enter_async_context(
            state.upstream.stream(
                request.method,
                path + (f"?{request.url.query}" if request.url.query else ""),
                headers=headers,
                content=body if body else None,
            )
        )
    except UpstreamError as exc:
        await stack.aclose()
        release_once()
        _log(
            logging.ERROR, "upstream.unreachable",
            key_id=ctx.key_id, endpoint=path, error=str(exc),
        )
        # Deliberately vague to the client: httpx error text can carry the
        # internal engine URL, which is not the public's business.
        return _error(502, "upstream_error", "The backend engine is unavailable.")

    status = response.status_code

    # THE SHARED UPSTREAM FAILED. Answer it ourselves.
    #
    # This used to fire on 402 and 429 only, and everything else fell through to
    # the passthrough below, which filters hop-by-hop headers and streams the
    # provider's body unchanged. The failure modes of one personal OAuth
    # credential are not confined to two statuses: an account awaiting
    # re-verification answers 403 with text that names it, and an engine that
    # cannot obtain a token answers 5xx with text that can name the internal
    # host. Those bodies reached the public client raw. Now the allowlist of
    # forwarded statuses is explicit and small (see
    # `_UPSTREAM_PASSTHROUGH_STATUSES`), and everything else gets the same
    # sanitised envelope with a reason that distinguishes "out of quota" from
    # "not working". BYOK callers are still passed through untouched — that is
    # their own account's error, and theirs to read.
    intercept = (
        billable
        and not (200 <= status < 300)
        and status not in _UPSTREAM_PASSTHROUGH_STATUSES
    )
    if billable and 200 <= status < 300:
        # Proof the SHARED account is serving. Clears the failure window, so a
        # burst of errors followed by a success does not leave /pool/status
        # claiming an outage that has ended.
        #
        # `billable` is load-bearing and must match the guard on record_failure
        # below. A BYOK request is served by the CALLER'S own credential, so its
        # 200 says nothing whatsoever about the operator's account -- but it was
        # clearing the window anyway. The shared pool could be returning 500 to
        # everyone while one active BYOK user kept /pool/status reporting
        # `accepting_requests: true` and `remaining_pct: 1.0`, which is the exact
        # outage-masking UpstreamHealth exists to prevent.
        state.upstream_health.record_success()
    if intercept:
        quota = status in _UPSTREAM_QUOTA_STATUSES
        state.upstream_health.record_failure(status)
        await stack.aclose()
        release_once()
        _log(
            logging.WARNING,
            "upstream.quota" if quota else "upstream.failed",
            key_id=ctx.key_id,
            endpoint=path,
            status=status,          # the REAL upstream status, operator-side only
        )
        if quota:
            return _degraded(
                settings,
                state.pool_state(),
                status=429,
                reason="upstream_quota_exhausted",
                message=(
                    "The upstream account behind the shared pool is out of quota or "
                    "rate-limited right now, so this request was not served. This is a "
                    "capacity limit, not a problem with your key. Attach your own "
                    "upstream credential to keep working immediately."
                ),
                retry_after=60,
            )
        return _degraded(
            settings,
            state.pool_state(),
            status=503,
            reason="upstream_unavailable",
            message=(
                "The upstream account behind the shared pool refused this request "
                "for a reason that is not a quota limit — it may need to be "
                "re-authorised, or the engine could not reach it. Nothing is wrong "
                "with your key and no allowance was spent. The operator has the "
                "real status in the gateway log. Attach your own upstream "
                "credential to keep working now."
            ),
            retry_after=30,
        )

    out_headers = filter_response_headers(response.headers)
    content_type = (out_headers.get("content-type") or out_headers.get("Content-Type") or "")
    streaming = "text/event-stream" in content_type.lower()
    # Authoritative, unlike guessing from the request's "stream" field: the
    # engine decides what it actually sends.
    scanner = UsageScanner(streaming=streaming, max_body_bytes=settings.max_usage_parse_bytes)
    if streaming:
        out_headers["X-Accel-Buffering"] = "no"
        out_headers["Cache-Control"] = "no-cache"

    async def body_stream() -> AsyncIterator[bytes]:
        try:
            async for chunk in response.aiter_raw():
                if spending:
                    scanner.feed(chunk)
                yield chunk
        finally:
            # Runs on completion AND on client disconnect, so a caller who hangs
            # up mid-stream is still charged for what the engine produced and
            # still gives their concurrency slot back.
            await stack.aclose()
            try:
                if spending:
                    await _record(
                        state, ctx, path, status, scanner, model, started, streaming,
                        billable=billable,
                    )
            finally:
                release_once()

    return StreamingResponse(
        body_stream(), status_code=status, headers=out_headers,
        media_type=content_type or None,
    )


def _apply_usage_floor(usage: TokenUsage, status: int, floor: int) -> tuple[TokenUsage, bool]:
    """Make an unparseable-but-successful response cost something.

    THE HOLE THIS CLOSES. `UsageScanner` reports `parsed=False` when the upstream
    body carried no usage object we recognised — a shape change, a body larger
    than MAX_USAGE_PARSE_BYTES, a truncated stream. Nothing acted on that flag,
    so the request was recorded with zero tokens and zero cost: it advanced
    neither the per-key daily budget nor the operator's global cap. A caller who
    can provoke an unparseable response reliably therefore has an UNMETERED
    channel through a service whose entire job is to meter.

    Charged as OUTPUT tokens, at the model's output rate, because that is the
    most expensive column in the price table — the floor should over-estimate a
    request we cannot see, never under-estimate it.

    Only applied to 2xx. A 4xx/5xx produced no completion and cost the operator
    nothing upstream; billing a floor for it would turn a provider outage into a
    bill and would let one broken client burn a stranger's daily allowance by
    failing repeatedly. `floor <= 0` disables the whole mechanism, which
    `startup_warnings()` says out loud.
    """
    if usage.parsed or floor <= 0 or not (200 <= status < 300):
        return usage, False
    return (
        TokenUsage(
            input_tokens=0,
            cached_input_tokens=0,
            cache_write_tokens=0,
            output_tokens=floor,
            # Still False: this is an ESTIMATE, and a log line claiming the
            # numbers were parsed would be a lie told to whoever debugs the
            # price table later.
            parsed=False,
        ),
        True,
    )


async def _record(
    state: GatewayState,
    ctx: AuthContext,
    path: str,
    status: int,
    scanner: UsageScanner,
    model: str | None,
    started: float,
    streamed: bool,
    *,
    billable: bool = True,
) -> None:
    """Charge the request and log its metadata.

    `billable=False` is BYOK traffic. The row is still written — the user can
    see their own history on /usage, and the operator can see the shape of their
    traffic — but it is flagged so that every pool aggregate skips it and the
    in-process spend tracker never counts it. Charging the shared pool for
    tokens the user paid for themselves would make BYOK pointless.
    """
    usage: TokenUsage = scanner.finish()
    usage, estimated = _apply_usage_floor(usage, status, state.settings.unparsed_usage_token_floor)
    price = state.settings.price_for(model)
    cost = compute_cost(usage, price)
    latency_ms = int((time.monotonic() - started) * 1000)

    await run_in_threadpool(
        lambda: state.storage.record_usage(
            key_id=ctx.key_id,
            endpoint=path,
            model=model,
            status=status,
            input_tokens=usage.input_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            streamed=streamed,
            billable=billable,
        )
    )
    if billable:
        state.spend.add(cost, usage.total_tokens)
    await run_in_threadpool(state.storage.touch_key, ctx.key_id)

    _log(
        logging.INFO,
        "request.completed",
        key_id=ctx.key_id,
        endpoint=path,
        model=model,
        status=status,
        streamed=streamed,
        input_tokens=usage.input_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
        cache_hit_ratio=round(usage.cache_hit_ratio, 4),
        cost_usd=round(cost, 6),
        latency_ms=latency_ms,
        usage_parsed=usage.parsed,
        usage_estimated=estimated,
        billable=billable,
    )


# ---------------------------------------------------------------------------
# admin routes
# ---------------------------------------------------------------------------
def _register_admin_routes(app: FastAPI, state: GatewayState) -> None:
    settings = state.settings

    def admin_ok(request: Request) -> bool:
        """Constant-time compare. Returns False when no admin key is configured,
        so an unset key means 'no admin surface', never 'no admin auth'."""
        if not settings.admin_api_key:
            return False
        presented = _bearer(request) or ""
        # Compared as bytes: hmac.compare_digest raises TypeError on str inputs
        # that are not ASCII-only, and an admin key with an accented character
        # must fail the comparison, not crash the endpoint into a 500.
        return hmac.compare_digest(
            presented.encode("utf-8"), settings.admin_api_key.encode("utf-8")
        )

    def guard(request: Request) -> JSONResponse | None:
        if admin_ok(request):
            return None
        # 404, not 403: do not confirm to an unauthenticated scanner that an
        # admin surface exists here at all.
        return _error(404, "not_found", "Not found.")

    @app.post("/admin/invites", include_in_schema=False)
    async def create_invite(request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        try:
            payload = InviteRequest.model_validate(await request.json())
        except Exception:
            return _error(400, "invalid_request_error", "Body must be a JSON object.")

        import secrets as _secrets

        code = payload.code or f"yb5inv_{_secrets.token_urlsafe(18)}"
        expires = (
            utcnow() + timedelta(days=payload.expires_in_days)
            if payload.expires_in_days
            else None
        )
        try:
            await run_in_threadpool(
                lambda: state.storage.create_invite(
                    code, label=payload.label, max_uses=payload.max_uses, expires_at=expires
                )
            )
        except Exception:
            return _error(409, "invite_exists", "That invite code already exists.")
        _log(logging.INFO, "admin.invite_created", max_uses=payload.max_uses)
        # Returned once. Only a salted hash of the code is stored.
        return JSONResponse(
            {
                "invite_code": code,
                "max_uses": payload.max_uses,
                "expires_at": expires.isoformat() if expires else None,
            },
            status_code=201,
        )

    @app.get("/admin/keys", include_in_schema=False)
    async def list_keys(request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        rows = await run_in_threadpool(state.storage.list_keys)
        return JSONResponse(
            {
                "keys": [
                    {
                        "key_id": row["key_id"],
                        "status": row["status"],
                        "email": row["email"],
                        "created_at": row["created_at"],
                        "last_used_at": row["last_used_at"],
                        "is_operator": bool(row["is_operator"]),
                        # Whether one is attached, never what it is.
                        "byok_attached": bool(row["has_byok"]),
                    }
                    for row in rows
                ]
            }
        )

    @app.post("/admin/keys/{key_id}/operator", include_in_schema=False)
    async def set_operator(key_id: str, request: Request) -> JSONResponse:
        """Flag a key as the operator's own daily driver.

        Operator keys are the only ones allowed into the reserved slice of the
        pool. Admin-only for the obvious reason: a self-service version of this
        endpoint would be a self-service version of the reserve.
        """
        denied = guard(request)
        if denied:
            return denied
        try:
            payload = OperatorFlagRequest.model_validate(await request.json())
        except Exception:
            return _error(
                400, "invalid_request_error", "Body must be {\"is_operator\": true|false}."
            )
        changed = await run_in_threadpool(
            state.storage.set_key_operator, key_id, payload.is_operator
        )
        if not changed:
            return _error(404, "not_found", "No such key.")
        _log(logging.INFO, "admin.key_operator", key_id=key_id, is_operator=payload.is_operator)
        return JSONResponse({"key_id": key_id, "is_operator": payload.is_operator})

    @app.post("/admin/keys/{key_id}/status", include_in_schema=False)
    async def set_status(key_id: str, request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        try:
            payload = KeyStatusRequest.model_validate(await request.json())
        except Exception:
            return _error(
                400, "invalid_request_error",
                "Body must be {\"status\": \"active|suspended|revoked\"}.",
            )
        changed = await run_in_threadpool(
            state.storage.set_key_status, key_id, payload.status, payload.reason
        )
        if not changed:
            return _error(404, "not_found", "No such key.")
        # Drop the cached KDF result so the change is effective immediately.
        state.auth_cache.invalidate(key_id)
        _log(logging.INFO, "admin.key_status", key_id=key_id, status=payload.status)
        return JSONResponse({"key_id": key_id, "status": payload.status})

    @app.get("/admin/stats", include_in_schema=False)
    async def stats(request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        cost, tokens = state.spend.current()
        day_cost, day_tokens = state.spend.current_day()
        svc = state.service_state()
        pool = svc.pool
        healthy, last_status, failures = state.upstream_health.snapshot()
        return JSONResponse(
            {
                "month": month_key(),
                "cost_usd": round(cost, 6),
                "total_tokens": tokens,
                "usd_cap": settings.global_monthly_usd_budget or None,
                "token_cap": settings.global_monthly_token_budget or None,
                "today": {
                    "day": day_key(),
                    "cost_usd": round(day_cost, 6),
                    "total_tokens": day_tokens,
                    "usd_cap": settings.global_daily_usd_budget or None,
                    "token_cap": settings.global_daily_token_budget or None,
                },
                "pool": {
                    "remaining_pct": pool.remaining_pct,
                    "reset_at": pool.reset_at,
                    "reserve_fraction": settings.operator_reserve_fraction,
                    "reserve_engaged": svc.reserve_engaged,
                },
                # Budget headroom and "is the account serving" are different
                # questions; the operator needs both, separately.
                "upstream": {
                    "ok": healthy,
                    "recent_failures": failures,
                    "last_failure_status": last_status,
                    "window_seconds": settings.upstream_health_window_seconds,
                    "max_concurrency": settings.upstream_max_concurrency,
                    "in_flight": state.upstream_concurrency.active(_UPSTREAM_SLOT),
                },
                "accepting_requests": svc.accepting,
                "prices_are_placeholder": settings.prices_are_placeholder,
                "byok_encrypted_at_rest": state.byok_cipher.encrypts,
            }
        )


# ---------------------------------------------------------------------------
# ASGI entry point
# ---------------------------------------------------------------------------
def __getattr__(name: str) -> Any:
    """Build the app lazily on `gateway.app:app`.

    WHY not a module-level `app = create_app()`: that would run Settings.from_env()
    at import time, so merely importing this module in a test (or a linter, or a
    doc build) would demand a live engine key. Lazy construction keeps the
    fail-fast behaviour exactly where it belongs — at process start, under
    uvicorn — while leaving the module importable everywhere else.
    """
    if name == "app":
        application = create_app()
        globals()["app"] = application
        return application
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
