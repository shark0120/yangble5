#!/usr/bin/env python3
"""BYOK setup for yangble5: turn your own upstream account into a cache-correct proxy.

WHY THIS EXISTS
---------------
Getting a free-tier upstream account takes a few minutes and anyone can do it.
What is genuinely hard to get is the CONFIGURATION: the 1:1 model alias that
does not silently destroy the upstream prompt cache, ``fill-first`` routing,
session affinity with a long TTL, the client-side context unlock, and a way to
verify all of it actually worked. That configuration is the product. This script
hands it to you in one command.

WHAT IT DOES, IN ORDER
----------------------
1. Explains what will happen. Nothing is uploaded, anywhere, ever -- there is no
   network call in this script except the local verification benchmark against
   your own engine on 127.0.0.1.
2. Walks you through getting your own upstream credential. API-key providers are
   read from an environment variable or a hidden prompt. OAuth providers are NOT
   automated: browser automation of somebody's login is both fragile and rude, so
   we print the exact steps and then detect the token file the engine wrote.
3. Renders ``byok/config.template.yaml`` into a real engine config OUTSIDE this
   repository, with the cache-preserving settings baked in and an alias that is
   structurally incapable of becoming a rotating model pool.
4. Wires an ISOLATED Claude Code config directory and an isolated Codex
   ``CODEX_HOME`` -- including ``CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000`` and
   ``model_context_window = 1000000`` -- without touching your existing setup.
   Every write backs up whatever was there first; nothing is ever clobbered.
5. Runs ``tools/cache_bench.py`` against your engine and prints the REAL measured
   hit rate. If it comes back low it prints a diagnostic checklist instead of
   congratulating you.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
* It never puts a credential on the command line. argv is world-readable on most
  systems and lands in shell history.
* It never writes a credential into this repository. The default output
  directory is ``~/.yangble5/byok``.
* It does not install, download or start CLIProxyAPI. That is a third-party MIT
  Go project (https://github.com/router-for-me/CLIProxyAPI) and supplying it is
  your decision, not this script's.
* It does not claim your setup works until a benchmark says so.

Standard library only, so this file can be copied onto a machine and run with
the system Python. Python 3.11+.

EXIT CODES
----------
    0  configuration written; verification passed, or was skipped on purpose
    1  configuration written, but verification came back below target
    2  nothing was written (bad input, aborted, or a missing prerequisite)
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
TEMPLATE_PATH = HERE / "config.template.yaml"
CACHE_BENCH = REPO_ROOT / "tools" / "cache_bench.py"

DEFAULT_ALIAS = "yangble5"
DEFAULT_ENGINE_HOST = "127.0.0.1"
DEFAULT_ENGINE_PORT = 8318
DEFAULT_SHIM_PORT = 8320
DEFAULT_SESSION_TTL = "12h"
DEFAULT_MAX_CONTEXT = 1_000_000
DEFAULT_MAX_OUTPUT = 65_536
DEFAULT_OAUTH_CHANNEL = "antigravity"

# Read the upstream credential from here rather than from a flag. See module
# docstring: argv is not a private channel.
UPSTREAM_KEY_ENV = "YANGBLE5_UPSTREAM_KEY"
LOCAL_KEY_ENV = "YANGBLE5_API_KEY"
BASE_URL_ENV = "YANGBLE5_BASE_URL"

# Provider kinds we can configure. The value is the top-level config key that
# carries the credential, which is also what decides where the alias goes.
PROVIDER_GEMINI = "gemini-api-key"
PROVIDER_OPENAI_COMPAT = "openai-compat"
PROVIDER_OAUTH = "oauth"
PROVIDER_KINDS = (PROVIDER_GEMINI, PROVIDER_OPENAI_COMPAT, PROVIDER_OAUTH)

# Channels that `oauth-model-alias` understands, per the CLIProxyAPI 7.1.23
# example config. Anything else silently fails to route.
OAUTH_CHANNELS = (
    "gemini-cli",
    "vertex",
    "aistudio",
    "antigravity",
    "claude",
    "codex",
    "kimi",
    "xai",
)

# Sensible upstream model defaults per kind. These are STARTING POINTS: model
# names change without notice and the only authority is your provider's own
# model list. setup asks you to confirm.
DEFAULT_MODELS = {
    PROVIDER_GEMINI: "gemini-2.5-pro",
    PROVIDER_OPENAI_COMPAT: "gpt-4o-mini",
    PROVIDER_OAUTH: "gemini-pro-agent",
}

# Verification defaults. 60K is a deliberate compromise: large enough that the
# upstream's cache granularity is not the dominant term (at ~30K the measured
# rate does not reach 99% even on a perfectly configured stack), small enough
# that a first-time user is not billed for a 750K-token round trip to find out
# their config is right.
DEFAULT_BENCH_PREFIX_TOKENS = 60_000
DEFAULT_BENCH_ROUNDS = 3
DEFAULT_BENCH_TARGET = 0.80

ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-\[\]]{0,63}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/:]{0,127}$")
PROVIDER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
TTL_RE = re.compile(r"^[0-9]{1,6}(ms|s|m|h)$")
PLACEHOLDER_RE = re.compile(r"__([A-Z0-9_]+)__")

# Substrings that mean "you pasted the documentation, not your key". This is a
# typo guard, not a validity check -- only the upstream can say whether a key is
# real, and this script never asks it.
KEY_LOOKS_FAKE = (
    "your-api-key",
    "your_api_key",
    "yourapikey",
    "replace_me",
    "replace-me",
    "changeme",
    "placeholder",
    "example",
    "...",
)

BACKUP_SUFFIX = ".yb5bak-"

DIAGNOSTIC_CHECKLIST = (
    "Is the alias listed exactly ONCE in the engine config? Two entries sharing an",
    "  alias make a pool, and the pool rotates upstreams per request via a global",
    "  counter (conductor.go nextModelPoolOffset in 7.1.23) that ignores both",
    "  routing.strategy and session-affinity. That alone caps the rate near 1/N.",
    "Is routing.strategy 'fill-first'? round-robin spreads one conversation across",
    "  credentials, and the upstream cache is per credential.",
    "Is routing.session-affinity true, with a TTL longer than your break between",
    "  requests? An expired binding is a cold write that looks like a cache bug.",
    "Did the engine restart between rounds? The affinity table is in memory.",
    "Is the prefix simply too small? Hit rate is prefix-size dependent because the",
    "  uncached tail is roughly constant: we measured 99.53% at a 749K prefix and",
    "  94.00% at 91K on the same stack. Re-run with --bench-prefix-tokens 200000",
    "  before concluding anything is broken.",
    "Did the upstream report ANY cached tokens at all? If every round says 0, this",
    "  upstream either does not expose cache accounting on this path or does not",
    "  cache below its minimum size. That is an upstream property, not a config bug,",
    "  and it should be reported as 'not achievable here' rather than tuned around.",
    "Is your client compacting? Claude Code assumes 200K for model names it does",
    "  not recognise and rewrites the prompt when it thinks it is full -- and a",
    "  rewritten prefix is a cache miss by construction.",
)


class SetupError(Exception):
    """Anything that should stop the run with a readable message and exit 2."""


class TemplateError(SetupError):
    """The template and the values handed to it do not agree."""


class AliasPoolError(SetupError):
    """An alias was about to be mapped to more than one upstream model.

    This is its own exception type because it is the one mistake this whole
    project exists to prevent, and a test asserts that it is raised.
    """


# --------------------------------------------------------------------------
# Pure layer. Everything above the I/O section takes plain data and returns
# plain data, which is what makes the interesting parts testable offline.
# --------------------------------------------------------------------------


def validate_alias(raw: str) -> str:
    """Validate a client-visible model alias.

    Also a YAML-injection guard: the config is assembled by templating, so a
    value containing a newline or a quote could otherwise invent config keys.
    Every interpolated value goes through a validator like this one AND through
    :func:`yaml_quote`; either alone would be enough, which is the point.
    """
    value = (raw or "").strip()
    if not value:
        raise SetupError("alias must not be empty (try: yangble5)")
    if not ALIAS_RE.match(value):
        raise SetupError(
            f"invalid alias {value!r}: use letters, digits, and . _ - [ ] only "
            f"(max 64 chars). Spaces, quotes and newlines are rejected because the "
            f"alias is interpolated into YAML."
        )
    return value


def validate_model_name(raw: str) -> str:
    """Validate an UPSTREAM model name. Allows ``/`` and ``:`` (``vendor/model:free``)."""
    value = (raw or "").strip()
    if not value:
        raise SetupError("upstream model name must not be empty")
    if not MODEL_RE.match(value):
        raise SetupError(
            f"invalid model name {value!r}: use letters, digits, and . _ - / : only (max 128 chars)"
        )
    return value


def validate_provider_name(raw: str) -> str:
    """Validate the local label for an openai-compatibility provider block."""
    value = (raw or "").strip().lower()
    if not value:
        raise SetupError("provider name must not be empty (try: byok)")
    if not PROVIDER_NAME_RE.match(value):
        raise SetupError(
            f"invalid provider name {value!r}: lowercase letters, digits, and . _ - only"
        )
    return value


def validate_channel(raw: str) -> str:
    """Validate an OAuth channel name against the set the engine actually knows."""
    value = (raw or "").strip().lower()
    if value not in OAUTH_CHANNELS:
        raise SetupError(
            f"unknown OAuth channel {value!r}. CLIProxyAPI 7.1.23 routes aliases for: "
            f"{', '.join(OAUTH_CHANNELS)}"
        )
    return value


def validate_port(raw: str | int) -> int:
    """Validate a TCP port."""
    try:
        port = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise SetupError(f"invalid port {raw!r}: not a number") from exc
    if not 1 <= port <= 65535:
        raise SetupError(f"invalid port {port}: must be between 1 and 65535")
    return port


def validate_ttl(raw: str) -> str:
    """Validate a Go-style duration for ``session-affinity-ttl`` (e.g. ``12h``)."""
    value = (raw or "").strip().lower()
    if not TTL_RE.match(value):
        raise SetupError(
            f"invalid session TTL {value!r}: use a Go duration such as 30m, 12h, 3600s"
        )
    return value


def validate_base_url(raw: str) -> str:
    """Validate an upstream base URL. http(s) only; trailing slash stripped."""
    value = (raw or "").strip()
    if not value:
        raise SetupError("base URL must not be empty")
    if any(ch.isspace() for ch in value) or any(ord(ch) < 0x20 for ch in value):
        raise SetupError(f"invalid base URL {value!r}: contains whitespace or control characters")
    if not value.lower().startswith(("http://", "https://")):
        raise SetupError(f"invalid base URL {value!r}: must start with http:// or https://")
    return value.rstrip("/")


def base_url_warnings(url: str) -> list[str]:
    """Non-fatal things worth saying out loud about an upstream base URL.

    Kept separate from validation because none of these should stop a setup: a
    plaintext endpoint on your own LAN is a legitimate choice, it just should
    not be a silent one when an API key is about to travel over it.
    """
    warnings: list[str] = []
    lowered = url.lower()
    host = lowered.split("://", 1)[1].split("/", 1)[0].split("@")[-1].split(":")[0]
    if lowered.startswith("http://") and host not in ("127.0.0.1", "localhost", "::1", "[::1]"):
        warnings.append(
            f"{url} is plaintext HTTP to a non-loopback host: your API key will cross "
            f"the network unencrypted."
        )
    if not lowered.startswith("http://") and not lowered.startswith("https://"):
        warnings.append(f"{url} has an unexpected scheme.")
    return warnings


def validate_upstream_key(raw: str) -> str:
    """Sanity-check a pasted upstream API key.

    Cannot and does not check that the key is *valid* -- only the provider can,
    and this script never talks to a provider. It checks that you pasted a key
    and not a fragment of documentation, which is the failure that otherwise
    surfaces forty seconds later as an opaque 401 from inside the engine.
    """
    value = (raw or "").strip()
    if not value:
        raise SetupError("no API key provided")
    if any(ch.isspace() for ch in value):
        raise SetupError("API key contains whitespace -- check for a stray copy/paste newline")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise SetupError("API key contains control characters")
    if len(value) < 8:
        raise SetupError(f"API key is only {len(value)} characters -- that is not a real key")
    if value.startswith("<") and value.endswith(">"):
        raise SetupError(f"{value!r} looks like a documentation placeholder, not a key")
    lowered = value.lower()
    for hint in KEY_LOOKS_FAKE:
        if hint in lowered:
            raise SetupError(
                f"the key you entered contains {hint!r}, so it looks like a copied "
                f"example rather than your own key"
            )
    return value


def generate_local_key(prefix: str = "yb5_local_") -> str:
    """Mint the key your client presents to your own engine.

    Not an upstream credential and worth nothing off this machine; it exists so
    that another process on the box cannot spend your quota by accident. 32
    urlsafe bytes because there is no reason to be stingy with a local secret.
    """
    return prefix + secrets.token_urlsafe(32)


def yaml_quote(value: str) -> str:
    """Emit a YAML double-quoted scalar. Rejects control characters outright."""
    if not isinstance(value, str):
        raise TypeError(f"yaml_quote expects str, got {type(value).__name__}")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError("refusing to emit a YAML scalar containing control characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def toml_quote(value: str) -> str:
    """Emit a TOML basic string. Same reasoning as :func:`yaml_quote`."""
    if not isinstance(value, str):
        raise TypeError(f"toml_quote expects str, got {type(value).__name__}")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError("refusing to emit a TOML string containing control characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def sh_quote(value: str) -> str:
    """POSIX single-quote a value for the generated ``env.sh``."""
    return "'" + str(value).replace("'", "'\\''") + "'"


def ps_quote(value: str) -> str:
    """PowerShell single-quote a value for the generated ``env.ps1``."""
    return "'" + str(value).replace("'", "''") + "'"


def render_template(text: str, values: dict[str, str]) -> str:
    """Substitute every ``__NAME__`` token in ``text`` from ``values``.

    Strict in both directions on purpose:

    * an unknown token in the template raises, so a template edit cannot ship a
      config with ``__UPSTREAM_KEY__`` still in it;
    * an unused value raises, so a renamed token cannot silently drop a setting
      you thought you had configured.

    Substitution is a SINGLE pass (``re.sub`` with a callback), so a replacement
    value that happens to look like a token is inserted literally rather than
    being re-expanded.
    """
    used: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise TemplateError(
                f"template placeholder __{name}__ has no value. "
                f"Known values: {', '.join(sorted(values)) or '(none)'}"
            )
        used.add(name)
        return values[name]

    rendered = PLACEHOLDER_RE.sub(replace, text)
    unused = sorted(set(values) - used)
    if unused:
        raise TemplateError(
            f"value(s) {', '.join(unused)} were supplied but the template has no "
            f"matching __PLACEHOLDER__ -- the template and setup.py have drifted apart"
        )
    return rendered


def validate_alias_entries(entries: Sequence[dict[str, str]]) -> None:
    """Refuse any alias that maps to more than one upstream model.

    THE guard. In CLIProxyAPI 7.1.23 a repeated alias is not a config error --
    it is a documented feature that builds an internal model pool, and the pool
    picks its upstream from a per-process counter (``nextModelPoolOffset``) that
    ignores ``routing.strategy`` and ``session-affinity`` alike. Since the prompt
    cache lives upstream, consecutive turns of one conversation then land on
    different caches and the hit rate collapses to roughly 1/N.

    A pool is therefore not something to warn about and render anyway.
    """
    seen: dict[str, str] = {}
    for entry in entries:
        alias = entry.get("alias", "")
        name = entry.get("name", "")
        if not alias or not name:
            raise AliasPoolError(f"alias entry is incomplete: {entry!r}")
        if alias in seen:
            raise AliasPoolError(
                f"alias {alias!r} would map to both {seen[alias]!r} and {name!r}. "
                f"CLIProxyAPI turns a repeated alias into a rotating model pool whose "
                f"upstream is chosen by a global counter that ignores routing.strategy "
                f"and session-affinity, which caps the prompt-cache hit rate near 1/N. "
                f"Give each upstream model its own alias instead."
            )
        seen[alias] = name


@dataclass
class Spec:
    """Everything a rendered BYOK install needs. Plain data; no I/O."""

    provider_kind: str = PROVIDER_GEMINI
    alias: str = DEFAULT_ALIAS
    model: str = DEFAULT_MODELS[PROVIDER_GEMINI]
    channel: str = DEFAULT_OAUTH_CHANNEL
    provider_name: str = "byok"
    upstream_base_url: str = ""
    upstream_key: str = ""
    engine_host: str = DEFAULT_ENGINE_HOST
    engine_port: int = DEFAULT_ENGINE_PORT
    shim_port: int | None = DEFAULT_SHIM_PORT
    session_ttl: str = DEFAULT_SESSION_TTL
    local_key: str = ""
    auth_dir: Path = field(default_factory=lambda: Path.home() / ".yangble5" / "byok" / "auth")
    out_dir: Path = field(default_factory=lambda: Path.home() / ".yangble5" / "byok")
    max_context: int = DEFAULT_MAX_CONTEXT
    max_output: int = DEFAULT_MAX_OUTPUT

    @property
    def engine_base_url(self) -> str:
        return f"http://{self.engine_host}:{self.engine_port}"

    @property
    def claude_base_url(self) -> str:
        """Claude Code's endpoint: the shim when one is configured, else the engine.

        The shim exists because CLIProxyAPI 7.1.23's antigravity STREAMING
        translator forwards ``messages[].role`` verbatim, and Claude Code 2.1.x+
        injects a ``role: "system"`` message mid-conversation, which Gemini's
        streamGenerateContent rejects with a 400. Upstream fixed it in 7.2.93 by
        mapping system->user; tools/claude_shim.py backports exactly that. On
        7.2.93 or newer, pass --no-shim and skip the extra hop.
        """
        if self.shim_port is None:
            return self.engine_base_url
        return f"http://{self.engine_host}:{self.shim_port}"

    @property
    def claude_dir(self) -> Path:
        return self.out_dir / "claude"

    @property
    def codex_home(self) -> Path:
        return self.out_dir / "codex"

    @property
    def config_path(self) -> Path:
        return self.out_dir / "config.yaml"


def alias_entries(spec: Spec) -> list[dict[str, str]]:
    """The alias table this spec implies: exactly one entry, by construction.

    Returned as a list rather than a single dict so that
    :func:`validate_alias_entries` guards the same shape a hand-written
    multi-model config would have. The invariant is enforced on the data, not
    assumed from the code path that produced it.
    """
    return [{"name": validate_model_name(spec.model), "alias": validate_alias(spec.alias)}]


def render_provider_block(spec: Spec) -> str:
    """Render the provider + alias section for one provider kind.

    Where the alias lives is NOT a style choice. Per the CLIProxyAPI 7.1.23
    example config: ``oauth-model-alias`` does not apply to ``gemini-api-key``,
    ``codex-api-key``, ``claude-api-key``, ``openai-compatibility`` or
    ``vertex-api-key`` entries -- those carry their alias in their own
    ``models:`` list. Getting this wrong yields a config that loads without
    complaint and then 404s the model, so each kind gets its own renderer.
    """
    entries = alias_entries(spec)
    validate_alias_entries(entries)
    name = entries[0]["name"]
    alias = entries[0]["alias"]

    if spec.provider_kind == PROVIDER_GEMINI:
        key = validate_upstream_key(spec.upstream_key)
        return "\n".join(
            [
                "# Google Gemini via API key. The alias lives in this provider's own",
                "# models: list -- oauth-model-alias does not apply to gemini-api-key.",
                "gemini-api-key:",
                f"  - api-key: {yaml_quote(key)}",
                "    models:",
                f"      - name: {yaml_quote(name)}",
                f"        alias: {yaml_quote(alias)}",
            ]
        )

    if spec.provider_kind == PROVIDER_OPENAI_COMPAT:
        key = validate_upstream_key(spec.upstream_key)
        base = validate_base_url(spec.upstream_base_url)
        label = validate_provider_name(spec.provider_name)
        return "\n".join(
            [
                "# Generic OpenAI-compatible upstream. ONE model, ONE alias: adding a",
                "# second entry with this same alias is what builds the rotating pool",
                "# described above, and setup.py will refuse to render it.",
                "openai-compatibility:",
                f"  - name: {yaml_quote(label)}",
                f"    base-url: {yaml_quote(base)}",
                "    api-key-entries:",
                f"      - api-key: {yaml_quote(key)}",
                "    models:",
                f"      - name: {yaml_quote(name)}",
                f"        alias: {yaml_quote(alias)}",
            ]
        )

    if spec.provider_kind == PROVIDER_OAUTH:
        channel = validate_channel(spec.channel)
        return "\n".join(
            [
                "# OAuth channel. The credential is a token file in auth-dir, written by",
                "# the engine's own login flow -- it is not in this file. fork: true keeps",
                "# the original model name available alongside the alias.",
                "oauth-model-alias:",
                f"  {channel}:",
                f"    - name: {yaml_quote(name)}",
                f"      alias: {yaml_quote(alias)}",
                "      fork: true",
            ]
        )

    raise SetupError(
        f"unknown provider kind {spec.provider_kind!r}; expected one of {PROVIDER_KINDS}"
    )


def build_config(template_text: str, spec: Spec) -> str:
    """Render the engine config for ``spec``."""
    if not spec.local_key:
        raise SetupError("spec.local_key is empty; call generate_local_key() first")
    return render_template(
        template_text,
        {
            "ENGINE_HOST": yaml_quote(spec.engine_host),
            "ENGINE_PORT": str(validate_port(spec.engine_port)),
            "AUTH_DIR": yaml_quote(spec.auth_dir.as_posix()),
            "LOCAL_API_KEY": yaml_quote(spec.local_key),
            "SESSION_AFFINITY_TTL": yaml_quote(validate_ttl(spec.session_ttl)),
            "PROVIDER_BLOCK": render_provider_block(spec),
        },
    )


def render_claude_settings(spec: Spec) -> dict[str, Any]:
    """Claude Code ``settings.json`` for the isolated config directory.

    The auth token is deliberately absent. Claude Code reads
    ``ANTHROPIC_AUTH_TOKEN`` from the environment, and the generated ``env.sh`` /
    ``env.ps1`` (mode 0600) set it -- so the only file carrying a secret is the
    one whose whole job is carrying a secret.

    ``CLAUDE_CODE_MAX_CONTEXT_TOKENS`` is the load-bearing line. Claude Code
    assumes a 200K window for model names it does not recognise, and your alias
    is by construction a name no client has ever heard of, so it begins
    auto-compacting long before the real window is reached -- and every
    compaction is also a prompt rewrite, which is a cache miss. Setting this
    does NOT create context; it only moves where the client decides to compact.
    """
    return {
        "env": {
            "ANTHROPIC_BASE_URL": spec.claude_base_url,
            "ANTHROPIC_MODEL": spec.alias,
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(spec.max_context),
        }
    }


def merge_claude_settings(existing: dict[str, Any] | None, ours: dict[str, Any]) -> dict[str, Any]:
    """Merge our env keys into an existing settings file without losing anything.

    A user's isolated directory may already hold permissions, hooks, a status
    line. Overwriting the file would take those with it, and the whole promise
    of this script is that it never destroys a config it did not write. Only the
    specific ``env`` keys we own are replaced; every other key, including env
    keys we know nothing about, survives untouched.
    """
    merged: dict[str, Any] = dict(existing or {})
    current_env = merged.get("env")
    # Checked BEFORE the dict() call: dict("nope") raises a bare ValueError, and
    # a user staring at one has no idea which of their files is the problem.
    if current_env is not None and not isinstance(current_env, dict):
        raise SetupError("existing settings.json has a non-object 'env'; refusing to merge")
    env = dict(current_env or {})
    env.update(ours.get("env") or {})
    merged["env"] = env
    for key, value in ours.items():
        if key != "env":
            merged[key] = value
    return merged


def render_codex_toml(spec: Spec) -> str:
    """Codex ``config.toml`` for the isolated ``CODEX_HOME``.

    ``env_key`` means Codex reads the key from the environment rather than from
    this file, so no secret is written here either. The provider table is always
    keyed ``yangble5`` regardless of the alias, because an alias like
    ``yangble5[1m]`` is not a valid bare TOML key and quoting it everywhere buys
    nothing.

    ``model_context_window`` is the Codex-side counterpart of
    ``CLAUDE_CODE_MAX_CONTEXT_TOKENS``, and carries the same caveat: it moves
    where the client compacts, it does not enlarge the upstream window.
    """
    alias = validate_alias(spec.alias)
    port = validate_port(spec.engine_port)
    base = f"http://{spec.engine_host}:{port}/v1"
    return "\n".join(
        [
            "# Generated by yangble5 byok/setup.py. Safe to commit? NO -- this file",
            "# names your local endpoint; the key itself comes from $" + LOCAL_KEY_ENV + ".",
            f"model = {toml_quote(alias)}",
            'model_provider = "yangble5"',
            "",
            "# Codex guesses a conservative window for unknown model names and compacts",
            "# early. Compaction rewrites the prompt, and a rewritten prefix is a cache",
            "# miss. Raising this moves the compaction point; it does not create context.",
            f"model_context_window = {int(spec.max_context)}",
            f"model_max_output_tokens = {int(spec.max_output)}",
            "",
            "[model_providers.yangble5]",
            'name = "yangble5"',
            f"base_url = {toml_quote(base)}",
            f"env_key = {toml_quote(LOCAL_KEY_ENV)}",
            'wire_api = "chat"',
            "",
        ]
    )


def render_env_sh(spec: Spec) -> str:
    """POSIX ``env.sh``: the one generated file that carries the local key."""
    lines = [
        "#!/bin/sh",
        "# Generated by yangble5 byok/setup.py. CONTAINS A SECRET (your local proxy",
        "# key). Mode 0600. Never commit it, never paste it into an issue.",
        "#   usage:  . " + str(spec.out_dir / "env.sh"),
        "",
        f"export {BASE_URL_ENV}={sh_quote(spec.engine_base_url)}",
        f"export {LOCAL_KEY_ENV}={sh_quote(spec.local_key)}",
        "",
        "# Claude Code: isolated config dir, so your existing setup is untouched.",
        f"export CLAUDE_CONFIG_DIR={sh_quote(str(spec.claude_dir))}",
        f"export ANTHROPIC_BASE_URL={sh_quote(spec.claude_base_url)}",
        f'export ANTHROPIC_AUTH_TOKEN="${LOCAL_KEY_ENV}"',
        f"export ANTHROPIC_MODEL={sh_quote(spec.alias)}",
        f"export CLAUDE_CODE_MAX_CONTEXT_TOKENS={int(spec.max_context)}",
        "",
        "# Codex: isolated CODEX_HOME.",
        f"export CODEX_HOME={sh_quote(str(spec.codex_home))}",
        "",
    ]
    return "\n".join(lines)


def render_env_ps1(spec: Spec) -> str:
    """PowerShell ``env.ps1``, same contents as :func:`render_env_sh`."""
    lines = [
        "# Generated by yangble5 byok/setup.py. CONTAINS A SECRET (your local proxy",
        "# key). Never commit it, never paste it into an issue.",
        "#   usage:  . " + str(spec.out_dir / "env.ps1"),
        "",
        f"$env:{BASE_URL_ENV} = {ps_quote(spec.engine_base_url)}",
        f"$env:{LOCAL_KEY_ENV} = {ps_quote(spec.local_key)}",
        "",
        "# Claude Code: isolated config dir, so your existing setup is untouched.",
        f"$env:CLAUDE_CONFIG_DIR = {ps_quote(str(spec.claude_dir))}",
        f"$env:ANTHROPIC_BASE_URL = {ps_quote(spec.claude_base_url)}",
        f"$env:ANTHROPIC_AUTH_TOKEN = $env:{LOCAL_KEY_ENV}",
        f"$env:ANTHROPIC_MODEL = {ps_quote(spec.alias)}",
        f"$env:CLAUDE_CODE_MAX_CONTEXT_TOKENS = {ps_quote(str(spec.max_context))}",
        "",
        "# Codex: isolated CODEX_HOME.",
        f"$env:CODEX_HOME = {ps_quote(str(spec.codex_home))}",
        "",
    ]
    return "\n".join(lines)


def shim_command(spec: Spec) -> list[str]:
    """The exact command line that starts tools/claude_shim.py for this spec.

    A function rather than an f-string in the middle of the output, so a test can
    check the flags against the shim's OWN argument parser. Printed instructions
    that quietly stop matching the tool they describe are worse than no
    instructions: the reader trusts them and loses the afternoon.
    """
    if spec.shim_port is None:
        return []
    return [
        "python",
        "tools/claude_shim.py",
        "--listen-host",
        spec.engine_host,
        "--listen-port",
        str(spec.shim_port),
        "--upstream",
        spec.engine_base_url,
    ]


def interpret_bench(payload: dict[str, Any], target: float) -> tuple[bool, list[str]]:
    """Turn a ``cache_bench.py --json`` payload into an honest verdict.

    Reports the measured number either way. A setup script that says "done!"
    without a measurement is exactly the kind of claim this repository exists to
    avoid making.
    """
    rate = float(payload.get("eligible_hit_rate") or 0.0)
    cached = int(payload.get("cached_tokens") or 0)
    prompt = int(payload.get("prompt_tokens") or 0)
    warm = payload.get("warm_rounds") or []
    cold = payload.get("cold_round") or {}

    lines: list[str] = []
    if cold:
        lines.append(
            f"  cold round 1 (excluded, every session pays one): "
            f"prompt={int(cold.get('prompt_total') or 0):,} "
            f"cached={int(cold.get('cache_read') or 0):,} "
            f"lat={int(cold.get('latency_ms') or 0)}ms"
        )
    lines.append(
        f"  MEASURED warm-round hit rate: {rate:.2%}  "
        f"({cached:,} / {prompt:,} tokens over {len(warm)} warm round(s))"
    )
    for note in payload.get("notes") or []:
        lines.append(f"  NOTE: {note}")

    ok = bool(warm) and prompt > 0 and rate >= target
    if ok:
        lines.append(f"  target {target:.0%} -> PASS. Your cache-preserving config is working.")
        lines.append(
            "  Reminder: this is a WARM-round number from a single run on one machine. "
            "It is not a promise about your next session."
        )
    else:
        lines.append(f"  target {target:.0%} -> BELOW TARGET. Not calling this a success.")
        lines.append("  Work through this before changing anything else:")
        lines.extend(
            f"    - {item}" if not item.startswith("  ") else f"    {item}"
            for item in DIAGNOSTIC_CHECKLIST
        )
    return ok, lines


def backup_path_for(path: Path, now: datetime, existing: Sequence[Path] = ()) -> Path:
    """Pick a backup filename beside ``path`` that collides with nothing.

    Timestamped to the second, then disambiguated with a counter, because two
    setup runs inside one second must not have the second one silently destroy
    the first one's backup. A backup that can be clobbered is not a backup.
    """
    stamp = now.strftime("%Y%m%dT%H%M%S")
    taken = {p.name for p in existing}
    candidate = path.with_name(path.name + BACKUP_SUFFIX + stamp)
    counter = 1
    while candidate.name in taken or candidate.exists():
        candidate = path.with_name(f"{path.name}{BACKUP_SUFFIX}{stamp}-{counter}")
        counter += 1
    return candidate


# --------------------------------------------------------------------------
# I/O layer
# --------------------------------------------------------------------------


def ensure_private_dir(path: Path, dry_run: bool = False) -> None:
    """Create a directory that only the owner can read.

    ``chmod`` is a no-op on Windows for group/other bits; the call is kept
    because it is correct where it works and harmless where it does not.
    """
    if dry_run:
        return
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.chmod(stat.S_IRWXU)


def write_with_backup(
    path: Path,
    text: str,
    *,
    private: bool = False,
    now: datetime | None = None,
    dry_run: bool = False,
) -> tuple[str, Path | None]:
    """Write ``text`` to ``path``, never destroying what was there.

    Returns ``(status, backup_path)`` where status is ``created``, ``unchanged``
    or ``replaced``. An identical file is left alone entirely -- rewriting it
    would churn its mtime and, worse, produce a pointless backup on every run.
    """
    now = now or datetime.now()
    if path.exists():
        try:
            current = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            current = None
        if current == text:
            return "unchanged", None
        backup = backup_path_for(path, now)
        if not dry_run:
            # Copy BYTES, not decoded text. A file we could not decode is
            # precisely the one whose exact contents we must not mangle on the
            # way into its backup.
            backup.write_bytes(path.read_bytes())
            path.write_text(text, encoding="utf-8")
            if private:
                _make_private(path)
        return "replaced", backup

    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if private:
            _make_private(path)
    return "created", None


def _make_private(path: Path) -> None:
    # Best effort by design: chmod cannot express "owner only" on Windows, and a
    # setup that refused to run there would help nobody. The README says so
    # rather than pretending the mode bits are a guarantee everywhere.
    with contextlib.suppress(OSError):
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def read_json_if_present(path: Path) -> dict[str, Any] | None:
    """Read a JSON object, or return None if it is absent or unreadable.

    A settings file that is present but corrupt must not be silently replaced,
    so callers treat None-with-an-existing-file as a reason to stop.
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SetupError(
            f"{path} exists but is not readable JSON ({exc}). Move it aside and re-run; "
            f"this script will not overwrite a file it cannot understand."
        ) from exc
    if not isinstance(data, dict):
        raise SetupError(f"{path} is valid JSON but not an object; refusing to merge into it")
    return data


