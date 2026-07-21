#!/usr/bin/env python3
"""Single consumer of the proxy usage queue -> durable, token-weighted stats.json.

WHY THIS EXISTS
---------------
``/v0/management/usage-queue`` is CONSUME-ON-READ: whoever polls it gets the
records and nobody else ever sees them. If a dashboard polls it directly and a
script also polls it, the two silently split the traffic and both report numbers
that are wrong in an unfalsifiable way. This sidecar is therefore designed to be
the ONE consumer: it drains the queue, folds each record into a durable
``stats.json``, and everything else (dashboards, health checks) reads that file.

The accounting rules it enforces, and why:

* **Token-weighted, not per-request.** ``sum(cached) / sum(prompt)``. A
  700K-token request and a 200-token request are not equally important; averaging
  per-request ratios lets a burst of tiny calls move the headline by tens of
  points.
* **Failures are excluded from denominators.** A request that errored out has no
  meaningful prompt size, and counting it as an uncached prompt would understate
  a working cache. Failures are still counted in ``requests``/``failures`` so the
  error rate stays visible.
* **Prompt-denominator guard.** See :func:`prompt_denominator`.
* **Rolling window as well as cumulative.** A cumulative rate from a long-lived
  process hides a regression that started an hour ago.
* **Atomic writes.** ``stats.json`` is written to a temp file, fsynced, then
  ``os.replace``'d, so a reader never sees a half-written file and a crash mid-write
  cannot destroy the accumulated history.

This is an observability aid, NOT the authoritative benchmark. It reports
whatever traffic happened to flow, cold requests included. The reproducible,
controlled measurement is ``cache_bench.py``.

CONFIGURATION (flag beats environment beats default)
----------------------------------------------------
    YANGBLE5_BASE_URL          proxy base URL      (default http://127.0.0.1:8318)
    YANGBLE5_MGMT_KEY          management key      REQUIRED, environment only
    YANGBLE5_STATS_PATH        output file         (default ./stats.json)
    YANGBLE5_POLL_SECONDS      poll interval       (default 5)
    YANGBLE5_ROLLING_WINDOW    window size         (default 100 requests)
    YANGBLE5_RECENT_KEEP       ring-log cap        (default 1000 records)
    YANGBLE5_LOCK_PORT         singleton lock port (default 8319, 0 disables)

Runs on Linux, macOS and Windows; standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

BASE_URL_ENV = "YANGBLE5_BASE_URL"
MGMT_KEY_ENV = "YANGBLE5_MGMT_KEY"
STATS_PATH_ENV = "YANGBLE5_STATS_PATH"
POLL_SECONDS_ENV = "YANGBLE5_POLL_SECONDS"
ROLLING_WINDOW_ENV = "YANGBLE5_ROLLING_WINDOW"
RECENT_KEEP_ENV = "YANGBLE5_RECENT_KEEP"
LOCK_PORT_ENV = "YANGBLE5_LOCK_PORT"

DEFAULT_BASE_URL = "http://127.0.0.1:8318"
DEFAULT_STATS_PATH = "stats.json"
DEFAULT_POLL_SECONDS = 5.0
DEFAULT_ROLLING_WINDOW = 100
DEFAULT_RECENT_KEEP = 1000
DEFAULT_LOCK_PORT = 8319
DEFAULT_HTTP_TIMEOUT = 6.0

SCHEMA = 2

QUEUE_PATH = "/v0/management/usage-queue"


class AlreadyRunning(RuntimeError):
    """Raised when another sidecar instance already holds the singleton lock."""


# --------------------------------------------------------------------------
# Pure accounting. Everything here takes and returns plain data so the maths can
# be unit-tested without a proxy, a socket or a clock.
# --------------------------------------------------------------------------


def prompt_denominator(input_tokens: int, cached_tokens: int) -> int:
    """Return a record's true prompt size, normalising two upstream conventions.

    WHY: providers disagree on whether ``input_tokens`` already contains the
    cached prefix. Gemini through CLIProxyAPI reports a prompt count that
    INCLUDES the cached read (``input >= cached``); the Anthropic wire convention
    reports only the uncached remainder (``cached`` can exceed ``input``).
    Dividing by raw ``input_tokens`` therefore produces either the right answer or
    a rate above 100%, depending on which upstream served the request.

    Deliberately duplicated in ``cache_bench.py`` rather than shared through a
    helper module: each tool has to stay a single copyable file. ``tests`` asserts
    the two implementations agree, so the duplication cannot drift.
    """
    inp = max(0, int(input_tokens))
    cached = max(0, int(cached_tokens))
    return inp if inp >= cached else inp + cached


def fresh_stats(window: int = DEFAULT_ROLLING_WINDOW, now: float | None = None) -> dict[str, Any]:
    """An empty stats document."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))
    return {
        "schema": SCHEMA,
        "since": stamp,
        "updated_at": "",
        "requests": 0,
        "failures": 0,
        "tokens": {"input": 0, "prompt": 0, "output": 0, "cached": 0, "total": 0},
        "hit_rate": 0.0,
        "rolling": {"window": window, "hit_rate": 0.0, "input": 0, "cached": 0},
        "by_alias": {},
        "by_source": {},
        "recent": [],
    }


