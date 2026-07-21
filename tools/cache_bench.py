#!/usr/bin/env python3
"""Live prompt-cache benchmark for the yangble5 proxy (Anthropic wire format).

WHY THIS EXISTS
---------------
Prompt-cache hit rate is the number that decides whether a large-context coding
session is affordable, and it is the number that is easiest to lie with. A
dashboard that averages whatever traffic happened to flow can be inflated by
replaying one hot session; folding the cold first request of a session into the
average makes the headline figure a function of how many rounds you ran. This
script exists so the claim can be *reproduced on demand* against a controlled
prompt, in a single session, with the cold round reported separately.

WHAT IT MEASURES
----------------
It simulates one coding-agent session: a large STABLE system prefix plus a
growing message history, with every round pinned to the same session through
``metadata.user_id``. Round 1 is the cold cache write. Rounds 2..N should read
the upstream cache.

    eligible hit rate = sum(cache_read) / sum(prompt) over rounds 2..N

Round 1 is EXCLUDED from that number and printed on its own line. Every
session's first request is a cold write; pretending otherwise is the standard
way this metric gets inflated.

Usage comes straight off each response. This script deliberately does NOT read
``/v0/management/usage-queue`` -- that endpoint is consume-on-read, so polling
it here would steal records from ``cache_stats_sidecar.py``.

CONFIGURATION
-------------
    YANGBLE5_BASE_URL   proxy base URL (default http://127.0.0.1:8318)
    YANGBLE5_API_KEY    client API key -- REQUIRED, environment only.

The key is read from the environment and never from a flag on purpose: argv is
readable by other users on most systems and lands in shell history.

EXIT CODES
----------
    0  eligible hit rate >= --target
    1  below target, or no warm rounds were run
    2  transport / HTTP / decode failure (measurement never happened)

Runs on Linux, macOS and Windows; standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from typing import Any

BASE_URL_ENV = "YANGBLE5_BASE_URL"
API_KEY_ENV = "YANGBLE5_API_KEY"

DEFAULT_BASE_URL = "http://127.0.0.1:8318"
DEFAULT_TARGET = 0.99

# Calibrated against the live tokenizer on 2026-07-21 (Gemini upstream behind
# CLIProxyAPI 7.1.23): one generated fact line is roughly 30 tokens. This is only
# used to size the synthetic prefix -- every number the benchmark reports is the
# prompt size the upstream itself returned, never this estimate.
TOKENS_PER_LINE = 30

PREFIX_HEADER = "# yangble5 cache-bench stable corpus (deterministic; do not summarize)"

ZERO_CACHE_NOTE = (
    "upstream returned ZERO cached tokens on every round. Either this upstream "
    "does not expose cache accounting on this path, or the prefix is below its "
    "minimum cacheable size. Raise --prefix-tokens and retry; if it stays 0 the "
    "99% goal is not reachable on this upstream and that must be reported as such."
)

NO_WARM_ROUNDS_NOTE = (
    "no warm rounds were run, so there is nothing to measure. Round 1 is always a "
    "cold cache write; use --rounds 2 or more."
)


# --------------------------------------------------------------------------
# Pure helpers. No I/O below this line until the HTTP section -- these are what
# the unit tests exercise, which is why they take plain data and return plain
# data instead of reaching for globals.
# --------------------------------------------------------------------------


def build_prefix(target_tokens: int, tokens_per_line: int = TOKENS_PER_LINE) -> str:
    """Build a deterministic, poorly-compressible system prefix of ~target_tokens.

    WHY deterministic: the whole point is that round N sends a byte-identical
    prefix to round 1. Anything random (uuid, timestamp, dict ordering) would
    silently invalidate the cache and make a broken cache look like a working
    one that simply missed.

    WHY numbered checksum lines: natural-language filler compresses and repeats,
    and some upstreams normalise whitespace. Distinct high-entropy-looking lines
    keep the token count roughly proportional to the line count.
    """
    if tokens_per_line <= 0:
        raise ValueError("tokens_per_line must be positive")
    line_count = max(1, int(target_tokens) // tokens_per_line)
    lines = [PREFIX_HEADER]
    for i in range(line_count):
        parity = "even" if i % 2 == 0 else "odd"
        lines.append(
            f"fact {i:06d}: the checksum of block {i} is {(i * 2654435761) % 10**9:09d} "
            f"and its parity tag is {parity}."
        )
    return "\n".join(lines)


def prompt_denominator(input_tokens: int, cache_read_tokens: int) -> int:
    """Return the round's true prompt size, normalising two upstream conventions.

    WHY: providers disagree on whether ``input_tokens`` already contains the
    cached prefix, and the disagreement is invisible in the payload.

    * Verified live on 2026-07-21: CLIProxyAPI maps Gemini's ``promptTokenCount``
      straight through to ``input_tokens``, and that count ALREADY INCLUDES the
      cached read, so ``input >= cache_read``.
    * The pure Anthropic convention is the opposite: ``input_tokens`` counts only
      the uncached remainder, so ``cache_read`` routinely exceeds it.

    Dividing cached tokens by raw ``input_tokens`` therefore gives either the
    right answer or a nonsensical rate above 100%, depending on which upstream
    answered. Taking ``input`` when it is already the larger of the two, and
    ``input + cache_read`` when it is not, yields the same denominator under both
    conventions and can never report more than 100%.
    """
    inp = max(0, int(input_tokens))
    cread = max(0, int(cache_read_tokens))
    return inp if inp >= cread else inp + cread


def usage_to_round(
    round_no: int,
    usage: dict[str, Any],
    latency_ms: int,
    reply: str = "",
) -> dict[str, Any]:
    """Turn one response ``usage`` block into a normalised round record."""
    inp = int(usage.get("input_tokens") or 0)
    cread = int(usage.get("cache_read_input_tokens") or 0)
    cwrite = int(usage.get("cache_creation_input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    prompt = prompt_denominator(inp, cread)
    return {
        "round": round_no,
        "prompt_total": prompt,
        "input": inp,
        "cache_read": cread,
        "cache_write": cwrite,
        "output": out,
        "ratio": round(cread / prompt, 4) if prompt else 0.0,
        "latency_ms": latency_ms,
        "reply": reply[:40],
        "usage_keys": sorted(usage.keys()),
    }


def totals(rounds: Sequence[dict[str, Any]]) -> tuple[int, int]:
    """Return ``(cached_tokens, prompt_tokens)`` summed over ``rounds``."""
    cached = sum(int(r.get("cache_read") or 0) for r in rounds)
    prompt = sum(int(r.get("prompt_total") or 0) for r in rounds)
    return cached, prompt


def token_weighted_hit_rate(rounds: Sequence[dict[str, Any]]) -> float:
    """Token-weighted hit rate: sum(cached) / sum(prompt).

    WHY token-weighted rather than the mean of each round's ratio: a 700K-token
    round and a 200-token round are not equally important, and averaging ratios
    lets a handful of tiny requests drag a real result around by tens of points.
    """
    cached, prompt = totals(rounds)
    return (cached / prompt) if prompt else 0.0


def summarize(rounds: Sequence[dict[str, Any]], target: float = DEFAULT_TARGET) -> dict[str, Any]:
    """Build the honest result object: cold round separated, warm rounds scored."""
    rounds = list(rounds)
    cold = rounds[0] if rounds else None
    warm = rounds[1:]
    cached, prompt = totals(warm)
    rate = token_weighted_hit_rate(warm)

    notes: list[str] = []
    if not warm:
        notes.append(NO_WARM_ROUNDS_NOTE)
    if rounds and all(int(r.get("cache_read") or 0) == 0 for r in rounds):
        notes.append(ZERO_CACHE_NOTE)

    passed = bool(warm) and prompt > 0 and rate >= target
    return {
        "cold_round": cold,
        "warm_rounds": warm,
        "warm_round_numbers": [r.get("round") for r in warm],
        "cached_tokens": cached,
        "prompt_tokens": prompt,
        "eligible_hit_rate": round(rate, 4),
        "target": target,
        "pass": passed,
        "notes": notes,
    }


# --------------------------------------------------------------------------
# Configuration + HTTP
# --------------------------------------------------------------------------


def require_http_url(url: str) -> str:
    """Reject anything that is not http(s) before it reaches ``urlopen``.

    WHY: ``urllib`` happily opens ``file://``, ``ftp://`` and friends. A typo in
    ``YANGBLE5_BASE_URL`` (or a config file someone else can write) would then turn
    a benchmark run into a local file read whose contents get decoded and printed.
    The base URL is operator-supplied rather than hostile input, so this is a
    guard rail rather than a security boundary -- but it costs one comparison.
    """
    # Schemes are case-insensitive (RFC 3986) and urlopen accepts "HTTP://", so
    # comparing case-sensitively here would reject a URL that actually works.
    if not url.lower().startswith(("http://", "https://")):
        raise SystemExit(
            f"error: base URL must start with http:// or https://, got {url!r}.\n"
            f"  Set {BASE_URL_ENV} or pass --base-url."
        )
    return url


def resolve_base_url(cli_value: str | None = None, env: dict[str, str] | None = None) -> str:
    """Flag beats environment beats the loopback default; trailing slash stripped."""
    env = os.environ if env is None else env
    return require_http_url((cli_value or env.get(BASE_URL_ENV) or DEFAULT_BASE_URL).rstrip("/"))


def resolve_api_key(env_name: str = API_KEY_ENV, env: dict[str, str] | None = None) -> str:
    """Read the client key from the environment, or fail loudly.

    There is deliberately no default and no ``--api-key`` flag: a literal default
    would end up committed, and a flag would end up in shell history and in every
    other process's view of the argument list.
    """
    env = os.environ if env is None else env
    key = (env.get(env_name) or "").strip()
    if not key:
        raise SystemExit(
            f"error: {env_name} is not set.\n"
            f"  Linux/macOS:  export {env_name}='<your proxy api key>'\n"
            f"  PowerShell:   $env:{env_name} = '<your proxy api key>'\n"
            "This tool never accepts the key as a command-line flag."
        )
    return key


def post_message(
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    session: str,
    max_tokens: int,
    timeout: float,
) -> dict[str, Any]:
    """POST one /v1/messages request and return the decoded response."""
    body = json.dumps(
        {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            # Session pinning. Upstream affinity is what makes rounds 2..N able to
            # hit the same cache entry; without it a pooled backend can answer
            # each round from a different worker and every round looks cold.
            "metadata": {"user_id": session},
        }
    ).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - scheme checked by require_http_url
        base_url + "/v1/messages",
        data=body,
        headers={
            # Both header styles: different proxy builds honour different ones.
            "Authorization": "Bearer " + api_key,
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    # S310: the scheme is validated by require_http_url() before we ever get here.
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        raw = response.read()
    return json.loads(raw)


def extract_text(response: dict[str, Any]) -> str:
    """Concatenate the text blocks of an Anthropic-format response."""
    out = []
    for block in response.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            out.append(block.get("text") or "")
    return "".join(out)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cache_bench",
        description="Live prompt-cache benchmark against a yangble5 proxy.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog=f"The API key is read from ${API_KEY_ENV} and cannot be passed as a flag.",
    )
    parser.add_argument("--base-url", default=None, help=f"overrides ${BASE_URL_ENV}")
    parser.add_argument("--model", default="yangble5")
    parser.add_argument("--prefix-tokens", type=int, default=30000)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--max-tokens", type=int, default=48)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--session", default="cache-bench-fixed-session")
    parser.add_argument("--target", type=float, default=DEFAULT_TARGET)
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the result object on stdout (progress moves to stderr)",
    )
    return parser


def _logger(json_mode: bool) -> Callable[..., None]:
    """Keep stdout parseable under --json by moving human output to stderr."""
    stream = sys.stderr if json_mode else sys.stdout

    def log(message: str = "") -> None:
        print(message, file=stream, flush=True)

    return log


def _format_round(record: dict[str, Any]) -> str:
    return (
        f"  round {record['round']}: prompt={record['prompt_total']:,} "
        f"cached={record['cache_read']:,} ratio={record['ratio']:.2%} "
        f"lat={record['latency_ms']}ms reply={record['reply']!r}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log = _logger(args.json)

    if args.rounds < 1:
        raise SystemExit("error: --rounds must be at least 1")

    base_url = resolve_base_url(args.base_url)
    api_key = resolve_api_key()

    system = build_prefix(args.prefix_tokens)
    history: list[dict[str, Any]] = []
    rounds: list[dict[str, Any]] = []

    log(
        f"cache_bench: {base_url} model={args.model} "
        f"prefix~{args.prefix_tokens:,} tok rounds={args.rounds} session={args.session!r}"
    )

    for number in range(1, args.rounds + 1):
        history.append(
            {"role": "user", "content": f"round {number}: reply with exactly OK-{number}"}
        )
        started = time.monotonic()
        try:
            response = post_message(
                base_url,
                api_key,
                args.model,
                system,
                history,
                args.session,
                args.max_tokens,
                args.timeout,
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", errors="replace")
            log(f"  round {number}: HTTP {exc.code} {detail}")
            return 2
        except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
            log(f"  round {number}: FAILED {type(exc).__name__}: {exc}")
            return 2

        latency_ms = int((time.monotonic() - started) * 1000)
        text = extract_text(response)
        record = usage_to_round(number, response.get("usage") or {}, latency_ms, text.strip())
        rounds.append(record)
        log(_format_round(record))

        # Feed the reply back so the prefix keeps growing exactly as a real
        # session's would; a fabricated reply would change the cached bytes.
        history.append({"role": "assistant", "content": text or f"OK-{number}"})
        if number < args.rounds and args.interval > 0:
            time.sleep(args.interval)

    result = summarize(rounds, args.target)
    cold = result["cold_round"]

    log("-" * 68)
    if cold:
        log(
            f"  cold round 1 (EXCLUDED from the headline number): "
            f"prompt={cold['prompt_total']:,} cached={cold['cache_read']:,} "
            f"ratio={cold['ratio']:.2%} lat={cold['latency_ms']}ms"
        )
    warm_label = f"rounds 2..{args.rounds}" if len(rounds) > 1 else "no warm rounds"
    log(
        f"  eligible hit rate ({warm_label}, token-weighted): "
        f"{result['eligible_hit_rate']:.2%} "
        f"({result['cached_tokens']:,} / {result['prompt_tokens']:,} tok) "
        f"target {args.target:.0%} -> {'PASS' if result['pass'] else 'FAIL'}"
    )
    for note in result["notes"]:
        log(f"  NOTE: {note}")

    if args.json:
        payload = dict(result)
        payload["model"] = args.model
        payload["base_url"] = base_url
        payload["rounds"] = rounds
        print(json.dumps(payload, indent=2))

    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