def detect_oauth_credentials(auth_dir: Path) -> list[Path]:
    """List token files the engine's login flow would have written."""
    if not auth_dir.is_dir():
        return []
    return sorted(p for p in auth_dir.glob("*.json") if p.is_file())


def port_is_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """True if something is listening. Used to tell 'not started' from 'broken'."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_port(host: str, port: int, timeout: float, log: Any) -> bool:
    """Poll until the engine answers or ``timeout`` seconds pass."""
    deadline = time.monotonic() + timeout
    announced = False
    while time.monotonic() < deadline:
        if port_is_open(host, port):
            return True
        if not announced:
            log(f"  waiting for the engine on {host}:{port} (up to {int(timeout)}s) ...")
            announced = True
        time.sleep(1.0)
    return port_is_open(host, port)


def run_cache_bench(
    spec: Spec,
    *,
    rounds: int,
    prefix_tokens: int,
    target: float,
    log: Any,
) -> dict[str, Any] | None:
    """Run tools/cache_bench.py against the freshly configured engine.

    The key is passed through the environment, never through argv -- cache_bench
    refuses an --api-key flag for the same reason.
    """
    if not CACHE_BENCH.exists():
        log(f"  cannot verify: {CACHE_BENCH} is missing (run setup from a full checkout)")
        return None

    env = dict(os.environ)
    env[BASE_URL_ENV] = spec.engine_base_url
    env[LOCAL_KEY_ENV] = spec.local_key

    cmd = [
        sys.executable,
        str(CACHE_BENCH),
        "--model",
        spec.alias,
        "--rounds",
        str(rounds),
        "--prefix-tokens",
        str(prefix_tokens),
        "--target",
        str(target),
        "--json",
    ]
    log(
        f"  $ python tools/cache_bench.py --model {spec.alias} --rounds {rounds} "
        f"--prefix-tokens {prefix_tokens} --json"
    )
    log("  (this sends real requests and spends real upstream quota)")

    # S603: fixed argv, no shell, interpreter path from sys.executable. The only
    # interpolated values are the alias and integers, all validated above.
    completed = subprocess.run(  # noqa: S603
        cmd,
        capture_output=True,
        text=True,
        env=env,
        check=False,
        cwd=str(REPO_ROOT),
    )
    for line in (completed.stderr or "").splitlines():
        log(f"  | {line}")
    if not (completed.stdout or "").strip():
        log(f"  benchmark produced no JSON (exit {completed.returncode}); cannot verify")
        return None
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        log("  benchmark output was not valid JSON; cannot verify")
        return None


# --------------------------------------------------------------------------
# Interactive prompts
# --------------------------------------------------------------------------


def prompt_text(question: str, default: str, interactive: bool) -> str:
    """Ask, or take the default when running unattended."""
    if not interactive:
        return default
    suffix = f" [{default}]" if default else ""
    answer = input(f"{question}{suffix}: ").strip()
    return answer or default


def prompt_choice(question: str, options: Sequence[str], default: str, interactive: bool) -> str:
    if not interactive:
        return default
    print(f"\n{question}")
    for index, option in enumerate(options, 1):
        marker = " (default)" if option == default else ""
        print(f"  {index}) {option}{marker}")
    while True:
        answer = input(f"choose 1-{len(options)} [{options.index(default) + 1}]: ").strip()
        if not answer:
            return default
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        if answer in options:
            return answer
        print("  not one of the options; try again")


def obtain_upstream_key(env_name: str, interactive: bool, env: dict[str, str] | None = None) -> str:
    """Get the upstream API key from the environment, or from a hidden prompt."""
    env = os.environ if env is None else env
    raw = env.get(env_name, "")
    if raw.strip():
        return validate_upstream_key(raw)
    if not interactive:
        raise SetupError(
            f"no upstream API key. Set {env_name} in the environment, or drop "
            f"--non-interactive so the key can be prompted for. It is never accepted "
            f"as a command-line flag: argv is readable by other processes and lands in "
            f"shell history."
        )
    print(
        "\n  Paste your upstream API key. It is not echoed, it is written only to\n"
        "  the local engine config (mode 0600), and it is never sent anywhere by\n"
        "  this script. Ctrl-C aborts and writes nothing."
    )
    return validate_upstream_key(getpass.getpass("  upstream API key: "))


PROVIDER_INSTRUCTIONS = {
    PROVIDER_GEMINI: (
        "Google Gemini, API key",
        (
            "1. Open Google AI Studio in a browser and sign in with a Google account.",
            "2. Create an API key (the free tier is enough to verify this setup).",
            "3. Copy it. You will paste it in a moment; it is not echoed.",
            "",
            "Free-tier quotas and model availability change without notice, and this",
            "project has no relationship with Google. Whatever your account gets is",
            "what you get -- we do not know what that is and will not guess a number.",
        ),
    ),
    PROVIDER_OPENAI_COMPAT: (
        "Any OpenAI-compatible endpoint, API key",
        (
            "1. Get an API key from the provider you intend to use.",
            "2. Find its OpenAI-compatible base URL (it usually ends in /v1).",
            "3. Find the EXACT upstream model name from the provider's model list.",
            "",
            "Prompt caching is a provider feature, not a proxy feature. If your",
            "provider does not cache, the verification step will report a low hit rate",
            "and it will be right; nothing in this repo can manufacture a cache.",
        ),
    ),
    PROVIDER_OAUTH: (
        "An OAuth provider (antigravity, gemini-cli, codex, claude, kimi, xai, ...)",
        (
            "This script will NOT drive your browser through somebody's login page.",
            "Automating a sign-in flow is fragile, and doing it on your behalf is not",
            "a thing a setup script should do. Instead:",
            "",
            "1. Get the CLIProxyAPI binary (third-party, MIT):",
            "     https://github.com/router-for-me/CLIProxyAPI",
            "2. Run ITS login flow, pointed at the auth directory printed below.",
            "     Check `cli-proxy-api --help` for the exact flag: it has changed",
            "     between versions and we will not guess it for your build.",
            "3. Finish the sign-in in the browser it opens.",
            "4. Come back here. This script watches the auth directory and continues",
            "   as soon as a token file appears.",
        ),
    ),
}


def wait_for_oauth_credentials(auth_dir: Path, interactive: bool, log: Any) -> list[Path]:
    """Detect the token file the engine's own login flow writes."""
    found = detect_oauth_credentials(auth_dir)
    if found:
        log(f"  found {len(found)} credential file(s) already in {auth_dir}")
        return found
    if not interactive:
        raise SetupError(
            f"no OAuth credential found in {auth_dir}. Complete the engine's login flow "
            f"first, then re-run."
        )
    log(f"\n  watching {auth_dir} for a credential file ...")
    log("  press Enter to re-check, or Ctrl-C to abort (nothing has been written yet)")
    for _ in range(30):
        input("  [Enter] when the login has finished: ")
        found = detect_oauth_credentials(auth_dir)
        if found:
            log(f"  detected: {', '.join(p.name for p in found)}")
            return found
        log(f"  still nothing in {auth_dir}. Is the login flow writing there?")
    raise SetupError("gave up waiting for an OAuth credential")


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


