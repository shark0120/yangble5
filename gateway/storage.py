"""SQLite persistence for the gateway (stdlib `sqlite3`, WAL, no ORM).

Design notes worth knowing before you change anything here:

* **Plaintext keys are never stored.** A key looks like `yb5_<key_id>_<secret>`.
  Only `key_id` is stored in the clear; it exists purely so a lookup is O(1).
  The secret is run through scrypt (or PBKDF2) with a per-key random salt and
  an optional server-side pepper, and only the derived bytes are persisted.
  There is no code path in this package that can print a secret after issuance.

* **Every statement is parameterized.** No f-string ever touches SQL values.

* **IP addresses are stored as salted hashes.** Abuse detection only needs to
  count *distinct* IPs per key; keeping the raw addresses would turn this
  database into a location log of everyone who ever used the service. The salt
  (`ip_pepper`) is generated once and kept in the `meta` table, which keeps the
  counts stable across restarts without making the values reversible by
  anyone who merely knows an IP.

* **One connection guarded by a lock.** Calls are sub-millisecond and the
  service is I/O bound on the upstream engine, so a connection pool would add
  failure modes without adding throughput.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

__all__ = [
    "KEY_PREFIX",
    "MACHINE_ID_MAX_CHARS",
    "MACHINE_ID_MIN_CHARS",
    "ApiKeyRecord",
    "DayUsage",
    "EmailInUseError",
    "InviteError",
    "IssuedKey",
    "MachineBinding",
    "RegistrationCapError",
    "Storage",
    "StoredByok",
    "hash_secret",
    "make_key_material",
    "normalize_machine_id",
    "parse_key",
    "pepper_fingerprint",
    "utcnow",
    "verify_secret",
]

KEY_PREFIX = "yb5"
SCHEMA_VERSION = 2

# A machine fingerprint is an OPAQUE sha256 hex digest produced by the installer.
# The gateway never learns what was hashed into it and does not want to: the
# whole point is that it identifies a machine without describing one. Anything
# that is not plain hex of a sane length is rejected outright rather than
# stored, because a fingerprint field that accepts arbitrary text is a free
# user-controlled column in someone else's database.
MACHINE_ID_MIN_CHARS = 16
MACHINE_ID_MAX_CHARS = 64
_HEX_DIGITS = frozenset("0123456789abcdef")


def normalize_machine_id(raw: str | None) -> str | None:
    """Return the canonical (lowercase hex) fingerprint, or None if unusable.

    None means "reject this input" — callers must not fall back to treating an
    invalid fingerprint as an absent one, or the validation would be optional in
    practice.
    """
    if raw is None:
        return None
    candidate = raw.strip().lower()
    if not MACHINE_ID_MIN_CHARS <= len(candidate) <= MACHINE_ID_MAX_CHARS:
        return None
    if len(candidate) % 2 or not _HEX_DIGITS.issuperset(candidate):
        return None
    return candidate

# scrypt cost. n=2**14 with r=8 costs ~16 MiB and tens of milliseconds — enough
# to make an offline attack on a stolen database expensive, and affordable
# online because app.py caches the *result* of a successful verification (see
# ratelimit.AuthCache) instead of re-deriving on every proxied request.
_SCRYPT_N = 1 << 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_PBKDF2_ROUNDS = 600_000


# `datetime.UTC` is 3.11+. It is not a new concept, just a spelling: the stdlib
# defines it as `UTC = timezone.utc`, the same singleton object. Aliasing it here
# keeps this module importable on the 3.10 that ships as the system Python on
# Ubuntu 22.04 LTS, which is what a self-hoster is most likely to run it on.
UTC = timezone.utc


def utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def day_key(moment: datetime | None = None) -> str:
    """UTC calendar day. Budgets reset at 00:00 UTC — one global, unambiguous
    boundary beats trying to guess each user's timezone."""
    return (moment or utcnow()).astimezone(UTC).strftime("%Y-%m-%d")


def month_key(moment: datetime | None = None) -> str:
    return (moment or utcnow()).astimezone(UTC).strftime("%Y-%m")


# ---------------------------------------------------------------------------
# key material
# ---------------------------------------------------------------------------
def make_key_material() -> tuple[str, str, str]:
    """Return (plaintext_key, key_id, secret).

    The key_id is public and appears in logs; the secret never does. 256 bits of
    `secrets.token_urlsafe` entropy means the secret is not guessable, which is
    why a single KDF pass (rather than an interactive-login-grade one) is enough.
    """
    key_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    return f"{KEY_PREFIX}_{key_id}_{secret}", key_id, secret


