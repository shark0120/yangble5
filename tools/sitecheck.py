#!/usr/bin/env python3
"""sitecheck.py — the whole of the static-page validation for ``site/``.

Usage
-----
    python tools/sitecheck.py            # self-test, then check the pages
    python tools/sitecheck.py --self-test  # self-test only

Exit codes
----------
0   the self-test passed and every page is clean
1   the self-test passed and a page has a finding
2   the CHECKER'S OWN SELF-TEST FAILED — no page result is reported at all,
    because a checker that cannot be shown to fail certifies nothing

Why the self-test is not optional
---------------------------------
This file replaces a numeral guard that was structurally unable to fail.  Its
regex carried a negative lookbehind containing ``.``, so in ``99.53`` it saw
``99`` (discarded as under three digits) and never saw ``53`` at all.  Every
percentage on the site — the headline 99.53% cache hit rate included — was
invisible to the only automated check over published numbers.  Three entries
in its allow-list (``9953``, ``000``, ``7460``) were unreachable by
construction, which is what a guard looks like when nobody has ever watched it
fail.  So: the self-test runs first, it asserts both directions (a bogus
figure IS reported, the authoritative figure is NOT), and a failure there is a
harder error than any page finding.

The coverage invariant
----------------------
THE SET OF FILES PRESENT MUST EQUAL THE SET OF FILES CLASSIFIED.
The page list used to be a literal tuple, ``FILES = ("index.html",
"verify.html")``, which made coverage opt-in by filename: the guard looked at
whatever somebody last remembered to type, and the share of ``site/`` it
covered shrank every time a file was added.  That was not a hypothetical.
``site/README.md``, ``site/install.sh`` and ``site/install.ps1`` each publish
``99.53%`` and ``748,918`` — two of them are 75 KB scripts users pipe into a
shell — and not one of them was ever read by this checker.  Coverage is now
discovered and total: every file under ``site/`` is an HTML page, a
text-bearing file, or named in ``EXEMPT`` with a written reason, and anything
else is a finding.  A new file cannot be born outside the guard.

The tokeniser invariant
-----------------------
THE SET OF CHARACTERS CONSUMED MUST EQUAL THE SET OF CHARACTERS CHECKED.
``scan_figures`` does not hunt for number-shaped things; it partitions the page
text into maximal ``[0-9A-Za-z_.,]`` atoms, classifies every atom that contains
a digit, and then ASSERTS that every ASCII digit in the text landed inside an
atom it produced.  A future edit that narrows the pattern cannot silently
reopen the hole — the digits it stops covering are reported by name.  The
assertion is itself exercised in the self-test by running the scanner with the
historical broken pattern and requiring it to complain.
"""

from __future__ import annotations

import contextlib
import pathlib
import re
import sys
from html.parser import HTMLParser

ROOT = pathlib.Path(__file__).resolve().parent.parent
SITE = ROOT / "site"

# ── which files the guard looks at (see "The coverage invariant" above) ─────
PAGE_SUFFIXES = (".html", ".htm")

# Suffixes whose contents are prose, configuration or script text that a
# published figure could plausibly be restated in.  Deliberately NOT .css/.js/
# .svg: those carry hundreds of geometry and percentage literals, and a guard
# that cries wolf on `100%` in a stylesheet is a guard nobody reads.  They are
# not silently ignored either — with no suffix rule they land in the "neither
# checked nor exempt" bucket below and somebody has to write down why.
TEXT_SUFFIXES = (
    ".bat",
    ".cmd",
    ".conf",
    ".csv",
    ".ini",
    ".json",
    ".md",
    ".ps1",
    ".sh",
    ".toml",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
)

_DIGEST_REASON = (
    "a bare SHA-256 digest and a filename: no prose, no figure, nothing that "
    "could misstate a measurement. The installer-digests CI job checks it "
    "against the payload it names."
)

# The escape hatch, and the only one.  An entry here is a decision somebody
# made in writing; a file with no entry and no known suffix is a finding.
EXEMPT: dict[str, str] = {
    "install.ps1.sha256": _DIGEST_REASON,
    "install.sh.sha256": _DIGEST_REASON,
    "uninstall.ps1.sha256": _DIGEST_REASON,
    "uninstall.sh.sha256": _DIGEST_REASON,
}

# The file that documents this guard, and therefore the one file that has to be
# able to print the figures the guard rejects.  See TEXT_ALLOW.
GUARD_DOC = "README.md"


def classify(site: pathlib.Path = SITE) -> tuple[list[str], list[str], list[str]]:
    """Partition every file under `site` into (pages, texts, problems).

    The coverage invariant, enforced: a file that is neither a page, nor a
    text-bearing file, nor exempt is reported by name.  So is an EXEMPT entry
    naming a file that no longer exists — a stale exemption is cover for the
    next file that lands on that name.
    """
    pages: list[str] = []
    texts: list[str] = []
    problems: list[str] = []
    if not site.is_dir():
        return pages, texts, [f"{site} is not a directory, so nothing was checked"]
    seen: set[str] = set()
    for path in sorted(site.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(site).as_posix()
        seen.add(rel)
        if rel in EXEMPT:
            continue
        suffix = path.suffix.lower()
        if suffix in PAGE_SUFFIXES:
            pages.append(rel)
        elif suffix in TEXT_SUFFIXES:
            texts.append(rel)
        else:
            problems.append(
                f"{rel}: neither checked nor exempt — the published-numbers "
                f"guard does not know what this file is, so every figure in it "
                f"is unguarded. Give it a checked suffix "
                f"({', '.join(PAGE_SUFFIXES + TEXT_SUFFIXES)}) or add it to "
                f"EXEMPT in tools/sitecheck.py with a reason."
            )
    for rel in sorted(set(EXEMPT) - seen):
        problems.append(
            f"EXEMPT names {rel!r}, which is not in {site} — a stale exemption "
            f"is cover for the next file that lands on that name"
        )
    return pages, texts, problems


# Discovered, not typed.  Kept under the old name because the CSP check, the
# inventory and the tests all read it.
FILES = tuple(classify()[0])

VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}