PREAMBLE = """
================================================================================
  yangble5 BYOK setup -- 用你自己的帳號, 拿到完整的設定
================================================================================

  What this does:
    - writes a CLIProxyAPI engine config with the cache-preserving settings
      (1:1 model alias, fill-first routing, session affinity) already correct
    - writes an ISOLATED Claude Code config dir and an isolated CODEX_HOME,
      including the 1M context unlock, without touching your existing setup
    - runs the benchmark in tools/ and prints the REAL measured hit rate

  What stays local:
    EVERYTHING. This script makes no outbound network call of its own. Your
    upstream key is written to one file on this machine and read back by the
    engine on this machine. Nothing is uploaded, registered, phoned home or
    shared -- there is nowhere for it to go, because this project has no server.

  What it will never do:
    - accept a credential on the command line (argv is not private)
    - overwrite a file without backing it up first
    - tell you it worked without measuring

  Ctrl-C at any prompt aborts before anything is written.
"""


def _logger(quiet: bool) -> Any:
    def log(message: str = "") -> None:
        if not quiet:
            print(message, flush=True)

    return log


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="byok/setup.py",
        description="Configure a yangble5 BYOK install against your own upstream account.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "The upstream API key is read from $" + UPSTREAM_KEY_ENV + " or prompted for.\n"
            "It is never accepted as a flag."
        ),
    )
    parser.add_argument("--provider", choices=PROVIDER_KINDS, help="upstream provider kind")
    parser.add_argument("--alias", default=DEFAULT_ALIAS, help="client-visible model name")
    parser.add_argument("--model", help="EXACT upstream model name")
    parser.add_argument(
        "--channel",
        default=DEFAULT_OAUTH_CHANNEL,
        help=f"OAuth channel ({', '.join(OAUTH_CHANNELS)})",
    )
    parser.add_argument("--provider-name", default="byok", help="label for an openai-compat block")
    parser.add_argument("--upstream-base-url", default="", help="openai-compat base URL")
    parser.add_argument(
        "--upstream-key-env",
        default=UPSTREAM_KEY_ENV,
        help="environment variable holding the upstream API key",
    )
    parser.add_argument("--engine-host", default=DEFAULT_ENGINE_HOST)
    parser.add_argument("--engine-port", type=int, default=DEFAULT_ENGINE_PORT)
    parser.add_argument(
        "--shim-port",
        type=int,
        default=DEFAULT_SHIM_PORT,
        help="port tools/claude_shim.py listens on (Claude Code points here)",
    )
    parser.add_argument(
        "--no-shim",
        action="store_true",
        help="point Claude Code straight at the engine (correct on engine >= 7.2.93)",
    )
    parser.add_argument("--session-ttl", default=DEFAULT_SESSION_TTL)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path.home() / ".yangble5" / "byok",
        help="where the generated config lives (default: ~/.yangble5/byok)",
    )
    parser.add_argument("--auth-dir", type=Path, help="engine auth dir (default: <out-dir>/auth)")
    parser.add_argument("--max-context", type=int, default=DEFAULT_MAX_CONTEXT)
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="never prompt; take every default and fail loudly on anything missing",
    )
    parser.add_argument("--dry-run", action="store_true", help="render everything, write nothing")
    parser.add_argument("--skip-bench", action="store_true", help="do not verify (not recommended)")
    parser.add_argument("--bench-rounds", type=int, default=DEFAULT_BENCH_ROUNDS)
    parser.add_argument("--bench-prefix-tokens", type=int, default=DEFAULT_BENCH_PREFIX_TOKENS)
    parser.add_argument("--bench-target", type=float, default=DEFAULT_BENCH_TARGET)
    parser.add_argument(
        "--bench-wait",
        type=float,
        default=90.0,
        help="seconds to wait for the engine before giving up on verification",
    )
    parser.add_argument("--quiet", action="store_true")
    return parser


