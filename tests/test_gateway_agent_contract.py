"""The contract between an AI agent and this gateway.

The intended way to obtain a key is for an agent — Claude Code, Codex — to
install this and then interview its user. That only works if the service is
*discoverable* and *unambiguous* through its HTTP surface alone, so everything
here defends one of those two properties:

  * discovery      — GET /auth/register describes itself; /health names a
                     support channel; a 404 says what IS served.
  * unambiguity    — one error envelope on every status; an error that names the
                     field it is about; two different failures that need two
                     different remedies never share a message.

Every test in this file was watched failing against the code as it was before,
with the message naming the real failure mode. Where that is not obvious from
the assertion, the comment says what the old behaviour was.

No network, no engine, no real key.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import gateway.app
from gateway.app import create_app
from gateway.config import ConfigError, Settings
from gateway.storage import Storage, parse_key

ENGINE_KEY = "sk-engine-test-only-not-a-real-key"
ADMIN_KEY = "admin-test-only-not-a-real-key"
USER_CREDENTIAL = "sk-user-own-upstream-test-only-not-a-real-key"
MACHINE_A = "a1b2c3d4" * 8
MACHINE_B = "f0e1d2c3" * 8

BASE_ENV = {
    "ENGINE_API_KEY": ENGINE_KEY,
    "ADMIN_API_KEY": ADMIN_KEY,
    # pbkdf2 keeps the KDF affordable in a test suite; production is scrypt.
    "KEY_HASH_SCHEME": "pbkdf2",
    "REGISTRATION_MODE": "open",
    "GLOBAL_MONTHLY_USD_BUDGET": "100",
}


class Harness:
    def __init__(self, settings, storage, client):
        self.settings = settings
        self.storage = storage
        self.client = client

    def register(self, **body):
        return self.client.post("/auth/register", json=body)

    def new_key(self, **body):
        body.setdefault("machine_id", MACHINE_A)
        response = self.register(**body)
        assert response.status_code in (200, 201), response.text
        return response.json()["api_key"]

    def auth(self, key):
        return {"Authorization": f"Bearer {key}"}

    def admin(self, method, path, **kwargs):
        return self.client.request(
            method, path, headers={"Authorization": f"Bearer {ADMIN_KEY}"}, **kwargs
        )

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
        app = create_app(settings=settings, storage=storage, upstream=_NullUpstream())
        harness = Harness(settings, storage, TestClient(app))
        created.append(harness)
        return harness

    yield _build

    for harness in created:
        harness.close()


class _NullUpstream:
    """Never called by anything in this file; present so create_app does not
    build a real httpx client and open sockets."""

    def stream(self, *args, **kwargs):  # pragma: no cover - never reached
        raise AssertionError("no test in this file proxies a request")

    async def aclose(self):
        return None


@pytest.fixture
def gw(build):
    return build()


# ---------------------------------------------------------------------------
# 1. discovery: GET /auth/register
# ---------------------------------------------------------------------------
def test_get_auth_register_serves_the_contract_unauthenticated(gw):
    """Before: 405 Method Not Allowed, so an agent had nowhere to learn the
    field names from the service itself and had to guess or trust a document
    describing a different deployment."""
    response = gw.client.get("/auth/register")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["endpoint"] == {"method": "POST", "path": "/auth/register"}
    assert set(body["fields"]) == {"machine_id", "email", "invite_code", "label"}
    for name, spec in body["fields"].items():
        assert isinstance(spec["required"], bool), f"{name} does not state whether it is required"
        assert spec["format"], f"{name} does not state its format"


def test_contract_states_the_machine_id_rule_and_says_to_keep_the_salt(gw):
    machine_id = gw.client.get("/auth/register").json()["fields"]["machine_id"]
    assert "sha256" in machine_id["derivation"]
    assert "salt" in machine_id["derivation"]
    # The single most expensive thing an installer can get wrong: without a
    # persisted salt every run is a new machine, which mints a second key and
    # strands the first one's history.
    persist = machine_id["persist_the_salt"].lower()
    assert "never sent" in persist
    for phrase in ("second key", "lose it"):
        assert phrase in persist
    assert str(gw.settings.registration_mode) == "open"


def test_contract_field_requirements_track_the_registration_mode(build):
    """Derived from Settings, not restated — an invite-only instance must say
    so, or an agent interviews the user for the wrong things."""
    invite = build(REGISTRATION_MODE="invite")
    body = invite.client.get("/auth/register").json()
    assert body["registration_mode"] == "invite"
    assert body["fields"]["invite_code"]["required"] is True
    assert "invite_code" in body["requirement"]

    open_mode = build(REGISTRATION_MODE="open")
    body = open_mode.client.get("/auth/register").json()
    assert body["fields"]["invite_code"]["required"] is False
    assert "machine_id" in body["requirement"] and "email" in body["requirement"]


def test_contract_limits_come_from_settings(build):
    gw = build(REGISTER_MAX_PER_IP_PER_DAY=7, MAX_KEYS_PER_IP=2, AUTH_RPM_PER_IP=11)
    limits = gw.client.get("/auth/register").json()["limits"]
    assert limits["register_attempts_per_ip_per_day"] == 7
    assert limits["keys_issued_per_ip_per_day"] == 2
    assert limits["requests_per_minute_per_ip"] == 11


def test_contract_reports_unlimited_rather_than_zero(build):
    """`0` means unlimited in the settings. Publishing the raw 0 would read as
    'you may not register at all' to anything that does not know that."""
    gw = build(REGISTER_MAX_PER_IP_PER_DAY=0, MAX_KEYS_PER_IP=0)
    limits = gw.client.get("/auth/register").json()["limits"]
    assert limits["register_attempts_per_ip_per_day"] == "unlimited"
    assert limits["keys_issued_per_ip_per_day"] == "unlimited"


def test_contract_documents_every_error_type_the_endpoint_can_emit(gw):
    documented = set(gw.client.get("/auth/register").json()["error_types"])
    # Provoke a representative sample and check each one was announced.
    provoked = {
        gw.client.post("/auth/register", content=b"nonsense").json()["error"]["type"],
        gw.register(machine_id="not-hex").json()["error"]["type"],
        gw.register().json()["error"]["type"],
        gw.client.post(
            "/auth/register", content=b"x" * 70_000, headers={"content-type": "application/json"}
        ).json()["error"]["type"],
    }
    assert provoked <= documented, f"undocumented error types: {provoked - documented}"


def test_the_contract_omits_no_error_type_the_code_can_raise(gw):
    """The direction the probe test structurally cannot check.

    `provoked <= documented` catches a type that is raised but not written
    down. It can never catch the opposite -- a type the code raises on a path
    no probe happens to reach -- because a smaller `provoked` only makes the
    subset assertion easier. `internal_error` was reachable from two places and
    absent from the document, and that test was green throughout.

    So this one reads the source. It walks the AST and collects the literal
    type passed to `_error(...)` inside the registration functions THEMSELVES,
    rather than grepping the whole module and subtracting an exclusion list.

    That distinction is the test. A whole-module grep needs a list of "types
    other routes raise", and the cheapest way to make such a test green is to
    add the offending type to the list -- which is how a guard quietly stops
    guarding. Scoping by function has no such lever: to silence it you have to
    either document the type or stop raising it.
    """
    tree = ast.parse(Path(gateway.app.__file__).read_text(encoding="utf-8"))

    # The functions that answer POST /auth/register. Nested inside create_app,
    # so a name walk is the reliable way to find them.
    REGISTER_FUNCS = {"register", "_reissue_for_machine"}
    raised: set[str] = set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in REGISTER_FUNCS:
            continue
        found.add(node.name)
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            if not (isinstance(call.func, ast.Name) and call.func.id == "_error"):
                continue
            if (
                len(call.args) >= 2
                and isinstance(call.args[1], ast.Constant)
                and isinstance(call.args[1].value, str)
            ):
                raised.add(call.args[1].value)

    assert found == REGISTER_FUNCS, (
        f"could not find {sorted(REGISTER_FUNCS - found)} in gateway/app.py; this "
        f"test is scanning nothing and would pass no matter what the code raises"
    )
    assert raised, "no _error(...) calls found in the registration path -- scan is broken"

    documented = set(gw.client.get("/auth/register").json()["error_types"])
    missing = raised - documented
    assert not missing, (
        f"gateway/app.py can emit {sorted(missing)} but the contract at "
        f"GET /auth/register does not document them. An agent that branches on "
        f"error.type meets a value the service told it did not exist."
    )


def test_contract_does_not_advertise_live_capacity(gw):
    """A contract that changes between two reads is not a contract. Live
    capacity has an endpoint and the document points at it."""
    body = gw.client.get("/auth/register").json()
    assert "/pool/status" in body["capacity"]
    assert "remaining_pct" not in json.dumps(body["fields"])


def test_contract_is_stable_across_reads(gw):
    first = gw.client.get("/auth/register").json()
    gw.new_key(machine_id=MACHINE_B)
    assert gw.client.get("/auth/register").json() == first


# ---------------------------------------------------------------------------
# 2. validation errors that name the field
# ---------------------------------------------------------------------------
def test_wrong_field_type_names_the_field_and_does_not_claim_the_body_is_not_json(gw):
    """Before: {"machine_id": 123} answered "Body must be a JSON object." — a
    sentence that is FALSE about a body that plainly is one, and that sends the
    reader to check their serialiser instead of their field type."""
    response = gw.register(machine_id=123)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["param"] == "machine_id"
    assert "machine_id" in error["message"]
    assert "must be a JSON object" not in error["message"]
    assert error["errors"][0]["code"] == "string_type"


def test_a_body_that_really_is_not_json_says_so_with_a_distinct_type(gw):
    response = gw.client.post(
        "/auth/register", content=b"{{{", headers={"content-type": "application/json"}
    )
    assert response.status_code == 400
    error = response.json()["error"]
    # A DIFFERENT type from a field problem: the remedies differ (fix the
    # serialiser vs. fix one field) and a client must be able to branch.
    assert error["type"] == "invalid_json"
    assert "not valid JSON" in error["message"]


def test_empty_body_is_reported_as_empty(gw):
    response = gw.client.post(
        "/auth/register", content=b"", headers={"content-type": "application/json"}
    )
    assert response.json()["error"]["type"] == "invalid_json"
    assert "empty" in response.json()["error"]["message"]


def test_non_object_json_names_the_type_actually_sent(gw):
    response = gw.client.post("/auth/register", json=[1, 2, 3])
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert "array" in error["message"]
    assert error["param"] is None


def test_constraint_violation_reports_the_limit_it_broke(gw):
    response = gw.register(machine_id=MACHINE_A, label="x" * 101)
    error = response.json()["error"]
    assert error["param"] == "label"
    assert error["errors"][0]["constraint"] == {"max_length": 100}


def test_several_bad_fields_are_all_reported(gw):
    response = gw.register(machine_id=123, email=[], label=7)
    params = {entry["param"] for entry in response.json()["error"]["errors"]}
    assert params == {"machine_id", "email", "label"}


def test_a_rejected_credential_is_never_echoed_back(build):
    """The offending value on /byok is somebody's upstream credential. Pydantic
    puts it in `input`; this service must not put it in a response body, a
    terminal, or a shell history."""
    gw = build()
    key = gw.new_key()
    response = gw.client.post(
        "/byok", headers=gw.auth(key), json={"credential": "sk-" + "S" * 9000}
    )
    assert response.status_code == 400
    assert "S" * 20 not in response.text
    assert response.json()["error"]["param"] == "credential"


def test_byok_and_admin_invites_use_the_same_validation_shape(build):
    """The collapse was a property of the class, not of one endpoint."""
    gw = build()
    key = gw.new_key()
    byok = gw.client.post("/byok", headers=gw.auth(key), json={"credential": 5})
    assert byok.json()["error"]["param"] == "credential"
    invite = gw.admin("POST", "/admin/invites", json={"max_uses": 0})
    assert invite.json()["error"]["param"] == "max_uses"
    assert byok.json()["error"]["type"] == invite.json()["error"]["type"]


def test_oversized_body_on_a_small_endpoint_is_refused_before_parsing(gw):
    response = gw.client.post(
        "/auth/register",
        content=b'{"label":"' + b"x" * 200_000 + b'"}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["error"]["type"] == "request_too_large"


def test_valid_registration_still_works(gw):
    """The parsing rewrite must not have broken the happy path."""
    response = gw.register(machine_id=MACHINE_A, email="a@b.com", label="my laptop")
    assert response.status_code == 201, response.text
    assert parse_key(response.json()["api_key"]) is not None


# ---------------------------------------------------------------------------
# 3. one envelope on every status
# ---------------------------------------------------------------------------
def test_unknown_path_carries_the_error_envelope(gw):
    """Before: Starlette's {"detail": "Not Found"}. A client reading
    error.type got None and could not tell a wrong URL from a refusal."""
    response = gw.client.get("/definitely-not-a-route")
    assert response.status_code == 404
    body = response.json()
    assert "detail" not in body
    assert body["error"]["type"] == "not_found"


def test_wrong_method_carries_the_envelope_and_names_the_allowed_methods(gw):
    response = gw.client.get("/v1/messages")
    assert response.status_code == 405
    body = response.json()
    assert "detail" not in body
    assert body["error"]["type"] == "method_not_allowed"
    assert body["error"]["allowed_methods"] == ["POST"]
    # Starlette's own Allow header must survive being wrapped.
    assert "POST" in response.headers["allow"]


def test_a_404_hands_back_the_route_index_it_reads_off_the_router(gw):
    routes = gw.client.get("/nope").json()["error"]["public_routes"]
    served = {entry["path"]: entry["methods"] for entry in routes}
    assert served["/auth/register"] == ["GET", "POST"]
    assert "POST" in served["/v1/messages"]
    assert "/health" in served


def test_the_route_index_never_names_the_admin_surface(build):
    """/admin/* answers 404 to an unauthenticated caller precisely so a scanner
    cannot learn it is there. An index that listed it would undo that."""
    gw = build()
    text = gw.client.get("/nope").text
    assert "/admin" not in text
    # And the admin surface itself still answers with the envelope, not a leak.
    denied = gw.client.get("/admin/keys")
    assert denied.status_code == 404
    assert denied.json()["error"]["type"] == "not_found"


def test_every_status_this_service_emits_uses_one_shape(build):
    gw = build(REGISTRATION_MODE="closed")
    probes = [
        gw.client.get("/definitely-not-a-route"),
        gw.client.get("/v1/messages"),
        gw.client.post("/auth/register", json={}),
        gw.client.get("/usage"),
        gw.client.get("/usage", headers=gw.auth("yb5_" + "0" * 16 + "_wrong")),
    ]
    for response in probes:
        assert response.status_code >= 400
        body = response.json()
        assert set(body) >= {"error"}, body
        assert isinstance(body["error"].get("type"), str)
        assert isinstance(body["error"].get("message"), str)
        assert "detail" not in body


def test_an_unhandled_exception_answers_the_envelope_and_leaks_nothing(build, monkeypatch):
    gw = build()
    client = TestClient(gw.client.app, raise_server_exceptions=False)

    def boom(*args, **kwargs):
        raise RuntimeError("secret path /var/lib/yangble5/gw.db")

    monkeypatch.setattr(gw.storage, "global_usage_for_month", boom)
    monkeypatch.setattr(gw.storage, "global_usage_for_day", boom)
    gw.client.app.state.gateway.spend.invalidate()
    response = client.get("/pool/status")
    assert response.status_code == 500
    assert response.json()["error"]["type"] == "internal_error"
    assert "RuntimeError" not in response.text
    assert "gw.db" not in response.text
    client.close()


# ---------------------------------------------------------------------------
# 4. a superseded key is not a wrong key
# ---------------------------------------------------------------------------
def test_a_key_rotated_out_by_re_registration_gets_its_own_error_type(gw):
    """Before: the identical "Invalid yangble5 key." as a random string, so a
    user could not tell "someone re-registered my machine" from "I typed it
    wrong" and re-typed a key that was never coming back."""
    first = gw.new_key(machine_id=MACHINE_A)
    second = gw.register(machine_id=MACHINE_A)
    assert second.status_code == 200 and second.json()["reused"] is True

    stale = gw.client.get("/usage", headers=gw.auth(first))
    assert stale.status_code == 401
    error = stale.json()["error"]
    assert error["type"] == "key_superseded"
    assert error["superseded_at"]
    assert "re-registered" in error["message"]
    # And the replacement really does work, so the advice in the message is true.
    current = gw.client.get("/usage", headers=gw.auth(second.json()["api_key"]))
    assert current.status_code == 200


def test_the_new_type_is_not_a_key_id_existence_oracle(gw):
    """The branch is on the SECRET, never on "does this key_id exist". A prober
    who guesses a real key_id with a junk secret must get the byte-identical
    answer they get for a key_id that was never issued."""
    gw.new_key(machine_id=MACHINE_A)
    real_key_id = gw.register(machine_id=MACHINE_A).json()["key_id"]

    probe_real = gw.client.get("/usage", headers=gw.auth(f"yb5_{real_key_id}_guessed-secret"))
    probe_fake = gw.client.get("/usage", headers=gw.auth("yb5_" + "0" * 16 + "_guessed-secret"))
    assert probe_real.status_code == probe_fake.status_code == 401
    assert probe_real.json() == probe_fake.json()
    assert probe_real.json()["error"]["type"] == "authentication_error"


def test_only_one_generation_back_is_distinguishable(gw):
    """Stated in the code as a limit; asserted here so it stays a known limit
    rather than becoming a surprise."""
    oldest = gw.new_key(machine_id=MACHINE_A)
    gw.register(machine_id=MACHINE_A)
    gw.register(machine_id=MACHINE_A)
    response = gw.client.get("/usage", headers=gw.auth(oldest))
    assert response.json()["error"]["type"] == "authentication_error"


def test_a_superseded_key_still_counts_as_a_failed_attempt(build):
    """It is an invalid credential presented to an unauthenticated surface. Not
    counting it would carve a retry channel out of the backoff."""
    gw = build(AUTH_FAIL_LOCKOUT_THRESHOLD=2, AUTH_FAIL_LOCKOUT_SECONDS=300)
    stale = gw.new_key(machine_id=MACHINE_A)
    gw.register(machine_id=MACHINE_A)
    assert gw.client.get("/usage", headers=gw.auth(stale)).json()["error"]["type"] == (
        "key_superseded"
    )
    gw.client.get("/usage", headers=gw.auth(stale))
    locked = gw.client.get("/usage", headers=gw.auth(stale))
    assert locked.json()["error"]["type"] == "too_many_auth_failures"


# ---------------------------------------------------------------------------
# 5. a support channel that exists
# ---------------------------------------------------------------------------
def test_health_publishes_the_support_contact(build):
    gw = build(SUPPORT_CONTACT="support@example.test")
    assert gw.client.get("/health").json()["support_contact"] == "support@example.test"


def test_health_reports_null_when_no_contact_is_configured(build):
    """Not "", not "unknown", not a placeholder address: the honest rendering of
    "the operator published nothing" is nothing, and a client can say so."""
    gw = build()
    assert gw.client.get("/health").json()["support_contact"] is None


def test_ask_the_operator_messages_carry_the_channel(build):
    gw = build(SUPPORT_CONTACT="ops@example.test", ALLOW_MULTIPLE_KEYS_PER_EMAIL=False)
    gw.register(machine_id=MACHINE_A, email="taken@example.test")
    response = gw.register(machine_id=MACHINE_B, email="taken@example.test")
    assert response.status_code == 409
    assert "ops@example.test" in response.json()["error"]["message"]
    assert response.json()["error"]["support_contact"] == "ops@example.test"


def test_those_messages_degrade_honestly_when_it_is_unset(build):
    """Before: "Ask the operator to revoke it" with no way to. Now the sentence
    says the instance publishes no channel, instead of implying one exists."""
    gw = build()
    gw.register(machine_id=MACHINE_A, email="taken@example.test")
    response = gw.register(machine_id=MACHINE_B, email="taken@example.test")
    message = response.json()["error"]["message"]
    assert "publishes no support contact" in message
    assert response.json()["error"]["support_contact"] is None
    # It still names a real remedy rather than leaving the user at a dead end.
    assert "github.com/shark0120/yangble5" in message


def test_a_suspended_key_is_told_who_can_lift_it(build):
    gw = build(SUPPORT_CONTACT="ops@example.test")
    key = gw.new_key(machine_id=MACHINE_A)
    key_id = parse_key(key)[0]
    gw.storage.set_key_status(key_id, "suspended", "testing")
    response = gw.client.get("/usage", headers=gw.auth(key))
    assert response.status_code == 403
    assert "ops@example.test" in response.json()["error"]["message"]


def test_the_contract_document_also_carries_the_contact(build):
    gw = build(SUPPORT_CONTACT="ops@example.test")
    body = gw.client.get("/auth/register").json()
    assert body["support_contact"] == "ops@example.test"
    assert "ops@example.test" in body["support"]


def test_support_contact_with_control_characters_is_refused_at_boot():
    """It is quoted verbatim into error bodies and into /health, both of which
    are printed to a terminal by installers."""
    env = dict(BASE_ENV, DB_PATH=":memory:", SUPPORT_CONTACT="ops@x.test\r\nX-Injected: 1")
    with pytest.raises(ConfigError, match="control characters"):
        Settings.from_env(env)


def test_an_unset_contact_is_a_startup_warning_not_a_silence(build):
    gw = build()
    assert any("SUPPORT_CONTACT" in w for w in gw.settings.startup_warnings())
    quiet = build(SUPPORT_CONTACT="ops@example.test")
    assert not any("SUPPORT_CONTACT" in w for w in quiet.settings.startup_warnings())


# ---------------------------------------------------------------------------
# 6. /pool/status says what it means
# ---------------------------------------------------------------------------
def test_pool_status_no_longer_publishes_the_ambiguous_capped_field(gw):
    """Before: "capped": true on a pool that was 100% free, because it meant "a
    ceiling is configured". A client branching on it read a full pool as an
    exhausted one."""
    body = gw.client.get("/pool/status").json()
    assert "capped" not in body
    assert body["pool_ceiling_configured"] is True
    assert body["remaining_pct"] == 1.0
    assert body["accepting_requests"] is True


def test_pool_status_reports_no_ceiling_honestly(build):
    gw = build(REGISTRATION_MODE="invite", GLOBAL_MONTHLY_USD_BUDGET=0)
    body = gw.client.get("/pool/status").json()
    assert body["pool_ceiling_configured"] is False
    # Which is what makes remaining_pct 1.0 readable: nothing is being rationed.
    assert body["remaining_pct"] == 1.0


def test_the_docstring_names_the_field_to_gate_on(gw):
    """The finding asked for the docstring to say which single field a caller
    should gate on. Asserted, because an unasserted docstring is a comment."""
    route = next(
        r for r in gw.client.app.routes if getattr(r, "path", None) == "/pool/status"
    )
    doc = route.endpoint.__doc__ or ""
    assert "accepting_requests" in doc
    assert "GATE ON" in doc


# ---------------------------------------------------------------------------
# 7. label is readable, or it would not be asked for
# ---------------------------------------------------------------------------
def test_label_is_returned_at_registration_and_readable_afterwards(gw):
    """Before: accepted, written to users.label, and returned by nothing —
    including admin. An interview must never ask a human for a value that
    nothing can read back."""
    created = gw.register(machine_id=MACHINE_A, label="Ada's laptop")
    assert created.status_code == 201
    assert created.json()["label"] == "Ada's laptop"

    key = created.json()["api_key"]
    assert gw.client.get("/usage", headers=gw.auth(key)).json()["label"] == "Ada's laptop"
    rows = gw.admin("GET", "/admin/keys").json()["keys"]
    assert rows[0]["label"] == "Ada's laptop"


def test_a_second_key_on_one_email_keeps_its_own_label(build):
    """The label used to live on `users`, one row per e-mail address, so two
    keys for one address shared a nickname and the second registration's label
    was dropped entirely."""
    gw = build(ALLOW_MULTIPLE_KEYS_PER_EMAIL=True, MAX_KEYS_PER_IP=5)
    gw.register(machine_id=MACHINE_A, email="a@b.test", label="laptop")
    second = gw.register(machine_id=MACHINE_B, email="a@b.test", label="desktop")
    assert second.status_code == 201, second.text
    assert second.json()["label"] == "desktop"
    labels = {row["label"] for row in gw.admin("GET", "/admin/keys").json()["keys"]}
    assert labels == {"laptop", "desktop"}


def test_re_registration_keeps_the_label_when_none_is_sent(gw):
    """Re-running an installer that carries no label must not erase the
    nickname the user typed the first time."""
    gw.register(machine_id=MACHINE_A, label="Ada's laptop")
    again = gw.register(machine_id=MACHINE_A)
    assert again.status_code == 200
    assert again.json()["label"] == "Ada's laptop"


def test_re_registration_can_update_the_label(gw):
    gw.register(machine_id=MACHINE_A, label="old name")
    again = gw.register(machine_id=MACHINE_A, label="new name")
    assert again.json()["label"] == "new name"
    key = again.json()["api_key"]
    assert gw.client.get("/usage", headers=gw.auth(key)).json()["label"] == "new name"


def test_a_label_with_control_characters_is_refused_not_silently_stripped(gw):
    """It is displayed back in a terminal, on /usage and to the operator."""
    response = gw.register(machine_id=MACHINE_A, label="laptop\x1b[2Jwiped")
    assert response.status_code == 400
    assert response.json()["error"]["param"] == "label"


def test_no_label_reads_back_as_null_rather_than_an_empty_string(gw):
    created = gw.register(machine_id=MACHINE_A)
    assert created.json()["label"] is None
    key = created.json()["api_key"]
    assert gw.client.get("/usage", headers=gw.auth(key)).json()["label"] is None


def test_the_contract_tells_an_agent_the_label_is_readable(gw):
    spec = gw.client.get("/auth/register").json()["fields"]["label"]
    assert "/usage" in spec["purpose"]
    # And warns against the one value it must never be derived from.
    assert "machine_id" in spec["purpose"]


def test_the_gateway_never_derives_a_label_from_the_fingerprint(build):
    """site/install.sh used to send `installer-<first 32 of the fingerprint>`,
    which put half of a value this service otherwise only stores hashed into
    the clear, in a column nothing could read. The installer stopped; this
    asserts the SERVER does not reintroduce it, and that the legacy users.label
    column is no longer written."""
    import sqlite3

    gw = build()
    response = gw.register(machine_id=MACHINE_A)
    assert response.status_code == 201
    conn = sqlite3.connect(gw.settings.db_path)
    try:
        values = [
            str(value)
            for table in ("users", "api_keys")
            for row in conn.execute(f"SELECT * FROM {table}")  # noqa: S608 - fixed names
            for value in row
        ]
    finally:
        conn.close()
    dump = "\n".join(values)
    assert MACHINE_A not in dump
    assert MACHINE_A[:32] not in dump
    assert "installer-" not in dump
    # The legacy users.label column is no longer written at all.
    assert [row[0] for row in _users_labels(gw)] == [None]


def test_a_label_derived_from_the_fingerprint_is_refused(gw):
    """Making the label readable is what justifies asking for one; it also means
    whatever is sent is now stored in the clear next to a fingerprint this
    service otherwise only keeps hashed. The rule the contract states is
    enforced, not merely documented."""
    response = gw.register(machine_id=MACHINE_A, label=f"installer-{MACHINE_A[:32]}")
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["param"] == "label"
    assert "machine_id" in error["message"]


def test_an_ordinary_nickname_is_not_caught_by_that_rule(gw):
    for name in ("Ada's laptop", "work-macbook-2", "deadbeef", "office pc #3"):
        response = gw.client.post(
            "/auth/register", json={"machine_id": MACHINE_A, "label": name}
        )
        assert response.status_code in (200, 201), (name, response.text)
        assert response.json()["label"] == name


def _users_labels(gw):
    import sqlite3

    conn = sqlite3.connect(gw.settings.db_path)
    try:
        return list(conn.execute("SELECT label FROM users"))
    finally:
        conn.close()


def test_an_existing_database_migrates_without_losing_anything(tmp_path):
    """The deployed instance already has a database at the previous schema. The
    five new columns are added by ALTER TABLE, which must leave the keys in it
    working — a migration that invalidates live credentials is an outage."""
    import sqlite3

    from gateway.storage import hash_secret, make_key_material, verify_secret

    # The api_keys shape as it shipped, written out literally: this is the table
    # the live instance's file actually has, and pinning it here is what makes
    # the assertion below about migration rather than about today's schema.
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE COLLATE NOCASE,
            label TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL
        );
        CREATE TABLE api_keys (
            key_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            digest BLOB NOT NULL,
            salt BLOB NOT NULL,
            scheme TEXT NOT NULL,
            pepper_fp TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            last_used_at TEXT,
            daily_token_budget INTEGER,
            daily_cost_budget_usd REAL,
            suspended_reason TEXT,
            is_operator INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    plaintext, key_id, secret = make_key_material()
    digest, salt, scheme_string = hash_secret(secret, scheme="pbkdf2")
    conn.execute(
        "INSERT INTO users(email, label, status, created_at)"
        " VALUES('early@b.test', 'a legacy label', 'active', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO api_keys(key_id, user_id, digest, salt, scheme, pepper_fp, status,"
        " created_at) VALUES(?, 1, ?, ?, ?, 'x', 'active', '2026-01-01T00:00:00+00:00')",
        (key_id, digest, salt, scheme_string),
    )
    conn.commit()
    conn.close()

    reopened = Storage(path)
    try:
        record = reopened.get_key(key_id)
        assert record is not None
        assert record.label is None                      # the new column, empty
        assert record.superseded_credential is None      # not three stray Nones
        assert record.superseded_at is None
        # The key issued before the migration still verifies. A migration that
        # invalidates live credentials is an outage, not an upgrade.
        assert verify_secret(secret, record.digest, record.salt, record.scheme)
        assert parse_key(plaintext) == (key_id, secret)
        # And the new behaviour works on the migrated row.
        again = reopened.reissue_key_secret(key_id, scheme="pbkdf2", label="renamed")
        assert again is not None
        migrated = reopened.get_key(key_id)
        assert migrated.superseded_credential is not None
        assert migrated.label == "renamed"
        assert [row["label"] for row in reopened.list_keys()] == ["renamed"]
    finally:
        reopened.close()


def test_a_byok_label_is_still_readable_too(build):
    gw = build()
    key = gw.new_key()
    gw.client.post(
        "/byok", headers=gw.auth(key), json={"credential": USER_CREDENTIAL, "label": "my account"}
    )
    assert gw.client.get("/byok", headers=gw.auth(key)).json()["label"] == "my account"