def load_stats(
    path: str | os.PathLike[str], window: int = DEFAULT_ROLLING_WINDOW
) -> dict[str, Any]:
    """Load an existing stats document, or start a fresh one.

    A corrupt or older-schema file is replaced rather than repaired: a wrong
    number that looks plausible is worse than an obviously restarted counter.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return fresh_stats(window)
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return fresh_stats(window)
    # Tolerate a file written by an older run with a different window size.
    data.setdefault("recent", [])
    data.setdefault("by_alias", {})
    data.setdefault("by_source", {})
    return data


def save_stats(path: str | os.PathLike[str], stats: dict[str, Any]) -> None:
    """Write ``stats`` atomically: temp file -> fsync -> os.replace.

    WHY: readers (dashboards, health_check) poll this file continuously. A plain
    truncate-and-write exposes a window in which the file is empty or truncated
    JSON, and a crash inside that window loses every accumulated counter.
    ``os.replace`` is atomic on POSIX and on Windows.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # ".tmp" is appended rather than substituted: with_suffix() on "a.b.json"
    # would clobber a real sibling file named "a.b.tmp".
    tmp = target.with_name(target.name + ".tmp")
    payload = json.dumps(stats, ensure_ascii=False)
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)


def ingest(
    stats: dict[str, Any],
    record: dict[str, Any],
    recent_keep: int = DEFAULT_RECENT_KEEP,
    now: float | None = None,
) -> None:
    """Fold one usage-queue record into ``stats`` in place."""
    tokens = record.get("tokens") or {}
    alias = record.get("alias") or record.get("model") or "?"
    source = record.get("source") or "?"
    failed = bool(record.get("failed"))
    latency = int(record.get("latency_ms") or 0)

    inp = int(tokens.get("input_tokens") or 0)
    out = int(tokens.get("output_tokens") or 0)
    cached = int(tokens.get("cached_tokens") or 0)
    total = int(tokens.get("total_tokens") or (inp + out))
    prompt = prompt_denominator(inp, cached)

    stats["requests"] += 1
    if failed:
        stats["failures"] += 1
    else:
        bucket = stats["tokens"]
        bucket["input"] += inp
        bucket["prompt"] = bucket.get("prompt", 0) + prompt
        bucket["output"] += out
        bucket["cached"] += cached
        bucket["total"] += total

    per_alias = stats["by_alias"].setdefault(
        alias, {"n": 0, "fail": 0, "inp": 0, "out": 0, "cached": 0, "latSum": 0}
    )
    per_alias["n"] += 1
    if failed:
        per_alias["fail"] += 1
    else:
        per_alias["inp"] += inp
        per_alias["out"] += out
        per_alias["cached"] += cached
    per_alias["latSum"] += latency

    per_source = stats["by_source"].setdefault(source, {"n": 0, "tok": 0})
    per_source["n"] += 1
    per_source["tok"] += total

    stats["recent"].append(
        {
            "ts": int(time.time() if now is None else now),
            "alias": alias,
            "failed": failed,
            "inp": inp,
            "prompt": prompt,
            "out": out,
            "cached": cached,
            "lat": latency,
        }
    )
    # Ring log: prune oldest. Unbounded growth would make stats.json unwritable
    # long before it became useful.
    overflow = len(stats["recent"]) - max(0, recent_keep)
    if overflow > 0:
        del stats["recent"][:overflow]