def spec_from_args(args: argparse.Namespace, interactive: bool, log: Any) -> Spec:
    """Turn parsed arguments plus prompts into a validated Spec."""
    kind = args.provider or prompt_choice(
        "Which kind of upstream account will you use?",
        list(PROVIDER_KINDS),
        PROVIDER_GEMINI,
        interactive,
    )
    if kind not in PROVIDER_KINDS:
        raise SetupError(f"unknown provider {kind!r}")

    title, steps = PROVIDER_INSTRUCTIONS[kind]
    log(f"\n  -- {title} --")
    for line in steps:
        log(f"  {line}")

    out_dir = Path(args.out_dir).expanduser().resolve()
    auth_dir = Path(args.auth_dir).expanduser().resolve() if args.auth_dir else out_dir / "auth"

    spec = Spec(
        provider_kind=kind,
        alias=validate_alias(args.alias),
        model=validate_model_name(
            args.model
            or prompt_text("\n  exact upstream model name", DEFAULT_MODELS[kind], interactive)
        ),
        channel=args.channel,
        provider_name=args.provider_name,
        engine_host=args.engine_host,
        engine_port=validate_port(args.engine_port),
        shim_port=None if args.no_shim else validate_port(args.shim_port),
        session_ttl=validate_ttl(args.session_ttl),
        local_key=generate_local_key(),
        auth_dir=auth_dir,
        out_dir=out_dir,
        max_context=int(args.max_context),
    )

    if kind == PROVIDER_OPENAI_COMPAT:
        spec.upstream_base_url = validate_base_url(
            args.upstream_base_url or prompt_text("  OpenAI-compatible base URL", "", interactive)
        )
        for warning in base_url_warnings(spec.upstream_base_url):
            log(f"  WARNING: {warning}")
    if kind == PROVIDER_OAUTH:
        spec.channel = validate_channel(args.channel)

    return spec


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log = _logger(args.quiet)
    interactive = not args.non_interactive and sys.stdin is not None and sys.stdin.isatty()

    log(PREAMBLE)
    if args.dry_run:
        log("  DRY RUN: nothing will be written.\n")

    try:
        template_text = TEMPLATE_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot read {TEMPLATE_PATH}: {exc}", file=sys.stderr)
        return 2

    try:
        spec = spec_from_args(args, interactive, log)

        ensure_private_dir(spec.out_dir, args.dry_run)
        ensure_private_dir(spec.auth_dir, args.dry_run)

        if spec.provider_kind == PROVIDER_OAUTH:
            log(f"\n  auth directory: {spec.auth_dir}")
            wait_for_oauth_credentials(spec.auth_dir, interactive, log)
        else:
            spec.upstream_key = obtain_upstream_key(args.upstream_key_env, interactive)

        config_text = build_config(template_text, spec)
    except SetupError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\naborted; nothing was written", file=sys.stderr)
        return 2

    log("\n  Writing configuration")
    written: list[tuple[str, Path, str, Path | None]] = []

    try:
        status, backup = write_with_backup(
            spec.config_path, config_text, private=True, dry_run=args.dry_run
        )
        written.append(("engine config", spec.config_path, status, backup))

        ensure_private_dir(spec.claude_dir, args.dry_run)
        settings_path = spec.claude_dir / "settings.json"
        merged = merge_claude_settings(
            read_json_if_present(settings_path), render_claude_settings(spec)
        )
        status, backup = write_with_backup(
            settings_path, json.dumps(merged, indent=2) + "\n", dry_run=args.dry_run
        )
        written.append(("Claude Code settings", settings_path, status, backup))

        ensure_private_dir(spec.codex_home, args.dry_run)
        codex_path = spec.codex_home / "config.toml"
        status, backup = write_with_backup(
            codex_path, render_codex_toml(spec), dry_run=args.dry_run
        )
        written.append(("Codex config", codex_path, status, backup))

        env_sh = spec.out_dir / "env.sh"
        status, backup = write_with_backup(
            env_sh, render_env_sh(spec), private=True, dry_run=args.dry_run
        )
        written.append(("env.sh (SECRET)", env_sh, status, backup))

        env_ps1 = spec.out_dir / "env.ps1"
        status, backup = write_with_backup(
            env_ps1, render_env_ps1(spec), private=True, dry_run=args.dry_run
        )
        written.append(("env.ps1 (SECRET)", env_ps1, status, backup))
    except (SetupError, OSError) as exc:
        print(f"\nerror while writing: {exc}", file=sys.stderr)
        return 2

    for label, path, status, backup in written:
        log(f"    [{status:>9}] {label}: {path}")
        if backup:
            log(f"                previous version kept at {backup.name}")

    log(f"""
  Start the engine with this config:
      cli-proxy-api --config {spec.config_path}

  Then, in the shell you will code in:
      POSIX:       . {spec.out_dir / "env.sh"}
      PowerShell:  . {spec.out_dir / "env.ps1"}
""")
    if spec.shim_port is not None:
        log(
            f"  Claude Code points at the shim on port {spec.shim_port}, so also run:\n"
            f"      {' '.join(shim_command(spec))}\n"
            f"  The shim backports the system-role fix from engine 7.2.93. On 7.2.93+\n"
            f"  re-run this script with --no-shim and drop the extra hop.\n"
        )

    if args.dry_run or args.skip_bench:
        log("  Verification skipped. Nothing here is proven until cache_bench.py says so:")
        log(
            f"      python tools/cache_bench.py --model {spec.alias} --rounds 3 "
            f"--prefix-tokens {args.bench_prefix_tokens}"
        )
        return 0

    log("\n  Verifying against your engine")
    if not wait_for_port(spec.engine_host, spec.engine_port, args.bench_wait, log):
        log(f"  nothing is listening on {spec.engine_host}:{spec.engine_port}, so there is")
        log("  nothing to measure. Start the engine with the command above and then run:")
        log(
            f"      python tools/cache_bench.py --model {spec.alias} --rounds 3 "
            f"--prefix-tokens {args.bench_prefix_tokens}"
        )
        log("  The configuration itself was written successfully.")
        return 0

    payload = run_cache_bench(
        spec,
        rounds=args.bench_rounds,
        prefix_tokens=args.bench_prefix_tokens,
        target=args.bench_target,
        log=log,
    )
    if payload is None:
        log("  verification did not produce a result; the configuration is still written")
        return 1

    ok, lines = interpret_bench(payload, args.bench_target)
    log("")
    for line in lines:
        log(line)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
