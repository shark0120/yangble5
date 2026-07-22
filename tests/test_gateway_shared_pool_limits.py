"""Regression tests for the SHARED-POOL protections in the public gateway.

Deliberately self-contained — its own harness, its own fakes — rather than
importing helpers from `test_gateway.py`. These properties are about the one
upstream account that serves the 1M tier, and a test that defends them should
not go red because an unrelated fixture in another file was refactored.

Every test here is written against a defect that was live in the shipped code:

  * aggregate concurrency was never bounded (only per-key), so N keys put
    N x RATE_LIMIT_CONCURRENCY requests on ONE upstream credential;
  * `/pool/status` reported budget headroom and called it capacity, so it read
    "plenty of room" while the upstream was refusing every request;
  * the operator-reserve refusal sent `Retry-After: <hours>` while the same body
    said the pool resets next month;
  * the per-key USD ceiling silently overrode the token allowance advertised in
    the same registration response;
  * `POST /auth/register` issued keys the pool could not serve, while
    `/pool/status` said registration was closed;
  * every upstream failure outside {402, 429} was streamed to the public raw.
"""

from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import Settings
from gateway.storage import Storage, parse_key

ENGINE_KEY = "sk-engine-test-only-not-a-real-key"
ADMIN_KEY = "admin-test-only-not-a-real-key"
USER_CREDENTIAL = "sk-user-own-upstream-test-only-not-a-real-key"

BASE_ENV = {
    "ENGINE_API_KEY": ENGINE_KEY,
    "ADMIN_API_KEY": ADMIN_KEY,
    "KEY_HASH_SCHEME": "pbkdf2",      # fast; production defaults to scrypt
    "REGISTRATION_MODE": "open",
    "GLOBAL_MONTHLY_USD_BUDGET": "100",
    "ALLOW_MULTIPLE_KEYS_PER_EMAIL": "true",
    "AUTH_RPM_PER_IP": "0",
    "REGISTER_MAX_PER_IP_PER_DAY": "0",
    "MAX_KEYS_PER_IP": "0",
}

NO_KEY_LIMIT = 10**12


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status, headers, chunks):
        self.status_code = status
        self.headers = headers
        self._chunks = chunks

    async def aiter_raw(self):
        for chunk in self._chunks:
            yield chunk


class FakeUpstream:
    """Engine stand-in. `gate` lets a test hold requests in flight."""

    def __init__(self):
        self.status = 200
        self.headers = {"content-type": "application/json"}
        self.chunks: list[bytes] = [b'{"ok":true}']
        self.calls: list[dict] = []
        self.gate: threading.Event | None = None
        self.entered = threading.Semaphore(0)

    def stream(self, method, path, *, headers, content=None):
        upstream = self

        class _Ctx:
            async def __aenter__(self):
                upstream.calls.append({"method": method, "path": path, "headers": dict(headers)})
                # SNAPSHOT the gate, and do it before announcing arrival.
                #
                # This used to read `upstream.gate` twice -- once for the None
                # check and once for `.wait()`. A test that rebinds the
                # attribute while this request is in flight (which
                # test_byok_traffic_is_not_queued_behind_the_shared_pool does,
                # to let a second caller through) could land between the two
                # reads, and `.wait()` was then called on None. The
                # AttributeError happens on a TestClient worker thread, so
                # pytest surfaces it as PytestUnhandledThreadExceptionWarning
                # against whatever test happens to be running when it lands --
                # which is how this appeared in CI as a failure in a test with
                # no threads in it, on one Python version, once.
                #
                # Snapshotting also fixes the quieter half: a rebind to None
                # made an already-blocked holder stop blocking, so a test that
                # believed it was holding the single upstream slot silently was
                # not.
                gate = upstream.gate
                upstream.entered.release()
                if gate is not None:
                    # Block the worker thread TestClient runs this on. Portal
                    # threads are real threads, so this genuinely holds a slot.
                    gate.wait(timeout=30)
                return FakeResponse(upstream.status, dict(upstream.headers), list(upstream.chunks))

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

    async def aclose(self):
        return None


