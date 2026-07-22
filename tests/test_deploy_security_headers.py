"""The guard over the security response headers, in all three places they live.

On 2026-07-22 https://yangble5.com served exactly ONE security header —
``strict-transport-security: max-age=31536000``, without ``includeSubDomains``,
which is Cloudflare's value and not this repo's.  No Content-Security-Policy, no
``X-Content-Type-Options``, no ``X-Frame-Options``, no ``Referrer-Policy``, and
``/install.sh`` served as ``application/octet-stream`` with nothing telling the
browser not to sniff it.  ``deploy/nginx/yangble5.com.conf.example`` PART 2j
declares all eight mandatory.  Nothing errored, and ``deploy/smoke_test.sh`` was
green, because the only header it looked for was HSTS — and on a Cloudflare
proxied zone, Cloudflare supplies an HSTS header of its own.  A presence test
for "any security header at all" cannot tell "origin configured" from "CDN
default", so the test passed on an origin that set nothing.

The headers now live in three files that must agree: the standalone Caddy
deployment (``deploy/Caddyfile``), the nginx snippet (PART 2j), and the
includable unit an operator is meant to apply as one atomic thing
(``deploy/nginx/security-headers.conf``).  Three copies is two chances to
drift, so the drift is what is asserted here.

These tests read the shipped files.  They cannot prove the running config on any
particular host is correct — nothing in a repository can.  What they prove is
that the artefacts an operator applies are internally consistent, that the CSP
hashes match the pages actually in ``site/``, and that the post-deploy check
which would have caught the live gap is still in ``smoke_test.sh``.
"""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NGINX_SNIPPET = ROOT / "deploy" / "nginx" / "yangble5.com.conf.example"
NGINX_HEADERS = ROOT / "deploy" / "nginx" / "security-headers.conf"
CADDYFILE = ROOT / "deploy" / "Caddyfile"
SMOKE = ROOT / "deploy" / "smoke_test.sh"
INSTALL = ROOT / "deploy" / "install.sh"
ENV_EXAMPLE = ROOT / "deploy" / ".env.example"
SITE = ROOT / "site"

# The set the live deployment was missing.  Names only; values are compared
# between files rather than pinned here, so that changing a policy is one edit
# and not a game of hunt-the-duplicate.
MANDATORY = (
    "Strict-Transport-Security",
    "X-Content-Type-Options",
    "X-Frame-Options",
    "Referrer-Policy",
    "Content-Security-Policy",
    "Cross-Origin-Opener-Policy",
    "Cross-Origin-Resource-Policy",
    "Permissions-Policy",
)


# ── nginx parsing ──────────────────────────────────────────────────────────

_ADD_HEADER = re.compile(
    r"""^\s*add_header\s+          # directive
        (?P<name>[A-Za-z0-9_-]+)\s+
        (?P<value>.*?)             # quoted string, or a $variable
        (?:\s+always)?\s*;\s*$""",
    re.VERBOSE,
)


def _strip_comment(line: str) -> str:
    """Drop a trailing ``#`` comment.

    Crude on purpose: no directive in either nginx file carries a ``#`` inside
    a quoted value, and a parser that handled that case would be a parser we
    would then have to trust.
    """
    return line.split("#", 1)[0] if "#" in line else line


def _add_headers(path: Path) -> dict[str, str]:
    """``{header name: value}`` for every add_header, with its brace depth 0.

    Depth is tracked so that a header declared inside a ``location`` is not
    silently counted as though it applied to the whole server — which is the
    exact nginx semantics people get wrong, and the reverse of it (a location
    with its own add_header dropping the inherited set) is asserted separately.
    """
    out: dict[str, str] = {}
    depth = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = _strip_comment(raw)
        m = _ADD_HEADER.match(line)
        if m and depth == 0:
            out[m["name"]] = m["value"].strip().strip('"')
        depth += line.count("{") - line.count("}")
    return out


def _locations_with_add_header(path: Path) -> list[str]:
    """Names of ``location`` blocks that declare an add_header of their own."""
    bad: list[str] = []
    stack: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = _strip_comment(raw)
        loc = re.match(r"\s*location\s+(.*?)\s*\{", line)
        for _ in range(line.count("{")):
            stack.append(loc.group(1) if loc else "<block>")
        if stack and _ADD_HEADER.match(line):
            bad.append(stack[-1])
        for _ in range(line.count("}")):
            if stack:
                stack.pop()
    return bad


def _caddy_headers(path: Path) -> dict[str, str]:
    """``{header name: value}`` from the Caddyfile's ``header { }`` blocks."""
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = _strip_comment(raw).strip()
        m = re.match(r'^(?:-?)([A-Za-z0-9-]+)\s+"(.*)"\s*$', line)
        if m and m.group(1) in MANDATORY:
            out[m.group(1)] = m.group(2)
    return out


# ── CSP hash recomputation ─────────────────────────────────────────────────

_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