def parse_key(presented: str) -> tuple[str, str] | None:
    """Split a presented key into (key_id, secret), or None if malformed."""
    if not presented:
        return None
    parts = presented.strip().split("_", 2)
    if len(parts) != 3 or parts[0] != KEY_PREFIX:
        return None
    key_id, secret = parts[1], parts[2]
    if len(key_id) != 16 or not all(c in "0123456789abcdef" for c in key_id) or not secret:
        return None
    return key_id, secret


def pepper_fingerprint(pepper: str) -> str:
    """Short, non-reversible tag recorded next to each key hash.

    WHY: if the operator rotates KEY_PEPPER, every existing key stops verifying.
    Without this tag that failure looks exactly like a wrong key and takes an
    afternoon to debug; with it, the gateway can say what actually happened.
    """
    return hashlib.sha256(("yangble5-pepper:" + pepper).encode("utf-8")).hexdigest()[:12]


def _prehash(secret: str, pepper: str) -> bytes:
    if not pepper:
        return secret.encode("utf-8")
    return hmac.new(pepper.encode("utf-8"), secret.encode("utf-8"), hashlib.sha256).digest()


def hash_secret(secret: str, *, scheme: str = "scrypt", pepper: str = "",
                salt: bytes | None = None) -> tuple[bytes, bytes, str]:
    """Return (digest, salt, scheme_string)."""
    salt = salt or secrets.token_bytes(16)
    material = _prehash(secret, pepper)
    if scheme == "scrypt":
        digest = hashlib.scrypt(
            material, salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
            dklen=_SCRYPT_DKLEN, maxmem=_SCRYPT_MAXMEM,
        )
        return digest, salt, f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_SCRYPT_DKLEN}"
    if scheme == "pbkdf2":
        digest = hashlib.pbkdf2_hmac("sha256", material, salt, _PBKDF2_ROUNDS, dklen=32)
        return digest, salt, f"pbkdf2_sha256${_PBKDF2_ROUNDS}$32"
    raise ValueError(f"unknown key hash scheme {scheme!r}")


def verify_secret(secret: str, digest: bytes, salt: bytes, scheme_string: str,
                  pepper: str = "") -> bool:
    """Constant-time verification against the stored scheme parameters.

    Parameters come from the row, not from config, so keys issued before a
    KDF-parameter change keep working.
    """
    material = _prehash(secret, pepper)
    try:
        head, *rest = scheme_string.split("$")
        if head == "scrypt":
            n, r, p, dklen = (int(x) for x in rest)
            candidate = hashlib.scrypt(
                material, salt=salt, n=n, r=r, p=p, dklen=dklen, maxmem=_SCRYPT_MAXMEM
            )
        elif head == "pbkdf2_sha256":
            rounds, dklen = (int(x) for x in rest)
            candidate = hashlib.pbkdf2_hmac("sha256", material, salt, rounds, dklen=dklen)
        else:
            return False
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(candidate, digest)


# ---------------------------------------------------------------------------
# row types
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ApiKeyRecord:
    key_id: str
    user_id: int
    digest: bytes
    salt: bytes
    scheme: str
    pepper_fp: str
    status: str                      # active | suspended | revoked
    created_at: str
    last_used_at: str | None
    daily_token_budget: int | None   # None -> use the global default
    daily_cost_budget_usd: float | None
    suspended_reason: str | None
    is_operator: bool = False

    @property
    def active(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True)
class IssuedKey:
    """Returned exactly once, at issuance. Never reconstructible afterwards."""

    plaintext: str
    key_id: str
    user_id: int
    created_at: str
    # True when this is a re-issue against an EXISTING key row (same key_id,
    # same usage history, same budgets) rather than a newly minted key.
    reissued: bool = False


@dataclass(frozen=True)
class MachineBinding:
    machine_hash: str
    key_id: str
    created_at: str
    last_seen_at: str
    reissue_count: int


@dataclass(frozen=True)
class StoredByok:
    """A user-supplied upstream credential, still sealed. Decryption lives in
    gateway.byok; Storage deliberately has no way to read one."""

    key_id: str
    scheme: str
    nonce: bytes | None
    ciphertext: bytes
    label: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class DayUsage:
    requests: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0


class InviteError(Exception):
    """Invite code missing, expired, exhausted or revoked."""