class Harness:
    def __init__(self, settings, storage, upstream, app, client):
        self.settings = settings
        self.storage = storage
        self.upstream = upstream
        self.app = app
        self.client = client

    @property
    def state(self):
        return self.app.state.gateway

    def register(self, **body):
        return self.client.post("/auth/register", json=body)

    def new_key(self, email="a@b.com"):
        response = self.register(email=email)
        assert response.status_code == 201, response.text
        return response.json()["api_key"]

    def call(self, key, path="/v1/messages"):
        return self.client.post(
            path,
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "yangble5", "messages": []},
        )

    def attach_byok(self, key):
        return self.client.post(
            "/byok",
            headers={"Authorization": f"Bearer {key}"},
            json={"credential": USER_CREDENTIAL},
        )

    def charge(self, key_id, *, tokens=0, cost=0.0):
        self.storage.record_usage(
            key_id=key_id, endpoint="/v1/messages", model="m", status=200,
            input_tokens=tokens, cached_input_tokens=0, cache_write_tokens=0,
            output_tokens=0, total_tokens=tokens, cost_usd=cost, latency_ms=1,
            streamed=False, billable=True,
        )
        self.state.spend.invalidate()

    def close(self):
        self.client.close()
        self.storage.close()


@pytest.fixture
def build(tmp_path):
    created: list[Harness] = []

    def _build(**overrides) -> Harness:
        env = dict(BASE_ENV)
        env["DB_PATH"] = str(tmp_path / f"gw{len(created)}" / "gw.db")
        env.update({k: str(v) for k, v in overrides.items()})
        settings = Settings.from_env(env)
        storage = Storage(settings.db_path)
        upstream = FakeUpstream()
        app = create_app(settings=settings, storage=storage, upstream=upstream)
        harness = Harness(settings, storage, upstream, app, TestClient(app))
        created.append(harness)
        return harness

    yield _build
    for harness in created:
        harness.close()


# ---------------------------------------------------------------------------
# FINDING 1 — aggregate concurrency
# ---------------------------------------------------------------------------
def test_concurrency_is_capped_across_keys_not_only_within_one(build):
    """THE DEFECT: `ConcurrencyLimiter` buckets on key_id, so the only
    concurrency cap in the request path was per-caller. Two keys at the limit
    each put 2 x RATE_LIMIT_CONCURRENCY requests on ONE upstream credential;
    fifty keys put 200. Nothing summed across keys.
    """
    gw = build(UPSTREAM_MAX_CONCURRENCY=2, RATE_LIMIT_CONCURRENCY=4, RATE_LIMIT_RPM=0)
    keys = [gw.new_key(f"k{i}@b.com") for i in range(3)]
    gw.upstream.gate = threading.Event()

    results: list[int] = []
    lock = threading.Lock()

    def hit(key):
        code = gw.call(key).status_code
        with lock:
            results.append(code)

    threads = [threading.Thread(target=hit, args=(k,)) for k in keys]
    for thread in threads:
        thread.start()
    try:
        # Two requests are admitted and are now parked inside the upstream.
        for _ in range(2):
            assert gw.upstream.entered.acquire(timeout=10), "no request reached the upstream"
        # The third must never be forwarded at all.
        assert not gw.upstream.entered.acquire(timeout=2.0), (
            "a THIRD request reached the single upstream account while two were "
            "already in flight. Each key is at 1 of its own limit of 4, so a "
            "per-key limiter cannot refuse this — only a limiter that sums "
            "across keys can."
        )
    finally:
        gw.upstream.gate.set()
        for thread in threads:
            thread.join(timeout=30)

    assert sorted(results) == [200, 200, 429], (
        f"three keys, each well under its own per-key limit of 4, produced {results}. "
        "Only an AGGREGATE limiter can refuse the third."
    )
    # Exactly two requests were forwarded: the refusal is a spend guard, not a
    # label applied after the fact.
    assert len(gw.upstream.calls) == 2

    # And the aggregate slot is given back.
    gw.upstream.gate = None
    assert gw.call(keys[0]).status_code == 200