def _inline_script_hashes(page: Path) -> list[str]:
    """The ``sha256-...`` tokens a browser will demand for ``page``."""
    text = page.read_text(encoding="utf-8")
    return [
        "sha256-" + base64.b64encode(hashlib.sha256(m.group(1).encode()).digest()).decode()
        for m in _INLINE_SCRIPT.finditer(text)
    ]


ALL_CSP_SOURCES = pytest.mark.parametrize(
    "getter",
    [
        pytest.param(lambda: _add_headers(NGINX_HEADERS), id="nginx/security-headers.conf"),
        pytest.param(lambda: _add_headers(NGINX_SNIPPET), id="nginx/yangble5.com.conf.example"),
        pytest.param(lambda: _caddy_headers(CADDYFILE), id="Caddyfile"),
    ],
)


# ── the headers are declared at all, in every deployment path ──────────────

@ALL_CSP_SOURCES
@pytest.mark.parametrize("header", MANDATORY)
def test_header_is_declared(getter, header):
    """Every mandatory header, in every config an operator can deploy.

    Deleting one here is how the live gap happened; it must be red, not quiet.
    """
    declared = getter()
    assert header in declared, (
        f"{header} is not declared. The live deployment served none of "
        f"{', '.join(MANDATORY)} and nothing anywhere errored — that silence "
        f"is why this is asserted per header. Declared: {sorted(declared)}")


def test_nginx_include_and_snippet_agree():
    """The includable file and PART 2j must be the same policy, not two.

    They exist as two copies deliberately: the include is atomic and cannot be
    half-applied, and the inline copy means a naive whole-file paste is still
    secure. Two copies is one chance to drift, so drift is the failure.
    """
    inc = _add_headers(NGINX_HEADERS)
    snippet = _add_headers(NGINX_SNIPPET)
    assert inc == snippet, (
        "deploy/nginx/security-headers.conf and PART 2j of "
        "yangble5.com.conf.example declare different headers. An operator who "
        "uses the include gets a different policy from one who pastes.\n"
        f"only in security-headers.conf: {sorted(set(inc) - set(snippet))}\n"
        f"only in the snippet: {sorted(set(snippet) - set(inc))}\n"
        f"differing values: "
        f"{sorted(k for k in set(inc) & set(snippet) if inc[k] != snippet[k])}")


def test_caddy_and_nginx_agree_on_every_mandatory_header():
    """Standalone (Caddy) and behind-proxy (nginx) must not diverge.

    Same product, same promise, two deployment paths. A user cannot see which
    one they got.
    """
    caddy = _caddy_headers(CADDYFILE)
    nginx = _add_headers(NGINX_HEADERS)
    for header in MANDATORY:
        assert caddy.get(header) == nginx.get(header), (
            f"{header} differs between deploy/Caddyfile and "
            f"deploy/nginx/security-headers.conf:\n"
            f"  caddy: {caddy.get(header)!r}\n"
            f"  nginx: {nginx.get(header)!r}")


def test_no_location_declares_its_own_add_header():
    """The nginx gotcha, asserted rather than merely commented.

    ``add_header`` does not merge across levels: ONE add_header inside a
    location drops every server-level add_header for that location. A config
    that does this loses its whole security header set on exactly the paths
    that have special handling, and reports no error.
    """
    bad = _locations_with_add_header(NGINX_SNIPPET)
    assert bad == [], (
        f"these location blocks declare an add_header of their own: {bad}. "
        f"Each one silently drops ALL {len(MANDATORY)} server-level security "
        f"headers on that location. Put the header at server level, or "
        f"re-include security-headers.conf inside the location.")


# ── the CSP actually pins the pages that exist ─────────────────────────────

def test_csp_pins_every_inline_script_on_the_site():
    """A hash pin that does not match the page is a policy that breaks it.

    Deliberately checked against ONE file, ``security-headers.conf``, and not
    against all three. ``tools/sitecheck.py`` already checks the pages against
    ``deploy/Caddyfile``, ``deploy/nginx/yangble5.com.conf.example`` and
    ``site/README.md``; it does not know about ``security-headers.conf``,
    which is the gap this closes. Asserting the same thing twice would mean
    two tests to satisfy on every page edit and no extra safety — the
    cross-file agreement tests above already carry the correctness from here
    to the other two.

    If ``tools/sitecheck.py`` is ever taught about ``security-headers.conf``,
    delete this test rather than keeping both.

    The failure it catches: ``site/index.html`` and ``site/verify.html`` each
    run one inline ``<script>``. If a hash is stale the page still RENDERS and
    only its copy buttons, OS detection and live pool status stop working —
    silently, explained nowhere but a browser console nobody opens.
    """
    csp = _add_headers(NGINX_HEADERS).get("Content-Security-Policy", "")
    assert csp, "no Content-Security-Policy in deploy/nginx/security-headers.conf"
    missing = []
    for page in sorted(SITE.glob("*.html")):
        for want in _inline_script_hashes(page):
            if want not in csp:
                missing.append(f"{page.name}: '{want}'")
    assert not missing, (
        "deploy/nginx/security-headers.conf does not pin these inline scripts, "
        "so they will not run:\n  " + "\n  ".join(missing)
        + "\nFix all three configs in one go:\n"
          "    bash deploy/nginx/recompute-csp-hashes.sh\n"
          "Do NOT add 'unsafe-inline' to script-src on a page whose job is "
          "convincing a visitor it is safe to paste a command into a shell.")