class RegistrationCapError(Exception):
    """A per-IP registration ceiling was reached.

    Raised from INSIDE the transaction that would have issued the key, which is
    the only place the decision can be made safely: a check made before the
    transaction is a check a concurrent request can slip past. `count` is what
    the transaction actually saw, so the message quoted to the caller is the
    number that stopped them rather than a number read a moment earlier.
    """

    def __init__(self, count: int, limit: int):
        super().__init__(f"{count} key(s) already issued from this address today (limit {limit})")
        self.count = count
        self.limit = limit


class EmailInUseError(Exception):
    """The address already has an active key.

    Same reasoning as RegistrationCapError: checked inside the issuing
    transaction so that two simultaneous registrations for one address cannot
    both observe "no existing key" and both proceed.
    """


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email      TEXT UNIQUE COLLATE NOCASE,
    label      TEXT,
    status     TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    key_id                TEXT PRIMARY KEY,
    user_id               INTEGER NOT NULL REFERENCES users(id),
    digest                BLOB NOT NULL,
    salt                  BLOB NOT NULL,
    scheme                TEXT NOT NULL,
    pepper_fp             TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'active',
    created_at            TEXT NOT NULL,
    last_used_at          TEXT,
    daily_token_budget    INTEGER,
    daily_cost_budget_usd REAL,
    suspended_reason      TEXT,
    is_operator           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_status ON api_keys(status);