def test_the_aggregate_refusal_names_the_shared_pool_not_the_key(build):
    gw = build(UPSTREAM_MAX_CONCURRENCY=1, RATE_LIMIT_CONCURRENCY=4, RATE_LIMIT_RPM=0)
    first, second = gw.new_key("a@b.com"), gw.new_key("b@b.com")
    gw.upstream.gate = threading.Event()

    holder = threading.Thread(target=lambda: gw.call(first))
    holder.start()
    try:
        assert gw.upstream.entered.acquire(timeout=10)
        refused = gw.call(second)
    finally:
        gw.upstream.gate.set()
        holder.join(timeout=30)

    assert refused.status_code == 429, (
        "a second key was served while the single upstream slot was held; the "
        "aggregate limiter is not in the path"
    )
    body = refused.json()["error"]
    assert body["type"] == "upstream_busy"
    # A user whose own key is idle must not be told their key is the problem.
    assert "your key" in body["message"].lower()
    assert refused.headers["Retry-After"] == "2"


def test_byok_traffic_is_not_queued_behind_the_shared_pool(build):
    """`byok_instructions` promises attaching a credential means "no queue
    behind anyone else". The aggregate limiter must therefore not count BYOK
    traffic, or that sentence becomes false the moment the pool is busy."""
    gw = build(UPSTREAM_MAX_CONCURRENCY=1, RATE_LIMIT_CONCURRENCY=4, RATE_LIMIT_RPM=0)
    pool_key = gw.new_key("pool@b.com")
    own_key = gw.new_key("own@b.com")
    assert gw.attach_byok(own_key).status_code == 201

    held = threading.Event()
    gw.upstream.gate = held
    holder = threading.Thread(target=lambda: gw.call(pool_key))
    holder.start()
    assert gw.upstream.entered.acquire(timeout=10)

    # The shared slot is taken. A BYOK caller pays their own upstream and is
    # served anyway. Clearing `gate` only affects requests that arrive from
    # here on; the holder snapshotted `held` on the way in and is still on it.
    gw.upstream.gate = None
    assert gw.call(own_key).status_code == 200

    # Release the holder on the object it is ACTUALLY waiting on. This used to
    # construct a fresh Event and set that instead, which the holder never saw:
    # the thread only ever ended by hitting its own 30-second wait timeout, and
    # `holder.join(timeout=30)` returning proved nothing because nobody checked
    # whether it had returned or merely timed out.
    held.set()
    holder.join(timeout=30)
    assert not holder.is_alive(), (
        "the holder thread did not finish after its gate was released; it is "
        "still occupying an upstream slot and will leak into the next test"
    )


def test_the_connection_pool_cannot_silently_queue_past_the_limiter():
    """THE SECOND HALF OF THE DEFECT: `httpx.Timeout(900, connect=10)` applies
    900 s to the POOL-ACQUIRE wait too, and `max_connections=200` was far above
    anything one account tolerates. A request that lost the race for a
    connection therefore sat for up to fifteen minutes before it was even sent.
    """
    from gateway.upstream import HttpxUpstream

    upstream = HttpxUpstream(
        "http://127.0.0.1:8318", timeout=900.0, connect_timeout=10.0,
        pool_timeout=15.0, max_connections=32,
    )
    try:
        timeout = upstream._client.timeout
        assert timeout.pool == 15.0, (
            f"pool timeout is {timeout.pool}; an unset pool timeout inherits the "
            "900 s read timeout and turns a full pool into a silent 15-minute wait"
        )
        assert timeout.read == 900.0        # long reads are still legitimate
        assert timeout.connect == 10.0
    finally:
        pass


def test_settings_refuse_a_concurrency_limit_the_pool_cannot_honour(tmp_path):
    from gateway.config import ConfigError

    env = dict(BASE_ENV)
    env["DB_PATH"] = str(tmp_path / "x.db")
    env["UPSTREAM_MAX_CONCURRENCY"] = "64"
    env["UPSTREAM_MAX_CONNECTIONS"] = "8"
    with pytest.raises(ConfigError, match="UPSTREAM_MAX_CONCURRENCY"):
        Settings.from_env(env)


