"""Gateway settings — every value comes from the environment.

WHY env-only: this repo is public. A settings file that *can* hold a secret
eventually *will* hold a secret and get committed. There is no config file
loader here on purpose; use a process manager, a systemd unit, or a .env that
is git-ignored.

WHY fail-fast: the operator is funding this service. A gateway that boots with
public registration enabled and no spend ceiling is a gateway that turns one
viral post into a surprise invoice. `Settings.from_env()` refuses to build such
a configuration instead of warning about it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["DEFAULT_PRICES", "ConfigError", "ModelPrice", "Settings"]

_ENV_PREFIX = "YANGBLE5_"

REGISTRATION_MODES = ("invite", "open", "closed")


class ConfigError(RuntimeError):
    """Raised at startup when the configuration is unsafe or incomplete."""


# ---------------------------------------------------------------------------
# env helpers
# ---------------------------------------------------------------------------
def _raw(env: Mapping[str, str], name: str) -> str | None:
    """Look up NAME, then YANGBLE5_NAME. Empty string counts as unset.

    WHY two spellings: the operator-facing names in the docs are short
    (REGISTRATION_MODE), but a shared machine may need namespacing to avoid
    colliding with another service's variables.
    """
    for candidate in (name, _ENV_PREFIX + name):
        value = env.get(candidate)
        if value is not None and value.strip() != "":
            return value.strip()
    return None


def _str(env: Mapping[str, str], name: str, default: str) -> str:
    value = _raw(env, name)
    return default if value is None else value


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    value = _raw(env, name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    value = _raw(env, name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = _raw(env, name)
    if value is None:
        return default
    lowered = value.lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be a boolean (true/false), got {value!r}")


# ---------------------------------------------------------------------------
# pricing
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ModelPrice:
    """USD per 1,000,000 tokens.

    WHY `cached_input` is a separate field: pricing cached prompt tokens at the
    same rate as fresh ones would erase the entire point of this project. The
    gateway bills what the operator is actually charged, so a session with a
    high prompt-cache hit rate costs the user's quota proportionally less.
    """

    input: float
    cached_input: float
    output: float
    cache_write: float | None = None  # None -> falls back to `input`

    @property
    def cache_write_price(self) -> float:
        return self.input if self.cache_write is None else self.cache_write

    @classmethod
    def from_mapping(cls, name: str, raw: Any) -> ModelPrice:
        if not isinstance(raw, Mapping):
            raise ConfigError(f"price table entry {name!r} must be an object")
        try:
            price = cls(
                input=float(raw["input"]),
                cached_input=float(raw["cached_input"]),
                output=float(raw["output"]),
                cache_write=(
                    float(raw["cache_write"]) if raw.get("cache_write") is not None else None
                ),
            )
        except KeyError as exc:
            raise ConfigError(
                f"price table entry {name!r} is missing required field {exc.args[0]!r} "
                "(need: input, cached_input, output)"
            ) from exc
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"price table entry {name!r} has a non-numeric price") from exc
        for label, value in (
            ("input", price.input),
            ("cached_input", price.cached_input),
            ("output", price.output),
            ("cache_write", price.cache_write_price),
        ):
            if value < 0:
                raise ConfigError(f"price table entry {name!r}: {label} must be >= 0")
        return price


# Deliberately CONSERVATIVE placeholder prices (USD / 1M tokens). These are NOT
# a claim about what any provider charges — they exist so that an operator who
# forgets to configure PRICE_TABLE_JSON over-estimates spend and therefore
# under-spends. Startup logs a warning whenever these are in use.
DEFAULT_PRICES: dict[str, ModelPrice] = {
    "default": ModelPrice(input=5.0, cached_input=0.5, output=15.0),
}


def _load_price_table(env: Mapping[str, str]) -> tuple[dict[str, ModelPrice], bool]:
    """Return (table, is_placeholder)."""
    inline = _raw(env, "PRICE_TABLE_JSON")
    path = _raw(env, "PRICE_TABLE_FILE")
    if inline and path:
        raise ConfigError("set PRICE_TABLE_JSON or PRICE_TABLE_FILE, not both")

    blob: str | None = inline
    if path:
        try:
            blob = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"PRICE_TABLE_FILE {path!r} is not readable: {exc}") from exc
    if not blob:
        return dict(DEFAULT_PRICES), True

    try:
        parsed = json.loads(blob)
    except ValueError as exc:
        raise ConfigError(f"price table is not valid JSON: {exc}") from exc
    if not isinstance(parsed, Mapping) or not parsed:
        raise ConfigError("price table must be a non-empty JSON object")

    table = {str(name): ModelPrice.from_mapping(str(name), raw) for name, raw in parsed.items()}
    if "default" not in table:
        raise ConfigError(
            "price table must contain a 'default' entry — an unpriced model would "
            "otherwise be billed at zero and bypass every cost budget"
        )
    return table, False


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Settings:
    # --- upstream engine -----------------------------------------------------
    engine_url: str
    engine_api_key: str
    engine_management_key: str | None
    upstream_timeout_seconds: float
    upstream_connect_timeout_seconds: float
    # How long a request may wait for a free connection in the httpx pool before
    # it fails. WITHOUT this, httpx applies the (deliberately long) read timeout
    # to the pool-acquire wait too, so a queued request sits for the full
    # UPSTREAM_TIMEOUT_SECONDS before it is even sent. A caller deserves a fast
    # "we are full" instead of a fifteen-minute silence.
    upstream_pool_timeout_seconds: float
    # Hard ceiling on TCP connections to the engine. Sized to the engine, not to
    # the shared account (BYOK traffic uses these connections too).
    upstream_max_connections: int
    # PROCESS-WIDE ceiling on in-flight SHARED-POOL requests. The per-key
    # concurrency limit bounds one key; this bounds the sum across every key,
    # which is what the single upstream account behind the pool actually sees.
    # BYOK traffic is deliberately NOT counted here — byok_instructions promises
    # "no queue behind anyone else" and that promise has to be true.
    upstream_max_concurrency: int
    # Rolling-window upstream failure detector, reported by /pool/status.
    upstream_health_window_seconds: float
    upstream_health_failure_threshold: int

    # --- storage -------------------------------------------------------------
    db_path: str

    # --- registration --------------------------------------------------------
    registration_mode: str
    allow_multiple_keys_per_email: bool
    register_max_per_ip_per_day: int
    max_keys_per_ip: int             # keys ISSUED per IP per UTC day; 0 => unlimited

    # --- machine binding (loose, on purpose) ---------------------------------
    max_ips_per_key: int             # 0 => unlimited
    ip_binding_window_hours: int
    binding_throttle_seconds: int

    # --- budgets -------------------------------------------------------------
    daily_token_budget: int          # 0 => unlimited
    daily_cost_usd_budget: float     # 0 => unlimited
    global_monthly_usd_budget: float  # 0 => unlimited (forbidden in open mode)
    global_monthly_token_budget: int  # 0 => unlimited; second, unit-independent cap
    global_daily_usd_budget: float   # 0 => unlimited; the "today's pool" ceiling
    global_daily_token_budget: int   # 0 => unlimited
    global_budget_warn_ratio: float
    operator_reserve_fraction: float  # bottom slice of the pool reserved for is_operator keys

    # --- BYOK ----------------------------------------------------------------
    byok_enabled: bool
    byok_encryption_key: str
    byok_docs_url: str

    # --- rate limits ---------------------------------------------------------
    rate_limit_rpm: int
    rate_limit_concurrency: int
    auth_rpm_per_ip: int
    auth_fail_lockout_threshold: int
    auth_fail_lockout_seconds: int

    # --- abuse signals -------------------------------------------------------
    abuse_distinct_ip_threshold: int
    abuse_ip_window_hours: int
    abuse_auto_suspend: bool

    # --- crypto --------------------------------------------------------------
    key_hash_scheme: str
    key_pepper: str
    admin_api_key: str | None
    auth_cache_ttl_seconds: int

    # --- transport -----------------------------------------------------------
    max_request_bytes: int
    max_usage_parse_bytes: int
    trust_proxy_headers: bool
    trusted_proxy_hops: int

    # --- metering ------------------------------------------------------------
    # Charged when a SUCCESSFUL response carried no usage report we could parse.
    # 0 restores the old behaviour, in which such a request was free and never
    # advanced any budget — which is a hole, not a feature. See
    # `startup_warnings()` and gateway/app.py::_record.
    unparsed_usage_token_floor: int

    # --- misc ----------------------------------------------------------------
    log_level: str
    prices: dict[str, ModelPrice] = field(default_factory=lambda: dict(DEFAULT_PRICES))
    prices_are_placeholder: bool = True

    # -- derived ---------------------------------------------------------------
    @property
    def registration_open(self) -> bool:
        return self.registration_mode == "open"

    @property
    def has_global_cap(self) -> bool:
        """A MONTHLY operator ceiling exists. Tripping it is a 402 read-only state."""
        return self.global_monthly_usd_budget > 0 or self.global_monthly_token_budget > 0

    @property
    def has_daily_pool_cap(self) -> bool:
        """A DAILY shared-pool ceiling exists. Tripping it is a 429 that clears at
        00:00 UTC, which is a materially friendlier failure than the monthly one."""
        return self.global_daily_usd_budget > 0 or self.global_daily_token_budget > 0

    @property
    def has_any_pool_cap(self) -> bool:
        return self.has_global_cap or self.has_daily_pool_cap

    @property
    def admin_enabled(self) -> bool:
        """/admin/* is served only when a key is configured. WHY: an unset admin
        key must not mean an open admin surface."""
        return bool(self.admin_api_key)

    def usd_budget_token_ceiling(self, usd: float) -> int | None:
        """Most tokens `usd` could possibly buy at the default price row.

        Uses the INPUT rate, which is the cheapest column, so this is an upper
        bound: a real request mixes in output and cache-write tokens at higher
        rates and therefore exhausts the dollar budget sooner. If even this
        optimistic number is below a token budget, the dollar budget is the one
        that binds — and that conclusion cannot be an artefact of the estimate.
        """
        if usd <= 0:
            return None
        rate = self.prices["default"].input
        if rate <= 0:
            return None
        return int(usd / rate * 1_000_000)

    def price_for(self, model: str | None) -> ModelPrice:
        """Exact match, then longest prefix match, then 'default'.

        Prefix matching exists because clients append suffixes to aliases
        (e.g. `yangble5[1m]`) and an operator should not have to enumerate
        every variant to keep cost accounting correct.
        """
        if model:
            exact = self.prices.get(model)
            if exact is not None:
                return exact
            best_name, best_len = None, -1
            for name in self.prices:
                if name != "default" and model.startswith(name) and len(name) > best_len:
                    best_name, best_len = name, len(name)
            if best_name is not None:
                return self.prices[best_name]
        return self.prices["default"]

    # -- construction ----------------------------------------------------------
    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env

        engine_api_key = _raw(env, "ENGINE_API_KEY")
        if not engine_api_key:
            raise ConfigError(
                "YANGBLE5_ENGINE_API_KEY (or ENGINE_API_KEY) is required: it is the "
                "server-side key the gateway injects when calling the internal engine. "
                "Never reuse a key you have handed to a user."
            )

        engine_url = _str(env, "ENGINE_URL", "http://127.0.0.1:8318").rstrip("/")
        if not engine_url.startswith(("http://", "https://")):
            raise ConfigError(f"ENGINE_URL must start with http:// or https://, got {engine_url!r}")

        # REGISTRATION_MODE is authoritative. REGISTRATION_OPEN is a boolean alias
        # honoured only when the tri-state is unset, because deploy/docker-compose
        # ships the boolean spelling. WHY honour it at all: silently ignoring an
        # operator's stated intent is how a config divergence becomes an incident.
        # Mapping it to "open" is safe precisely because "open" then has to clear
        # the budget-ceiling check below, so a mismatch fails loudly at boot.
        if _raw(env, "REGISTRATION_MODE") is None and _raw(env, "REGISTRATION_OPEN") is not None:
            registration_mode = "open" if _bool(env, "REGISTRATION_OPEN", False) else "closed"
        else:
            registration_mode = _str(env, "REGISTRATION_MODE", "invite").lower()
        if registration_mode not in REGISTRATION_MODES:
            raise ConfigError(
                f"REGISTRATION_MODE must be one of {REGISTRATION_MODES}, got {registration_mode!r}"
            )

        # USER_DAILY_TOKENS / GLOBAL_BUDGET_TOKENS are the deploy/.env spellings.
        daily_token_budget = _int(
            env, "DAILY_TOKEN_BUDGET", _int(env, "USER_DAILY_TOKENS", 2_000_000)
        )
        # DEFAULT 0 (= ration in tokens only), and that is a deliberate change of
        # unit rather than a loosening.
        #
        # The old default was $2.00, which at the PLACEHOLDER input price of
        # $5.00/1M is 400,000 tokens — less than a fifth of the 2,000,000-token
        # allowance advertised in the same registration response, and less than
        # ONE request of the 748,918-token size this project exists to serve. So
        # the USD ceiling always bound first, and it bound on a number
        # DEFAULT_PRICES says out loud is "NOT a claim about what any provider
        # charges". Rationing in a unit you cannot state truthfully is not
        # rationing; it is an arbitrary refusal wearing a dollar sign.
        #
        # Tokens are the honest unit here. An operator who has calibrated
        # PRICE_TABLE_JSON can set DAILY_COST_USD_BUDGET and get a meaningful
        # dollar ceiling; until then `startup_warnings()` says what it is made of.
        daily_cost_usd_budget = _float(env, "DAILY_COST_USD_BUDGET", 0.0)
        global_monthly_usd_budget = _float(env, "GLOBAL_MONTHLY_USD_BUDGET", 0.0)
        # A second ceiling in TOKENS. It exists because an operator who has not
        # calibrated a price table cannot express their limit in USD honestly,
        # and a cap you cannot state truthfully is a cap you will not set.
        global_monthly_token_budget = _int(
            env, "GLOBAL_MONTHLY_TOKEN_BUDGET", _int(env, "GLOBAL_BUDGET_TOKENS", 0)
        )
        # The DAILY pool ceiling. Separate from the monthly one because they fail
        # differently: a daily cap says "come back after 00:00 UTC" and a monthly
        # cap says "this instance is done until the 1st". Users deserve to be told
        # which one they hit, so the gateway tracks them independently.
        global_daily_usd_budget = _float(env, "GLOBAL_DAILY_USD_BUDGET", 0.0)
        global_daily_token_budget = _int(env, "GLOBAL_DAILY_TOKEN_BUDGET", 0)
        for label, value in (
            ("DAILY_TOKEN_BUDGET", daily_token_budget),
            ("DAILY_COST_USD_BUDGET", daily_cost_usd_budget),
            ("GLOBAL_MONTHLY_USD_BUDGET", global_monthly_usd_budget),
            ("GLOBAL_MONTHLY_TOKEN_BUDGET", global_monthly_token_budget),
            ("GLOBAL_DAILY_USD_BUDGET", global_daily_usd_budget),
            ("GLOBAL_DAILY_TOKEN_BUDGET", global_daily_token_budget),
        ):
            if value < 0:
                raise ConfigError(f"{label} must be >= 0 (0 means unlimited)")

        # HARD RULE: open registration without an operator spend ceiling is the
        # single most expensive misconfiguration available here, so it is fatal.
        # A DAILY pool ceiling counts as a ceiling too — it bounds the month by
        # construction — so an operator who thinks in days is not forced to
        # restate their limit in months before they are allowed to open the door.
        if registration_mode == "open":
            if (
                global_monthly_usd_budget <= 0
                and global_monthly_token_budget <= 0
                and global_daily_usd_budget <= 0
                and global_daily_token_budget <= 0
            ):
                raise ConfigError(
                    "REGISTRATION_MODE=open requires an operator ceiling: set "
                    "GLOBAL_MONTHLY_USD_BUDGET > 0 (or GLOBAL_MONTHLY_TOKEN_BUDGET > 0 "
                    "if you have not calibrated a price table, or one of the "
                    "GLOBAL_DAILY_* caps). Anyone on the internet could otherwise "
                    "mint keys against your uncapped balance."
                )
            if daily_token_budget <= 0 and daily_cost_usd_budget <= 0:
                raise ConfigError(
                    "REGISTRATION_MODE=open requires a per-key ceiling too: set "
                    "DAILY_TOKEN_BUDGET or DAILY_COST_USD_BUDGET > 0, otherwise one "
                    "account can drain the whole global cap on day one."
                )

        warn_ratio = _float(env, "GLOBAL_BUDGET_WARN_RATIO", 0.9)
        if not 0 < warn_ratio <= 1:
            raise ConfigError("GLOBAL_BUDGET_WARN_RATIO must be in (0, 1]")

        # The operator's own daily driver must not be starved by public traffic.
        # This is the bottom slice of the shared pool that only is_operator keys
        # may spend. 0 disables the reserve; 1 would reserve everything, i.e.
        # close the public service entirely, which is what `closed` mode is for.
        operator_reserve_fraction = _float(env, "OPERATOR_RESERVE_FRACTION", 0.25)
        if not 0 <= operator_reserve_fraction < 1:
            raise ConfigError(
                "OPERATOR_RESERVE_FRACTION must be in [0, 1) — 0 disables the reserve; "
                "reserving the whole pool is REGISTRATION_MODE=closed, not a fraction."
            )

        byok_encryption_key = _str(env, "BYOK_ENCRYPTION_KEY", "")
        if byok_encryption_key:
            # Fail at boot, not at the first attach: an operator who set this
            # variable believes their users' credentials are encrypted at rest,
            # and silently falling back to plaintext would make that belief false.
            from .byok import AESGCM_AVAILABLE

            if not AESGCM_AVAILABLE:
                raise ConfigError(
                    "BYOK_ENCRYPTION_KEY is set but the 'cryptography' package is not "
                    "installed, so BYOK credentials could only be stored in plaintext. "
                    "Install it (pip install 'yangble5[byok]') or unset the variable and "
                    "accept the documented plaintext-at-rest behaviour deliberately."
                )

        key_hash_scheme = _str(env, "KEY_HASH_SCHEME", "scrypt").lower()
        if key_hash_scheme not in ("scrypt", "pbkdf2"):
            raise ConfigError("KEY_HASH_SCHEME must be 'scrypt' or 'pbkdf2'")

        trusted_proxy_hops = _int(env, "TRUSTED_PROXY_HOPS", 1)
        if trusted_proxy_hops < 1:
            raise ConfigError("TRUSTED_PROXY_HOPS must be >= 1")

        # AGGREGATE upstream limits. The per-key limits below bound ONE caller;
        # these bound the sum of all of them, which is the only number the
        # single account behind the shared pool ever experiences. The default is
        # a single digit on purpose: this instance's 1M tier is served by ONE
        # upstream credential, and fifty keys at four in flight each is 200
        # simultaneous requests onto it.
        upstream_max_concurrency = _int(env, "UPSTREAM_MAX_CONCURRENCY", 6)
        if upstream_max_concurrency < 0:
            raise ConfigError("UPSTREAM_MAX_CONCURRENCY must be >= 0 (0 means unlimited)")
        upstream_max_connections = _int(env, "UPSTREAM_MAX_CONNECTIONS", 32)
        if upstream_max_connections < 1:
            raise ConfigError("UPSTREAM_MAX_CONNECTIONS must be >= 1")
        if upstream_max_concurrency > upstream_max_connections:
            # Otherwise the concurrency limiter admits requests the connection
            # pool then queues invisibly, which is the failure this pair exists
            # to make visible.
            raise ConfigError(
                "UPSTREAM_MAX_CONCURRENCY must not exceed UPSTREAM_MAX_CONNECTIONS "
                f"({upstream_max_concurrency} > {upstream_max_connections}); admitted "
                "requests would queue in the connection pool instead of being refused."
            )
        upstream_pool_timeout = _float(env, "UPSTREAM_POOL_TIMEOUT_SECONDS", 15.0)
        if upstream_pool_timeout <= 0:
            raise ConfigError(
                "UPSTREAM_POOL_TIMEOUT_SECONDS must be > 0 — an unbounded pool wait "
                "makes a full connection pool indistinguishable from a hung engine."
            )
        upstream_health_window = _float(env, "UPSTREAM_HEALTH_WINDOW_SECONDS", 120.0)
        if upstream_health_window <= 0:
            raise ConfigError("UPSTREAM_HEALTH_WINDOW_SECONDS must be > 0")
        upstream_health_threshold = _int(env, "UPSTREAM_HEALTH_FAILURE_THRESHOLD", 3)
        if upstream_health_threshold < 1:
            raise ConfigError(
                "UPSTREAM_HEALTH_FAILURE_THRESHOLD must be >= 1 — 0 would mark the "
                "upstream unhealthy before it had failed once."
            )

        # A successful response whose usage report we could not parse used real
        # upstream capacity. Charging it zero makes it invisible to every budget,
        # so a client that can reliably provoke an unparseable response gets an
        # unmetered channel. The floor is the conservative answer: bill SOMETHING
        # so the budget still moves. Negative is rejected rather than clamped —
        # a negative charge would run budgets backwards.
        unparsed_usage_token_floor = _int(env, "UNPARSED_USAGE_TOKEN_FLOOR", 1000)
        if unparsed_usage_token_floor < 0:
            raise ConfigError("UNPARSED_USAGE_TOKEN_FLOOR must be >= 0 (0 disables the floor)")

        prices, placeholder = _load_price_table(env)

        default_db = str(Path(__file__).resolve().parent / "data" / "gateway.db")

        return cls(
            engine_url=engine_url,
            engine_api_key=engine_api_key,
            engine_management_key=_raw(env, "ENGINE_MANAGEMENT_KEY"),
            upstream_timeout_seconds=_float(env, "UPSTREAM_TIMEOUT_SECONDS", 900.0),
            upstream_connect_timeout_seconds=_float(env, "UPSTREAM_CONNECT_TIMEOUT_SECONDS", 10.0),
            upstream_pool_timeout_seconds=upstream_pool_timeout,
            upstream_max_connections=upstream_max_connections,
            upstream_max_concurrency=upstream_max_concurrency,
            upstream_health_window_seconds=upstream_health_window,
            upstream_health_failure_threshold=upstream_health_threshold,
            db_path=_str(env, "DB_PATH", default_db),
            registration_mode=registration_mode,
            allow_multiple_keys_per_email=_bool(env, "ALLOW_MULTIPLE_KEYS_PER_EMAIL", False),
            register_max_per_ip_per_day=_int(env, "REGISTER_MAX_PER_IP_PER_DAY", 5),
            max_keys_per_ip=_int(env, "MAX_KEYS_PER_IP", 3),
            max_ips_per_key=_int(env, "MAX_IPS_PER_KEY", 5),
            ip_binding_window_hours=_int(
                env, "IP_BINDING_WINDOW_HOURS", _int(env, "ABUSE_IP_WINDOW_HOURS", 24)
            ),
            binding_throttle_seconds=_int(env, "BINDING_THROTTLE_SECONDS", 60),
            daily_token_budget=daily_token_budget,
            daily_cost_usd_budget=daily_cost_usd_budget,
            global_monthly_usd_budget=global_monthly_usd_budget,
            global_monthly_token_budget=global_monthly_token_budget,
            global_daily_usd_budget=global_daily_usd_budget,
            global_daily_token_budget=global_daily_token_budget,
            global_budget_warn_ratio=warn_ratio,
            operator_reserve_fraction=operator_reserve_fraction,
            byok_enabled=_bool(env, "BYOK_ENABLED", True),
            byok_encryption_key=byok_encryption_key,
            byok_docs_url=_str(env, "BYOK_DOCS_URL", ""),
            rate_limit_rpm=_int(env, "RATE_LIMIT_RPM", 60),
            rate_limit_concurrency=_int(env, "RATE_LIMIT_CONCURRENCY", 4),
            auth_rpm_per_ip=_int(env, "AUTH_RPM_PER_IP", 10),
            auth_fail_lockout_threshold=_int(env, "AUTH_FAIL_LOCKOUT_THRESHOLD", 8),
            auth_fail_lockout_seconds=_int(env, "AUTH_FAIL_LOCKOUT_SECONDS", 300),
            abuse_distinct_ip_threshold=_int(env, "ABUSE_DISTINCT_IP_THRESHOLD", 8),
            abuse_ip_window_hours=_int(env, "ABUSE_IP_WINDOW_HOURS", 24),
            abuse_auto_suspend=_bool(env, "ABUSE_AUTO_SUSPEND", False),
            key_hash_scheme=key_hash_scheme,
            key_pepper=_str(env, "KEY_PEPPER", ""),
            admin_api_key=_raw(env, "ADMIN_API_KEY") or _raw(env, "ADMIN_KEY"),
            auth_cache_ttl_seconds=_int(env, "AUTH_CACHE_TTL_SECONDS", 300),
            max_request_bytes=_int(env, "MAX_REQUEST_BYTES", 32 * 1024 * 1024),
            max_usage_parse_bytes=_int(env, "MAX_USAGE_PARSE_BYTES", 2 * 1024 * 1024),
            trust_proxy_headers=_bool(env, "TRUST_PROXY_HEADERS", False),
            trusted_proxy_hops=trusted_proxy_hops,
            unparsed_usage_token_floor=unparsed_usage_token_floor,
            log_level=_str(env, "LOG_LEVEL", "INFO").upper(),
            prices=prices,
            prices_are_placeholder=placeholder,
        )

    def startup_warnings(self) -> list[str]:
        """Non-fatal things the operator should see once, at boot."""
        warnings: list[str] = []
        if self.prices_are_placeholder:
            warnings.append(
                "PRICE_TABLE_JSON is not set — using conservative PLACEHOLDER prices. "
                "Cost budgets will not match your real invoice until you set them."
            )
        if not self.has_global_cap:
            warnings.append(
                "GLOBAL_MONTHLY_USD_BUDGET and GLOBAL_MONTHLY_TOKEN_BUDGET are both 0 "
                "(unlimited). There is no operator spend ceiling; only per-key budgets "
                "apply, so total spend scales with the number of keys you have issued."
            )
        # A per-key DOLLAR ceiling is only as honest as the price table under it,
        # and it silently overrides the token allowance the same registration
        # response advertises. Both halves are said out loud.
        if self.daily_cost_usd_budget > 0 and self.prices_are_placeholder:
            warnings.append(
                f"DAILY_COST_USD_BUDGET is ${self.daily_cost_usd_budget:g} but "
                "PRICE_TABLE_JSON is not set, so that ceiling is denominated in "
                "PLACEHOLDER prices and does not correspond to any real invoice. "
                "Ration in tokens (DAILY_TOKEN_BUDGET) until you have calibrated "
                "prices, or set DAILY_COST_USD_BUDGET=0."
            )
        ceiling = self.usd_budget_token_ceiling(self.daily_cost_usd_budget)
        tokens = self.daily_token_budget
        if ceiling is not None and tokens > 0 and ceiling < tokens:
            warnings.append(
                f"DAILY_COST_USD_BUDGET (${self.daily_cost_usd_budget:g}) is reached "
                f"after at most {ceiling:,} tokens at the configured input price, "
                f"which is BELOW DAILY_TOKEN_BUDGET ({self.daily_token_budget:,}). "
                "The dollar ceiling is the one that will actually stop your users, "
                "so do not advertise the token number as their allowance."
            )
        if self.registration_mode == "invite" and not self.admin_enabled:
            warnings.append(
                "REGISTRATION_MODE=invite but no ADMIN_API_KEY is set, so /admin/invites "
                "is disabled and no invite code can be minted. Nobody can register."
            )
        if not self.key_pepper:
            warnings.append(
                "KEY_PEPPER is empty. Key hashes are still salted per key, but a "
                "stolen database would not need an extra secret to attack them."
            )
        if self.byok_enabled and not self.byok_encryption_key:
            warnings.append(
                "BYOK is enabled but BYOK_ENCRYPTION_KEY is empty, so any upstream "
                "credential a user attaches is stored AS-IS (plaintext) in the SQLite "
                "file. Set BYOK_ENCRYPTION_KEY, or set BYOK_ENABLED=false and tell "
                "users to self-host instead. This is stated verbatim to the user at "
                "the moment they attach, so nobody is surprised by it."
            )
        if self.registration_mode == "open" and self.operator_reserve_fraction <= 0:
            warnings.append(
                "OPERATOR_RESERVE_FRACTION is 0 with open registration: public traffic "
                "can consume the entire pool and starve your own daily-driver key."
            )
        if self.trust_proxy_headers:
            warnings.append(
                "TRUST_PROXY_HEADERS is on. Only enable this behind a reverse proxy "
                "that sets X-Real-IP itself (deploy/Caddyfile does), otherwise clients "
                "can spoof per-IP limits by sending their own X-Real-IP."
            )
        if self.unparsed_usage_token_floor <= 0:
            warnings.append(
                "UNPARSED_USAGE_TOKEN_FLOOR is 0, so a successful request whose usage "
                "report could not be parsed is billed nothing and advances no budget. "
                "That is an unmetered path. Set it to a small positive number unless "
                "you have a specific reason not to."
            )
        host = self.engine_url.split("://", 1)[1].split("/", 1)[0].split(":")[0]
        if host not in ("127.0.0.1", "localhost", "::1") and self.engine_url.startswith("http://"):
            warnings.append(
                "ENGINE_URL is a plaintext http:// URL to a non-loopback host — the "
                "engine key would cross the network in the clear. Use https or a tunnel."
            )
        return warnings
