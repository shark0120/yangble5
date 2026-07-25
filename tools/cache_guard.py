#!/usr/bin/env python3
"""Cache-hostility linter for LLM prompts. Offline, no API key, CI-friendly.

WHY THIS EXISTS
---------------
``cache_bench.py`` proves a hit rate against a live upstream. This tool answers
the question you actually hit in day-to-day work: *did this pull request quietly
break prompt caching and start charging me full price?*

Prompt caching only works when the front of the prompt is **byte-identical**
request to request. The classic regression is a single line -- a timestamp, a
UUID, a "generated at <now>", a request id -- rendered into the system prompt or
another cached prefix. It changes every request, so the upstream can never reuse
the cache, and nothing errors: the bill just goes up. A dashboard will not tell
you which commit did it. A CI check can.

WHAT IT DOES
------------
* ``scan``  reads one or more prompt payloads and flags volatile substrings
  (timestamps, epochs, UUIDs, dates, "now"/"today" markers) sitting in the
  cacheable prefix. It needs only ONE payload: a timestamp in a system prompt is
  self-evidently cache-hostile without a second sample to diff against.
* ``diff``  compares the cacheable (byte-stable) prefix of a *before* and an
  *after* set of payloads and reports the change. If the after-set's stable
  prefix shrank, it estimates the extra uncached tokens per request and, given a
  price you supply, a cost delta per 1,000 requests. Every dollar figure is an
  ESTIMATE with its assumptions printed on the same line -- this file never
  invents a request volume or a measured saving.

Exit codes: 0 = clean / no regression; 1 = a finding (use in CI); 2 = bad input.

Standard library only. Runs on Linux, macOS and Windows.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# A prompt payload may be a bare string, an object with a "prompt" field, or an
# Anthropic-style {"system": ..., "messages": [...]} body. The cacheable prefix
# is the stable front of the prompt: the system text plus every message except
# the last one, which is the fresh turn that is allowed to vary.
DEFAULT_PRICE_PER_MTOK = 3.0  # EXAMPLE ONLY -- see note printed beside any cost.
CHARS_PER_TOKEN_EST = 4  # Coarse text estimate; labelled as an estimate on use.


@dataclass(frozen=True)
class Volatility:
    name: str
    severity: str  # "high" (almost always volatile) or "medium" (often volatile)
    pattern: re.Pattern[str]
    hint: str


# Ordered most-specific first so a full ISO datetime is not also reported as a
# bare date. ``scan`` de-overlaps by preferring the earlier (more specific) rule.
VOLATILITY_RULES: tuple[Volatility, ...] = (
    Volatility(
        "iso8601_datetime",
        "high",
        re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?"),
        "an ISO timestamp changes every request; move it out of the cached prefix",
    ),
    Volatility(
        "uuid",
        "high",
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        "a UUID/request-id is unique per call; it invalidates the whole prefix after it",
    ),
    Volatility(
        "unix_epoch_ms",
        "high",
        re.compile(r"\b1[0-9]{12}\b"),
        "a millisecond epoch changes every request",
    ),
    Volatility(
        "unix_epoch_s",
        "high",
        re.compile(r"\b1[0-9]{9}\b"),
        "a second epoch changes most requests",
    ),
    Volatility(
        "clock_time",
        "high",
        re.compile(r"\b\d{1,2}:\d{2}:\d{2}\b"),
        "a wall-clock time changes every request",
    ),
    Volatility(
        "bare_date",
        "medium",
        re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
        "a date is fine if it is a constant, but a rendered 'today' breaks caching daily",
    ),
    Volatility(
        "now_marker",
        "medium",
        re.compile(
            r"(?i)\b(current (date|time)|generated (at|on)|today('?s)? date|as of|timestamp)\b"
        ),
        "phrases like 'current date' usually sit next to a value that changes",
    ),
)


# --------------------------------------------------------------------------
# Pure helpers -- what the unit tests exercise. No I/O below this line.
# --------------------------------------------------------------------------


def prompt_text(payload: Any) -> str:
    """Extract the cacheable-prefix text from a payload.

    A bare string is used as-is. ``{"prompt": s}`` uses ``s``. An Anthropic-style
    body contributes its ``system`` text and every message *except the last*
    (the last is the fresh turn and is allowed to differ). Message content may be
    a string or a list of ``{"type": "text", "text": ...}`` blocks.
    """
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        raise ValueError("payload must be a string or an object")
    if isinstance(payload.get("prompt"), str):
        return payload["prompt"]
    parts: list[str] = []
    system = payload.get("system")
    if isinstance(system, str):
        parts.append(system)
    elif isinstance(system, list):
        parts.append(_blocks_text(system))
    messages = payload.get("messages")
    if isinstance(messages, list) and messages:
        for message in messages[:-1]:  # exclude the fresh final turn
            if isinstance(message, dict):
                parts.append(_blocks_text(message.get("content")))
    return "\n".join(p for p in parts if p)


def _blocks_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    out.append(text)
        return "\n".join(out)
    return ""


def common_prefix_len(texts: Sequence[str]) -> int:
    """Length of the longest byte-identical leading run shared by all texts."""
    texts = list(texts)
    if not texts:
        return 0
    if len(texts) == 1:
        return len(texts[0])
    shortest = min(texts, key=len)
    for i, ch in enumerate(shortest):
        if any(t[i] != ch for t in texts):
            return i
    return len(shortest)


def est_tokens(chars: int) -> int:
    """Coarse token estimate from a character count. Always labelled as an estimate."""
    return max(0, chars) // CHARS_PER_TOKEN_EST


def find_volatility(text: str, limit: int | None = None) -> list[dict[str, Any]]:
    """Return volatile matches within ``text`` (optionally only before ``limit``).

    Overlapping matches keep the most-specific (earliest rule), so a full ISO
    datetime is not double-reported as a bare date.
    """
    window = text if limit is None else text[:limit]
    claimed: list[tuple[int, int]] = []
    findings: list[dict[str, Any]] = []
    for rule in VOLATILITY_RULES:
        for match in rule.pattern.finditer(window):
            start, end = match.start(), match.end()
            if any(start < c_end and end > c_start for c_start, c_end in claimed):
                continue
            claimed.append((start, end))
            findings.append(
                {
                    "rule": rule.name,
                    "severity": rule.severity,
                    "at_char": start,
                    "match": match.group(0),
                    "hint": rule.hint,
                }
            )
    findings.sort(key=lambda f: f["at_char"])
    return findings


@dataclass
class ScanReport:
    payloads: int
    prefix_chars: int
    findings: list[dict[str, Any]] = field(default_factory=list)
    high: int = 0
    medium: int = 0

    @property
    def clean(self) -> bool:
        return not self.findings


def scan_payloads(payloads: Sequence[Any], strict: bool = False) -> ScanReport:
    """Flag volatile substrings in the cacheable prefix of each payload.

    ``strict`` makes medium-severity findings count as failures too. By default
    only high-severity findings (timestamps, epochs, UUIDs, clock times) fail.
    """
    texts = [prompt_text(p) for p in payloads]
    all_findings: list[dict[str, Any]] = []
    for idx, text in enumerate(texts):
        for finding in find_volatility(text):
            enriched = dict(finding)
            enriched["payload"] = idx
            all_findings.append(enriched)
    counted = [f for f in all_findings if strict or f["severity"] == "high"]
    return ScanReport(
        payloads=len(payloads),
        prefix_chars=sum(len(t) for t in texts),
        findings=counted,
        high=sum(1 for f in counted if f["severity"] == "high"),
        medium=sum(1 for f in counted if f["severity"] == "medium"),
    )


@dataclass
class DiffReport:
    before_prefix_chars: int
    after_prefix_chars: int
    before_min_chars: int
    after_min_chars: int
    before_ratio: float
    after_ratio: float
    churned_tokens_est: int
    regressed: bool

    def cost_delta_per_1k(self, price_per_mtok: float) -> float:
        return (self.churned_tokens_est * 1000 / 1_000_000) * price_per_mtok


def diff_payloads(before: Sequence[Any], after: Sequence[Any]) -> DiffReport:
    """Compare the byte-stable cacheable prefix of two payload sets.

    ``ratio`` = shared-prefix length / shortest prompt length within a set. A
    drop means the *after* set caches less of its prefix than the *before* set
    did -- the signature of a PR that made the prefix less stable. The churned
    token estimate is the shrink in stable prefix, in estimated tokens.
    """
    b_texts = [prompt_text(p) for p in before]
    a_texts = [prompt_text(p) for p in after]
    b_prefix = common_prefix_len(b_texts)
    a_prefix = common_prefix_len(a_texts)
    b_min = min((len(t) for t in b_texts), default=0)
    a_min = min((len(t) for t in a_texts), default=0)
    b_ratio = (b_prefix / b_min) if b_min else 0.0
    a_ratio = (a_prefix / a_min) if a_min else 0.0
    churn_chars = max(0, b_prefix - a_prefix)
    return DiffReport(
        before_prefix_chars=b_prefix,
        after_prefix_chars=a_prefix,
        before_min_chars=b_min,
        after_min_chars=a_min,
        before_ratio=round(b_ratio, 4),
        after_ratio=round(a_ratio, 4),
        churned_tokens_est=est_tokens(churn_chars),
        regressed=a_ratio < b_ratio,
    )


# --------------------------------------------------------------------------
# I/O + CLI
# --------------------------------------------------------------------------


def load_payloads(path: Path) -> list[Any]:
    """Load payloads from a .jsonl (one per line) or a .json array/object file."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        out: list[Any] = []
        for lineno, raw in enumerate(text.splitlines(), start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"error: {path} line {lineno}: {exc}") from exc
        return out
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: {path}: {exc}") from exc
    return obj if isinstance(obj, list) else [obj]