def recompute_rates(
    stats: dict[str, Any], window: int = DEFAULT_ROLLING_WINDOW, now: float | None = None
) -> None:
    """Recompute cumulative and rolling hit rates in place."""
    tokens = stats["tokens"]
    denominator = tokens.get("prompt") or tokens.get("input") or 0
    stats["hit_rate"] = round(tokens["cached"] / denominator, 4) if denominator else 0.0

    successes = [r for r in stats["recent"] if not r.get("failed")]
    recent = successes[-window:] if window > 0 else []
    window_prompt = sum(int(r.get("prompt", r.get("inp", 0)) or 0) for r in recent)
    window_cached = sum(int(r.get("cached") or 0) for r in recent)
    stats["rolling"] = {
        "window": window,
        "hit_rate": round(window_cached / window_prompt, 4) if window_prompt else 0.0,
        "input": window_prompt,
        "cached": window_cached,
    }
    stats["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))


def apply_records(
    stats: dict[str, Any],
    records: Iterable[Any],
    window: int = DEFAULT_ROLLING_WINDOW,
    recent_keep: int = DEFAULT_RECENT_KEEP,
) -> int:
    """Ingest every dict in ``records`` and recompute rates. Returns count ingested."""
    ingested = 0
    for record in records:
        if isinstance(record, dict):
            ingest(stats, record, recent_keep=recent_keep)
            ingested += 1
    if ingested:
        recompute_rates(stats, window)
    return ingested


# --------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------


def require_http_url(url: str) -> str:
    """Reject anything that is not http(s) before it reaches ``urlopen``.

    WHY: ``urllib`` will happily open ``file://`` and ``ftp://``. This process runs
    unattended for days holding a management key, so a mistyped base URL should
    stop it at startup rather than have it quietly poll something that is not the
    proxy. Kept identical to the copy in ``cache_bench.py`` on purpose; the tests
    assert the two agree.
    """
    # Schemes are case-insensitive (RFC 3986) and urlopen accepts "HTTP://", so
    # comparing case-sensitively here would reject a URL that actually works.
    if not url.lower().startswith(("http://", "https://")):
        raise SystemExit(
            f"error: base URL must start with http:// or https://, got {url!r}.\n"
            f"  Set {BASE_URL_ENV} or pass --base-url."
        )
    return url


def drain_queue(base_url: str, mgmt_key: str, timeout: float = DEFAULT_HTTP_TIMEOUT) -> list[Any]:
    """Drain the consume-on-read usage queue. Returns [] on a non-list body."""
    request = urllib.request.Request(  # noqa: S310 - scheme checked by require_http_url
        base_url.rstrip("/") + QUEUE_PATH,
        headers={"Authorization": "Bearer " + mgmt_key},
    )
    # S310: main() validates the scheme before the poll loop starts.
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        rows = json.loads(response.read())
    return rows if isinstance(rows, list) else []


def acquire_singleton_lock(port: int) -> socket.socket | None:
    """Bind a loopback port as a portable single-instance lock.

    Returns the bound socket (keep it alive for the process lifetime), or None
    when locking is disabled with ``port <= 0``. Raises :class:`AlreadyRunning`
    if the port is taken.

    WHY a port and not a lock file: a lock file left behind by a killed process
    needs stale-PID logic that is different on every OS. A bound socket is
    released by the kernel the instant the process dies, on Linux, macOS and
    Windows alike.

    WHY ``SO_REUSEADDR`` is deliberately NOT set: on Windows it lets a second
    process take over a port another process is already listening on, which is
    exactly the collision this lock exists to prevent.
    """
    if port <= 0:
        return None
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", port))
        lock.listen(1)
    except OSError as exc:
        lock.close()
        raise AlreadyRunning(
            f"port {port} is already bound -- another cache_stats_sidecar is probably "
            f"running. Use --lock-port 0 to disable this check."
        ) from exc
    return lock


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _env_float(name: str, default: float, env: dict[str, str] | None = None) -> float:
    env = os.environ if env is None else env
    try:
        return float(env[name])
    except (KeyError, TypeError, ValueError):
        return default


def _env_int(name: str, default: int, env: dict[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    try:
        return int(env[name])
    except (KeyError, TypeError, ValueError):
        return default


def resolve_mgmt_key(env: dict[str, str] | None = None) -> str:
    """Read the management key from the environment, or fail loudly.

    No flag and no default on purpose: the management API can enumerate accounts,
    so its key must never reach argv or a committed file.
    """
    env = os.environ if env is None else env
    key = (env.get(MGMT_KEY_ENV) or "").strip()
    if not key:
        raise SystemExit(
            f"error: {MGMT_KEY_ENV} is not set.\n"
            f"  Linux/macOS:  export {MGMT_KEY_ENV}='<your management key>'\n"
            f"  PowerShell:   $env:{MGMT_KEY_ENV} = '<your management key>'\n"
            "This tool never accepts the management key as a command-line flag."
        )
    return key


def build_parser(env: dict[str, str] | None = None) -> argparse.ArgumentParser:
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(
        prog="cache_stats_sidecar",
        description="Drain the proxy usage queue into a durable token-weighted stats.json.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog=f"The management key is read from ${MGMT_KEY_ENV} and cannot be passed as a flag.",
    )
    parser.add_argument("--base-url", default=env.get(BASE_URL_ENV, DEFAULT_BASE_URL).rstrip("/"))
    parser.add_argument("--stats-path", default=env.get(STATS_PATH_ENV, DEFAULT_STATS_PATH))
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=_env_float(POLL_SECONDS_ENV, DEFAULT_POLL_SECONDS, env),
    )
    parser.add_argument(
        "--window", type=int, default=_env_int(ROLLING_WINDOW_ENV, DEFAULT_ROLLING_WINDOW, env)
    )
    parser.add_argument(
        "--recent-keep", type=int, default=_env_int(RECENT_KEEP_ENV, DEFAULT_RECENT_KEEP, env)
    )
    parser.add_argument(
        "--lock-port",
        type=int,
        default=_env_int(LOCK_PORT_ENV, DEFAULT_LOCK_PORT, env),
        help="loopback port used as a single-instance lock; 0 disables",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_HTTP_TIMEOUT)
    parser.add_argument("--once", action="store_true", help="drain once and exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    require_http_url(args.base_url)
    mgmt_key = resolve_mgmt_key()

    try:
        lock = acquire_singleton_lock(args.lock_port)
    except AlreadyRunning as exc:
        print(f"cache_stats_sidecar: {exc}", file=sys.stderr)
        return 0  # not an error: the other instance is doing the job

    stats = load_stats(args.stats_path, args.window)
    print(
        f"cache_stats_sidecar: {args.base_url} -> {args.stats_path} "
        f"(poll {args.poll_seconds}s, window {args.window}, lock {args.lock_port or 'off'})",
        flush=True,
    )

    try:
        while True:
            try:
                rows = drain_queue(args.base_url, mgmt_key, args.timeout)
                if apply_records(stats, rows, args.window, args.recent_keep):
                    save_stats(args.stats_path, stats)
            except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
                # Proxy restarting or transient: keep the accumulated stats and
                # retry. Losing the process here would lose the history.
                print(
                    f"cache_stats_sidecar: poll failed ({type(exc).__name__}: {exc})",
                    file=sys.stderr,
                    flush=True,
                )
            if args.once:
                break
            time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        pass
    finally:
        if lock is not None:
            lock.close()

    save_stats(args.stats_path, stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
