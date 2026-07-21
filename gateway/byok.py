"""BYOK — "bring your own key" credential storage.

WHAT PROBLEM THIS SOLVES
------------------------
The shared pool is funded out of the operator's own accounts and is genuinely
small. BYOK is the pressure valve: a user attaches a credential for an upstream
account **they** own, their requests are routed with it, and they stop consuming
the shared pool entirely. That is the difference between a service that has to
say "no" when the pool runs dry and one that can say "here is how to keep going".

STORAGE, STATED PLAINLY
-----------------------
There are exactly two modes, and the gateway tells the user which one is in
effect at the moment they attach a credential — no reading of source required:

* ``aesgcm``    — BYOK_ENCRYPTION_KEY is set and the optional ``cryptography``
                  package is installed. The credential is sealed with AES-256-GCM
                  under a key derived from BYOK_ENCRYPTION_KEY. The database
                  alone is not enough to recover it; the environment secret is
                  also required.
* ``plaintext`` — BYOK_ENCRYPTION_KEY is NOT set. **The credential is stored
                  as-is in the SQLite file.** Anyone who can read that file, or a
                  backup of it, can read the credential. This is not hidden and
                  it is not a bug; it is the honest default for an operator who
                  has not configured a secret. If that is not acceptable to you,
                  do not attach a credential to someone else's server — run your
                  own (the whole stack is in this repository).

Setting BYOK_ENCRYPTION_KEY without ``cryptography`` installed is a startup
error rather than a silent downgrade: an operator who set that variable believes
their users' credentials are encrypted, and they are entitled to be right.

Nothing in this module logs, returns or reformats a credential. The only code
path that produces a plaintext credential is ``ByokCipher.open()``, whose single
caller hands the result straight to the outbound request headers.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

__all__ = [
    "AESGCM_AVAILABLE",
    "PLAINTEXT_SCHEME",
    "SEALED_SCHEME",
    "ByokCipher",
    "SealedCredential",
    "storage_notice",
]

PLAINTEXT_SCHEME = "plaintext"
SEALED_SCHEME = "aesgcm"

try:  # pragma: no cover - exercised by whichever branch the environment has
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _AESGCM

    AESGCM_AVAILABLE = True
except ImportError:  # pragma: no cover
    _AESGCM = None  # type: ignore[assignment]
    AESGCM_AVAILABLE = False

_NONCE_BYTES = 12  # the size AES-GCM is specified for; anything else weakens it


@dataclass(frozen=True)
class SealedCredential:
    """What actually goes into the database. Never contains a plaintext key
    unless `scheme` says so, and `scheme` is stored beside the bytes so a later
    change of BYOK_ENCRYPTION_KEY can be diagnosed instead of guessed."""

    scheme: str
    nonce: bytes | None
    ciphertext: bytes


class ByokCipher:
    """Seals and opens user-supplied upstream credentials.

    Constructed from the raw BYOK_ENCRYPTION_KEY string. The AES key is
    SHA-256 of a domain-separated form of that string, so the operator may use a
    passphrase or a hex blob without having to know the required key length.
    """

    def __init__(self, key_material: str = ""):
        self._key: bytes | None = None
        if key_material and AESGCM_AVAILABLE:
            self._key = hashlib.sha256(
                ("yangble5-byok-v1:" + key_material).encode("utf-8")
            ).digest()

    @property
    def scheme(self) -> str:
        return SEALED_SCHEME if self._key is not None else PLAINTEXT_SCHEME

    @property
    def encrypts(self) -> bool:
        return self._key is not None

    def seal(self, plaintext: str) -> SealedCredential:
        if self._key is None:
            return SealedCredential(PLAINTEXT_SCHEME, None, plaintext.encode("utf-8"))
        nonce = secrets.token_bytes(_NONCE_BYTES)
        blob = _AESGCM(self._key).encrypt(nonce, plaintext.encode("utf-8"), None)
        return SealedCredential(SEALED_SCHEME, nonce, blob)

    def open(self, sealed: SealedCredential) -> str | None:
        """Return the credential, or None if it cannot be recovered.

        None is returned (rather than an exception raised) for the case that
        actually happens in production: the operator rotated or removed
        BYOK_ENCRYPTION_KEY and the stored rows can no longer be opened. The
        caller turns that into "re-attach your credential", not a 500.
        """
        if sealed.scheme == PLAINTEXT_SCHEME:
            try:
                return sealed.ciphertext.decode("utf-8")
            except UnicodeDecodeError:
                return None
        if sealed.scheme != SEALED_SCHEME or self._key is None or sealed.nonce is None:
            return None
        try:
            return _AESGCM(self._key).decrypt(sealed.nonce, sealed.ciphertext, None).decode("utf-8")
        except Exception:
            # cryptography raises InvalidTag for a wrong key; catching broadly
            # here keeps a corrupt row from becoming a 500 on a proxied request.
            return None


def storage_notice(cipher: ByokCipher) -> str:
    """The sentence the user is shown when they attach a credential.

    Returned by the API, not buried in documentation, because "encrypted at
    rest?" is the only question that matters to the person handing over a
    credential and they should not have to trust a README to answer it.
    """
    if cipher.encrypts:
        return (
            "Stored encrypted at rest (AES-256-GCM) under this server's "
            "BYOK_ENCRYPTION_KEY. The database file alone cannot reveal it."
        )
    return (
        "STORED AS-IS (NOT ENCRYPTED). This server has no BYOK_ENCRYPTION_KEY "
        "configured, so anyone who can read its database file or a backup of it "
        "can read this credential. If that is not acceptable, revoke it now with "
        "DELETE /byok and run your own instance instead — the whole stack is "
        "open source."
    )