def _cmd_scan(args: argparse.Namespace) -> int:
    payloads: list[Any] = []
    for path in args.paths:
        payloads.extend(load_payloads(Path(path)))
    if not payloads:
        raise SystemExit("error: no payloads to scan")
    report = scan_payloads(payloads, strict=args.strict)

    if args.json:
        print(
            json.dumps(
                {
                    "payloads": report.payloads,
                    "prefix_chars": report.prefix_chars,
                    "high": report.high,
                    "medium": report.medium,
                    "findings": report.findings,
                    "clean": report.clean,
                },
                indent=2,
            )
        )
        return 0 if report.clean else 1

    print(
        f"cache_guard scan: {report.payloads} payload(s), "
        f"{report.prefix_chars:,} chars of cacheable prefix"
    )
    if report.clean:
        print("  OK: no volatile content found in the cacheable prefix.")
        return 0
    for f in report.findings:
        print(
            f"  [{f['severity'].upper():6}] {f['rule']} at char {f['at_char']} "
            f"in payload {f['payload']}: {f['match']!r}"
        )
        print(f"           -> {f['hint']}")
    print(
        f"  {report.high} high / {report.medium} medium finding(s). "
        f"Volatile content in a cached prefix invalidates the cache every request."
    )
    return 1


def _cmd_diff(args: argparse.Namespace) -> int:
    before = load_payloads(Path(args.before))
    after = load_payloads(Path(args.after))
    if not before or not after:
        raise SystemExit("error: both --before and --after need at least one payload")
    report = diff_payloads(before, after)
    delta = report.cost_delta_per_1k(args.price_per_mtok)
    price_note = f"at ${args.price_per_mtok:g}/Mtok" + (
        " (EXAMPLE price -- pass --price-per-mtok)"
        if args.price_per_mtok == DEFAULT_PRICE_PER_MTOK
        else ""
    )

    if args.json:
        print(
            json.dumps(
                {
                    "before_ratio": report.before_ratio,
                    "after_ratio": report.after_ratio,
                    "before_prefix_chars": report.before_prefix_chars,
                    "after_prefix_chars": report.after_prefix_chars,
                    "churned_tokens_est": report.churned_tokens_est,
                    "regressed": report.regressed,
                    "cost_delta_per_1k_requests_est": round(delta, 4),
                    "price_per_mtok": args.price_per_mtok,
                },
                indent=2,
            )
        )
        return 1 if report.regressed else 0

    print("cache_guard diff: stable cacheable prefix, before vs after")
    print(
        f"  before: {report.before_ratio:.2%} stable "
        f"({report.before_prefix_chars:,}/{report.before_min_chars:,} chars)"
    )
    print(
        f"  after:  {report.after_ratio:.2%} stable "
        f"({report.after_prefix_chars:,}/{report.after_min_chars:,} chars)"
    )
    if not report.regressed:
        print("  OK: the cacheable prefix did not get less stable.")
        return 0
    print(
        f"  REGRESSION: the stable prefix shrank by ~{report.churned_tokens_est:,} "
        f"estimated tokens per request (~{CHARS_PER_TOKEN_EST} chars/token)."
    )
    print(f"  Estimated extra cost: ${delta:,.2f} per 1,000 requests {price_note}.")
    print(
        "  This is an ESTIMATE from the byte diff, not a measured bill. Supply your "
        "own price and request volume for your real number."
    )
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cache_guard",
        description="Catch prompt changes that silently break caching, before they cost money.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="flag volatile content in a prompt's cacheable prefix")
    scan.add_argument("paths", nargs="+", help="one or more .json/.jsonl payload files")
    scan.add_argument("--strict", action="store_true", help="fail on medium-severity findings too")
    scan.add_argument("--json", action="store_true")
    scan.set_defaults(func=_cmd_scan)

    diff = sub.add_parser(
        "diff", help="compare cacheable-prefix stability before vs after a change"
    )
    diff.add_argument("--before", required=True, help="payload file for the baseline")
    diff.add_argument("--after", required=True, help="payload file for the change under test")
    diff.add_argument(
        "--price-per-mtok",
        type=float,
        default=DEFAULT_PRICE_PER_MTOK,
        help="input token price per million, for the cost estimate",
    )
    diff.add_argument("--json", action="store_true")
    diff.set_defaults(func=_cmd_diff)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