CREATE TABLE IF NOT EXISTS usage_records (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id              TEXT NOT NULL,
    ts                  TEXT NOT NULL,
    day                 TEXT NOT NULL,
    month               TEXT NOT NULL,
    endpoint            TEXT NOT NULL,
    model               TEXT,
    status              INTEGER NOT NULL,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    total_tokens        INTEGER NOT NULL DEFAULT 0,
    cost_usd            REAL NOT NULL DEFAULT 0,
    latency_ms          INTEGER NOT NULL DEFAULT 0,
    streamed            INTEGER NOT NULL DEFAULT 0,
    -- 0 for BYOK traffic: the user paid their own upstream, so the request is
    -- still recorded (they can see it on /usage) but it must never move the
    -- shared pool's counters. Every pool aggregate filters on this column.
    billable            INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_usage_key_day ON usage_records(key_id, day);
CREATE INDEX IF NOT EXISTS idx_usage_key_month ON usage_records(key_id, month);
CREATE INDEX IF NOT EXISTS idx_usage_month ON usage_records(month);
CREATE INDEX IF NOT EXISTS idx_usage_day ON usage_records(day);

CREATE TABLE IF NOT EXISTS invites (
    code_hash  TEXT PRIMARY KEY,
    label      TEXT,
    max_uses   INTEGER NOT NULL DEFAULT 1,
    used_count INTEGER NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'active',
    expires_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ip_observations (
    key_id     TEXT NOT NULL,
    ip_hash    TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    hits       INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (key_id, ip_hash)
);
CREATE INDEX IF NOT EXISTS idx_ipobs_key_last ON ip_observations(key_id, last_seen);

CREATE TABLE IF NOT EXISTS register_attempts (
    ip_hash TEXT NOT NULL,
    day     TEXT NOT NULL,
    count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ip_hash, day)
);

-- One row per machine fingerprint. This is what makes "just re-run the
-- installer" safe: the second run finds this row and re-issues against the
-- SAME key_id instead of minting a second key with a second daily allowance.
CREATE TABLE IF NOT EXISTS machine_bindings (
    machine_hash  TEXT PRIMARY KEY,
    key_id        TEXT NOT NULL,
    first_ip_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    reissue_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_machine_bindings_key ON machine_bindings(key_id);

-- The first-seen IP for each key, and the counter behind MAX_KEYS_PER_IP.
-- Counting ISSUED keys is deliberately different from counting register
-- ATTEMPTS (register_attempts above): a user who fat-fingers an invite code
-- five times has farmed nothing, and should not be treated as if they had.
CREATE TABLE IF NOT EXISTS key_registrations (
    key_id       TEXT PRIMARY KEY,
    ip_hash      TEXT NOT NULL,
    day          TEXT NOT NULL,
    machine_hash TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_key_registrations_ip_day ON key_registrations(ip_hash, day);

CREATE TABLE IF NOT EXISTS byok_credentials (
    key_id     TEXT PRIMARY KEY,
    scheme     TEXT NOT NULL,
    nonce      BLOB,
    ciphertext BLOB NOT NULL,
    label      TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# (table, column, DDL type) added after SCHEMA_VERSION 1 shipped. Applied by
# _migrate() to databases that already exist; the CREATE TABLE statements above
# carry the same columns so a fresh database never needs the migration.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("api_keys", "is_operator", "INTEGER NOT NULL DEFAULT 0"),
    ("usage_records", "billable", "INTEGER NOT NULL DEFAULT 1"),
)


class Storage:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # isolation_level=None -> autocommit; transactions are explicit below so
        # that invite consumption and key creation cannot half-apply.
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            self._migrate()
        self.set_meta_default("schema_version", str(SCHEMA_VERSION))
        self._ip_pepper = self._ensure_ip_pepper()

    def _migrate(self) -> None:
        """Add columns that post-date the first release. Caller holds the lock.

        Deliberately additive only: this database holds the operator's billing
        history, so a migration that could drop or rewrite a column is not a
        migration this project is willing to run unattended.
        """
        for table, column, ddl in _ADDED_COLUMNS:
            existing = {
                row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")
            }
            if column not in existing:
                # Identifiers, not values: they come from the constant above,
                # never from input, so there is nothing here to parameterize.
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    # -- plumbing --------------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _query(self, sql: str, params: Sequence = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _one(self, sql: str, params: Sequence = ()) -> sqlite3.Row | None:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    # -- meta ------------------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self._one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_meta_default(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO NOTHING",
                (key, value),
            )

    def _ensure_ip_pepper(self) -> str:
        existing = self.get_meta("ip_pepper")
        if existing:
            return existing
        generated = secrets.token_hex(16)
        self.set_meta_default("ip_pepper", generated)
        return self.get_meta("ip_pepper") or generated

    def hash_ip(self, ip: str) -> str:
        """Salted, non-reversible IP tag. See module docstring for the rationale."""
        return hashlib.sha256(
            (self._ip_pepper + "|" + (ip or "unknown")).encode("utf-8")
        ).hexdigest()[:32]

    def hash_machine_id(self, machine_id: str) -> str:
        """Salted tag for a machine fingerprint.

        Deterministic, because the whole feature is "look this machine up again".
        Salted with the per-database pepper, because a raw fingerprint table
        would let anyone holding a stolen copy test candidate fingerprints
        against every yangble5 install at once instead of only this one.
        """
        return hashlib.sha256(
            (self._ip_pepper + "|machine|" + machine_id).encode("utf-8")
        ).hexdigest()

    # -- users -----------------------------------------------------------------
    def get_user_by_email(self, email: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email,))

    def count_active_keys_for_email(self, email: str) -> int:
        row = self._one(
            "SELECT COUNT(*) AS n FROM api_keys k JOIN users u ON u.id = k.user_id "
            "WHERE u.email = ? COLLATE NOCASE AND k.status = 'active'",
            (email,),
        )
        return int(row["n"]) if row else 0

    # -- keys ------------------------------------------------------------------
    def issue_key(
        self,
        *,
        email: str | None,
        label: str | None = None,
        scheme: str = "scrypt",
        pepper: str = "",
        daily_token_budget: int | None = None,
        daily_cost_budget_usd: float | None = None,
        is_operator: bool = False,
        machine_hash: str | None = None,
        registration_ip_hash: str | None = None,
        max_keys_per_ip_per_day: int = 0,
        enforce_unique_email: bool = False,
        moment: datetime | None = None,
    ) -> IssuedKey:
        """Create user (if needed) + key. The plaintext is returned once and is
        not written anywhere — not to the database, not to the log.

        The machine binding and the registration record are written in the SAME
        transaction as the key. A key that exists without its binding would be
        re-mintable from the same fingerprint, which is precisely the farming
        hole the binding is there to close.

        The two CEILINGS are enforced in that same transaction, for the same
        reason `consume_invite` claims a use with the precondition in its WHERE
        clause. `max_keys_per_ip_per_day` and `enforce_unique_email` used to be
        checked by the caller and acted on afterwards, with `await` points in
        between; a burst of simultaneous registrations all read the same stale
        count, all decided they were under the cap, and all got a key. Under
        BEGIN IMMEDIATE only one writer holds the database at a time, so the
        count each one reads already includes every key its predecessors wrote.

        0 / False mean "no ceiling", matching the settings that feed them.
        """
        plaintext, key_id, secret = make_key_material()
        digest, salt, scheme_string = hash_secret(secret, scheme=scheme, pepper=pepper)
        moment = moment or utcnow()
        created = _iso(moment)
        with self._tx() as conn:
            if max_keys_per_ip_per_day > 0 and registration_ip_hash is not None:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM key_registrations WHERE ip_hash = ? AND day = ?",
                    (registration_ip_hash, day_key(moment)),
                ).fetchone()
                issued_here = int(row["n"]) if row else 0
                if issued_here >= max_keys_per_ip_per_day:
                    raise RegistrationCapError(issued_here, max_keys_per_ip_per_day)
            if enforce_unique_email and email:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM api_keys k JOIN users u ON u.id = k.user_id"
                    " WHERE u.email = ? COLLATE NOCASE AND k.status = 'active'",
                    (email,),
                ).fetchone()
                if row and int(row["n"]) > 0:
                    raise EmailInUseError(email)

            user_id: int | None = None
            if email:
                row = conn.execute(
                    "SELECT id FROM users WHERE email = ? COLLATE NOCASE", (email,)
                ).fetchone()
                if row:
                    user_id = int(row["id"])
            if user_id is None:
                cur = conn.execute(
                    "INSERT INTO users(email, label, status, created_at) VALUES(?, ?, 'active', ?)",
                    (email, label, created),
                )
                user_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO api_keys(key_id, user_id, digest, salt, scheme, pepper_fp, status,"
                " created_at, daily_token_budget, daily_cost_budget_usd, is_operator)"
                " VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)",
                (key_id, user_id, digest, salt, scheme_string, pepper_fingerprint(pepper),
                 created, daily_token_budget, daily_cost_budget_usd, int(is_operator)),
            )
            if registration_ip_hash is not None:
                conn.execute(
                    "INSERT INTO key_registrations(key_id, ip_hash, day, machine_hash, created_at)"
                    " VALUES(?, ?, ?, ?, ?)",
                    (key_id, registration_ip_hash, day_key(moment), machine_hash, created),
                )
            if machine_hash is not None:
                conn.execute(
                    "INSERT INTO machine_bindings(machine_hash, key_id, first_ip_hash,"
                    " created_at, last_seen_at, reissue_count) VALUES(?, ?, ?, ?, ?, 0)",
                    (machine_hash, key_id, registration_ip_hash or "unknown", created, created),
                )
        return IssuedKey(plaintext=plaintext, key_id=key_id, user_id=user_id, created_at=created)

    def reissue_key_secret(
        self, key_id: str, *, scheme: str = "scrypt", pepper: str = ""
    ) -> IssuedKey | None:
        """Mint a fresh secret for an EXISTING key row. Returns None if unknown.

        WHY this rather than returning the original string: the original string
        does not exist any more. Only a salted, peppered KDF digest of it was
        ever stored, and there is no code path in this package that can turn
        that back into a key — which is the property that makes a stolen
        database useless, and it is not one worth trading away for convenience.

        So "idempotent re-registration" is idempotent in the dimension that
        protects the operator: the same fingerprint keeps the same `key_id`, the
        same usage history, the same daily allowance and the same operator flag.
        Nothing new is minted. The previous key STRING stops working, which is
        also the correct answer for a cloned fingerprint used from two machines.
        """
        # Keep the original key_id so usage rows, budgets and bindings still
        # point at this key; only the secret half of the credential changes.
        secret = secrets.token_urlsafe(32)
        plaintext = f"{KEY_PREFIX}_{key_id}_{secret}"
        digest, salt, scheme_string = hash_secret(secret, scheme=scheme, pepper=pepper)
        with self._tx() as conn:
            row = conn.execute(
                "SELECT user_id, created_at FROM api_keys WHERE key_id = ?", (key_id,)
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE api_keys SET digest = ?, salt = ?, scheme = ?, pepper_fp = ?"
                " WHERE key_id = ?",
                (digest, salt, scheme_string, pepper_fingerprint(pepper), key_id),
            )
            user_id = int(row["user_id"])
            created_at = row["created_at"]
        return IssuedKey(
            plaintext=plaintext, key_id=key_id, user_id=user_id,
            created_at=created_at, reissued=True,
        )

    # -- machine bindings ------------------------------------------------------
    def get_machine_binding(self, machine_hash: str) -> MachineBinding | None:
        row = self._one(
            "SELECT * FROM machine_bindings WHERE machine_hash = ?", (machine_hash,)
        )
        if row is None:
            return None
        return MachineBinding(
            machine_hash=row["machine_hash"],
            key_id=row["key_id"],
            created_at=row["created_at"],
            last_seen_at=row["last_seen_at"],
            reissue_count=int(row["reissue_count"]),
        )

    def touch_machine_binding(self, machine_hash: str, moment: datetime | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE machine_bindings SET last_seen_at = ?, reissue_count = reissue_count + 1"
                " WHERE machine_hash = ?",
                (_iso(moment or utcnow()), machine_hash),
            )

    def count_keys_issued_from_ip(self, ip_hash: str, day: str | None = None) -> int:
        row = self._one(
            "SELECT COUNT(*) AS n FROM key_registrations WHERE ip_hash = ? AND day = ?",
            (ip_hash, day or day_key()),
        )
        return int(row["n"]) if row else 0

    def get_key(self, key_id: str) -> ApiKeyRecord | None:
        row = self._one("SELECT * FROM api_keys WHERE key_id = ?", (key_id,))
        if row is None:
            return None
        return ApiKeyRecord(
            key_id=row["key_id"],
            user_id=int(row["user_id"]),
            digest=bytes(row["digest"]),
            salt=bytes(row["salt"]),
            scheme=row["scheme"],
            pepper_fp=row["pepper_fp"],
            status=row["status"],
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            daily_token_budget=row["daily_token_budget"],
            daily_cost_budget_usd=row["daily_cost_budget_usd"],
            suspended_reason=row["suspended_reason"],
            is_operator=bool(row["is_operator"]),
        )

    def set_key_operator(self, key_id: str, is_operator: bool) -> bool:
        """Flag a key as the operator's own. Operator keys are the only ones
        allowed to spend the reserve slice of the pool."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE api_keys SET is_operator = ? WHERE key_id = ?",
                (int(is_operator), key_id),
            )
        return cur.rowcount > 0

    def touch_key(self, key_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE api_keys SET last_used_at = ? WHERE key_id = ?", (_iso(utcnow()), key_id)
            )

    def set_key_status(self, key_id: str, status: str, reason: str | None = None) -> bool:
        if status not in ("active", "suspended", "revoked"):
            raise ValueError(f"invalid key status {status!r}")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE api_keys SET status = ?, suspended_reason = ? WHERE key_id = ?",
                (status, reason, key_id),
            )
        return cur.rowcount > 0

    def list_keys(self, limit: int = 200) -> list[sqlite3.Row]:
        return self._query(
            "SELECT k.key_id, k.status, k.created_at, k.last_used_at, k.is_operator, u.email,"
            " EXISTS(SELECT 1 FROM byok_credentials b WHERE b.key_id = k.key_id) AS has_byok"
            " FROM api_keys k JOIN users u ON u.id = k.user_id"
            " ORDER BY k.created_at DESC LIMIT ?",
            (limit,),
        )

    # -- usage -----------------------------------------------------------------
    def record_usage(
        self,
        *,
        key_id: str,
        endpoint: str,
        model: str | None,
        status: int,
        input_tokens: int,
        cached_input_tokens: int,
        cache_write_tokens: int,
        output_tokens: int,
        total_tokens: int,
        cost_usd: float,
        latency_ms: int,
        streamed: bool,
        billable: bool = True,
        moment: datetime | None = None,
    ) -> None:
        moment = moment or utcnow()
        with self._lock:
            self._conn.execute(
                "INSERT INTO usage_records(key_id, ts, day, month, endpoint, model, status,"
                " input_tokens, cached_input_tokens, cache_write_tokens, output_tokens,"
                " total_tokens, cost_usd, latency_ms, streamed, billable)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (key_id, _iso(moment), day_key(moment), month_key(moment), endpoint, model,
                 status, input_tokens, cached_input_tokens, cache_write_tokens, output_tokens,
                 total_tokens, cost_usd, latency_ms, int(streamed), int(billable)),
            )

    _USAGE_SELECT = (
        "SELECT COUNT(*) AS requests, COALESCE(SUM(total_tokens), 0) AS tokens,"
        " COALESCE(SUM(cost_usd), 0) AS cost FROM usage_records"
    )

    @staticmethod
    def _usage_row(row: sqlite3.Row | None) -> DayUsage:
        if row is None:
            return DayUsage()
        return DayUsage(int(row["requests"]), int(row["tokens"]), float(row["cost"]))

    def usage_for_day(
        self, key_id: str, day: str | None = None, *, billable_only: bool = False
    ) -> DayUsage:
        """One key's usage today.

        `billable_only` is what the per-key daily budget asks for: a BYOK user
        spent their own upstream quota, so charging it against the allowance
        they get from the shared pool would be billing them twice. The default
        is False so /usage still shows a user everything they actually did.
        """
        clause = " AND billable = 1" if billable_only else ""
        return self._usage_row(
            self._one(
                f"{self._USAGE_SELECT} WHERE key_id = ? AND day = ?{clause}",
                (key_id, day or day_key()),
            )
        )

    def usage_for_month(self, key_id: str, month: str | None = None) -> DayUsage:
        return self._usage_row(
            self._one(
                f"{self._USAGE_SELECT} WHERE key_id = ? AND month = ?",
                (key_id, month or month_key()),
            )
        )

    def global_cost_for_month(self, month: str | None = None) -> float:
        row = self._one(
            "SELECT COALESCE(SUM(cost_usd), 0) AS cost FROM usage_records"
            " WHERE month = ? AND billable = 1",
            (month or month_key(),),
        )
        return float(row["cost"]) if row else 0.0

    def global_usage_for_month(self, month: str | None = None) -> DayUsage:
        """Operator-wide totals for the month, backing the hard global cap.

        Returns tokens as well as cost so the cap can be enforced even when the
        operator has not calibrated a price table (see GLOBAL_MONTHLY_TOKEN_BUDGET).
        BYOK rows are excluded: they did not touch the operator's accounts.
        """
        return self._usage_row(
            self._one(
                f"{self._USAGE_SELECT} WHERE month = ? AND billable = 1",
                (month or month_key(),),
            )
        )

    def global_usage_for_day(self, day: str | None = None) -> DayUsage:
        """Operator-wide totals for today — the shared pool's daily ceiling and
        the denominator behind /pool/status."""
        return self._usage_row(
            self._one(
                f"{self._USAGE_SELECT} WHERE day = ? AND billable = 1",
                (day or day_key(),),
            )
        )

    # -- invites ---------------------------------------------------------------
    def _invite_hash(self, code: str) -> str:
        # Deterministic (so it is lookup-able) but salted with the per-database
        # pepper so a leaked table cannot be replayed against another install.
        return hashlib.sha256(
            (self._ip_pepper + "|invite|" + code.strip()).encode("utf-8")
        ).hexdigest()

    def create_invite(self, code: str, *, label: str | None = None, max_uses: int = 1,
                      expires_at: datetime | None = None) -> str:
        if max_uses < 1:
            raise ValueError("max_uses must be >= 1")
        code_hash = self._invite_hash(code)
        with self._lock:
            self._conn.execute(
                "INSERT INTO invites(code_hash, label, max_uses, used_count, status,"
                " expires_at, created_at) VALUES(?, ?, ?, 0, 'active', ?, ?)",
                (code_hash, label, max_uses,
                 _iso(expires_at) if expires_at else None, _iso(utcnow())),
            )
        return code_hash

    def consume_invite(self, code: str, moment: datetime | None = None) -> str:
        """Atomically claim one use. Raises InviteError on any rejection.

        The UPDATE carries every precondition in its WHERE clause, so two
        simultaneous registrations cannot both claim the last use of a code.
        """
        now = _iso(moment or utcnow())
        code_hash = self._invite_hash(code)
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE invites SET used_count = used_count + 1"
                " WHERE code_hash = ? AND status = 'active' AND used_count < max_uses"
                "   AND (expires_at IS NULL OR expires_at > ?)",
                (code_hash, now),
            )
            if cur.rowcount == 0:
                raise InviteError("invite code is invalid, expired, revoked or already used")
        return code_hash

    def revoke_invite(self, code: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE invites SET status = 'revoked' WHERE code_hash = ?",
                (self._invite_hash(code),),
            )
        return cur.rowcount > 0

    # -- BYOK credentials ------------------------------------------------------
    def put_byok(
        self,
        key_id: str,
        *,
        scheme: str,
        nonce: bytes | None,
        ciphertext: bytes,
        label: str | None = None,
        moment: datetime | None = None,
    ) -> None:
        """Attach (or replace) one user-supplied upstream credential.

        Storage only. This class has no cipher and no key material, so it can
        neither read what it is holding nor be tricked into logging it.
        """
        now = _iso(moment or utcnow())
        with self._lock:
            self._conn.execute(
                "INSERT INTO byok_credentials(key_id, scheme, nonce, ciphertext, label,"
                " created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(key_id) DO UPDATE SET scheme = excluded.scheme,"
                " nonce = excluded.nonce, ciphertext = excluded.ciphertext,"
                " label = excluded.label, updated_at = excluded.updated_at",
                (key_id, scheme, nonce, ciphertext, label, now, now),
            )

    def get_byok(self, key_id: str) -> StoredByok | None:
        row = self._one("SELECT * FROM byok_credentials WHERE key_id = ?", (key_id,))
        if row is None:
            return None
        return StoredByok(
            key_id=row["key_id"],
            scheme=row["scheme"],
            nonce=bytes(row["nonce"]) if row["nonce"] is not None else None,
            ciphertext=bytes(row["ciphertext"]),
            label=row["label"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def delete_byok(self, key_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM byok_credentials WHERE key_id = ?", (key_id,))
        return cur.rowcount > 0

    # -- abuse signals ---------------------------------------------------------
    def observe_ip(self, key_id: str, ip_hash: str, moment: datetime | None = None) -> bool:
        """Record a (key, ip) sighting. Returns True if this IP is new for the key.

        Callers use the return value to avoid running the distinct-IP COUNT on
        every single request.
        """
        now = _iso(moment or utcnow())
        with self._lock:
            self._conn.execute(
                "INSERT INTO ip_observations(key_id, ip_hash, first_seen, last_seen, hits)"
                " VALUES(?, ?, ?, ?, 1)"
                " ON CONFLICT(key_id, ip_hash) DO UPDATE SET last_seen = excluded.last_seen,"
                " hits = hits + 1",
                (key_id, ip_hash, now, now),
            )
        # rowcount is 1 for both INSERT and DO UPDATE, so ask the table instead.
        row = self._one(
            "SELECT hits FROM ip_observations WHERE key_id = ? AND ip_hash = ?", (key_id, ip_hash)
        )
        return bool(row and int(row["hits"]) == 1)

    def distinct_ip_count(self, key_id: str, window_hours: int) -> int:
        since = _iso(utcnow() - timedelta(hours=max(1, window_hours)))
        row = self._one(
            "SELECT COUNT(*) AS n FROM ip_observations WHERE key_id = ? AND last_seen >= ?",
            (key_id, since),
        )
        return int(row["n"]) if row else 0

    def prune_ip_observations(self, older_than_hours: int) -> int:
        cutoff = _iso(utcnow() - timedelta(hours=max(1, older_than_hours)))
        with self._lock:
            cur = self._conn.execute("DELETE FROM ip_observations WHERE last_seen < ?", (cutoff,))
        return cur.rowcount

    # -- registration throttling ----------------------------------------------
    def bump_register_attempt(self, ip_hash: str, day: str | None = None) -> int:
        """Unconditional +1, returning the new count.

        Delegates rather than duplicating the SQL: a second copy of "read the
        count, then write it" is exactly how the ceiling got raced in the first
        place, and a copy that no longer gates on anything is the copy a future
        caller would reach for. Gate with `claim_register_attempt`.
        """
        _, count = self.claim_register_attempt(ip_hash, 0, day)
        return count

    def register_attempts_today(self, ip_hash: str, day: str | None = None) -> int:
        """Read-only. Fine for a cheap early reject; NOT the authoritative check —
        use `claim_register_attempt` for anything that must hold under load."""
        row = self._one(
            "SELECT count FROM register_attempts WHERE ip_hash = ? AND day = ?",
            (ip_hash, day or day_key()),
        )
        return int(row["count"]) if row else 0

    def claim_register_attempt(
        self, ip_hash: str, max_per_day: int, day: str | None = None
    ) -> tuple[bool, int]:
        """Check the daily attempt ceiling and consume one, atomically.

        Returns (claimed, count). When `claimed` is False the counter was NOT
        incremented — being over the cap must not push you further over it, or a
        client retrying in a loop would extend its own lockout indefinitely.

        The check and the increment share one BEGIN IMMEDIATE, so N simultaneous
        registrations from one address see N distinct counts rather than N copies
        of the same stale one. That is the whole point: a read followed by a
        write with an `await` between them is not a ceiling, it is a suggestion.

        `max_per_day <= 0` means unlimited; the attempt is still counted, because
        the counter is also what the operator reads to spot a farm.
        """
        day = day or day_key()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT count FROM register_attempts WHERE ip_hash = ? AND day = ?",
                (ip_hash, day),
            ).fetchone()
            current = int(row["count"]) if row else 0
            if max_per_day > 0 and current >= max_per_day:
                return False, current
            conn.execute(
                "INSERT INTO register_attempts(ip_hash, day, count) VALUES(?, ?, 1)"
                " ON CONFLICT(ip_hash, day) DO UPDATE SET count = count + 1",
                (ip_hash, day),
            )
        return True, current + 1