@ALL_CSP_SOURCES
def test_csp_script_src_does_not_allow_unsafe_inline(getter):
    """``script-src 'unsafe-inline'`` makes the sha256 pinning decorative."""
    csp = getter().get("Content-Security-Policy", "")
    assert csp, "no Content-Security-Policy to check"
    script_src = re.search(r"script-src([^;]*)", csp)
    assert script_src, f"no script-src directive in the CSP: {csp!r}"
    assert "unsafe-inline" not in script_src.group(1), (
        "script-src allows 'unsafe-inline'. Every sha256 pin next to it is "
        "then decorative, and the page can be made to run injected script.")


def test_hsts_is_not_cloudflares_default_value():
    """The value must be ours, so a presence check cannot be fooled by the CDN.

    The live site's ``max-age=31536000`` with no ``includeSubDomains`` was
    Cloudflare's, not this repo's. Asserting the distinguishing token is what
    lets smoke_test tell "origin configured" from "CDN default".
    """
    value = _add_headers(NGINX_HEADERS)["Strict-Transport-Security"]
    assert "includeSubDomains" in value, (
        "HSTS no longer carries includeSubDomains, which is the only token "
        "distinguishing this repo's header from the one Cloudflare adds by "
        "itself. Without it, a header-presence check goes green against an "
        "origin that sets nothing at all.")


# ── the post-deploy check that would have caught it ────────────────────────

@pytest.mark.parametrize("header", MANDATORY)
def test_smoke_test_verifies_each_header_from_off_host(header):
    """smoke_test.sh must assert every header, or the gap ships again."""
    text = SMOKE.read_text(encoding="utf-8")
    assert re.search(rf"^{re.escape(header)}\|", text, re.M), (
        f"deploy/smoke_test.sh check 9 does not verify {header}. That check is "
        f"the only thing standing between this repo and shipping a deployment "
        f"whose security headers are absent — which already happened once.")


def test_smoke_test_does_not_pass_on_mere_hsts_presence():
    """The original false green, named so it cannot come back.

    The old check was ``grep -i '^strict-transport-security:'`` and passed on
    any value. Cloudflare supplies one, so it passed on an origin serving
    nothing.
    """
    text = SMOKE.read_text(encoding="utf-8")
    assert "Strict-Transport-Security|includeSubDomains" in text, (
        "smoke_test.sh no longer checks the HSTS VALUE. A presence-only check "
        "goes green on Cloudflare's own HSTS header while the origin serves "
        "no security headers at all.")


# ── the other half: open registration on personal OAuth credentials ────────

def test_installer_refuses_open_registration_without_a_licence_assertion():
    """docs/OPERATING_A_PUBLIC_SERVICE.md §1, enforced instead of merely stated.

    The pre-existing gate on ``open`` asked only about MONEY (a global monthly
    budget). Money caps bound what abuse can cost; they say nothing about what
    abuse can get suspended, and the suspension lands on the upstream account,
    not on yangble5.
    """
    text = INSTALL.read_text(encoding="utf-8")
    assert "YANGBLE5_POOL_LICENSED_FOR_THIRD_PARTIES" in text, (
        "deploy/install.sh no longer gates REGISTRATION_MODE=open on the "
        "operator asserting the upstream pool is licensed for serving third "
        "parties. Without it the installer will happily configure the exact "
        "arrangement docs/OPERATING_A_PUBLIC_SERVICE.md §1 forbids.")
    # The die() must be reachable from the open branch, not merely mentioned.
    open_branch = text.split('if [ "$mode" = "open" ]', 1)
    assert len(open_branch) == 2, "the open-registration branch is gone from install.sh"
    assert "YANGBLE5_POOL_LICENSED_FOR_THIRD_PARTIES" in open_branch[1].split("\n}", 1)[0], (
        "the licence assertion is no longer checked inside the "
        "REGISTRATION_MODE=open branch.")


def test_env_example_defaults_the_licence_assertion_to_no():
    """Default must be the safe answer: an operator who reads nothing gets it."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert re.search(r"^YANGBLE5_POOL_LICENSED_FOR_THIRD_PARTIES=no\s*$", text, re.M), (
        "deploy/.env.example must ship "
        "YANGBLE5_POOL_LICENSED_FOR_THIRD_PARTIES=no. Defaulting it to yes, or "
        "omitting it, turns install.sh's gate into a no-op for every fresh "
        "install — which is every install that matters.")
