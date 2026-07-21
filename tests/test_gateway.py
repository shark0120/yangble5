"""Offline tests for the public gateway.

Everything here runs without a network, an engine, or a real key: the upstream
is a fake that records what it was handed, and the database is a temp file.

The tests are grouped by the property they defend, because that is what a
reviewer needs to check: credential handling, quota, the global cap, rate
limits, invites, and information leakage.
"""

from __future__ import annotations

import json
import pathlib
import re
import sqlite3
import threading
import time
from datetime import datetime

import pytest
import yaml
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest

from gateway.app import client_ip, create_app, extract_model
from gateway.config import ConfigError, ModelPrice, Settings
from gateway.ratelimit import TimedThrottle
from gateway.storage import Storage, hash_secret, normalize_machine_id, parse_key, verify_secret

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

ENGINE_KEY = "sk-engine-test-only-not-a-real-key"
ADMIN_KEY = "admin-test-only-not-a-real-key"
# What a user would attach with POST /byok. Obviously fake, like every other
# credential in this file.
USER_CREDENTIAL = "sk-user-own-upstream-test-only-not-a-real-key"
# A well-formed machine fingerprint: 64 lowercase hex characters, the shape the
# installer produces with sha256.
MACHINE_A = "a1b2c3d4" * 8
MACHINE_B = "f0e1d2c3" * 8


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status: int, headers: dict[str, str], chunks: list[bytes], delay: float):
        self.status_code = status
        self.headers = headers
        self._chunks = chunks
        self._delay = delay

    async def aiter_raw(self):
        for chunk in self._chunks:
            if self._delay:
                import asyncio

                await asyncio.sleep(self._delay)
            yield chunk


class FakeUpstream:
    """Stands in for the engine. Records every call so tests can assert on
    exactly which headers crossed the boundary."""

    def __init__(self):
        self.calls: list[dict] = []
        self.status = 200
        self.headers = {"content-type": "application/json"}
        self.chunks: list[bytes] = [b'{"ok":true}']
        self.delay = 0.0
        self.error: Exception | None = None
        self.on_call = None

    def set_json_usage(self, **usage):
        self.headers = {"content-type": "application/json"}
        self.chunks = [json.dumps({"usage": usage}).encode()]

    def set_sse(self, events: list[dict]):
        self.headers = {"content-type": "text/event-stream"}
        self.chunks = [f"data: {json.dumps(e)}\n\n".encode() for e in events]

    def stream(self, method, path, *, headers, content=None):
        upstream = self

        class _Ctx:
            async def __aenter__(self):
                upstream.calls.append(
                    {
                        "method": method,
                        "path": path,
                        "headers": dict(headers),
                        "content": content,
                    }
                )
                if upstream.on_call:
                    upstream.on_call()
                if upstream.error:
                    raise upstream.error
                return FakeResponse(
                    upstream.status, dict(upstream.headers), list(upstream.chunks), upstream.delay
                )

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

    async def aclose(self):
        return None


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
BASE_ENV = {
    "ENGINE_API_KEY": ENGINE_KEY,
    "ADMIN_API_KEY": ADMIN_KEY,
    # pbkdf2 is used in tests only to keep the suite fast; production defaults
    # to scrypt. Both paths are exercised by test_scrypt_scheme_roundtrip.
    "KEY_HASH_SCHEME": "pbkdf2",
    "REGISTRATION_MODE": "open",
    "GLOBAL_MONTHLY_USD_BUDGET": "100",
    "AUTH_CACHE_TTL_SECONDS": "300",
}


# A per-key ceiling high enough never to bind. Used by the tests that isolate the
# GLOBAL cap: `0` would mean "unlimited", which open registration rightly refuses.
NO_KEY_LIMIT = 10**12


class Harness:
    def __init__(self, settings, storage, upstream, app, client):
        self.settings = settings
        self.storage = storage
        self.upstream = upstream
        self.app = app
        self.client = client

    @property
    def spend(self):
        return self.app.state.gateway.spend

    def register(self, email="a@b.com", **body):
        payload = {"email": email}
        payload.update(body)
        return self.client.post("/auth/register", json=payload)

    def new_key(self, email="a@b.com", **body):
        response = self.register(email, **body)
        assert response.status_code == 201, response.text
        return response.json()["api_key"]

    def call(self, key, path="/v1/messages", **kwargs):
        return self.client.post(
            path, headers={"Authorization": f"Bearer {key}"},
            json=kwargs.pop("json", {"model": "yangble5", "messages": []}), **kwargs,
        )

    # -- helpers for the auto-registration / reserve / BYOK surface -------------
    def register_machine(self, machine_id, **body):
        """Registration the way the installer does it: a fingerprint, no email."""
        payload = {"machine_id": machine_id}
        payload.update(body)
        return self.client.post("/auth/register", json=payload)

    def admin(self, method, path, **kwargs):
        return self.client.request(
            method, path, headers={"Authorization": f"Bearer {ADMIN_KEY}"}, **kwargs
        )

    def attach_byok(self, key, credential=USER_CREDENTIAL, **body):
        payload = {"credential": credential}
        payload.update(body)
        return self.client.post(
            "/byok", headers={"Authorization": f"Bearer {key}"}, json=payload
        )

    def charge(self, key_id, *, tokens=0, cost=0.0, billable=True):
        """Write a usage row directly, standing in for traffic another process
        already served. Used to move the pool without a hundred fake requests."""
        self.storage.record_usage(
            key_id=key_id, endpoint="/v1/messages", model="m", status=200,
            input_tokens=tokens, cached_input_tokens=0, cache_write_tokens=0,
            output_tokens=0, total_tokens=tokens, cost_usd=cost, latency_ms=1,
            streamed=False, billable=billable,
        )
        # The row was written behind the tracker's back, exactly as another
        # worker process would have. Force the re-read the TTL would do anyway.
        self.spend.invalidate()

    def db_dump(self):
        """Every value of every column of every table, as one string."""
        conn = sqlite3.connect(self.settings.db_path)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        values = []
        for table in tables:
            for row in conn.execute(f"SELECT * FROM {table}"):  # noqa: S608 - names from schema
                values.extend(str(value) for value in row)
        conn.close()
        return "\n".join(values)

    def close(self):
        self.client.close()
        self.storage.close()


@pytest.fixture
def build(tmp_path):
    """Factory for gateway harnesses, with guaranteed teardown.

    Storage is closed explicitly: the suite runs with `filterwarnings = error`,
    so a leaked sqlite connection is a test failure rather than a slow leak
    nobody notices.
    """
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


@pytest.fixture
def gw(build):
    """Default harness: open registration, generous budgets, fake upstream."""
    return build()


# ---------------------------------------------------------------------------
# 1. key issuance, hashing, and the once-only guarantee
# ---------------------------------------------------------------------------
def test_issued_key_verifies_and_authenticates(gw):
    key = gw.new_key()
    assert key.startswith("yb5_")
    parsed = parse_key(key)
    assert parsed is not None
    key_id, secret = parsed

    record = gw.storage.get_key(key_id)
    assert record is not None
    assert verify_secret(secret, record.digest, record.salt, record.scheme, gw.settings.key_pepper)
    assert not verify_secret(
        secret + "x", record.digest, record.salt, record.scheme, gw.settings.key_pepper
    )
    assert gw.call(key).status_code == 200


def test_plaintext_key_is_never_stored_anywhere(gw):
    key = gw.new_key()
    _, secret = parse_key(key)

    # Dump every value of every column of every table and assert the secret is
    # absent. This is broader than checking api_keys on purpose: a future column
    # that accidentally captures the key would fail here.
    conn = sqlite3.connect(gw.settings.db_path)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    haystack = []
    for table in tables:
        for row in conn.execute(f"SELECT * FROM {table}"):  # noqa: S608 - table names from schema
            haystack.extend(str(value) for value in row)
    conn.close()
    blob = "\n".join(haystack)
    assert secret not in blob
    assert key not in blob
    # The key_id is expected in the clear — it is the public handle.
    assert parse_key(key)[0] in blob


def test_key_is_returned_exactly_once(gw):
    response = gw.register()
    key = response.json()["api_key"]

    # No endpoint re-reveals it.
    usage = gw.client.get("/usage", headers={"Authorization": f"Bearer {key}"})
    assert usage.status_code == 200
    assert key not in usage.text

    listing = gw.client.get("/admin/keys", headers={"Authorization": f"Bearer {ADMIN_KEY}"})
    assert listing.status_code == 200
    assert key not in listing.text

    # Registering again with the same email does not re-issue or reveal.
    again = gw.register()
    assert again.status_code == 409
    assert key not in again.text


def test_each_key_gets_a_distinct_salt(gw):
    first = parse_key(gw.new_key("one@b.com"))[0]
    second = parse_key(gw.new_key("two@b.com"))[0]
    a, b = gw.storage.get_key(first), gw.storage.get_key(second)
    assert a.salt != b.salt
    assert a.digest != b.digest


def test_scrypt_scheme_roundtrip():
    """Production default. Kept out of the HTTP suite because it is slow."""
    digest, salt, scheme = hash_secret("s3cret", scheme="scrypt", pepper="pep")
    assert scheme.startswith("scrypt$")
    assert verify_secret("s3cret", digest, salt, scheme, "pep")
    assert not verify_secret("s3cret", digest, salt, scheme, "wrong-pepper")


# ---------------------------------------------------------------------------
# 2. the engine credential must never cross either boundary
# ---------------------------------------------------------------------------
def test_engine_key_is_injected_and_client_credentials_are_dropped(gw):
    key = gw.new_key()
    gw.client.post(
        "/v1/messages",
        headers={
            "Authorization": f"Bearer {key}",
            "x-api-key": "attacker-supplied-value",
            "x-forwarded-for": "9.9.9.9",
        },
        json={"model": "yangble5", "messages": []},
    )
    sent = gw.upstream.calls[-1]["headers"]
    assert sent["Authorization"] == f"Bearer {ENGINE_KEY}"
    assert sent["x-api-key"] == ENGINE_KEY
    # The caller's own key never reaches the engine...
    assert key not in json.dumps(sent)
    # ...and neither does their forged forwarding chain.
    assert "x-forwarded-for" not in {k.lower() for k in sent}


def test_engine_key_never_appears_in_a_response(gw):
    key = gw.new_key()
    for response in (
        gw.call(key),
        gw.client.get("/usage", headers={"Authorization": f"Bearer {key}"}),
        gw.client.get("/health"),
        gw.client.get("/v1/models", headers={"Authorization": f"Bearer {key}"}),
    ):
        assert ENGINE_KEY not in response.text
        assert ADMIN_KEY not in response.text


def test_upstream_failure_does_not_leak_internal_detail(gw):
    from gateway.upstream import UpstreamError

    key = gw.new_key()
    gw.upstream.error = UpstreamError("ConnectError")
    response = gw.call(key)
    assert response.status_code == 502
    assert "127.0.0.1" not in response.text
    assert "8318" not in response.text
    assert response.json()["error"]["message"] == "The backend engine is unavailable."