# ---------------------------------------------------------------------------
# FINDING 2 — /pool/status must observe the upstream, not just the budget
# ---------------------------------------------------------------------------
def test_pool_status_stops_claiming_capacity_while_the_upstream_refuses(build):
    """THE DEFECT: `remaining_pct` was a pure budget ratio. The refusal path
    returns before `_record()`, so no usage row is written and no spend is
    added — the ratio therefore CANNOT move during an outage. The widget whose
    only job is to answer "is there room for me?" reported 100% through a total
    failure to serve.
    """
    gw = build(GLOBAL_MONTHLY_USD_BUDGET=100.0, DAILY_TOKEN_BUDGET=NO_KEY_LIMIT,
               UPSTREAM_HEALTH_FAILURE_THRESHOLD=2, RATE_LIMIT_RPM=0)
    key = gw.new_key()

    healthy = gw.client.get("/pool/status").json()
    assert healthy["accepting_requests"] is True
    assert healthy["remaining_pct"] == 1.0

    gw.upstream.status = 429
    gw.upstream.chunks = [b'{"error":"quota exceeded for project operator-private-42"}']
    for _ in range(2):
        assert gw.call(key).status_code == 429

    body = gw.client.get("/pool/status").json()
    assert body["accepting_requests"] is False, (
        "the pool served nothing and the widget still says it is accepting requests"
    )
    assert body["remaining_pct"] == 0.0, (
        "budget headroom is not capacity: no spend was recorded, so the raw ratio "
        "is still 1.0 while the service is unusable"
    )

    # /admin/stats carries the operator-side detail the public page must not.
    stats = gw.client.get(
        "/admin/stats", headers={"Authorization": f"Bearer {ADMIN_KEY}"}
    ).json()
    assert stats["upstream"]["ok"] is False
    assert stats["upstream"]["last_failure_status"] == 429
    assert stats["accepting_requests"] is False


def test_one_success_clears_the_outage_signal(build):
    gw = build(GLOBAL_MONTHLY_USD_BUDGET=100.0, DAILY_TOKEN_BUDGET=NO_KEY_LIMIT,
               UPSTREAM_HEALTH_FAILURE_THRESHOLD=1, RATE_LIMIT_RPM=0)
    key = gw.new_key()
    gw.upstream.status = 503
    assert gw.call(key).status_code == 503
    assert gw.client.get("/pool/status").json()["accepting_requests"] is False

    gw.upstream.status = 200
    assert gw.call(key).status_code == 200
    assert gw.client.get("/pool/status").json()["accepting_requests"] is True, (
        "a working request is stronger evidence than an older failed one; the "
        "signal must not latch"
    )


def test_a_byok_failure_does_not_mark_the_shared_pool_down(build):
    """A BYOK caller's own account failing says nothing about the operator's."""
    gw = build(UPSTREAM_HEALTH_FAILURE_THRESHOLD=1, RATE_LIMIT_RPM=0)
    key = gw.new_key()
    assert gw.attach_byok(key).status_code == 201
    gw.upstream.status = 403
    assert gw.call(key).status_code == 403          # passed through untouched
    assert gw.client.get("/pool/status").json()["accepting_requests"] is True


# ---------------------------------------------------------------------------
# FINDING 3 — Retry-After must follow the window that is actually binding
# ---------------------------------------------------------------------------
def test_reserve_retry_after_matches_a_month_window(build):
    """THE DEFECT: `reserve_verdict` hardcoded `_seconds_until_utc_midnight()`
    regardless of `pool.window`. With a month-windowed pool the header said
    "retry in a few hours" while `reset_at` in the same body said next month —
    so a client obeying the header retried nightly for up to four weeks and was
    refused every time.
    """
    gw = build(GLOBAL_MONTHLY_USD_BUDGET=10.0, OPERATOR_RESERVE_FRACTION=0.5,
               DAILY_TOKEN_BUDGET=NO_KEY_LIMIT, RATE_LIMIT_RPM=0)
    whale = gw.storage.issue_key(email="whale@b.com", scheme="pbkdf2")
    gw.charge(whale.key_id, cost=6.0)               # 40% left, reserve is 50%

    response = gw.call(gw.new_key())
    assert response.status_code == 429
    body = response.json()
    assert body["reason"] == "operator_reserve_engaged"
    assert body["reset_at"].startswith(("2", "1"))  # ISO-8601

    retry_after = int(response.headers["Retry-After"])
    # A month window never refills within a day. 25 h is the smallest number
    # that cannot be produced by "seconds until midnight".
    assert retry_after > 25 * 3600, (
        f"Retry-After is {retry_after}s (~{retry_after / 3600:.1f}h) for a MONTH "
        "window; nothing refills a monthly counter before the 1st"
    )
    # And the header agrees with the body it is attached to.
    assert "monthly" in body["message"]
    assert body["reset_at"] in body["message"]