DIGITS = "0123456789"

# ── the authoritative measurement record (the ONLY permitted measurements) ──
# 2026-07-21, one Windows 11 machine, one run.  Nothing else may be published
# as a measurement; everything derived from it is RECOMPUTED below rather than
# transcribed, so a typo in a derived total is a finding, not a rounding.
PROMPT = [748918, 748933, 748948, 748963]  # rounds 1-4
CACHED = [0, 745438, 745430, 745422]  # rounds 1-4
ROUND_MS = [21410, 10753, 23457, 22381]  # rounds 1-4

_WARM_P, _WARM_C = sum(PROMPT[1:]), sum(CACHED[1:])
_ALL_P, _ALL_C = sum(PROMPT), sum(CACHED)

MEASURED: dict[str, str] = {}


def _measured(value: object, why: str) -> None:
    MEASURED.setdefault(str(value), why)


for _i in range(4):
    _measured(PROMPT[_i], f"round {_i + 1} prompt tokens")
    _measured(CACHED[_i], f"round {_i + 1} tokens read from cache")
    _measured(ROUND_MS[_i], f"round {_i + 1} round-trip ms")
    _measured(
        PROMPT[_i] - CACHED[_i], f"round {_i + 1} uncached tail = {PROMPT[_i]} - {CACHED[_i]}"
    )
_measured(_WARM_P, "warm prompt total = rounds 2+3+4")
_measured(_WARM_C, "warm cached total = rounds 2+3+4")
_measured(_WARM_P - _WARM_C, "warm uncached tail = warm prompt - warm cached")
_measured(_ALL_P, "all-four prompt total")
_measured(_ALL_C, "all-four cached total")
_measured(_ALL_P - _ALL_C, "all-four uncached tail")
# The page also writes the prefix size as rounded shorthand.  Derive it, so a
# page that says 750K fails; do not allow-list the string.
for _p in PROMPT:
    _measured(f"{round(_p / 1000)}K", f"prefix shorthand for {_p:,} tokens")

# ── percentages, recomputed rather than allow-listed ────────────────────────
# A hit rate is accepted only if some authoritative cached/prompt pair, printed
# to the same number of decimal places the page used, is exactly that string.
# No pair rounds to 99.54 at any precision, so 99.54 cannot pass.
PERCENT: dict[str, str] = {}
_PAIRS = [(f"round {i + 1}", CACHED[i], PROMPT[i]) for i in range(4)] + [
    ("warm token-weighted", _WARM_C, _WARM_P),
    ("all four rounds", _ALL_C, _ALL_P),
]
for _label, _c, _p in _PAIRS:
    _r = 100.0 * _c / _p
    for _dp in (1, 2):
        PERCENT.setdefault(f"{_r:.{_dp}f}", f"{_label} hit rate = {_c}/{_p} = {_r:.4f}%")

# A PAGE may print a hit rate only to the 1 or 2 places a page would publish.
# Files that quote this checker's own output also carry the 4-place provenance
# string it prints (`= 99.5333%`).  Recompute those separately rather than
# widening PERCENT, so nothing changes about what a page is allowed to say.
TEXT_PERCENT: dict[str, str] = dict(PERCENT)
for _label, _c, _p in _PAIRS:
    _r = 100.0 * _c / _p
    TEXT_PERCENT.setdefault(f"{_r:.4f}", f"{_label} hit rate to 4 dp = {_c}/{_p}")

# ── non-measurement numerals that legitimately appear as page text ──────────
# Every entry must be REACHED by a real run.  An entry nothing matches is
# reported as a finding: an allow-list that is not exercised is a wish list,
# and dead entries are exactly how the previous version hid the fact that it
# had never seen a percentage.
ALLOW: dict[str, str] = {
    "3.14.3": "Python 3.14.3 — the interpreter the run was made on",
    "7.1.23": "CLIProxyAPI 7.1.23 — the engine version under test",
    "7.2.93": "engine 7.2.93 — the version that made the shim workaround unnecessary",
    "2026": "2026-07-21, the measurement date",
    "2024": "quoted wrong answer from the Gemini upstream (no live web search)",
    "2025": "quoted wrong answer from the Grok upstream (no live web search)",
    "256": "shasum -a 256 / sha256",
    "400": "quoted upstream error 'API Error: 400'",
    "402": "HTTP 402 returned by the gateway when the shared pool is exhausted",
    "443": "80/443, the ports a pre-existing web server may already hold",
    "600": "chmod 600, a file mode",
    "700": "directory mode 700",
    "0600": "file mode 0600 as printed in the verify listing",
    "0700": "file mode 0700 as printed in the verify listing",
    "600000": "--prefix-tokens 600000 in the bench command, and the API_TIMEOUT_MS default",
    "1000000": "CLAUDE_CODE_MAX_CONTEXT_TOKENS / model_context_window default",
    "65536": "CLAUDE_CODE_MAX_OUTPUT_TOKENS default",
    "1M": "the 1,000,000-token context window the page is about",
    "200K": "the 200K window a client assumes for an unrecognised model name",
    "12h": "session-affinity TTL written as 12h",
}

