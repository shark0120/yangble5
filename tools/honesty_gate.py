#!/usr/bin/env python3
"""Honesty gate: make the repo's disclosure discipline machine-enforced.

WHY THIS EXISTS
---------------
This project's whole pitch is that its numbers are checkable rather than
believed. That is only credible if the discipline is enforced, not remembered.
A tired maintainer will eventually paste "99.53% cache hit rate" into a README
without its qualifiers, or let a borrowed shock statistic sneak back in. When
that happens the project becomes exactly the thing it criticises: a marketing
number with no conditions attached.

So the discipline is a test. CI fails if either of these appears in the prose
(``.md`` / ``.html`` / ``.txt``) the project publishes:

  1. The bundled skill (``yb5``, "the yangble5 skill") and "hit rate" / "cache
     hit" in the same paragraph. The skill's stable-prefix ratio is a structural
     proxy, never a cache-hit rate, and conflating the two is the single easiest
     way to overclaim. Note the asymmetry this rule depends on: ``yangble5`` on
     its own is the proxy stack, whose cache hit rate is measured and may be
     called one.
  2. A ``$38,000`` / ``$38k`` Bedrock-bill style hook. It is a borrowed number
     with no source in this repo; the project does not get to use other
     people's outrage as evidence.

WHAT THIS GATE DELIBERATELY DOES NOT DO
---------------------------------------
An earlier draft also failed on any ``99.5x`` figure that lacked a qualifier in
its paragraph. On this repository -- which is already disciplined -- that rule
produced only false positives: the number legitimately appears in table rows,
arithmetic, diagram nodes, a Chinese-language "this is an anecdote" disclaimer,
and prose whose qualifier sits one paragraph away. Telling a clean repo it is
lying, in eight places, is how an honesty check earns its own removal. Detecting
a genuinely *naked* marketing claim ("achieves 99.53%") apart from a reference,
a datum, or a caveat is not something a regex does without that false-positive
tax, so the headline-qualifier rule is left to human review rather than shipped
as a wolf-crier. This gate enforces only the two regressions it can catch with
zero false positives, so a green result means something.

Exit codes: 0 = clean; 1 = a violation; 2 = bad invocation.
Standard library only; no network.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

# Match the SKILL, and specifically not the project it is named after.
#
# The skill is namespaced under the project (`yb5` in code, "the yangble5 skill"
# in prose) rather than separately branded, which makes this pattern harder than
# the two names it replaced. Those were distinct words, so matching them was
# unambiguous. This one shares its stem with `yangble5` -- the proxy stack -- and
# the proxy stack measures a real, observed cache hit rate on real traffic and
# says so in a dozen places across this repo. That sentence is the honest claim
# this whole project is built on. A pattern loose enough to catch `yangble5`
# alone would fail the build on the truth, and a gate that fires on the truth is
# one somebody switches off within a week.
#
# So the rule needs the qualifier: bare `yangble5` is the proxy and is fine;
# `yb5` or "yangble5 skill" is the skill and is not. tests/test_honesty_gate.py
# pins both halves, because getting only one right leaves the gate either blind
# or intolerable.
SKILL_RE = re.compile(r"\byb5\b|\byangble5[\s\-]+skill\b", re.IGNORECASE)
HITRATE_RE = re.compile(r"hit[\s-]?rate|cache[\s-]?hit|命中率", re.IGNORECASE)

# The borrowed "$38,000 Bedrock bill" shock hook, in its currency-anchored forms.
# These are unambiguous: a dollar sign in front of the figure means money.
BORROWED_HOOK_RE = re.compile(r"\$\s*38[,.]?0{3}\b|\$\s*38\s*k\b", re.IGNORECASE)
# The same figure WITHOUT a currency sign ("38,000", "38k") only counts as the
# borrowed hook when a billing word shares the paragraph. Otherwise "38k tokens"
# or "38,000 requests" is an ordinary count, not the shock statistic -- and a
# gate that fails on "38k tokens" is a false positive that gets it switched off.
BARE_HOOK_RE = re.compile(r"\b38[,.]?0{3}\b|\b38\s*k\b", re.IGNORECASE)
BILL_CONTEXT_RE = re.compile(
    r"bill|bedrock|invoice|charge|overspen|\bcost|帳單|費用|美元|dollar", re.IGNORECASE
)

TEXT_SUFFIXES = {".md", ".html", ".txt"}

# Paths that legitimately discuss these strings as rules rather than use them:
# this gate itself, and its own test. Everything else is held to the discipline.
SELF_REFERENTIAL = {"tools/honesty_gate.py", "tests/test_honesty_gate.py"}


def prose_only(text: str) -> str:
    """Strip the regions where a bare number is legitimate and no qualifier can live.

    A headline figure inside a fenced code block, an arithmetic line, an SVG or
    mermaid diagram node, a markdown heading, or a table row is not a naked
    marketing claim -- the surrounding prose carries the conditions and the
    diagram/table/heading physically cannot. Enforcing "a qualifier in the same
    paragraph" on those produces false positives, and a gate that cries wolf on a
    clean repo gets switched off. So we remove those regions and hold only the
    running prose to the discipline.
    """
    # Fenced code blocks (``` and ~~~), including mermaid/sequence diagrams.
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"~~~.*?~~~", "", text, flags=re.DOTALL)
    # HTML/SVG diagram and script/style bodies, then any remaining tags.
    text = re.sub(r"<svg\b.*?</svg>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<(script|style)\b.*?</\1>", "", text, flags=re.DOTALL | re.IGNORECASE)
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):  # markdown heading -- a label, not a claim
            continue
        if stripped.startswith("|") or re.match(r"^[-+|: ]+$", stripped):  # table row/rule
            continue
        if stripped.startswith(">"):  # blockquote handled as prose below; keep
            pass
        lines.append(line)
    text = "\n".join(lines)
    # Drop inline HTML tags so an SVG <text>NN%</text> that survived isn't prose.
    text = re.sub(r"<[^>]+>", " ", text)
    return text


def paragraphs(text: str) -> list[str]:
    """Split prose on blank lines; a paragraph is the unit a qualifier must live in."""
    return re.split(r"\n\s*\n", prose_only(text))


def scan_text(text: str) -> list[dict[str, object]]:
    """Return honesty violations in a single document's text."""
    violations: list[dict[str, object]] = []
    for idx, para in enumerate(paragraphs(text)):
        if SKILL_RE.search(para) and HITRATE_RE.search(para):
            violations.append(
                {
                    "kind": "skill_called_hit_rate",
                    "paragraph": idx,
                    "detail": "the skill and 'hit rate'/'cache hit' in one paragraph; "
                    "the skill's stable-prefix ratio is a structural proxy, not a "
                    "cache-hit rate (the proxy stack's measured hit rate is a "
                    "different number and may be described as one)",
                }
            )
        if BORROWED_HOOK_RE.search(para) or (
            BARE_HOOK_RE.search(para) and BILL_CONTEXT_RE.search(para)
        ):
            violations.append(
                {
                    "kind": "borrowed_hook",
                    "paragraph": idx,
                    "detail": "a $38,000/$38k-style borrowed shock number has no "
                    "source in this repo and must not be used as evidence",
                }
            )
    return violations


def iter_text_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in TEXT_SUFFIXES or not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith((".git/", "node_modules/", ".venv/")):
            continue
        if rel in SELF_REFERENTIAL:
            continue
        yield path


def scan_repo(root: Path) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for path in iter_text_files(root):
        for violation in scan_text(path.read_text(encoding="utf-8", errors="replace")):
            enriched = dict(violation)
            enriched["file"] = path.relative_to(root).as_posix()
            findings.append(enriched)
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="honesty_gate",
        description="Fail if the repo's published prose breaks its disclosure discipline.",
    )
    parser.add_argument(
        "--root", default=".", help="repository root to scan (default: current dir)"
    )
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2

    findings = scan_repo(root)
    if not findings:
        print("honesty_gate: OK -- no skill/hit-rate conflation, no borrowed shock number.")
        return 0

    print(f"honesty_gate: {len(findings)} violation(s):")
    for f in findings:
        print(f"  [{f['kind']}] {f['file']} (paragraph {f['paragraph']})")
        print(f"      {f['detail']}")
    print(
        "\nThese are the specific ways this project would overclaim about itself. "
        "Add the missing qualifier, rename the skill's metric, or drop the borrowed number."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