def test_reserve_retry_after_matches_a_day_window(build):
    """The same code path with a DAILY binding cap must still say 'today'."""
    gw = build(GLOBAL_MONTHLY_USD_BUDGET=100.0, GLOBAL_DAILY_USD_BUDGET=10.0,
               OPERATOR_RESERVE_FRACTION=0.5, DAILY_TOKEN_BUDGET=NO_KEY_LIMIT,
               RATE_LIMIT_RPM=0)
    whale = gw.storage.issue_key(email="whale@b.com", scheme="pbkdf2")
    gw.charge(whale.key_id, cost=6.0)

    response = gw.call(gw.new_key())
    assert response.status_code == 429
    assert response.json()["reason"] == "operator_reserve_engaged"
    assert int(response.headers["Retry-After"]) <= 24 * 3600
    assert "daily" in response.json()["message"]


# ---------------------------------------------------------------------------
# FINDING 5 — the per-key USD ceiling must not silently undercut the token one
# ---------------------------------------------------------------------------
def test_the_default_per_key_ceiling_is_denominated_in_tokens(tmp_path):
    """THE DEFECT: DAILY_COST_USD_BUDGET defaulted to $2.00 while the
    placeholder table charges input at $5.00/1M, so the dollar ceiling was
    reached after 400,000 tokens — a fifth of the 2,000,000-token allowance the
    registration response advertised, and less than ONE request of the
    748,918-token size this project exists to serve.
    """
    env = dict(BASE_ENV)
    env["DB_PATH"] = str(tmp_path / "x.db")
    settings = Settings.from_env(env)
    assert settings.daily_token_budget == 2_000_000
    assert settings.daily_cost_usd_budget == 0.0, (
        "a dollar ceiling built out of prices the code itself calls placeholders "
        "must not be the default rationing unit"
    )


def test_startup_names_the_ceiling_that_actually_binds(tmp_path):
    env = dict(BASE_ENV)
    env["DB_PATH"] = str(tmp_path / "x.db")
    env["DAILY_COST_USD_BUDGET"] = "2.0"
    env["DAILY_TOKEN_BUDGET"] = "2000000"
    warnings = "\n".join(Settings.from_env(env).startup_warnings())
    assert "400,000 tokens" in warnings, (
        "the operator must be told, in tokens, where their dollar ceiling lands"
    )
    assert "PLACEHOLDER" in warnings


def test_registration_names_the_binding_ceiling(build):
    gw = build(DAILY_COST_USD_BUDGET=2.0, DAILY_TOKEN_BUDGET=2_000_000)
    body = gw.register(email="new@b.com").json()
    allowance = body["daily_allowance"]
    assert allowance["binds"] == "usd", (
        "with both ceilings set, an agent reading this response must not be free "
        "to quote the looser one"
    )
    assert {limit["unit"] for limit in allowance["limits"]} == {"tokens", "usd"}


def test_registration_reports_one_ceiling_when_only_one_is_configured(build):
    gw = build(DAILY_COST_USD_BUDGET=0, DAILY_TOKEN_BUDGET=2_000_000)
    allowance = gw.register(email="new@b.com").json()["daily_allowance"]
    assert allowance["binds"] == "tokens"
    assert allowance["limits"] == [{"unit": "tokens", "value": 2_000_000}]


# ---------------------------------------------------------------------------
# FINDING 6 — /auth/register and /pool/status must not disagree
# ---------------------------------------------------------------------------
def _exhaust_daily_pool(gw):
    whale = gw.storage.issue_key(email="whale@b.com", scheme="pbkdf2")
    gw.charge(whale.key_id, cost=10.0)