# A digit-leading atom ending in one of these is a figure with a unit, not an
# identifier.  Without this, `749K` — the published prefix size — would be
# waved through as a name, which is how it escaped the previous checker.
UNITS = ("KB", "MB", "GB", "ms", "K", "M", "G", "B", "h", "s", "x")

# Maximal run of identifier/number characters.  Not a number pattern: a
# partition.  See the module docstring.
ATOM = re.compile(r"[0-9A-Za-z_](?:[0-9A-Za-z_.,]*[0-9A-Za-z_])?")
UNIT_FIGURE = re.compile(r"^[0-9][0-9.,]*(" + "|".join(UNITS) + r")$")
COMMA_FORM = re.compile(r"^[0-9]{1,3}(,[0-9]{3})+(\.[0-9]+)?$")

# The pattern this file replaces.  Kept ONLY so the self-test can prove the
# invariant assertion fires on it.  Never used to check a page.
HISTORICAL_BROKEN = re.compile(
    r"(?<![0-9A-Za-z_.])\d{1,3}(?:,\d{3})+(?![0-9A-Za-z_])"
    r"|(?<![0-9A-Za-z_.])\d+(?![0-9A-Za-z_])"
)

# ── the three shapes a published measurement wears outside an HTML page ─────
# A percentage, a comma-grouped total, a figure carrying a unit.  See
# check_text for why a text file is not put through the whole page audit.
TEXT_PERCENT_RE = re.compile(r"(?<![0-9A-Za-z_.,])([0-9]+(?:\.[0-9]+)?)%")
TEXT_GROUPED_RE = re.compile(r"(?<![0-9A-Za-z_.,])([0-9]{1,3}(?:,[0-9]{3})+)(?![0-9A-Za-z_])")
TEXT_UNIT_RE = re.compile(
    r"(?<![0-9A-Za-z_.,])([0-9][0-9.,]*(?:" + "|".join(UNITS) + r"))(?![0-9A-Za-z_])"
)


def scan_figures(text: str, atom_re: re.Pattern = ATOM):
    """Partition `text` and return (figures, invariant_problems).

    `figures` is a list of (canonical, as_written).  `invariant_problems` is
    non-empty when a digit in `text` was not covered by any token the scanner
    produced — i.e. when the set consumed stopped equalling the set checked.
    """
    figures: list[tuple[str, str]] = []
    covered = bytearray(len(text))
    for m in atom_re.finditer(text):
        start, end = m.span()
        for i in range(start, end):
            covered[i] = 1
        atom = m.group(0)
        if not any(c in DIGITS for c in atom):
            continue
        has_letter = any(c.isalpha() or c == "_" for c in atom)
        if has_letter and not UNIT_FIGURE.match(atom):
            continue  # sha256, i5, 11400H, index.html, shark0120
        figures.append((atom.replace(",", ""), atom))

    problems: list[str] = []
    uncovered: list[str] = []
    for i, ch in enumerate(text):
        if ch in DIGITS:
            if not covered[i]:
                uncovered.append(
                    f"{ch!r} at offset {i} (context {text[max(0, i - 12) : i + 12]!r})"
                )
        elif ch.isdigit():
            problems.append(
                f"INVARIANT: non-ASCII digit {ch!r} (U+{ord(ch):04X}) in page "
                f"text at offset {i}; the figure scanner only understands "
                f"ASCII digits, so this numeral would never be checked"
            )
    if uncovered:
        problems.append(
            "INVARIANT VIOLATED: the set of characters consumed is not the "
            "set checked — " + str(len(uncovered)) + " digit(s) fell outside "
            "every token the scanner produced: " + "; ".join(uncovered[:8])
        )
    return figures, problems


def account_figures(figures, used: set[str]) -> list[str]:
    """Rule every figure in or out against the authoritative record."""
    problems: list[str] = []
    unknown: dict[str, str] = {}
    for canonical, raw in figures:
        if "," in raw and not COMMA_FORM.match(raw):
            problems.append(
                f"malformed thousands separator: {raw!r} — grouped digits must "
                f"be 1-3 then groups of exactly 3"
            )
            continue
        n_digits = sum(c in DIGITS for c in canonical)
        is_decimal = "." in canonical
        has_unit = bool(UNIT_FIGURE.match(canonical))
        # 1- and 2-digit bare integers are not figures.  Every decimal and
        # every unit-bearing figure is checked regardless of length: those are
        # the classes every hit-rate and every capacity claim belongs to.
        if not is_decimal and not has_unit and n_digits < 3:
            continue
        for table in (MEASURED, PERCENT, ALLOW):
            if canonical in table:
                used.add(canonical)
                break
        else:
            unknown.setdefault(canonical, raw)
    for canonical, raw in sorted(unknown.items()):
        problems.append(f"unaccounted figure: {canonical}  (as written: {raw})")
    return problems