# ---------------------------------------------------------------------------
# 3. quota enforcement at the boundary
# ---------------------------------------------------------------------------
def test_daily_token_budget_blocks_exactly_at_the_limit(build):
    gw = build(DAILY_TOKEN_BUDGET=1000, DAILY_COST_USD_BUDGET=0)
    key = gw.new_key()
    key_id = parse_key(key)[0]

    # 999 used: still under, allowed.
    gw.storage.record_usage(
        key_id=key_id, endpoint="/v1/messages", model="m", status=200,
        input_tokens=999, cached_input_tokens=0, cache_write_tokens=0, output_tokens=0,
        total_tokens=999, cost_usd=0.0, latency_ms=1, streamed=False,
    )
    assert gw.call(key).status_code == 200

    # One more token puts it at 1000 == the budget. The boundary is inclusive:
    # reaching your allowance means you have spent it.
    gw.storage.record_usage(
        key_id=key_id, endpoint="/v1/messages", model="m", status=200,
        input_tokens=1, cached_input_tokens=0, cache_write_tokens=0, output_tokens=0,
        total_tokens=1, cost_usd=0.0, latency_ms=1, streamed=False,
    )
    blocked = gw.call(key)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["type"] == "daily_quota_exhausted"
    assert int(blocked.headers["Retry-After"]) > 0


def test_daily_cost_budget_blocks(build):
    gw = build(DAILY_TOKEN_BUDGET=0, DAILY_COST_USD_BUDGET=1.0)
    key = gw.new_key()
    gw.storage.record_usage(
        key_id=parse_key(key)[0], endpoint="/v1/messages", model="m", status=200,
        input_tokens=0, cached_input_tokens=0, cache_write_tokens=0, output_tokens=0,
        total_tokens=0, cost_usd=1.0, latency_ms=1, streamed=False,
    )
    assert gw.call(key).status_code == 429


def test_usage_is_recorded_and_charged_after_a_request(build):
    gw = build(
        PRICE_TABLE_JSON=json.dumps(
            {"default": {"input": 10.0, "cached_input": 1.0, "output": 30.0}}
        ),
    )
    key = gw.new_key()
    gw.upstream.set_json_usage(input_tokens=1_000_000, output_tokens=0)
    assert gw.call(key).status_code == 200

    day = gw.storage.usage_for_day(parse_key(key)[0])
    assert day.requests == 1
    assert day.total_tokens == 1_000_000
    assert day.cost_usd == pytest.approx(10.0)


def test_cached_tokens_are_priced_separately(build):
    """The whole point of the project: a cached prompt must not be billed as fresh."""
    gw = build(
        PRICE_TABLE_JSON=json.dumps(
            {"default": {"input": 10.0, "cached_input": 1.0, "output": 30.0}}
        ),
    )
    key = gw.new_key()
    # CLIProxyAPI reports input_tokens INCLUDING the cached reads, so this is
    # 1,000,000 prompt tokens of which 900,000 came from the cache.
    gw.upstream.set_json_usage(
        input_tokens=1_000_000, cache_read_input_tokens=900_000, output_tokens=0
    )
    assert gw.call(key).status_code == 200

    day = gw.storage.usage_for_day(parse_key(key)[0])
    assert day.total_tokens == 1_000_000
    # 100k fresh @ $10/M + 900k cached @ $1/M = $1.00 + $0.90
    assert day.cost_usd == pytest.approx(1.90)
    # Billing all of it as fresh would have been $10.00.
    assert day.cost_usd < 10.0


def test_get_models_is_not_charged(gw):
    key = gw.new_key()
    gw.upstream.set_json_usage(input_tokens=500, output_tokens=500)
    listed = gw.client.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert listed.status_code == 200
    assert gw.storage.usage_for_day(parse_key(key)[0]).requests == 0


# ---------------------------------------------------------------------------
# 4. the global operator cap
# ---------------------------------------------------------------------------
def test_global_cap_trips_and_degrades_to_read_only(build):
    gw = build(GLOBAL_MONTHLY_USD_BUDGET=5.0, DAILY_COST_USD_BUDGET=0,
               DAILY_TOKEN_BUDGET=NO_KEY_LIMIT)
    key = gw.new_key()
    assert gw.call(key).status_code == 200

    # Someone else burns the operator's whole month.
    other = gw.storage.issue_key(email="whale@b.com", scheme="pbkdf2")
    gw.storage.record_usage(
        key_id=other.key_id, endpoint="/v1/messages", model="m", status=200,
        input_tokens=0, cached_input_tokens=0, cache_write_tokens=0, output_tokens=0,
        total_tokens=0, cost_usd=5.0, latency_ms=1, streamed=False,
    )
    # That charge was written by another "process", so drop the cached total to
    # force a re-read — the same thing the TTL does in a real deployment.
    gw.spend.invalidate()

    blocked = gw.call(key)
    assert blocked.status_code == 402
    assert blocked.json()["error"]["type"] == "operator_budget_exhausted"

    # ...and nothing was forwarded upstream.
    calls_before = len(gw.upstream.calls)
    gw.call(key)
    assert len(gw.upstream.calls) == calls_before

    # Read-only, not dead: health and usage still answer, models still lists.
    assert gw.client.get("/health").json()["accepting_requests"] is False
    assert gw.client.get("/health").json()["status"] == "degraded"
    assert gw.client.get("/usage", headers={"Authorization": f"Bearer {key}"}).status_code == 200
    assert gw.client.get(
        "/v1/models", headers={"Authorization": f"Bearer {key}"}
    ).status_code == 200
    # And no new keys are issued while capped.
    assert gw.register(email="new@b.com").status_code == 503


def test_global_token_cap_trips(build):
    gw = build(GLOBAL_MONTHLY_USD_BUDGET=0, GLOBAL_MONTHLY_TOKEN_BUDGET=1000,
               DAILY_TOKEN_BUDGET=NO_KEY_LIMIT, DAILY_COST_USD_BUDGET=0)
    key = gw.new_key()
    gw.upstream.set_json_usage(input_tokens=1000, output_tokens=0)
    assert gw.call(key).status_code == 200
    # The tracker counted that charge in-process; no refresh needed.
    assert gw.call(key).status_code == 402


def test_spend_tracker_counts_charges_immediately(build):
    """The cap must not depend on the cache TTL expiring to notice spending."""
    gw = build(GLOBAL_MONTHLY_USD_BUDGET=1.0, DAILY_TOKEN_BUDGET=NO_KEY_LIMIT,
               DAILY_COST_USD_BUDGET=0,
               PRICE_TABLE_JSON=json.dumps(
                   {"default": {"input": 1000.0, "cached_input": 1.0, "output": 1.0}}))
    key = gw.new_key()
    gw.upstream.set_json_usage(input_tokens=1_000_000, output_tokens=0)  # $1000 in one shot
    assert gw.call(key).status_code == 200
    assert gw.call(key).status_code == 402


# ---------------------------------------------------------------------------
# 5. rate limiting and concurrency
# ---------------------------------------------------------------------------
def test_per_key_rpm_limit(build):
    gw = build(RATE_LIMIT_RPM=3)
    key = gw.new_key()
    codes = [gw.call(key).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200]
    assert codes[3] == 429
    assert codes[4] == 429


def test_rate_limit_is_per_key_not_global(build):
    gw = build(RATE_LIMIT_RPM=2, ALLOW_MULTIPLE_KEYS_PER_EMAIL=True)
    first = gw.new_key("one@b.com")
    second = gw.new_key("two@b.com")
    assert [gw.call(first).status_code for _ in range(3)] == [200, 200, 429]
    # The second key still has its own allowance.
    assert gw.call(second).status_code == 200