def test_a_key_issued_into_a_dry_pool_says_so(build):
    """THE DEFECT: `register()` gated on the MONTHLY cap alone while
    `/pool/status` computed `registration_open` from the monthly cap AND the
    daily pool. During a daily-exhausted window the widget said registration was
    closed and the endpoint returned 201 with a key that could not be used.
    """
    gw = build(GLOBAL_MONTHLY_USD_BUDGET=100.0, GLOBAL_DAILY_USD_BUDGET=10.0,
               DAILY_TOKEN_BUDGET=NO_KEY_LIMIT, RATE_LIMIT_RPM=0)
    _exhaust_daily_pool(gw)

    status = gw.client.get("/pool/status").json()
    issued = gw.register(email="new@b.com")

    # ONE expression: whatever the widget claims about registration is what the
    # endpoint does.
    assert status["registration_open"] is (issued.status_code == 201)
    assert issued.status_code == 201, (
        "a dry pool is exactly when BYOK matters most, and attaching a credential "
        "needs a key to attach it to"
    )

    body = issued.json()
    assert body.get("usable_now") is False, (
        "the key works as designed and every request it makes is refused; an "
        "installer that stores it and reports success is lying for us"
    )
    assert body["not_usable_reason"] == "pool_exhausted"
    assert body["retry_after_seconds"] > 0
    assert body["byok_instructions"]["available"] is True

    # And the claim is true: the key really is refused.
    assert gw.call(body["api_key"]).status_code == 429


def test_a_key_issued_into_a_healthy_pool_says_it_is_usable(build):
    gw = build(GLOBAL_MONTHLY_USD_BUDGET=100.0, DAILY_TOKEN_BUDGET=NO_KEY_LIMIT)
    body = gw.register(email="new@b.com").json()
    assert body.get("usable_now") is True, (
        "the pool is healthy; the registration response must say the key works"
    )
    assert "not_usable_reason" not in body
    assert gw.call(body["api_key"]).status_code == 200


def test_registration_reports_the_operator_reserve_too(build):
    gw = build(GLOBAL_MONTHLY_USD_BUDGET=10.0, OPERATOR_RESERVE_FRACTION=0.5,
               DAILY_TOKEN_BUDGET=NO_KEY_LIMIT, RATE_LIMIT_RPM=0)
    whale = gw.storage.issue_key(email="whale@b.com", scheme="pbkdf2")
    gw.charge(whale.key_id, cost=6.0)

    body = gw.register(email="new@b.com").json()
    assert body.get("usable_now") is False, (
        "a key issued into an engaged operator reserve is refused on every request; "
        "the response must say so"
    )
    assert body["not_usable_reason"] == "operator_reserve_engaged"


def test_registration_reports_an_upstream_outage(build):
    gw = build(GLOBAL_MONTHLY_USD_BUDGET=100.0, DAILY_TOKEN_BUDGET=NO_KEY_LIMIT,
               UPSTREAM_HEALTH_FAILURE_THRESHOLD=1, RATE_LIMIT_RPM=0)
    key = gw.new_key()
    gw.upstream.status = 500
    assert gw.call(key).status_code == 503

    body = gw.register(email="new@b.com").json()
    assert body.get("usable_now") is False, (
        "the shared upstream is refusing; a key handed out now cannot be served"
    )
    assert body["not_usable_reason"] == "upstream_unavailable"