def check_text(name: str, text: str, used: set[tuple[str, str]]) -> list[str]:
    """Published-figure claims in a file under site/ that is not an HTML page.

    NOT the page audit, and the difference is deliberate.  A 75 KB installer
    legitimately carries ports, HTTP status codes, file modes, retry counts and
    array offsets; demanding a provenance for each of them would bury the two
    figures that matter under ninety that do not, and a guard nobody reads is
    the exact failure mode this file exists to prevent.  So only the three
    shapes a published measurement wears are checked — a percentage, a
    comma-grouped total, a figure with a unit — plus the non-ASCII digit
    invariant, because a numeral the scanner cannot see is worse than one it
    rejects.  A bare 1- or 2-digit integer is left alone, exactly as it is on a
    page.

    `used` collects (file, token) pairs so a per-file allowance that nothing
    matches is reported as drift, the same rule the page allow-list lives by.
    """
    allow = TEXT_ALLOW.get(name, {})
    problems: list[str] = []

    def rule(token: str, kind: str, accounted: bool) -> None:
        if token in allow:
            used.add((name, token))
        elif not accounted:
            problems.append(
                f"unaccounted {kind}: {token} — not in the authoritative "
                f"measurement record, and not allow-listed for this file"
            )

    for m in TEXT_PERCENT_RE.finditer(text):
        value = m.group(1)
        if "." not in value and len(value) < 3:
            continue  # `50%` in prose is not a measurement claim
        rule(value, "percentage", value in TEXT_PERCENT)
    shaped = ((TEXT_GROUPED_RE, "grouped figure"), (TEXT_UNIT_RE, "figure with a unit"))
    for pattern, kind in shaped:
        for m in pattern.finditer(text):
            canonical = m.group(1).replace(",", "")
            rule(canonical, kind, any(canonical in t for t in (MEASURED, PERCENT, ALLOW)))

    for i, ch in enumerate(text):
        if ch.isdigit() and ch not in DIGITS:
            if ch in allow:
                used.add((name, ch))
            else:
                problems.append(
                    f"non-ASCII digit {ch!r} (U+{ord(ch):04X}) at offset {i}: the "
                    f"figure scanner only understands ASCII digits, so a number "
                    f"written this way would never be checked"
                )
    # The same figure repeated forty times is one finding, not forty.
    return sorted(set(problems))