def test_concurrency_limit_rejects_the_extra_request(build):
    gw = build(RATE_LIMIT_CONCURRENCY=1, RATE_LIMIT_RPM=100)
    key = gw.new_key()
    gw.upstream.delay = 0.15
    results: list[int] = []

    def hit():
        results.append(gw.call(key).status_code)

    threads = [threading.Thread(target=hit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [200, 429]
    # The slot is returned, so a later request succeeds.
    gw.upstream.delay = 0.0
    assert gw.call(key).status_code == 200


def test_auth_endpoint_is_rate_limited_per_ip(build):
    gw = build(AUTH_RPM_PER_IP=3, REGISTER_MAX_PER_IP_PER_DAY=0,
               ALLOW_MULTIPLE_KEYS_PER_EMAIL=True)
    codes = [gw.register(email=f"u{i}@b.com").status_code for i in range(5)]
    assert 429 in codes
    assert codes.count(201) == 3


def test_registration_is_capped_per_ip_per_day(build):
    gw = build(REGISTER_MAX_PER_IP_PER_DAY=2, AUTH_RPM_PER_IP=0,
               ALLOW_MULTIPLE_KEYS_PER_EMAIL=True)
    codes = [gw.register(email=f"u{i}@b.com").status_code for i in range(4)]
    assert codes[:2] == [201, 201]
    assert codes[2] == 429


def test_repeated_auth_failures_lock_the_ip_out(build):
    gw = build(AUTH_FAIL_LOCKOUT_THRESHOLD=3, AUTH_FAIL_LOCKOUT_SECONDS=60)
    bad = "yb5_" + "0" * 16 + "_wrongsecret"
    codes = [gw.call(bad).status_code for _ in range(5)]
    assert codes[-1] == 429
    assert "auth" in gw.call(bad).json()["error"]["type"]

    # A valid key from the same IP is caught by the lockout too — that is the
    # intended trade-off, and it is why the threshold is configurable.
    assert gw.client.post(
        "/auth/register", json={"email": "x@b.com"}
    ).status_code in (201, 429)


# ---------------------------------------------------------------------------
# 6. registration modes and invites
# ---------------------------------------------------------------------------
def test_invite_mode_requires_a_valid_code(build):
    gw = build(REGISTRATION_MODE="invite")
    assert gw.register(email="a@b.com").status_code == 400          # no code
    assert gw.register(email="a@b.com", invite_code="guess").status_code == 403

    created = gw.client.post(
        "/admin/invites", headers={"Authorization": f"Bearer {ADMIN_KEY}"}, json={"max_uses": 1}
    )
    assert created.status_code == 201
    code = created.json()["invite_code"]

    ok = gw.register(email="a@b.com", invite_code=code)
    assert ok.status_code == 201
    # Single use: the second attempt fails even though the code is well-formed.
    assert gw.register(email="c@b.com", invite_code=code).status_code == 403


def test_invite_code_is_stored_only_as_a_hash(build):
    gw = build(REGISTRATION_MODE="invite")
    created = gw.client.post(
        "/admin/invites", headers={"Authorization": f"Bearer {ADMIN_KEY}"}, json={}
    )
    code = created.json()["invite_code"]
    conn = sqlite3.connect(gw.settings.db_path)
    rows = [str(r) for r in conn.execute("SELECT * FROM invites")]
    conn.close()
    assert code not in "\n".join(rows)


def test_expired_invite_is_rejected(build):
    from datetime import timedelta

    from gateway.storage import utcnow

    gw = build(REGISTRATION_MODE="invite")
    gw.storage.create_invite("expired-code", expires_at=utcnow() - timedelta(days=1))
    assert gw.register(email="a@b.com", invite_code="expired-code").status_code == 403


def test_closed_mode_refuses_everyone(build):
    gw = build(REGISTRATION_MODE="closed")
    assert gw.register().status_code == 403


def test_open_mode_requires_an_email(build):
    gw = build()
    assert gw.client.post("/auth/register", json={}).status_code == 400
    assert gw.client.post("/auth/register", json={"email": "not-an-email"}).status_code == 400


def test_open_registration_without_a_budget_cap_is_fatal(tmp_path):
    """The single most expensive misconfiguration must not be a warning."""
    env = dict(BASE_ENV)
    env["DB_PATH"] = str(tmp_path / "x.db")
    env["REGISTRATION_MODE"] = "open"
    env["GLOBAL_MONTHLY_USD_BUDGET"] = "0"
    with pytest.raises(ConfigError, match="operator ceiling"):
        Settings.from_env(env)


def test_open_registration_without_a_per_key_cap_is_fatal(tmp_path):
    env = dict(BASE_ENV)
    env["DB_PATH"] = str(tmp_path / "x.db")
    env["GLOBAL_MONTHLY_USD_BUDGET"] = "50"
    env["DAILY_TOKEN_BUDGET"] = "0"
    env["DAILY_COST_USD_BUDGET"] = "0"
    with pytest.raises(ConfigError, match="per-key ceiling"):
        Settings.from_env(env)


def test_missing_engine_key_is_fatal():
    with pytest.raises(ConfigError, match="ENGINE_API_KEY"):
        Settings.from_env({"REGISTRATION_MODE": "closed"})


def test_deploy_env_spelling_is_honoured_not_ignored(tmp_path):
    """deploy/docker-compose.yml ships REGISTRATION_OPEN / GLOBAL_BUDGET_TOKENS.

    Silently ignoring them would leave the operator believing they had set a
    spend guard when they had not.
    """
    settings = Settings.from_env(
        {
            "ENGINE_API_KEY": ENGINE_KEY,
            "DB_PATH": str(tmp_path / "x.db"),
            "YANGBLE5_REGISTRATION_OPEN": "true",
            "YANGBLE5_GLOBAL_BUDGET_TOKENS": "5000",
            "YANGBLE5_USER_DAILY_TOKENS": "100",
        }
    )
    assert settings.registration_mode == "open"
    assert settings.global_monthly_token_budget == 5000
    assert settings.daily_token_budget == 100


# ---------------------------------------------------------------------------
# 7. authentication and authorisation
# ---------------------------------------------------------------------------
def test_unauthenticated_requests_are_refused(gw):
    assert gw.client.post("/v1/messages", json={}).status_code == 401
    assert gw.client.get("/usage").status_code == 401
    assert gw.client.post(
        "/v1/messages", headers={"Authorization": "Bearer nonsense"}, json={}
    ).status_code == 401


def test_suspended_key_is_refused_immediately(gw):
    key = gw.new_key()
    assert gw.call(key).status_code == 200          # populates the auth cache
    gw.storage.set_key_status(parse_key(key)[0], "suspended", "testing")
    response = gw.call(key)
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "key_suspended"


def test_usage_endpoint_shows_only_the_callers_own_data(build):
    gw = build(ALLOW_MULTIPLE_KEYS_PER_EMAIL=True)
    mine = gw.new_key("mine@b.com")
    theirs = gw.new_key("theirs@b.com")
    gw.storage.record_usage(
        key_id=parse_key(theirs)[0], endpoint="/v1/messages", model="m", status=200,
        input_tokens=777, cached_input_tokens=0, cache_write_tokens=0, output_tokens=0,
        total_tokens=777, cost_usd=7.77, latency_ms=1, streamed=False,
    )
    body = gw.client.get("/usage", headers={"Authorization": f"Bearer {mine}"}).json()
    assert body["key_id"] == parse_key(mine)[0]
    assert body["today"]["total_tokens"] == 0
    assert body["this_month"]["cost_usd"] == 0
    assert parse_key(theirs)[0] not in json.dumps(body)


def test_admin_surface_is_hidden_without_the_admin_key(gw):
    for path in ("/admin/keys", "/admin/stats"):
        assert gw.client.get(path).status_code == 404
        assert gw.client.get(
            path, headers={"Authorization": "Bearer wrong"}
        ).status_code == 404
    # A user key is not an admin key.
    user = gw.new_key()
    as_user = gw.client.get("/admin/keys", headers={"Authorization": f"Bearer {user}"})
    assert as_user.status_code == 404


def test_admin_key_with_non_ascii_does_not_crash_the_endpoint(build):
    """A non-ASCII ADMIN_API_KEY must not turn every admin request into a 500.

    `hmac.compare_digest` on `str` requires BOTH arguments to be ASCII-only, so
    comparing an ASCII presented value against a non-ASCII configured key raises
    TypeError — an operator who picked a passphrase with an accent in it would
    have had a permanently 500-ing admin surface. Comparing as bytes fixes it.

    Note the correct key cannot be tested over HTTP at all: header values are
    latin-1 on the wire and the client refuses to encode it, which is a good
    reason to keep admin keys ASCII (as `openssl rand -hex` produces).
    """
    gw = build(ADMIN_API_KEY="admin-key-with-umläut")
    response = gw.client.get("/admin/keys", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 404


def test_admin_routes_are_absent_when_no_admin_key_is_configured(tmp_path):
    env = dict(BASE_ENV)
    env.pop("ADMIN_API_KEY")
    env["DB_PATH"] = str(tmp_path / "gw.db")
    settings = Settings.from_env(env)
    storage = Storage(settings.db_path)
    try:
        client = TestClient(create_app(settings=settings, storage=storage, upstream=FakeUpstream()))
        assert client.get(
            "/admin/keys", headers={"Authorization": "Bearer anything"}
        ).status_code == 404
    finally:
        storage.close()


# ---------------------------------------------------------------------------
# 8. streaming
# ---------------------------------------------------------------------------
def test_sse_passes_through_and_usage_is_scanned(gw):
    key = gw.new_key()
    gw.upstream.set_sse(
        [
            {"type": "message_start",
             "message": {"usage": {"input_tokens": 1000, "cache_read_input_tokens": 900}}},
            {"type": "message_delta", "usage": {"output_tokens": 50}},
        ]
    )
    response = gw.call(key)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers.get("x-accel-buffering") == "no"
    assert "message_start" in response.text

    day = gw.storage.usage_for_day(parse_key(key)[0])
    assert day.total_tokens == 1050        # 1000 prompt + 50 output
    conn = sqlite3.connect(gw.settings.db_path)
    row = conn.execute(
        "SELECT cached_input_tokens, input_tokens, streamed FROM usage_records"
    ).fetchone()
    conn.close()
    assert row == (900, 100, 1)


def test_stream_is_not_buffered(gw):
    """The first chunk must reach the client before the last one is produced."""
    key = gw.new_key()
    gw.upstream.headers = {"content-type": "text/event-stream"}
    gw.upstream.chunks = [b"data: 1\n\n", b"data: 2\n\n", b"data: 3\n\n"]
    gw.upstream.delay = 0.1

    with gw.client.stream(
        "POST", "/v1/messages", headers={"Authorization": f"Bearer {key}"},
        json={"model": "yangble5"},
    ) as response:
        started = time.monotonic()
        first_chunk_at = None
        for _ in response.iter_raw():
            if first_chunk_at is None:
                first_chunk_at = time.monotonic() - started
        total = time.monotonic() - started

    # Three chunks at 0.1s each: a buffered proxy would deliver the first only
    # after ~0.3s. Incremental delivery gets it at ~0.1s.
    assert first_chunk_at is not None
    assert first_chunk_at < total * 0.75


# ---------------------------------------------------------------------------
# 9. /health must leak nothing
# ---------------------------------------------------------------------------
def test_health_needs_no_auth_and_exposes_no_secrets(gw):
    response = gw.client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"

    text = response.text
    for secret in (ENGINE_KEY, ADMIN_KEY, gw.settings.db_path, gw.settings.engine_url):
        assert secret not in text
    for forbidden in ("sk-", "password", "pepper", "8318", "cost", "usd", "budget"):
        assert forbidden not in text.lower()
    assert set(body) == {
        "status", "service", "version", "uptime_seconds", "accepting_requests", "registration",
    }


def test_no_openapi_or_docs_are_exposed(gw):
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert gw.client.get(path).status_code == 404


# ---------------------------------------------------------------------------
# 10. odds and ends
# ---------------------------------------------------------------------------
def test_oversized_body_is_rejected(build):
    gw = build(MAX_REQUEST_BYTES=500)
    key = gw.new_key()
    response = gw.client.post(
        "/v1/messages", headers={"Authorization": f"Bearer {key}"},
        content=b'{"model":"m","x":"' + b"a" * 2000 + b'"}',
    )
    assert response.status_code == 413


def test_abuse_auto_suspend_on_ip_fanout(build):
    gw = build(ABUSE_DISTINCT_IP_THRESHOLD=3, ABUSE_AUTO_SUSPEND=True,
               TRUST_PROXY_HEADERS=True)
    key = gw.new_key()
    key_id = parse_key(key)[0]
    codes = []
    for i in range(4):
        codes.append(
            gw.client.post(
                "/v1/messages",
                headers={"Authorization": f"Bearer {key}", "x-forwarded-for": f"203.0.113.{i}"},
                json={"model": "yangble5"},
            ).status_code
        )
    assert gw.storage.get_key(key_id).status == "suspended"
    assert "auto:" in gw.storage.get_key(key_id).suspended_reason
    assert gw.call(key).status_code == 403


def test_unknown_model_falls_back_to_the_default_price(build):
    gw = build(
        PRICE_TABLE_JSON=json.dumps(
            {"default": {"input": 2.0, "cached_input": 0.2, "output": 6.0}}
        ),
    )
    key = gw.new_key()
    gw.upstream.set_json_usage(input_tokens=1_000_000, output_tokens=0)
    gw.client.post(
        "/v1/messages", headers={"Authorization": f"Bearer {key}"},
        json={"model": "a-model-nobody-priced", "messages": []},
    )
    assert gw.storage.usage_for_day(parse_key(key)[0]).cost_usd == pytest.approx(2.0)


def test_price_table_must_define_a_default(tmp_path):
    env = dict(BASE_ENV)
    env["DB_PATH"] = str(tmp_path / "x.db")
    env["PRICE_TABLE_JSON"] = json.dumps({"gemini": {"input": 1, "cached_input": 1, "output": 1}})
    with pytest.raises(ConfigError, match="default"):
        Settings.from_env(env)


def test_extract_model_handles_both_paths():
    body = json.dumps({"model": "yangble5[1m]", "messages": []}).encode()
    assert extract_model(body, 10_000) == "yangble5[1m]"
    # Over the parse threshold -> regex path.
    assert extract_model(body, 1) == "yangble5[1m]"
    assert extract_model(b"not json at all", 10_000) is None
    assert extract_model(b"", 10_000) is None


def test_price_lookup_matches_alias_prefixes():
    settings = Settings.from_env(
        {
            "ENGINE_API_KEY": ENGINE_KEY,
            "REGISTRATION_MODE": "closed",
            "PRICE_TABLE_JSON": json.dumps(
                {
                    "default": {"input": 1, "cached_input": 1, "output": 1},
                    "yangble5": {"input": 5, "cached_input": 0.5, "output": 15},
                }
            ),
        }
    )
    assert settings.price_for("yangble5[1m]") == ModelPrice(5, 0.5, 15)
    assert settings.price_for("something-else") == ModelPrice(1, 1, 1)
    assert settings.price_for(None) == ModelPrice(1, 1, 1)


# ---------------------------------------------------------------------------
# 11. open auto-registration and machine binding
# ---------------------------------------------------------------------------
def test_open_registration_needs_only_a_machine_id(build):
    """Zero friction is the whole point: a fingerprint, no email, no invite,
    no verification step, a usable key in one round trip."""
    gw = build()
    response = gw.register_machine(MACHINE_A)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["api_key"].startswith("yb5_")
    assert body["machine_bound"] is True
    assert body["reused"] is False
    # It works immediately — no confirmation step stands between the fan and
    # their first request.
    assert gw.call(body["api_key"]).status_code == 200


def test_re_registration_from_the_same_fingerprint_reuses_the_key(build):
    """Re-running the installer must be safe AND must not mint a second
    allowance — that is the cheapest quota-farming trick there is."""
    gw = build(REGISTER_MAX_PER_IP_PER_DAY=0, AUTH_RPM_PER_IP=0)
    first = gw.register_machine(MACHINE_A)
    assert first.status_code == 201
    key_id = first.json()["key_id"]

    # Spend some of the allowance, so we can prove the history is not reset.
    gw.charge(key_id, tokens=1234, cost=0.5)

    second = gw.register_machine(MACHINE_A)
    assert second.status_code == 200, second.text          # 200: nothing created
    assert second.json()["reused"] is True
    assert second.json()["key_id"] == key_id               # the SAME key

    # Exactly one key exists. A second row here would be the farming hole.
    assert len(gw.storage.list_keys()) == 1
    # And the usage history came with it, so re-running the installer buys
    # nobody a fresh daily budget.
    assert gw.storage.usage_for_day(key_id).total_tokens == 1234
    assert gw.storage.usage_for_day(key_id).cost_usd == pytest.approx(0.5)

    # The returned key string works...
    assert gw.call(second.json()["api_key"]).status_code == 200
    # ...and the previous string does not, because the server only ever held a
    # hash of it. The response says so rather than leaving it to be discovered.
    assert "not recoverable" in second.json()["warning"]
    assert gw.call(first.json()["api_key"]).status_code == 401


def test_fingerprint_is_normalized_so_case_does_not_mint_a_second_key(build):
    gw = build(REGISTER_MAX_PER_IP_PER_DAY=0, AUTH_RPM_PER_IP=0)
    first = gw.register_machine(MACHINE_A.upper())
    assert first.status_code == 201
    again = gw.register_machine(MACHINE_A.lower())
    assert again.status_code == 200
    assert again.json()["key_id"] == first.json()["key_id"]
    assert len(gw.storage.list_keys()) == 1


def test_a_different_machine_gets_a_different_key(build):
    gw = build(REGISTER_MAX_PER_IP_PER_DAY=0, AUTH_RPM_PER_IP=0)
    first = gw.register_machine(MACHINE_A)
    second = gw.register_machine(MACHINE_B)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["key_id"] != second.json()["key_id"]


def test_machine_id_validation_rejects_non_hex_and_oversized_input(build):
    """The fingerprint is an opaque hex digest. Anything else is refused rather
    than stored — a fingerprint column that accepts arbitrary text is a free
    user-controlled column in someone else's database."""
    gw = build(REGISTER_MAX_PER_IP_PER_DAY=0, AUTH_RPM_PER_IP=0)

    rejected_by_our_validator = [
        "z" * 64,                 # not hex
        "a1b2c3!" + "0" * 25,     # punctuation smuggled in
        "abc",                    # too short to be a digest
        "a" * 63,                 # odd length: not whole bytes
        "a" * 15,                 # under the minimum
        " ".join(["ab"] * 10),    # spaces
        "0x" + "a" * 62,          # a hex *literal*, not hex digits
    ]
    for value in rejected_by_our_validator:
        response = gw.client.post("/auth/register", json={"machine_id": value})
        assert response.status_code == 400, f"{value!r} was not rejected"
        assert response.json()["error"]["type"] == "invalid_machine_id", value

    # Over 64 characters is refused too (by the request parser, one layer out).
    assert gw.client.post("/auth/register", json={"machine_id": "a" * 65}).status_code == 400
    assert gw.client.post("/auth/register", json={"machine_id": "a" * 4096}).status_code == 400

    # Nothing above created a key or a binding.
    assert gw.storage.list_keys() == []
    for value in [*rejected_by_our_validator, "a" * 65]:
        assert gw.storage.get_machine_binding(gw.storage.hash_machine_id(value)) is None


def test_normalize_machine_id_unit():
    assert normalize_machine_id("AB" * 32) == "ab" * 32
    assert normalize_machine_id("  " + "ab" * 8 + "  ") == "ab" * 8
    assert normalize_machine_id(None) is None
    assert normalize_machine_id("") is None
    assert normalize_machine_id("g" * 32) is None
    assert normalize_machine_id("a" * 65) is None
    assert normalize_machine_id("a" * 15) is None


def test_open_registration_still_accepts_an_email_instead_of_a_fingerprint(build):
    """The fingerprint path is additive. A browser-only user with no installer
    can still register the way they always could."""
    gw = build()
    assert gw.register(email="fan@b.com").status_code == 201


def test_reissue_refuses_a_suspended_key_instead_of_laundering_it(build):
    """Otherwise a suspension would be a one-installer-rerun problem."""
    gw = build(REGISTER_MAX_PER_IP_PER_DAY=0, AUTH_RPM_PER_IP=0)
    first = gw.register_machine(MACHINE_A)
    key_id = first.json()["key_id"]
    gw.storage.set_key_status(key_id, "suspended", "testing")

    again = gw.register_machine(MACHINE_A)
    assert again.status_code == 403
    assert again.json()["error"]["type"] == "key_suspended"
    assert "api_key" not in again.json()
    assert gw.storage.get_key(key_id).status == "suspended"
    assert len(gw.storage.list_keys()) == 1


def test_max_keys_per_ip_throttles_farming_without_banning(build):
    """Counts keys ISSUED, not registration attempts: fat-fingering an invite
    code five times farms nothing and must not be punished as if it had."""
    gw = build(MAX_KEYS_PER_IP=2, REGISTER_MAX_PER_IP_PER_DAY=0, AUTH_RPM_PER_IP=0,
               ALLOW_MULTIPLE_KEYS_PER_EMAIL=True)
    codes = [gw.register(email=f"u{i}@b.com").status_code for i in range(4)]
    assert codes[:2] == [201, 201]
    assert codes[2] == 429

    blocked = gw.register(email="late@b.com")
    assert blocked.json()["error"]["type"] == "registration_throttled"
    message = blocked.json()["error"]["message"]
    assert "not a ban" in message.lower()
    assert "machine_id" in message          # tells them the way out
    assert int(blocked.headers["Retry-After"]) > 0
    # Soft: the keys already issued are untouched and still work.
    assert all(row["status"] == "active" for row in gw.storage.list_keys())


def test_re_registration_is_exempt_from_the_per_ip_key_cap(build):
    """Re-running the installer must keep working even on a network that has
    already used its allowance — no new key is being minted."""
    gw = build(MAX_KEYS_PER_IP=1, REGISTER_MAX_PER_IP_PER_DAY=0, AUTH_RPM_PER_IP=0)
    first = gw.register_machine(MACHINE_A)
    assert first.status_code == 201
    # A different machine on the same network is now over the cap...
    assert gw.register_machine(MACHINE_B).status_code == 429
    # ...but the machine that already has a key still gets served.
    again = gw.register_machine(MACHINE_A)
    assert again.status_code == 200
    assert again.json()["key_id"] == first.json()["key_id"]


def test_registration_records_the_first_seen_ip_as_a_hash_only(build):
    """The binding needs a first-seen IP; the database must not become a
    location log to get one."""
    gw = build(TRUST_PROXY_HEADERS=True)
    response = gw.client.post(
        "/auth/register",
        json={"machine_id": MACHINE_A},
        headers={"x-forwarded-for": "203.0.113.77"},
    )
    assert response.status_code == 201
    dump = gw.db_dump()
    assert "203.0.113.77" not in dump          # raw address never stored
    assert MACHINE_A not in dump               # nor the raw fingerprint
    assert gw.storage.hash_ip("203.0.113.77") in dump
    binding = gw.storage.get_machine_binding(gw.storage.hash_machine_id(MACHINE_A))
    assert binding is not None
    assert binding.key_id == response.json()["key_id"]


# ---------------------------------------------------------------------------
# 12. loose IP binding — a soft throttle, never a ban
# ---------------------------------------------------------------------------
def test_max_ips_per_key_soft_throttles_and_does_not_suspend(build):
    gw = build(MAX_IPS_PER_KEY=2, ABUSE_DISTINCT_IP_THRESHOLD=0, TRUST_PROXY_HEADERS=True,
               RATE_LIMIT_RPM=100)
    key = gw.new_key()
    key_id = parse_key(key)[0]

    codes = [
        gw.client.post(
            "/v1/messages",
            headers={"Authorization": f"Bearer {key}", "x-forwarded-for": f"198.51.100.{i}"},
            json={"model": "yangble5"},
        ).status_code
        for i in range(3)
    ]
    assert codes[:2] == [200, 200]
    assert codes[2] == 429

    throttled = gw.client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {key}", "x-forwarded-for": "198.51.100.0"},
        json={"model": "yangble5"},
    )
    # Still throttled from an ALREADY-SEEN address: the mark is on the key.
    assert throttled.status_code == 429
    body = throttled.json()
    assert body["reason"] == "key_binding_throttled"
    assert "still active" in body["message"]
    assert int(throttled.headers["Retry-After"]) > 0

    # NOT a ban, by every measure that matters:
    assert gw.storage.get_key(key_id).status == "active"
    assert gw.storage.get_key(key_id).suspended_reason is None
    assert gw.client.get("/usage", headers={"Authorization": f"Bearer {key}"}).status_code == 200


def test_soft_throttle_expires_on_its_own(build):
    """The 'soft' in soft throttle: nothing has to be cleared by a human."""
    now = [1000.0]
    throttle = TimedThrottle(clock=lambda: now[0])
    throttle.throttle("k", 60)
    assert throttle.remaining("k") == pytest.approx(60)
    now[0] += 30
    assert throttle.remaining("k") == pytest.approx(30)
    # Re-marking never shortens an existing throttle.
    throttle.throttle("k", 5)
    assert throttle.remaining("k") == pytest.approx(30)
    now[0] += 31
    assert throttle.remaining("k") == 0.0
    assert throttle.remaining("never-throttled") == 0.0


def test_soft_throttle_fires_before_the_hard_abuse_suspension(build):
    """MAX_IPS_PER_KEY (5) is below ABUSE_DISTINCT_IP_THRESHOLD (8) by default,
    so a wandering user is slowed down long before anything is suspended."""
    gw = build()
    assert gw.settings.max_ips_per_key < gw.settings.abuse_distinct_ip_threshold


# ---------------------------------------------------------------------------
# 13. the operator reserve
# ---------------------------------------------------------------------------
def _reserve_harness(build):
    """Pool at 40% remaining, with a 50% reserve: public traffic is locked out
    of the last slice, the operator is not."""
    gw = build(GLOBAL_MONTHLY_USD_BUDGET=10.0, OPERATOR_RESERVE_FRACTION=0.5,
               DAILY_TOKEN_BUDGET=NO_KEY_LIMIT, DAILY_COST_USD_BUDGET=0)
    whale = gw.storage.issue_key(email="whale@b.com", scheme="pbkdf2")
    gw.charge(whale.key_id, cost=6.0)
    return gw


def test_operator_reserve_blocks_the_public_but_not_the_operator(build):
    gw = _reserve_harness(build)
    key = gw.new_key()
    key_id = parse_key(key)[0]

    blocked = gw.call(key)
    assert blocked.status_code == 429
    assert blocked.json()["reason"] == "operator_reserve_engaged"
    # Nothing was forwarded: the reserve is a spend guard, not a label.
    assert gw.upstream.calls == []

    flagged = gw.admin("POST", f"/admin/keys/{key_id}/operator", json={"is_operator": True})
    assert flagged.status_code == 200
    assert flagged.json()["is_operator"] is True

    # Same key, same pool, same instant — the flag is the only difference.
    assert gw.call(key).status_code == 200
    assert len(gw.upstream.calls) == 1

    # And it is revocable.
    assert gw.admin(
        "POST", f"/admin/keys/{key_id}/operator", json={"is_operator": False}
    ).status_code == 200
    assert gw.call(key).status_code == 429


def test_operator_reserve_does_not_engage_while_the_pool_is_healthy(build):
    gw = build(GLOBAL_MONTHLY_USD_BUDGET=10.0, OPERATOR_RESERVE_FRACTION=0.25,
               DAILY_TOKEN_BUDGET=NO_KEY_LIMIT, DAILY_COST_USD_BUDGET=0)
    whale = gw.storage.issue_key(email="whale@b.com", scheme="pbkdf2")
    gw.charge(whale.key_id, cost=5.0)          # 50% left, reserve is 25%
    assert gw.call(gw.new_key()).status_code == 200
    assert gw.client.get("/pool/status").json()["reserve_engaged"] is False


def test_operator_flag_is_admin_only(build):
    gw = build()
    key = gw.new_key()
    key_id = parse_key(key)[0]
    # No admin key, a wrong admin key, and a valid USER key are all just 404 —
    # a self-service version of this endpoint would be a self-service reserve.
    assert gw.client.post(
        f"/admin/keys/{key_id}/operator", json={"is_operator": True}
    ).status_code == 404
    assert gw.client.post(
        f"/admin/keys/{key_id}/operator",
        headers={"Authorization": "Bearer wrong"}, json={"is_operator": True},
    ).status_code == 404
    assert gw.client.post(
        f"/admin/keys/{key_id}/operator",
        headers={"Authorization": f"Bearer {key}"}, json={"is_operator": True},
    ).status_code == 404
    assert gw.storage.get_key(key_id).is_operator is False

    assert gw.admin(
        "POST", "/admin/keys/does-not-exist/operator", json={"is_operator": True}
    ).status_code == 404
    assert gw.admin("POST", f"/admin/keys/{key_id}/operator", json={}).status_code == 400


def test_operator_reserve_fraction_is_validated(tmp_path):
    for bad in ("1.0", "1.5", "-0.1"):
        env = dict(BASE_ENV)
        env["DB_PATH"] = str(tmp_path / "x.db")
        env["OPERATOR_RESERVE_FRACTION"] = bad
        with pytest.raises(ConfigError, match="OPERATOR_RESERVE_FRACTION"):
            Settings.from_env(env)


def test_operator_reserve_default_is_a_quarter(tmp_path):
    env = dict(BASE_ENV)
    env["DB_PATH"] = str(tmp_path / "x.db")
    assert Settings.from_env(env).operator_reserve_fraction == 0.25


# ---------------------------------------------------------------------------
# 14. graceful degradation instead of hard failure
# ---------------------------------------------------------------------------
def _assert_degradation_shape(response, *, reason):
    """Every 'you cannot spend right now' answer carries the same four facts."""
    body = response.json()
    assert body["reason"] == reason
    assert set(body) >= {"reason", "reset_at", "remaining_pct", "byok_instructions", "error"}
    # An SDK reads error.message; a shell script reads the top level. Both work.
    assert body["error"]["type"] == reason
    assert body["error"]["message"]
    assert 0.0 <= body["remaining_pct"] <= 1.0
    reset = datetime.fromisoformat(body["reset_at"])
    assert reset.tzinfo is not None                      # unambiguous instant
    assert reset > datetime.now(reset.tzinfo)            # in the future
    instructions = body["byok_instructions"]
    assert instructions["available"] is True
    assert instructions["attach_endpoint"] == "POST /byok"
    assert instructions["steps"]
    assert int(response.headers["Retry-After"]) > 0
    return body


def test_reserve_degradation_payload_shape(build):
    gw = _reserve_harness(build)
    response = gw.call(gw.new_key())
    assert response.status_code == 429
    body = _assert_degradation_shape(response, reason="operator_reserve_engaged")
    assert body["remaining_pct"] == pytest.approx(0.4)


def test_daily_pool_exhaustion_degrades_with_a_midnight_reset(build):
    gw = build(GLOBAL_MONTHLY_USD_BUDGET=0, GLOBAL_DAILY_TOKEN_BUDGET=1000,
               DAILY_TOKEN_BUDGET=NO_KEY_LIMIT, DAILY_COST_USD_BUDGET=0)
    key = gw.new_key()
    gw.upstream.set_json_usage(input_tokens=1000, output_tokens=0)
    assert gw.call(key).status_code == 200

    blocked = gw.call(key)
    assert blocked.status_code == 429
    body = _assert_degradation_shape(blocked, reason="pool_exhausted")
    assert body["remaining_pct"] == 0.0
    # A daily cap resets tonight, not on the 1st — and says so.
    assert datetime.fromisoformat(body["reset_at"]).hour == 0
    assert "00:00 UTC" in body["message"]

    # Nothing is forwarded past the cap. Ever.
    calls_before = len(gw.upstream.calls)
    gw.call(key)
    assert len(gw.upstream.calls) == calls_before


def test_monthly_cap_degradation_keeps_its_status_but_gains_the_payload(build):
    """402 is kept: 'come back on the 1st' is a different fact from 'come back
    tomorrow', and clients already distinguish them. The helpful body is new."""
    gw = build(GLOBAL_MONTHLY_USD_BUDGET=5.0, DAILY_COST_USD_BUDGET=0,
               DAILY_TOKEN_BUDGET=NO_KEY_LIMIT, OPERATOR_RESERVE_FRACTION=0)
    key = gw.new_key()
    whale = gw.storage.issue_key(email="whale@b.com", scheme="pbkdf2")
    gw.charge(whale.key_id, cost=5.0)

    blocked = gw.call(key)
    assert blocked.status_code == 402
    assert blocked.json()["reason"] == "operator_budget_exhausted"
    assert blocked.json()["remaining_pct"] == 0.0
    assert blocked.json()["byok_instructions"]["available"] is True
    assert datetime.fromisoformat(blocked.json()["reset_at"]).day == 1


def test_per_key_quota_exhaustion_also_offers_byok(build):
    """The moment a user runs out is the only moment they will read how to stop
    depending on the pool."""
    gw = build(DAILY_TOKEN_BUDGET=1000, DAILY_COST_USD_BUDGET=0)
    key = gw.new_key()
    gw.charge(parse_key(key)[0], tokens=1000)
    blocked = gw.call(key)
    assert blocked.status_code == 429
    _assert_degradation_shape(blocked, reason="daily_quota_exhausted")


def test_upstream_quota_error_degrades_and_hides_the_providers_body(build):
    """An upstream 429 is answered by us: the provider's text can name the
    operator's account, and the user needs the way out, not a stack trace."""
    gw = build()
    key = gw.new_key()
    gw.upstream.status = 429
    gw.upstream.chunks = [
        b'{"error":"quota exceeded for project operator-private-account-42"}'
    ]
    response = gw.call(key)
    assert response.status_code == 429
    _assert_degradation_shape(response, reason="upstream_quota_exhausted")
    assert "operator-private-account-42" not in response.text
    assert "quota exceeded for project" not in response.text


def test_byok_instructions_are_honest_when_byok_is_switched_off(build):
    gw = build(BYOK_ENABLED=False, DAILY_TOKEN_BUDGET=1000, DAILY_COST_USD_BUDGET=0)
    key = gw.new_key()
    gw.charge(parse_key(key)[0], tokens=1000)
    instructions = gw.call(key).json()["byok_instructions"]
    assert instructions["available"] is False
    assert "self_host" in instructions
    assert "attach_endpoint" not in instructions


# ---------------------------------------------------------------------------
# 15. /pool/status — the public capacity widget
# ---------------------------------------------------------------------------
POOL_STATUS_KEYS = {
    "remaining_pct", "reset_at", "reset_window", "registration_open",
    "accepting_requests", "capped", "reserve_engaged", "operator_reserve_fraction",
    "byok_available",
}


def test_pool_status_is_public_and_exposes_no_secrets(build):
    gw = build(GLOBAL_MONTHLY_USD_BUDGET=10.0, DAILY_TOKEN_BUDGET=NO_KEY_LIMIT,
               DAILY_COST_USD_BUDGET=0)
    whale = gw.storage.issue_key(email="whale@b.com", scheme="pbkdf2")
    gw.charge(whale.key_id, cost=2.5, tokens=987_654)

    response = gw.client.get("/pool/status")           # no Authorization header
    assert response.status_code == 200
    body = response.json()
    assert set(body) == POOL_STATUS_KEYS

    text = response.text
    for secret in (ENGINE_KEY, ADMIN_KEY, gw.settings.db_path, gw.settings.engine_url):
        assert secret not in text
    for forbidden in ("sk-", "password", "pepper", "8318", "cost", "usd",
                      "budget", "email", "whale", "key_id", "yb5_"):
        assert forbidden not in text.lower()

    # Fractions only: the absolute spend and the cap are both unlearnable from
    # this endpoint, so a scraper cannot reconstruct the operator's invoice.
    assert body["remaining_pct"] == pytest.approx(0.75)
    assert "987654" not in text and "987,654" not in text
    assert "2.5" not in text and "10" not in text.replace("0.1", "")


def test_pool_status_tracks_the_tightest_cap(build):
    """The honest answer to 'is there room for me?' is the minimum across every
    ceiling, not an average that hides an exhausted one."""
    gw = build(GLOBAL_MONTHLY_USD_BUDGET=100.0, GLOBAL_DAILY_USD_BUDGET=10.0,
               DAILY_TOKEN_BUDGET=NO_KEY_LIMIT, DAILY_COST_USD_BUDGET=0)
    whale = gw.storage.issue_key(email="whale@b.com", scheme="pbkdf2")
    gw.charge(whale.key_id, cost=9.0)
    body = gw.client.get("/pool/status").json()
    # Monthly is 91% free, but today's slice is 10% free. Report the 10%.
    assert body["remaining_pct"] == pytest.approx(0.1)
    assert body["reset_window"] == "day"
    assert datetime.fromisoformat(body["reset_at"]).hour == 0


def test_pool_status_reports_registration_closed_when_capped(build):
    gw = build(GLOBAL_MONTHLY_USD_BUDGET=5.0, DAILY_TOKEN_BUDGET=NO_KEY_LIMIT,
               DAILY_COST_USD_BUDGET=0)
    assert gw.client.get("/pool/status").json()["registration_open"] is True
    whale = gw.storage.issue_key(email="whale@b.com", scheme="pbkdf2")
    gw.charge(whale.key_id, cost=5.0)

    body = gw.client.get("/pool/status").json()
    assert body["remaining_pct"] == 0.0
    assert body["accepting_requests"] is False
    assert body["registration_open"] is False        # matches what /auth/register does
    assert gw.register(email="new@b.com").status_code == 503


def test_pool_status_says_uncapped_honestly(build):
    """No ceiling configured must not be reported as '0% left'."""
    gw = build(REGISTRATION_MODE="closed", GLOBAL_MONTHLY_USD_BUDGET=0)
    body = gw.client.get("/pool/status").json()
    assert body["capped"] is False
    assert body["remaining_pct"] == 1.0
    assert body["reserve_engaged"] is False


def test_health_still_exposes_nothing_new(build):
    """The pool widget got its own endpoint precisely so /health did not have to
    start carrying capacity data."""
    gw = build()
    body = gw.client.get("/health").json()
    assert set(body) == {
        "status", "service", "version", "uptime_seconds", "accepting_requests", "registration",
    }


# ---------------------------------------------------------------------------
# 16. BYOK
# ---------------------------------------------------------------------------
def test_byok_requests_do_not_decrement_the_shared_budget(build):
    gw = build(
        DAILY_TOKEN_BUDGET=1_000_000, DAILY_COST_USD_BUDGET=0,
        PRICE_TABLE_JSON=json.dumps(
            {"default": {"input": 10.0, "cached_input": 1.0, "output": 30.0}}
        ),
    )
    key = gw.new_key()
    key_id = parse_key(key)[0]
    assert gw.attach_byok(key).status_code == 201

    gw.upstream.set_json_usage(input_tokens=1_000_000, output_tokens=0)
    before = gw.spend.current()
    assert gw.call(key).status_code == 200
    assert gw.spend.current() == before                       # tracker untouched

    # The pool's own aggregates skip it entirely.
    assert gw.storage.global_usage_for_month().total_tokens == 0
    assert gw.storage.global_usage_for_month().cost_usd == 0.0
    assert gw.storage.global_usage_for_day().total_tokens == 0
    # The user's slice of the pool is untouched too — they paid for this
    # themselves, so charging their allowance would be billing them twice.
    assert gw.storage.usage_for_day(key_id, billable_only=True).total_tokens == 0
    # ...but they can still see what they did.
    assert gw.storage.usage_for_day(key_id).total_tokens == 1_000_000

    # A 1,000,000-token request is exactly the per-key daily budget. On the
    # shared pool that is the last one they get; on BYOK it does not count.
    assert gw.call(key).status_code == 200

    # Detach, and the very same request starts costing the pool again. This is
    # the control that proves the assertions above are not vacuous.
    assert gw.client.delete("/byok", headers={"Authorization": f"Bearer {key}"}).status_code == 200
    assert gw.call(key).status_code == 200
    assert gw.storage.global_usage_for_month().total_tokens == 1_000_000
    assert gw.spend.current()[1] == 1_000_000
    # And now the per-key budget bites.
    assert gw.call(key).status_code == 429


def test_byok_routes_with_the_users_credential_not_the_engine_key(build):
    gw = build()
    key = gw.new_key()
    assert gw.attach_byok(key).status_code == 201
    assert gw.call(key).status_code == 200

    sent = gw.upstream.calls[-1]["headers"]
    assert sent["Authorization"] == f"Bearer {USER_CREDENTIAL}"
    assert sent["x-api-key"] == USER_CREDENTIAL
    assert sent["X-Yangble5-Byok"] == "1"
    # The operator's engine key is NOT spent on a BYOK request.
    assert ENGINE_KEY not in json.dumps(sent)

    # Detaching puts them back on the operator's credential.
    gw.client.delete("/byok", headers={"Authorization": f"Bearer {key}"})
    assert gw.call(key).status_code == 200
    sent = gw.upstream.calls[-1]["headers"]
    assert sent["Authorization"] == f"Bearer {ENGINE_KEY}"
    assert sent["X-Yangble5-Byok"] == "0"
    assert USER_CREDENTIAL not in json.dumps(sent)


def test_a_client_cannot_forge_the_byok_marker(build):
    gw = build()
    key = gw.new_key()
    gw.client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {key}", "x-yangble5-byok": "1"},
        json={"model": "yangble5"},
    )
    sent = gw.upstream.calls[-1]["headers"]
    assert sent["X-Yangble5-Byok"] == "0"
    assert sent["Authorization"] == f"Bearer {ENGINE_KEY}"


def test_byok_credential_is_never_readable_back(build):
    gw = build()
    key = gw.new_key()
    gw.attach_byok(key, label="my laptop")
    auth = {"Authorization": f"Bearer {key}"}

    for response in (
        gw.client.get("/byok", headers=auth),
        gw.client.get("/usage", headers=auth),
        gw.admin("GET", "/admin/keys"),
        gw.admin("GET", "/admin/stats"),
        gw.client.get("/pool/status"),
        gw.client.get("/health"),
    ):
        assert USER_CREDENTIAL not in response.text
        # Not even a prefix that would narrow a guess.
        assert USER_CREDENTIAL[:12] not in response.text

    status = gw.client.get("/byok", headers=auth).json()
    assert status["attached"] is True
    assert status["label"] == "my laptop"
    assert gw.admin("GET", "/admin/keys").json()["keys"][0]["byok_attached"] is True


def test_byok_is_encrypted_at_rest_when_a_key_is_configured(build):
    pytest.importorskip("cryptography")
    gw = build(BYOK_ENCRYPTION_KEY="test-only-byok-encryption-secret")
    key = gw.new_key()
    attached = gw.attach_byok(key)
    assert attached.status_code == 201
    assert attached.json()["encrypted_at_rest"] is True
    assert "encrypted at rest" in attached.json()["storage_notice"].lower()

    assert USER_CREDENTIAL not in gw.db_dump()
    # And it still works: sealing is useless if it cannot be opened.
    assert gw.call(key).status_code == 200
    assert gw.upstream.calls[-1]["headers"]["Authorization"] == f"Bearer {USER_CREDENTIAL}"


def test_byok_plaintext_mode_says_so_instead_of_hiding_it(build):
    """The documented fallback. This test exists to make sure the documentation
    and the behaviour cannot drift apart: if the credential is stored as-is,
    the user is told so in those words, at the moment they hand it over."""
    gw = build()                                   # no BYOK_ENCRYPTION_KEY
    key = gw.new_key()
    attached = gw.attach_byok(key)
    assert attached.status_code == 201
    assert attached.json()["encrypted_at_rest"] is False

    notice = attached.json()["storage_notice"]
    assert "NOT ENCRYPTED" in notice
    assert "DELETE /byok" in notice
    assert "run your own" in notice.lower()
    # The notice is accurate, which is the part that matters.
    assert USER_CREDENTIAL in gw.db_dump()

    warnings = gw.settings.startup_warnings()
    assert any("BYOK_ENCRYPTION_KEY is empty" in w for w in warnings)


def test_byok_encryption_key_without_the_library_is_fatal(tmp_path, monkeypatch):
    """A silent downgrade to plaintext would make an operator's belief that
    their users' credentials are encrypted simply false."""
    import gateway.byok as byok_module

    monkeypatch.setattr(byok_module, "AESGCM_AVAILABLE", False)
    env = dict(BASE_ENV)
    env["DB_PATH"] = str(tmp_path / "x.db")
    env["BYOK_ENCRYPTION_KEY"] = "something"
    with pytest.raises(ConfigError, match="cryptography"):
        Settings.from_env(env)


def test_byok_can_be_switched_off(build):
    gw = build(BYOK_ENABLED=False)
    key = gw.new_key()
    response = gw.attach_byok(key)
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "byok_disabled"
    assert gw.storage.get_byok(parse_key(key)[0]) is None


def test_byok_endpoints_require_authentication(build):
    gw = build()
    assert gw.client.post("/byok", json={"credential": USER_CREDENTIAL}).status_code == 401
    assert gw.client.get("/byok").status_code == 401
    assert gw.client.delete("/byok").status_code == 401


def test_byok_rejects_a_malformed_body(build):
    gw = build()
    key = gw.new_key()
    auth = {"Authorization": f"Bearer {key}"}
    assert gw.client.post("/byok", headers=auth, json={}).status_code == 400
    assert gw.client.post("/byok", headers=auth, json={"credential": "x"}).status_code == 400
    assert gw.client.post(
        "/byok", headers=auth, json={"credential": "y" * 5000}
    ).status_code == 400
    assert gw.storage.get_byok(parse_key(key)[0]) is None


def test_byok_users_skip_the_operator_reserve(build):
    """Someone paying their own way is not competing for the operator's slice."""
    gw = _reserve_harness(build)
    key = gw.new_key()
    assert gw.call(key).status_code == 429                 # blocked by the reserve
    assert gw.attach_byok(key).status_code == 201
    assert gw.call(key).status_code == 200                 # and now not


def test_unreadable_byok_falls_back_to_the_pool_instead_of_failing(build):
    """An operator rotating BYOK_ENCRYPTION_KEY must not become an outage for
    everyone who ever attached a credential."""
    pytest.importorskip("cryptography")
    gw = build(BYOK_ENCRYPTION_KEY="the-original-secret")
    key = gw.new_key()
    assert gw.attach_byok(key).status_code == 201

    # Simulate the restart-with-a-new-secret case.
    from gateway.byok import ByokCipher

    gw.app.state.gateway.byok_cipher = ByokCipher("a-completely-different-secret")
    assert gw.call(key).status_code == 200
    sent = gw.upstream.calls[-1]["headers"]
    assert sent["Authorization"] == f"Bearer {ENGINE_KEY}"   # back on the pool
    assert sent["X-Yangble5-Byok"] == "0"


# ---------------------------------------------------------------------------
# 17. the caps still cannot be silently overspent
# ---------------------------------------------------------------------------
def test_daily_pool_cap_alone_satisfies_the_open_mode_ceiling_requirement(tmp_path):
    """An operator who thinks in days should not have to restate their limit in
    months before they are allowed to open registration."""
    settings = Settings.from_env(
        {
            "ENGINE_API_KEY": ENGINE_KEY,
            "DB_PATH": str(tmp_path / "x.db"),
            "REGISTRATION_MODE": "open",
            "GLOBAL_MONTHLY_USD_BUDGET": "0",
            "GLOBAL_DAILY_USD_BUDGET": "2.0",
        }
    )
    assert settings.has_daily_pool_cap
    assert settings.has_any_pool_cap


def test_billable_flag_defaults_to_true_for_direct_writes(build):
    """A caller that forgets the flag must be counted, not excused."""
    gw = build()
    key = gw.new_key()
    gw.charge(parse_key(key)[0], tokens=500, cost=1.0)
    assert gw.storage.global_usage_for_month().total_tokens == 500
    assert gw.storage.usage_for_day(parse_key(key)[0], billable_only=True).total_tokens == 500


# ---------------------------------------------------------------------------
# 18. the health contract the infrastructure depends on
#
# The original defect was not subtle in effect and was invisible in review: the
# compose healthcheck probed /healthz, the app served only /health, so the
# gateway container was permanently "unhealthy" — and caddy, which waited on
# `condition: service_healthy`, never started at all. No edge, no certificate,
# no site. Nothing in the test suite could see it, because the mismatch lived
# between two files that no test read.
#
# These tests read those files.
# ---------------------------------------------------------------------------
def _app_paths(app) -> set[str]:
    return {route.path for route in app.routes if hasattr(route, "path")}


def _compose_gateway_probe_paths(path: pathlib.Path) -> set[str]:
    """URL paths the gateway's compose healthcheck actually requests.

    Only the gateway service: the engine's healthcheck talks to CLIProxyAPI,
    which is a different application with a different URL space, and asserting
    its paths against our route table would be nonsense.
    """
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    test = doc["services"]["gateway"]["healthcheck"]["test"]
    blob = " ".join(test) if isinstance(test, list) else str(test)
    # The probe is a Python one-liner, so the path is a quoted token inside it.
    return set(re.findall(r"['\"](/[A-Za-z0-9_./-]*)['\"]", blob))


def _caddyfile_health_targets(text: str) -> set[str]:
    """What the edge's health routes ask the APP for, after any rewrite.

    Named matchers are collected first because Caddy's inline matcher token is a
    single path — multi-path routes have to be written as `@name path a b`, so
    the paths and the handle block that uses them are two separate statements.
    """
    matchers = {
        name: paths.split()
        for name, paths in re.findall(r"^\s*@(\w+)\s+path\s+(.+)$", text, re.M)
    }
    targets: set[str] = set()
    for name, body in re.findall(r"^\s*handle\s+@(\w+)\s*\{(.*?)^\s*\}", text, re.M | re.S):
        if not name.startswith("health"):
            continue
        rewrite = re.search(r"^\s*rewrite\s+\*\s+(\S+)\s*$", body, re.M)
        for declared in matchers.get(name, []):
            targets.add(rewrite.group(1) if rewrite else declared)
    return targets


def test_health_and_healthz_are_the_same_endpoint(gw):
    """Both spellings exist and agree. /healthz is not a stub that returns a
    different, thinner payload — infrastructure would still call it healthy."""
    canonical = gw.client.get("/health")
    alias = gw.client.get("/healthz")
    assert canonical.status_code == 200
    assert alias.status_code == 200
    assert alias.json() == canonical.json()
    assert canonical.json()["service"] == "yangble5-gateway"


def test_compose_healthcheck_probes_a_path_the_app_serves(gw):
    """Reads the shipped compose files. This is the assertion that would have
    caught /healthz-vs-/health before it took the whole edge down with it."""
    served = _app_paths(gw.app)
    checked = 0
    for name in ("docker-compose.yml", "docker-compose.behind-proxy.yml"):
        compose = REPO_ROOT / "deploy" / name
        if not compose.exists():
            continue
        probes = _compose_gateway_probe_paths(compose)
        assert probes, f"no probe path found in {name} — the parser or the file changed"
        for probe in probes:
            assert probe in served, (
                f"{name} healthchecks {probe}, which gateway/app.py does not route. "
                f"The container can never become healthy. Served: {sorted(served)}"
            )
        checked += 1
    assert checked, "no compose file was checked — the deploy/ layout moved"


def test_caddyfile_health_routes_reach_a_real_route(gw):
    """Every health path the edge accepts must land on a path the app serves —
    directly, or through the rewrite the Caddyfile declares."""
    caddyfile = (REPO_ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    targets = _caddyfile_health_targets(caddyfile)
    assert targets, "no health handle block found in the Caddyfile"

    served = _app_paths(gw.app)
    for target in targets:
        assert target in served, (
            f"the Caddyfile routes a health request to {target}, which the app "
            f"does not serve. Served: {sorted(served)}"
        )


def test_caddyfile_does_not_forward_unstripped_api_paths(gw):
    """/api/* used to be forwarded verbatim into an app that serves none of it.

    The tempting repair — strip the prefix for the whole /api tree — is worse
    than the bug: /api/auth/register would arrive at the app as /auth/register
    having skipped the @auth block's strict credential-guessing rate limit,
    because Caddy matches on the URL as RECEIVED. So the fix is that only the
    health spellings exist under /api, and they are rewritten explicitly.
    """
    caddyfile = (REPO_ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    api_matcher = re.search(r"@api\s*\{(.*?)\}", caddyfile, re.S)
    assert api_matcher, "the @api matcher disappeared"
    assert "/api/*" not in api_matcher.group(1), (
        "@api matches /api/* again. Those requests reach the app unstripped and "
        "404; stripping them wholesale would bypass the @auth rate limit."
    )
    # No blanket prefix strip crept in either.
    assert "handle_path /api" not in caddyfile
    assert "strip_prefix /api" not in caddyfile


def test_smoke_test_probes_a_path_the_app_serves(gw):
    """The smoke test is what an operator runs at 3am. Its health path is part
    of the same contract as the compose probe and drifts the same way."""
    smoke = REPO_ROOT / "deploy" / "smoke_test.sh"
    if not smoke.exists():
        pytest.skip("smoke_test.sh not present")
    text = smoke.read_text(encoding="utf-8")
    probed = {
        path for path in re.findall(r"\$\{BASE_URL\}(/[A-Za-z0-9_/-]*)", text)
        if "health" in path
    }
    assert probed, "smoke_test.sh no longer probes a health path"
    served = _app_paths(gw.app)
    for path in probed:
        assert path in served, f"smoke_test.sh probes {path}, which the app does not route"


def test_caddyfile_csp_allows_the_landing_pages_inline_scripts():
    """The shipped CSP said `script-src 'self'` with no hashes, which blocks
    site/index.html's inline script — silently, because a CSP violation is a
    console message. The hashes are recomputed here from the actual files, so
    editing a <script> block by one byte fails this test instead of shipping a
    page whose copy buttons and status widget quietly stop working."""
    import base64
    import hashlib

    caddyfile = (REPO_ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    csp = re.search(r'Content-Security-Policy "([^"]+)"', caddyfile)
    assert csp, "the CSP header disappeared from the security_headers snippet"
    policy = csp.group(1)

    script_src = policy.split("script-src")[1].split(";")[0]
    assert "'unsafe-inline'" not in script_src, (
        "script-src gained 'unsafe-inline'. Not on a page whose job is to "
        "convince someone it is safe to pipe a script into their shell."
    )

    for filename in ("index.html", "verify.html"):
        page = REPO_ROOT / "site" / filename
        if not page.exists():
            continue
        html = page.read_text(encoding="utf-8")
        for script in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S):
            digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
            assert f"'sha256-{digest}'" in policy, (
                f"site/{filename} has an inline <script> whose hash sha256-{digest} "
                "is not in the Caddyfile CSP, so the browser will refuse to run it. "
                "Recompute with the snippet in site/README.md."
            )


# ---------------------------------------------------------------------------
# 19. which address a request is attributed to
#
# Behind Caddy — and behind Caddy behind Cloudflare — reading the wrong entry
# does not fail loudly. Every request just gets attributed to a handful of edge
# addresses, so per-IP registration caps, the auth throttle and the abuse
# fan-out counter all collapse into one shared bucket that no real user can
# exhaust and no attacker is ever caught by.
# ---------------------------------------------------------------------------
def _request_with(headers: dict[str, str], peer: str = "172.20.0.5"):
    """A Request as the ASGI server would build it. `peer` is the TCP peer —
    the Caddy container, in production."""
    return StarletteRequest(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "scheme": "http",
            "http_version": "1.1",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": (peer, 41234),
            "server": ("gateway", 8000),
        }
    )


def _settings_with(**overrides) -> Settings:
    env = dict(BASE_ENV)
    env.update({k: str(v) for k, v in overrides.items()})
    return Settings.from_env(env)


def test_x_real_ip_beats_x_forwarded_for():
    """The exact production shape. Caddy sets X-Real-IP to its own verdict and
    APPENDS its peer — a Cloudflare edge node — to X-Forwarded-For. Reading the
    tail of XFF therefore names Cloudflare, not the user."""
    settings = _settings_with(TRUST_PROXY_HEADERS=True, TRUSTED_PROXY_HOPS=1)
    request = _request_with(
        {
            "x-forwarded-for": "203.0.113.9, 172.68.44.7",  # client, then CF edge
            "x-real-ip": "203.0.113.9",
        }
    )
    assert client_ip(request, settings) == "203.0.113.9"


def test_a_forged_x_forwarded_for_cannot_beat_x_real_ip():
    """A client prepending its own XFF entries must not move the attribution:
    X-Real-IP is set by the edge with `header_up`, which overwrites."""
    settings = _settings_with(TRUST_PROXY_HEADERS=True, TRUSTED_PROXY_HOPS=1)
    request = _request_with(
        {
            "x-forwarded-for": "1.1.1.1, 2.2.2.2, 203.0.113.9, 172.68.44.7",
            "x-real-ip": "203.0.113.9",
        }
    )
    assert client_ip(request, settings) == "203.0.113.9"


def test_forwarded_headers_are_ignored_entirely_when_not_trusted():
    """With no proxy in front, both headers are just strings the caller chose.
    Honouring either one would hand every per-IP limit an unlimited supply of
    fresh buckets."""
    settings = _settings_with(TRUST_PROXY_HEADERS=False)
    request = _request_with(
        {"x-forwarded-for": "9.9.9.9", "x-real-ip": "8.8.8.8"}, peer="198.51.100.4"
    )
    assert client_ip(request, settings) == "198.51.100.4"


def test_trusted_hop_count_picks_the_entry_that_many_from_the_end():
    """The X-Forwarded-For fallback, for an edge that does not set X-Real-IP.
    Each proxy APPENDS, so with N of them the client is N entries from the end;
    anything to the left of that is caller-supplied and worthless."""
    forwarded = {"x-forwarded-for": "1.1.1.1, 203.0.113.9, 172.68.44.7"}

    one_hop = _settings_with(TRUST_PROXY_HEADERS=True, TRUSTED_PROXY_HOPS=1)
    assert client_ip(_request_with(forwarded), one_hop) == "172.68.44.7"

    two_hops = _settings_with(TRUST_PROXY_HEADERS=True, TRUSTED_PROXY_HOPS=2)
    assert client_ip(_request_with(forwarded), two_hops) == "203.0.113.9"

    # More hops than entries clamps to the first rather than raising.
    many = _settings_with(TRUST_PROXY_HEADERS=True, TRUSTED_PROXY_HOPS=9)
    assert client_ip(_request_with(forwarded), many) == "1.1.1.1"


def test_non_addresses_in_forwarded_headers_are_discarded():
    """Every per-IP limit buckets on this value. A caller that can put arbitrary
    text here can mint unlimited distinct buckets, so anything that is not an IP
    literal falls through to the next source."""
    settings = _settings_with(TRUST_PROXY_HEADERS=True, TRUSTED_PROXY_HOPS=1)

    # Junk X-Real-IP falls back to XFF...
    assert client_ip(
        _request_with({"x-real-ip": "not-an-ip", "x-forwarded-for": "203.0.113.9"}), settings
    ) == "203.0.113.9"
    # ...and junk in both falls back to the real TCP peer.
    assert client_ip(
        _request_with(
            {"x-real-ip": "<script>", "x-forwarded-for": "unknown, garbage"}, peer="10.1.2.3"
        ),
        settings,
    ) == "10.1.2.3"


def test_forwarded_addresses_with_ports_are_understood():
    """Some proxies append host:port. Treating `1.2.3.4:5678` as junk would
    silently demote a correct edge to the fallback path."""
    settings = _settings_with(TRUST_PROXY_HEADERS=True, TRUSTED_PROXY_HOPS=1)
    assert client_ip(_request_with({"x-real-ip": "203.0.113.9:5678"}), settings) == "203.0.113.9"
    assert client_ip(_request_with({"x-real-ip": "[2001:db8::1]:443"}), settings) == "2001:db8::1"


def test_the_recorded_registration_ip_is_the_client_not_the_edge(build):
    """End to end: the hash written to key_registrations must be the user's
    address. If it were the Cloudflare node's, every visitor on earth would
    share one per-IP registration allowance."""
    gw = build(TRUST_PROXY_HEADERS=True, TRUSTED_PROXY_HOPS=1)
    response = gw.client.post(
        "/auth/register",
        json={"machine_id": MACHINE_A},
        headers={
            "x-forwarded-for": "203.0.113.9, 172.68.44.7",
            "x-real-ip": "203.0.113.9",
        },
    )
    assert response.status_code == 201
    dump = gw.db_dump()
    assert gw.storage.hash_ip("203.0.113.9") in dump
    assert gw.storage.hash_ip("172.68.44.7") not in dump


# ---------------------------------------------------------------------------
# 20. the registration ceilings hold under a concurrent burst
#
# Both caps used to be read-then-act with `await` points between the read and
# the write: the count was fetched, the request awaited a JSON parse and a key
# derivation, and every sibling in the burst had already read the same stale
# number. A cap that only holds when requests arrive one at a time is not a cap
# — and "arrive one at a time" is exactly what an abuser will not do.
# ---------------------------------------------------------------------------
def _burst(fn, count: int) -> list:
    """Run `fn(i)` on `count` threads released together."""
    barrier = threading.Barrier(count)
    lock = threading.Lock()
    results: list = []

    def worker(index: int) -> None:
        barrier.wait()
        outcome = fn(index)
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert len(results) == count, "a worker thread did not finish"
    return results


def test_daily_registration_attempt_cap_holds_under_a_burst(build):
    gw = build(
        REGISTER_MAX_PER_IP_PER_DAY=3,
        MAX_KEYS_PER_IP=0,
        AUTH_RPM_PER_IP=0,
        ALLOW_MULTIPLE_KEYS_PER_EMAIL=True,
    )
    codes = _burst(lambda i: gw.register(email=f"burst{i}@b.com").status_code, 12)

    assert codes.count(201) == 3, (
        f"the daily attempt cap of 3 let {codes.count(201)} registrations through. "
        "check-and-increment must share one transaction."
    )
    assert codes.count(429) == 9
    assert gw.storage.register_attempts_today(gw.storage.hash_ip("testclient")) == 3


def test_per_ip_key_cap_holds_under_a_burst(build):
    """The other counter, and a stricter test: it is enforced inside the same
    transaction that inserts the key, so the database itself cannot end up
    holding more keys than the cap allows."""
    gw = build(
        MAX_KEYS_PER_IP=2,
        REGISTER_MAX_PER_IP_PER_DAY=0,
        AUTH_RPM_PER_IP=0,
        ALLOW_MULTIPLE_KEYS_PER_EMAIL=True,
    )
    codes = _burst(lambda i: gw.register(email=f"rush{i}@b.com").status_code, 10)

    assert codes.count(201) == 2, (
        f"the per-IP key cap of 2 issued {codes.count(201)} keys under load"
    )
    assert codes.count(429) == 8
    # The ceiling is a property of the DATABASE, not of the response codes.
    assert len(gw.storage.list_keys()) == 2
    assert gw.storage.count_keys_issued_from_ip(gw.storage.hash_ip("testclient")) == 2


def test_one_key_per_email_holds_under_a_burst(build):
    """Same race, third counter: two simultaneous registrations for one address
    both used to observe 'no existing key' and both proceed."""
    gw = build(
        ALLOW_MULTIPLE_KEYS_PER_EMAIL=False,
        MAX_KEYS_PER_IP=0,
        REGISTER_MAX_PER_IP_PER_DAY=0,
        AUTH_RPM_PER_IP=0,
    )
    codes = _burst(lambda _i: gw.register(email="one@b.com").status_code, 8)

    assert codes.count(201) == 1
    assert codes.count(409) == 7
    assert gw.storage.count_active_keys_for_email("one@b.com") == 1


def test_the_cap_is_not_pushed_further_over_by_rejected_attempts(build):
    """A rejected attempt must not consume allowance. Otherwise a client that
    retries in a loop extends its own lockout, and the counter stops meaning
    'attempts made' — which is what the operator reads it as."""
    gw = build(REGISTER_MAX_PER_IP_PER_DAY=1, AUTH_RPM_PER_IP=0, MAX_KEYS_PER_IP=0,
               ALLOW_MULTIPLE_KEYS_PER_EMAIL=True)
    assert gw.register(email="first@b.com").status_code == 201
    for i in range(5):
        assert gw.register(email=f"later{i}@b.com").status_code == 429
    assert gw.storage.register_attempts_today(gw.storage.hash_ip("testclient")) == 1


# ---------------------------------------------------------------------------
# 21. usage that could not be parsed is still metered
#
# UsageScanner reports parsed=False when the upstream body carried no usage
# object it recognised — a shape change, a body over MAX_USAGE_PARSE_BYTES, a
# truncated stream. Nothing acted on that flag, so the request was recorded with
# zero tokens and zero cost and advanced no budget at all. A caller who can
# provoke an unparseable response reliably had an unmetered channel through a
# service whose entire purpose is metering.
# ---------------------------------------------------------------------------
FLOOR = 4000


def _no_usage_json(gw) -> None:
    gw.upstream.headers = {"content-type": "application/json"}
    gw.upstream.chunks = [b'{"content":[{"text":"a reply with no usage object"}]}']


def test_unparseable_usage_advances_both_the_key_and_the_global_budget(build):
    gw = build(UNPARSED_USAGE_TOKEN_FLOOR=FLOOR)
    key = gw.new_key()
    key_id = parse_key(key)[0]
    _no_usage_json(gw)

    before_cost, before_tokens = gw.spend.current()
    assert gw.call(key).status_code == 200

    # per-key
    day = gw.storage.usage_for_day(key_id)
    assert day.total_tokens == FLOOR, (
        f"an unparseable 200 charged {day.total_tokens} tokens; it must charge the floor"
    )
    assert day.cost_usd > 0

    # global — both the persisted aggregate and the in-process tracker
    assert gw.storage.global_usage_for_month().total_tokens == FLOOR
    after_cost, after_tokens = gw.spend.current()
    assert after_tokens - before_tokens == FLOOR
    assert after_cost > before_cost

    # and it is visible to the user rather than hidden
    usage = gw.client.get("/usage", headers={"Authorization": f"Bearer {key}"}).json()
    assert usage["today"]["total_tokens"] == FLOOR


def test_the_floor_never_overrides_a_usage_report_that_was_parsed(build):
    """The floor is a fallback, not a minimum. A request that really did use
    fewer tokens than the floor must be billed for what it used."""
    gw = build(UNPARSED_USAGE_TOKEN_FLOOR=FLOOR)
    key = gw.new_key()
    gw.upstream.set_json_usage(input_tokens=11, output_tokens=7)
    assert gw.call(key).status_code == 200
    assert gw.storage.usage_for_day(parse_key(key)[0]).total_tokens == 18


def test_a_failed_request_is_not_charged_the_floor(build):
    """4xx/5xx produced no completion and cost the operator nothing upstream.
    Billing a floor for it would turn a provider outage into an invoice and let
    one broken client burn a stranger's allowance by failing in a loop."""
    gw = build(UNPARSED_USAGE_TOKEN_FLOOR=FLOOR)
    key = gw.new_key()
    gw.upstream.headers = {"content-type": "application/json"}
    gw.upstream.chunks = [b'{"error":"upstream said no"}']
    gw.upstream.status = 400

    assert gw.call(key).status_code == 400
    assert gw.storage.usage_for_day(parse_key(key)[0]).total_tokens == 0
    assert gw.spend.current()[1] == 0


def test_an_unparseable_stream_is_charged_too(build):
    """Streaming is the path most likely to end without a usage event — a
    disconnect mid-stream, or a translator that drops the final chunk."""
    gw = build(UNPARSED_USAGE_TOKEN_FLOOR=FLOOR)
    key = gw.new_key()
    gw.upstream.headers = {"content-type": "text/event-stream"}
    gw.upstream.chunks = [b'data: {"type":"content_block_delta"}\n\n', b"data: [DONE]\n\n"]

    with gw.client.stream(
        "POST", "/v1/messages",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": "yangble5", "messages": []},
    ) as response:
        assert response.status_code == 200
        list(response.iter_bytes())

    assert gw.storage.usage_for_day(parse_key(key)[0]).total_tokens == FLOOR


def test_the_floor_pushes_a_key_towards_its_quota(build):
    """The point of charging it: an unmetered path is one a caller can keep
    using forever. With the floor, the daily budget still runs out."""
    gw = build(UNPARSED_USAGE_TOKEN_FLOOR=FLOOR, DAILY_TOKEN_BUDGET=FLOOR * 2)
    key = gw.new_key()
    _no_usage_json(gw)

    assert gw.call(key).status_code == 200
    assert gw.call(key).status_code == 200
    assert gw.call(key).status_code == 429


def test_the_floor_can_be_switched_off_but_says_so(build, tmp_path):
    """0 restores the old unmetered behaviour. That is allowed — an operator may
    have a reason — but it is not allowed to be silent."""
    gw = build(UNPARSED_USAGE_TOKEN_FLOOR=0)
    key = gw.new_key()
    _no_usage_json(gw)
    assert gw.call(key).status_code == 200
    assert gw.storage.usage_for_day(parse_key(key)[0]).total_tokens == 0

    warnings = gw.settings.startup_warnings()
    assert any("UNPARSED_USAGE_TOKEN_FLOOR" in warning for warning in warnings)
    assert not any(
        "UNPARSED_USAGE_TOKEN_FLOOR" in warning
        for warning in Settings.from_env(
            dict(BASE_ENV, DB_PATH=str(tmp_path / "warn.db"))
        ).startup_warnings()
    )


def test_a_negative_floor_is_rejected_rather_than_clamped(tmp_path):
    """A negative charge would run every budget backwards."""
    with pytest.raises(ConfigError):
        Settings.from_env(
            dict(BASE_ENV, DB_PATH=str(tmp_path / "n.db"), UNPARSED_USAGE_TOKEN_FLOOR="-1")
        )