# ---------------------------------------------------------------------------
# FINDING 7 — upstream failure bodies must not reach the public
# ---------------------------------------------------------------------------
# The address is at a reserved domain on purpose. CI fails the build on any
# committed address outside RFC 2606, because the leak this repository most
# needs to prevent is the operator's own -- and a fixture that models a real
# gmail address to test a leak guard would be caught by the leak guard. What
# this fixture actually exercises is a substring never appearing in a response,
# so the domain is irrelevant to the mechanism and relevant to the check.
LEAKY_BODY = (
    b'{"error":{"message":"account operator-private-42@example.com requires '
    b'verification","upstream":"http://127.0.0.1:8318/internal"}}'
)


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 502, 503, 302])
def test_no_upstream_failure_body_reaches_a_shared_pool_caller(build, status):
    """THE DEFECT: only 402 and 429 were intercepted. Everything else fell
    through to the streaming passthrough, which filters hop-by-hop headers and
    forwards the provider's body unchanged. A single personal OAuth credential
    fails in far more ways than two: 403 when the account needs
    re-verification, 5xx when the engine cannot mint a token.
    """
    gw = build(UPSTREAM_HEALTH_FAILURE_THRESHOLD=99, RATE_LIMIT_RPM=0)
    key = gw.new_key()
    gw.upstream.status = status
    gw.upstream.chunks = [LEAKY_BODY]

    response = gw.call(key)
    assert "operator-private-42@example.com" not in response.text
    assert "127.0.0.1" not in response.text
    assert "8318" not in response.text

    body = response.json()
    if status in (402, 429):
        assert response.status_code == 429
        assert body["reason"] == "upstream_quota_exhausted"
    else:
        assert response.status_code == 503
        assert body["reason"] == "upstream_unavailable"
    # The way out ships with the refusal, every time.
    assert "byok_instructions" in body


@pytest.mark.parametrize("status", [400, 413, 415, 422])
def test_a_verdict_on_the_callers_own_request_is_still_forwarded(build, status):
    """The other half of the property. Withholding "your JSON is malformed"
    would leave a user unable to fix a request only they can fix."""
    gw = build(RATE_LIMIT_RPM=0)
    key = gw.new_key()
    gw.upstream.status = status
    gw.upstream.chunks = [json.dumps({"error": {"message": "messages: required"}}).encode()]

    response = gw.call(key)
    assert response.status_code == status
    assert "messages: required" in response.text


def test_byok_callers_still_read_their_own_accounts_errors(build):
    gw = build(RATE_LIMIT_RPM=0)
    key = gw.new_key()
    assert gw.attach_byok(key).status_code == 201
    gw.upstream.status = 403
    gw.upstream.chunks = [b'{"error":"your account needs verification"}']

    response = gw.call(key)
    assert response.status_code == 403
    assert "your account needs verification" in response.text


def test_the_real_upstream_status_is_logged_for_the_operator(build, capsys):
    # Read stdout rather than caplog: `configure_logging` sets propagate=False
    # and installs its own JSON handler, which is the thing under test.
    gw = build(RATE_LIMIT_RPM=0)
    key = gw.new_key()
    gw.upstream.status = 403
    gw.upstream.chunks = [LEAKY_BODY]

    assert gw.call(key).status_code == 503

    printed = capsys.readouterr().out
    events = [
        json.loads(line)
        for line in printed.splitlines()
        if line.startswith("{") and '"upstream.failed"' in line
    ]
    assert events, "the sanitised answer must not also erase the operator's diagnosis"
    assert events[-1]["status"] == 403, (
        "the client is told 503; the operator log must carry the REAL status or "
        "the outage is undiagnosable"
    )
    # The log carries metadata only — never the provider's body.
    assert "operator-private-42" not in printed


def test_an_intercepted_failure_charges_nobody(build):
    gw = build(RATE_LIMIT_RPM=0)
    key = gw.new_key()
    key_id = parse_key(key)[0]
    gw.upstream.status = 500
    gw.upstream.chunks = [LEAKY_BODY]

    assert gw.call(key).status_code == 503
    assert gw.storage.usage_for_day(key_id).total_tokens == 0
    assert gw.state.spend.current()[0] == 0.0


def test_a_byok_success_does_not_clear_the_shared_pools_outage(build):
    """A BYOK 200 is evidence about the CALLER'S account, not the operator's.

    `record_failure` is gated on `billable`; `record_success` was not. So while
    the shared pool was returning 500 to everyone, a single active BYOK user's
    success wiped the failure window and `/pool/status` went back to
    `accepting_requests: true` with `remaining_pct: 1.0` -- the precise
    outage-masking `UpstreamHealth` exists to prevent, and a lie told in the
    operator's favour on a page whose whole purpose is telling visitors whether
    there is room for them.
    """
    gw = build(RATE_LIMIT_RPM=0)
    pool_key = gw.new_key("pool@b.com")
    own_key = gw.new_key("own@b.com")
    assert gw.attach_byok(own_key).status_code == 201

    # Shared pool is down. Enough failures to open the window.
    gw.upstream.status = 500
    for _ in range(6):
        gw.call(pool_key)
    assert gw.client.get("/pool/status").json()["accepting_requests"] is False

    # A BYOK caller succeeds against their OWN credential.
    gw.upstream.status = 200
    assert gw.call(own_key).status_code == 200

    status = gw.client.get("/pool/status").json()
    assert status["accepting_requests"] is False, (
        "a BYOK success cleared the shared pool's outage signal: the pool is "
        "still returning 500 to everyone, but /pool/status now says it is "
        "accepting requests"
    )