class Doc(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.errors = [], []
        self.ids, self.dupes = set(), []
        self.refs = []  # (kind, target)
        self.external = []
        self.inline_handlers = []
        self.styles = self.scripts = 0
        self.lang = None
        self.buttons_no_type = 0
        self.text = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html":
            self.lang = a.get("lang")
        if tag == "style":
            self.styles += 1
        if tag == "script":
            self.scripts += 1
            if a.get("src"):
                self.external.append(f"script src={a['src']}")
        if tag in ("style", "script"):
            self._skip += 1
        if tag == "button" and "type" not in a:
            self.buttons_no_type += 1
        if a.get("id"):
            if a["id"] in self.ids:
                self.dupes.append(a["id"])
            self.ids.add(a["id"])
        for k in a:
            if k.startswith("on"):
                self.inline_handlers.append(f"<{tag} {k}=>")
        for k in ("aria-controls", "aria-labelledby", "data-copy-target", "data-copy-status"):
            if a.get(k):
                for tok in a[k].split():
                    self.refs.append((k, tok))
        href = a.get("href", "")
        if href.startswith("#") and len(href) > 1:
            self.refs.append(("href", href[1:]))
        # only *subresources* count: a canonical/alternate <link> and ordinary
        # <a href> are navigations, not fetches the browser makes for us.
        subresource = tag in ("img", "script", "iframe", "source", "video", "audio") or (
            tag == "link"
            and a.get("rel", "").lower() not in ("canonical", "alternate", "author", "license")
        )
        for k in ("src", "href"):
            v = a.get(k, "")
            if subresource and re.match(r"^(https?:)?//", v):
                self.external.append(f"<{tag} {k}={v}>")
        if tag not in VOID:
            self.stack.append((tag, self.getpos()))

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if tag in ("style", "script"):
            self._skip -= 1
        if not self.stack:
            self.errors.append(f"line {self.getpos()[0]}: stray </{tag}>")
            return
        if self.stack[-1][0] == tag:
            self.stack.pop()
        else:
            t, p = self.stack[-1]
            self.errors.append(f"line {self.getpos()[0]}: </{tag}> closes <{t}> opened line {p[0]}")

    def handle_data(self, data):
        if self._skip == 0:
            self.text.append(data)


def check_page(name: str, src: str, used: set[str]) -> list[str]:
    """Every check, run over one page's source.  The self-test drives this
    exact function, so the guard and the thing the guard is trusted for are
    never two different code paths."""
    d = Doc()
    d.feed(src)
    d.close()
    problems: list[str] = []

    problems += d.errors
    problems += [f"<{t}> never closed (line {p[0]})" for t, p in d.stack]
    problems += [f"duplicate id: {i}" for i in d.dupes]

    for kind, target in d.refs:
        if target not in d.ids:
            problems.append(f'{kind}="{target}" has no matching id')

    for m in re.finditer(r'getElementById\(\s*"([^"]+)"\s*\)', src):
        if m.group(1) not in d.ids:
            problems.append(f'getElementById("{m.group(1)}") has no matching id')

    problems += [f"external subresource: {e}" for e in d.external]
    problems += [f"inline event handler: {h}" for h in d.inline_handlers]
    for m in re.finditer(r"@import|url\(\s*['\"]?https?:", src):
        problems.append(f"external CSS reference: {m.group(0)}")

    if d.styles != 1:
        problems.append(f"expected exactly 1 <style>, found {d.styles}")
    if d.scripts != 1:
        problems.append(f"expected exactly 1 <script>, found {d.scripts}")
    if not d.lang:
        problems.append("<html> has no lang attribute")
    if d.buttons_no_type:
        problems.append(f"{d.buttons_no_type} <button> without type=")

    # Separator, not "".  Concatenating adjacent text nodes fuses the end of
    # one against the start of the next: "…yangble" + "5 rounds" becomes the
    # atom "yangble5" and the figure is silently reclassified as a name.  That
    # is the same consumed-is-not-checked failure in a different place, and it
    # was in the version this file replaces.  A separator can only ever SPLIT a
    # figure, which is loud (an unaccounted fragment), never silent.
    text = "\n".join(d.text)
    figures, invariant = scan_figures(text)
    problems += invariant
    problems += account_figures(figures, used)
    return problems


# ── CSP hashes: recomputed and compared, not grepped for a literal ─────────
# The recipe this replaces printed the recomputed hash and then grepped the
# deploy configs for a hash spelled out in the prose.  Change an inline script
# and the recompute prints something new while the grep -- still carrying the
# old literal -- reports 1/1/2 and looks green.  The comparison only ever
# happened in the reader's head.  Here the recomputed value IS the needle, and
# a consumer carrying any OTHER script hash is reported as stale, which is the
# case a presence-only grep cannot see.
CSP_CONSUMERS = ("deploy/Caddyfile", "deploy/nginx/yangble5.com.conf.example", "site/README.md")
# A CSP hash source-expression includes its quotes: script-src 'sha256-…='.
# Scanning for the quoted form is what the directive actually is, and it keeps
# prose (this checker's own self-test output, quoted in site/README.md) from
# being read as a directive.
SCRIPT_HASH = re.compile(r"'(sha256-[A-Za-z0-9+/]{43}=)'")


class _InlineScripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.grab = False
        self.out = []

    def handle_starttag(self, tag, attrs):
        self.grab = tag == "script" and not dict(attrs).get("src")

    def handle_data(self, data):
        if self.grab:
            self.out.append(data)

    def handle_endtag(self, tag):
        if tag == "script":
            self.grab = False


def csp_hashes(src: str) -> list[str]:
    """CSP source-expression hashes for a page's inline scripts.

    Parser, not regex: a literal '<script' inside a comment or string makes a
    regex hash the wrong bytes, and the only symptom is a blocked script on the
    deployed page.
    """
    import base64
    import hashlib

    p = _InlineScripts()
    p.feed(src)
    p.close()
    return [
        "sha256-" + base64.b64encode(hashlib.sha256(s.encode()).digest()).decode() for s in p.out
    ]


def csp_problems(pages=None, consumers=None) -> list[str]:
    if pages is None:
        pages = {f: (SITE / f).read_text(encoding="utf-8") for f in FILES}
    if consumers is None:
        consumers = {c: (ROOT / c).read_text(encoding="utf-8") for c in CSP_CONSUMERS}
    current: dict[str, str] = {}
    for name, src in pages.items():
        for h in csp_hashes(src):
            current[h] = name

    problems = []
    for path, text in consumers.items():
        found = set(SCRIPT_HASH.findall(text))
        for h in sorted(found - set(current)):
            problems.append(
                f"{path}: stale inline-script hash {h} — no page produces it, "
                f"so the deployed CSP would block the script it names"
            )
        for h, page in sorted(current.items()):
            if h not in found:
                problems.append(
                    f"{path}: missing the current {page} inline-script hash "
                    f"{h} — recompute and update this file"
                )
    return problems


def unused_allow_problems(used: set[str]) -> list[str]:
    """An allow-list entry nothing on the site matches is drift, and it is the
    tell that the guard was never watched.  Report it."""
    return [
        f"allow-list entry never matched (stale, or the checker cannot see it): {k!r} — {why}"
        for k, why in sorted(ALLOW.items())
        if k not in used
    ]


def unused_text_allow_problems(used: set[tuple[str, str]]) -> list[str]:
    """Same rule as the page allow-list, applied per file.

    Only the EXPLICIT entries are held to it.  The fixture-derived ones
    (TEXT_ALLOW below) follow MUST_FAIL mechanically, and requiring the
    documentation to name every fixture would couple two things that have no
    reason to move together.
    """
    return [
        f"text allow-list entry never matched (stale, or the checker cannot "
        f"see it): {name}: {token!r} — {why}"
        for name, entries in sorted(TEXT_ALLOW_EXPLICIT.items())
        for token, why in sorted(entries.items())
        if (name, token) not in used
    ]


# ───────────────────────────── self-test ─────────────────────────────────────

_PAGE = (
    '<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
    "<title>t</title><style>.a{color:red}</style></head><body>"
    "<p>@@</p><script>void 0;</script></body></html>"
)


def _page(payload: str) -> str:
    """A structurally clean page carrying `payload` as its only text, so the
    only problems the self-test can see are numeral problems."""
    return _PAGE.replace("@@", payload)


# (name, page text, substring that MUST appear in some problem)
MUST_FAIL = [
    ("bogus hit rate 99.54", "命中率 99.54%", "99.54"),
    ("bogus cold hit rate 0.01", "冷輪 0.01%", "0.01"),
    ("bogus integer", "12345", "12345"),
    ("bogus prompt total", "748,919", "748919"),
    ("bogus derived warm cached total", "2,236,291", "2236291"),
    ("bogus round-trip ms", "21,411 ms", "21411"),
    ("bogus prefix shorthand", "~750K 前綴", "750K"),
    ("bogus context claim", "3M 上下文", "3M"),
    ("malformed thousands separator", "74,8918", "thousands separator"),
    # The full-width digits below are the fixture, not a typo: a page that
    # renders them must be reported, not silently skipped.
    ("full-width digits evade an ASCII scanner", "９９.５４%", "non-ASCII digit"),  # noqa: RUF001
]

# (name, page text) — must produce NO problem at all
MUST_PASS = [
    ("authoritative warm hit rate", "暖輪 99.53% 命中"),
    ("authoritative cold hit rate", "冷輪 0.00%"),
    ("authoritative all-four hit rate", "2,236,290 / 2,995,762 = 74.6%"),
    ("authoritative prompt/cached/ms", "748,918 / 745,438 / 21,410 ms / 3,495"),
    ("prefix shorthand", "~749K 前綴"),
    ("config figures", "1M / 200K / 12h / 65536 / 1000000"),
    ("versions", "Python 3.14.3, CLIProxyAPI 7.1.23, engine 7.2.93"),
    ("identifiers are not figures", "sha256 i5-11400H shark0120 index.html yangble5"),
    ("short bare integers are not figures", "1 2 4 80 99"),
]


def _fixture_figures() -> dict[str, str]:
    """The figures this checker's own must-fail fixtures contain.

    site/README.md documents the negative control, so it has to be able to
    name the figures that control plants — it prints `99.54%` in prose as the
    thing CI proves the guard rejects.  Deriving that set from MUST_FAIL rather
    than retyping it means the only bogus figures the documentation may print
    are ones this guard demonstrably rejects on a page.  It is scoped to that
    one file: the same string in site/install.sh is still a finding.
    """
    out: dict[str, str] = {}
    for case, payload, _needle in MUST_FAIL:
        for pattern in (TEXT_PERCENT_RE, TEXT_GROUPED_RE, TEXT_UNIT_RE):
            for m in pattern.finditer(payload):
                out.setdefault(
                    m.group(1).replace(",", ""),
                    f"planted by the must-fail fixture {case!r}, which {GUARD_DOC} documents",
                )
    return out


# Per-file allowances, each with a reason, each held to unused_text_allow_problems.
TEXT_ALLOW_EXPLICIT: dict[str, dict[str, str]] = {
    GUARD_DOC: {
        "1ms": (
            "the ~1ms poll interval in the grab-a-copy-of-the-page recipe — a "
            "loop delay, not a measurement"
        ),
        # U+FF19 FULLWIDTH DIGIT NINE.  The fixture character is the key on
        # purpose: README quotes the transcript in which the guard names it.
        "９": (  # noqa: RUF001
            "this checker's own report of the full-width-digit fixture, quoted "
            "verbatim in the self-test transcript"
        ),
    },
}

TEXT_ALLOW: dict[str, dict[str, str]] = {k: dict(v) for k, v in TEXT_ALLOW_EXPLICIT.items()}
for _fig, _why in _fixture_figures().items():
    TEXT_ALLOW.setdefault(GUARD_DOC, {}).setdefault(_fig, _why)

# (name, file the payload is pretending to live in, file text, substring that
# MUST appear in some problem).  The filename matters: allowances are per file.
TEXT_MUST_FAIL = [
    ("a bogus hit rate in an installer", "install.sh", "warm rounds hit 99.6%", "99.6"),
    ("a bogus token total in an installer", "install.sh", "a 748,919-token prefix", "748919"),
    ("a bogus prefix shorthand in an installer", "install.sh", "~750K prefix", "750K"),
    ("a bogus context claim in an installer", "install.sh", "3M context", "3M"),
    # The allowance that lets the documentation quote the negative control must
    # not follow the figure into a file users pipe into a shell.
    ("a per-file allowance does not leak to another file", "install.sh", "99.54%", "99.54"),
    (
        "full-width digits evade an ASCII scanner",
        "install.sh",
        "９９.５３%",  # noqa: RUF001 - the fixture is the point
        "non-ASCII digit",
    ),
    ("a file with no per-file allowance of its own", "llms.txt", "hit rate 99.61%", "99.61"),
]

# (name, file, file text) — must produce NO problem at all
TEXT_MUST_PASS = [
    ("the authoritative warm hit rate", "install.sh", "99.53% — warm rounds only"),
    ("grouped totals", "install.sh", "748,918 tokens in a 1,000,000 window"),
    ("unit figures", "install.sh", "a ~749K prefix, 200K assumed, 12h affinity"),
    ("short bare percentages are prose, not claims", "install.sh", "0% 24% 50%"),
    ("a batch substring expression is not a percentage", "install.ps1", "%KEY:~0,24%"),
    ("the checker's own 4-place provenance", GUARD_DOC, "= 99.5333% and = 74.6485%"),
    ("the negative control, in the file that documents it", GUARD_DOC, "CI plants `99.54%`"),
]


def selftest(verbose: bool = True) -> bool:
    """Prove the guard can fail before trusting it to pass.

    A checker that has only ever printed OK is indistinguishable from a
    checker that cannot print anything else.
    """
    failures: list[str] = []

    def say(line: str) -> None:
        if verbose:
            print(line)

    say("self-test: the guard must fail on a bogus figure")
    for name, payload, needle in MUST_FAIL:
        used: set[str] = set()
        problems = check_page("<selftest>", _page(payload), used)
        hit = [p for p in problems if needle in p]
        if hit:
            say(f"    PASS  must-fail  {name}\n              -> {hit[0]}")
        else:
            failures.append(
                f"MUST-FAIL CASE DID NOT FAIL: {name}: payload {payload!r} "
                f"produced {problems!r}, expected a problem naming {needle!r}"
            )
            say(f"    FAIL  must-fail  {name}  (nothing named {needle!r})")

    say("self-test: the guard must pass on the authoritative record")
    for name, payload in MUST_PASS:
        used = set()
        problems = check_page("<selftest>", _page(payload), used)
        if not problems:
            say(f"    PASS  must-pass  {name}")
        else:
            failures.append(
                f"MUST-PASS CASE FAILED: {name}: payload {payload!r} produced {problems!r}"
            )
            say(f"    FAIL  must-pass  {name}  -> {problems}")

    say(
        "self-test: the consumed-equals-checked invariant must itself fail "
        "when the tokeniser narrows"
    )
    figs, inv = scan_figures("命中率 99.54%", HISTORICAL_BROKEN)
    if inv and any("INVARIANT VIOLATED" in p for p in inv):
        say(f"    PASS  invariant  historical pattern flagged\n              -> {inv[0][:150]}")
    else:
        failures.append(
            "INVARIANT ASSERTION IS DEAD: the historical broken pattern "
            f"produced figures={figs!r} problems={inv!r}; it must be reported "
            "as leaving digits unchecked"
        )
        say("    FAIL  invariant  historical pattern was not flagged")

    say("self-test: an unmatched allow-list entry must be reported")
    stale = unused_allow_problems(set())
    if len(stale) == len(ALLOW):
        say(f"    PASS  stale-allow  all {len(ALLOW)} entries reported when nothing matches them")
    else:
        failures.append(
            f"STALE-ALLOW CHECK IS DEAD: expected {len(ALLOW)} reports with an "
            f"empty used-set, got {len(stale)}"
        )
        say("    FAIL  stale-allow")

    say("self-test: a stale or missing CSP hash must be reported")
    _good = {"index.html": "<html><script>alert(1)</script></html>"}
    _h = csp_hashes(_good["index.html"])[0]
    _csp_cases = [
        ("correct", {"cfg": f"script-src '{_h}'"}, None),
        (
            "a bare hash in prose is not a directive",
            {"cfg": f"script-src '{_h}' -- was sha256-{'A' * 43}="},
            None,
        ),
        (
            "stale hash beside the right one",
            {"cfg": f"script-src '{_h}' 'sha256-{'A' * 43}='"},
            "stale",
        ),
        ("hash missing entirely", {"cfg": "script-src 'self'"}, "missing"),
    ]
    for _name, _cons, _needle in _csp_cases:
        got = csp_problems(_good, _cons)
        ok = (not got) if _needle is None else any(_needle in p for p in got)
        if ok:
            say(f"    PASS  csp  {_name}" + (f"\n              -> {got[0]}" if got else ""))
        else:
            failures.append(f"CSP CASE {_name!r} behaved wrong: {got!r}")
            say(f"    FAIL  csp  {_name} -> {got!r}")

    # ── the non-HTML cases, and why they are silent ─────────────────────────
    # These run on every invocation and turn the self-test red exactly like the
    # cases above.  They print nothing when they pass, and that is a compromise
    # rather than an oversight: site/README.md quotes this transcript verbatim
    # and tests/test_sitecheck.py holds it to that, so a new PASS line here
    # would make the published documentation stale the moment this file grew a
    # case.  Silence on success, loud on failure, and `--coverage` shows the
    # scope on demand.  tests/test_sitecheck.py proves they are not decorative
    # by breaking one and requiring selftest() to return False.
    for case, fname, payload, needle in TEXT_MUST_FAIL:
        got = check_text(fname, payload, set())
        if not any(needle in p for p in got):
            failures.append(
                f"TEXT MUST-FAIL CASE DID NOT FAIL: {case}: {payload!r} in "
                f"{fname} produced {got!r}, expected a problem naming {needle!r}"
            )
    for case, fname, payload in TEXT_MUST_PASS:
        got = check_text(fname, payload, set())
        if got:
            failures.append(
                f"TEXT MUST-PASS CASE FAILED: {case}: {payload!r} in {fname} produced {got!r}"
            )
    if not classify()[0]:
        failures.append(
            "NO PAGES DISCOVERED under site/ — the page set is discovered, so "
            "an empty one means this run would certify nothing while printing OK"
        )

    say("self-test: 99.54 must not be reachable from the record at any printed precision")
    if "99.54" in PERCENT or "99.54" in MEASURED or "99.54" in ALLOW:
        failures.append("99.54 IS ACCEPTED BY SOME TABLE — the record is wrong")
        say("    FAIL  record")
    else:
        say(f"    PASS  record  99.53 provenance: {PERCENT['99.53']}")

    if failures:
        print("\nSELF-TEST FAILED — the checker is not trustworthy, so no page result is reported.")
        for f in failures:
            print(f"  - {f}")
        return False
    say("self-test: OK\n")
    return True


def inventory() -> int:
    """Print every figure the guard sees and where each one is ruled in from.

    A guard whose coverage is invisible is as hard to trust as one that has
    never failed: "0 problems" reads the same whether it checked 36 figures or
    none.  This is how you confirm by eye that 99.53 is actually in scope.
    """
    rows: dict[str, tuple[set[str], set[str], str]] = {}
    for f in FILES:
        d = Doc()
        d.feed((SITE / f).read_text(encoding="utf-8"))
        d.close()
        figures, _ = scan_figures("\n".join(d.text))
        for canonical, raw in figures:
            n_digits = sum(c in DIGITS for c in canonical)
            if "." not in canonical and not UNIT_FIGURE.match(canonical) and n_digits < 3:
                continue
            why = (
                MEASURED.get(canonical)
                or PERCENT.get(canonical)
                or ALLOW.get(canonical)
                or "UNACCOUNTED"
            )
            seen_raw, seen_file, _ = rows.setdefault(canonical, (set(), set(), why))
            seen_raw.add(raw)
            seen_file.add(f)
    print(f"{'figure':12s} {'as written':14s} {'page':22s} ruled in by")
    print("-" * 110)
    for canonical in sorted(rows, key=lambda s: (len(s), s)):
        raws, files, why = rows[canonical]
        print(
            f"{canonical:12s} {'/'.join(sorted(raws)):14s} "
            f"{','.join(sorted(f[:-5] for f in files)):22s} {why}"
        )
    print(f"\n{len(rows)} distinct figures in scope.")
    return 0


def coverage(site: pathlib.Path = SITE) -> int:
    """Print how every file under `site` is checked, or why it is not.

    `--inventory` answers "which figures are in scope"; this answers the
    question one level up, "which FILES are in scope", which is the one the
    literal page tuple used to make unanswerable.
    """
    pages, texts, problems = classify(site)
    width = max([28, *(len(f) for f in pages + texts + list(EXEMPT))])
    for f in pages:
        print(f"{f:{width}s}  page audit: structure, references, every figure accounted")
    for f in texts:
        n = len(TEXT_ALLOW.get(f, {}))
        extra = f" ({n} allow-listed for this file)" if n else ""
        print(f"{f:{width}s}  figure claims: percentages, grouped totals, units{extra}")
    for f, why in sorted(EXEMPT.items()):
        print(f"{f:{width}s}  EXEMPT — {why}")
    print(
        f"\n{len(pages)} page(s), {len(texts)} text file(s), {len(EXEMPT)} exempt; "
        f"every file under {site.name}/ is accounted for."
        if not problems
        else f"\n{len(problems)} COVERAGE PROBLEM(S)"
    )
    for p in problems:
        print(f"    - {p}")
    return 1 if problems else 0


def main(argv: list[str]) -> int:
    # The report names figures as they appear on a Traditional-Chinese page.
    # Left on the platform default, that output is cp950 on a Windows dev box
    # and UTF-8 in CI, so the same run is byte-different depending on where it
    # ran -- and a caller that captures it as UTF-8 dies on the em-dash rather
    # than reading the finding.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, OSError, ValueError):
            stream.reconfigure(encoding="utf-8")
    if "--help" in argv or "-h" in argv:
        print(__doc__.strip())
        print(
            "\nOptions:\n"
            "  --self-test   run only the checker's own self-test\n"
            "  --inventory   list every figure in scope and its provenance\n"
            "  --coverage    list every FILE in scope and how it is checked\n"
            "  --site DIR    check a copy of the site somewhere else\n"
            "  --quiet       suppress the per-case self-test log\n"
            "  --help        this text"
        )
        return 0
    if not selftest(verbose="--quiet" not in argv):
        return 2
    if "--self-test" in argv:
        return 0

    # --site DIR checks a copy of the pages somewhere else. It exists so the
    # negative control (plant a bogus figure, require a red run) never has to
    # mutate the working tree and then restore it -- `git checkout -- <file>`
    # as a cleanup step silently discards whatever else was uncommitted in
    # that file, which is a destructive way to run a read-only check.
    site = SITE
    if "--site" in argv:
        i = argv.index("--site") + 1
        if i >= len(argv):
            print("--site needs a directory", file=sys.stderr)
            return 2
        site = pathlib.Path(argv[i])
        if not site.is_dir():
            print(f"--site {site} is not a directory", file=sys.stderr)
            return 2

    if "--inventory" in argv:
        return inventory()
    if "--coverage" in argv:
        return coverage(site)

    pages, texts, coverage_problems = classify(site)
    used: set[str] = set()
    used_text: set[tuple[str, str]] = set()
    rc = 0
    reports = []

    def read(name: str) -> str | None:
        """Undecodable bytes are a finding, not a traceback.

        A file the checker cannot read is a file it cannot check, and a
        UnicodeDecodeError escaping to the top would abandon every remaining
        page mid-run -- an unchecked site reported as a crash rather than as
        the coverage hole it is.
        """
        try:
            return (site / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            reports.append(
                (name, [f"cannot be read as UTF-8, so nothing in it was checked: {exc}"])
            )
            return None

    for f in pages:
        src = read(f)
        if src is not None:
            reports.append((f, check_page(f, src, used)))
    stale = unused_allow_problems(used)
    if site == SITE:
        # Only meaningful against the real tree: --site points at a copy whose
        # consumers (deploy/, README) are not copied with it.
        csp = csp_problems()
        reports.append(("CSP hashes", csp))

    # Text files, the coverage invariant and the per-file allow-list are
    # appended ONLY when they have something to say.  That silence is a
    # compromise, not an oversight: site/README.md quotes this program's stdout
    # verbatim and tests/test_sitecheck.py holds it to that, so an extra "OK"
    # line per installer would make the published documentation stale.
    # `--coverage` prints the whole scope on demand and CI runs it on every
    # push, so nothing here is invisible -- it is just not pinned into prose
    # that has to be re-pasted by hand.
    for f in texts:
        body = read(f)
        if body is not None and (problems := check_text(f, body, used_text)):
            reports.append((f, problems))
    if coverage_problems:
        reports.append(("site/ coverage", coverage_problems))
    text_stale = unused_text_allow_problems(used_text)
    if text_stale:
        reports.append(("text allow-list", text_stale))

    for f, problems in reports:
        if problems:
            rc = 1
            print(f"{f}: {len(problems)} PROBLEM(S)")
            for x in problems:
                print(f"    - {x}")
        else:
            print(f"{f}: OK")
    if stale:
        rc = 1
        print(f"allow-list: {len(stale)} PROBLEM(S)")
        for x in stale:
            print(f"    - {x}")
    else:
        print(f"allow-list: OK ({len(ALLOW)} entries, all matched)")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