def test_a_captured_machine_id_cannot_rotate_a_key_forever(build):
    """Re-registration is bounded per machine, not merely per IP.

    A machine id is a possession factor, and re-registering with one returns a
    working key with a FRESH secret -- which invalidates whatever the previous
    holder had. Reissue deliberately does not consume the per-IP registration
    allowance, but it consumed nothing at all: the per-IP counter is read on the
    way in and never incremented on this path. So a replay of one captured
    machine id could rotate the victim's key without limit, from any address,
    while the counter that was supposed to bound it stayed at zero.
    """
    gw = build(REGISTER_MAX_PER_IP_PER_DAY=0, RATE_LIMIT_RPM=0)
    mid = "b" * 64

    first = gw.client.post("/auth/register", json={"machine_id": mid})
    assert first.status_code == 201
    key_id = first.json()["key_id"]

    # The attacker replays the id. Each success rotates the victim's secret.
    codes = [
        gw.client.post("/auth/register", json={"machine_id": mid}).status_code
        for _ in range(8)
    ]

    assert 429 in codes, (
        "a captured machine id re-registered 8 times unchecked; each one "
        "invalidates the previous holder's key"
    )
    refused = gw.client.post("/auth/register", json={"machine_id": mid})
    assert refused.status_code == 429
    body = refused.json()["error"]["message"]
    assert "someone else has a copy" in body, (
        "the refusal does not tell the victim what it actually means"
    )

    # It bounded the MACHINE, not the address: a different machine from the
    # same client still registers.
    other = gw.client.post("/auth/register", json={"machine_id": "c" * 64})
    assert other.status_code == 201
    assert other.json()["key_id"] != key_id


def test_the_gate_is_read_once_so_a_rebind_cannot_crash_the_worker():
    """The interleaving that turned one CI cell red, made deterministic.

    ``FakeUpstream.stream`` used to read ``upstream.gate`` twice — once for the
    ``is not None`` test and once to call ``.wait()``. A test rebinding the
    attribute between those two reads made the second one return ``None``, and
    ``None.wait()`` raised on a TestClient worker thread. pytest reports a
    worker-thread exception as ``PytestUnhandledThreadExceptionWarning``
    against whichever test is running when it surfaces, so it appeared in CI as
    a failure in a test that starts no threads at all, on one Python version,
    once.

    Racing it on purpose would give a flaky test, which is no better than the
    flake it replaces. Instead the interleaving is forced: ``gate`` is a
    property that yields the event the first time it is read and ``None`` every
    time after. Code that reads it once works. Code that reads it twice raises
    exactly the AttributeError that was seen in CI.
    """
    import asyncio

    class RebindingUpstream(FakeUpstream):
        def __init__(self):
            super().__init__()
            self._event = threading.Event()
            self._event.set()          # never actually blocks; we only care about the reads
            self._reads = 0

        @property
        def gate(self):
            self._reads += 1
            return self._event if self._reads == 1 else None

        @gate.setter
        def gate(self, value):         # __init__ assigns to it
            self._event = value

    upstream = RebindingUpstream()

    async def drive():
        async with upstream.stream("POST", "/v1/messages", headers={}) as response:
            return response

    response = asyncio.run(drive())

    assert response.status_code == 200
    assert upstream._reads == 1, (
        f"FakeUpstream.stream read `gate` {upstream._reads} times. Every read "
        "after the first is a chance for a concurrent rebind to hand it None, "
        "which is the AttributeError this test exists for. Snapshot it once."
    )
