#!/usr/bin/env python3
"""Is the site that is SERVED the site that is in this repository?

Nothing else in this project answers that. CI proves the repo is consistent;
the digest files prove `site/install.sh` matches `site/install.sh.sha256` on
disk. Neither looks at what a visitor actually receives, and the gap between
those two things is where this project spent a whole day:

  * The deployed pages were a day older than the repo, so six audit findings
    cited line numbers that were wrong by 100-470 lines and several "fixed"
    defects were still live.
  * Cloudflare's Email Address Obfuscation rewrote `--email you@example.com`
    inside a <pre> into an obfuscation link, so the published install command
    was broken for every visitor while the origin served the correct bytes.

The second one is why this is not `sha256sum`. An edge legitimately transforms
a page, so a byte comparison against a proxied site fails forever and gets
switched off. This compares against the repo copy with the KNOWN, ENUMERATED
transformations applied, and fails on anything else. A new edge feature turned
on in a dashboard shows up here as an unexplained difference, which is exactly
what you want to hear about.

It then asks the same question about the service rather than the pages: does the
gateway answering `/health` name the version this repository ships? Files and
container are deployed by different acts and go stale independently, so a site
that matches to the byte says nothing about the build behind it.

Usage:
    python tools/drift_check.py                      # against yangble5.com
    python tools/drift_check.py --base https://host  # against a staging host

Exit status is 0 only when every published file matches and the live gateway
does not name a different build. A gateway that cannot be asked at all is
reported, but is not a failure: not every deployment has one. Run it from a
machine that is NOT the origin: resolving the name to the origin skips the edge,
which is the thing being tested.
"""

from __future__ import annotations

import argparse
import ast
import json
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = pathlib.Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
DEFAULT_BASE = "https://yangble5.com"

# Files a visitor can fetch. `.sha256` files are included deliberately: a
# published digest that no longer describes the published script is worse than
# no digest at all, because the documented verification step then fails for
# honest users and teaches them to skip it.
PUBLISHED = (
    "index.html",
    "verify.html",
    "install.sh",
    "install.sh.sha256",
    "install.ps1",
    "install.ps1.sha256",
    "uninstall.sh",
    "uninstall.sh.sha256",
    "uninstall.ps1",
    "uninstall.ps1.sha256",
    # The agent-facing surface. AGENTS.md is what the published one-liner points
    # an AI at, so it is the single document most worth substituting: an agent
    # that fetches it will do what it says, on somebody's machine, without a
    # human reading it first. A stale or replaced copy here is worse than a
    # stale landing page, not better, which is why it is checked like the
    # installers rather than treated as documentation.
    "AGENTS.md",
    "llms.txt",
    "sitemap.xml",
    ".well-known/security.txt",
    # The yb5 release. YANGBLE5-SKILL.md is to the skill what AGENTS.md is to the
    # installer — the sheet an agent acts on — and the archive/.sha256 pair is
    # only meaningful when the served bytes are the repository's bytes, so all
    # four are compared like the installers. normalise() is a no-op on bytes
    # that carry no email_off marker, so for the archive this is an exact
    # byte-for-byte comparison — and the `got == raw_local` fallback below
    # already covers a served copy the edge left untouched.
    "skill/index.html",
    "skill/YANGBLE5-SKILL.md",
    "skill/yangble5-skill-v1.0.0.tar.gz",
    "skill/yangble5-skill-v1.0.0.tar.gz.sha256",
)

# robots.txt is PUBLISHED but is deliberately NOT in the list above, and the
# omission is written down because an unexplained gap in an enumerated list is
# indistinguishable from an oversight.
#
# Cloudflare's managed robots.txt PREPENDS its own block to the origin's, so on
# this deployment the served bytes legitimately differ from the repository's and
# a byte comparison would fail on every run. Adding it here would make this tool
# permanently red over a dashboard setting nobody can change from a checkout,
# and a gate that is always red is a gate that gets switched off -- which would
# take the other eighteen files down with it.
#
# It is covered instead by `deploy/smoke_test.sh` check 12b, which compares the
# served DIRECTIVE lines against site/robots.txt and WARNS, naming exactly what
# was injected. If the managed robots.txt is ever turned off, move it up into
# PUBLISHED and delete this note.
UNCHECKED_BECAUSE_THE_EDGE_REWRITES_IT = ("robots.txt",)

# Files that live under site/ and must NOT reach the webroot at all. This is the
# third and last bucket, and the three together are required to account for every
# tracked file in site/ -- see test_the_published_set_accounts_for_the_whole_tree.
#
# The reason that test exists rather than a note asking people to remember: this
# list is what stops a file from being SERVED WITHOUT BEING CHECKED. A new file
# under site/ is deployed by the publish command whether or not anyone adds it to
# PUBLISHED, so an omission here is not a gap in coverage that someone notices --
# it is a live URL that no gate in this repository has an opinion about.
NOT_DEPLOYED: dict[str, str] = {
    "README.md": (
        "the long-form documentation for the site, kept beside what it "
        "describes and read on GitHub. site/robots.txt says in as many words "
        "that there is no /README.md and that the URL has always answered 404, "
        "so publishing it would make a crawler policy that is committed to this "
        "repository into a lie. The publish command in deploy/GO_LIVE.md copies "
        "site/ wholesale, which is why that runbook has to remove this file by "
        "name -- and why a test checks that it still does."
    ),
}

# Transformations the edge is ALLOWED to apply, applied to the repo copy before
# comparing. Each entry needs a reason, because every entry is a hole: anything
# listed here is a difference this check will no longer report.
#
#   email_off markers -- Cloudflare consumes these two comments and leaves the
#   address alone. They exist precisely to stop the rewrite that corrupted the
#   install command, so their removal is expected and their ABSENCE from the
#   repo copy would be the bug.
EDGE_STRIPS = (
    b"<!--email_off-->",
    b"<!--/email_off-->",
)


def normalise(raw: bytes) -> bytes:
    for marker in EDGE_STRIPS:
        raw = raw.replace(marker, b"")
    return raw


def fetch(url: str, timeout: float) -> tuple[bytes | None, str]:
    # urlopen honours file:, ftp: and anything else registered, so `--base
    # file:///etc` would happily "pass" this check by reading local files. The
    # answer is an allowlist rather than a lint suppression: this tool exists to
    # test what the internet is served, and http(s) is the only thing that can
    # answer that question.
    scheme = urllib.parse.urlsplit(url).scheme
    if scheme not in ("http", "https"):
        return None, f"refusing to fetch a {scheme or 'schemeless'} URL"

    request = urllib.request.Request(  # noqa: S310 - scheme allowlisted above
        url,
        headers={
            # Default urllib identifies itself as Python-urllib, which some
            # edges answer differently or block outright. Ask for the page a
            # visitor gets.
            "User-Agent": "yangble5-drift-check",
            "Accept": "*/*",
            # Asks the edge not to answer from cache. Sent because a cached copy
            # would make this check pass on a stale deploy -- but do not rely on
            # it: this site's CDN ignores both headers from ordinary clients,
            # which was measured on 2026-07-26 when a cached 404 survived them
            # and only a changed URL got through. That is why a 404 is re-probed
            # with a cache-buster rather than believed. See probe().
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    try:
        # Safe by the scheme allowlist at the top of this function: `url` cannot
        # be file: or a custom scheme by the time it reaches here.
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.read(), ""
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, f"unreachable: {exc}"


# A 404 has two completely different causes with two completely different
# remedies, and reporting the wrong one sends somebody to redeploy a site that
# is already correct -- or, far worse, tells them the deploy worked when the
# file is unreachable.
#
#   the deploy did not happen    -> origin does not have the file. Redeploy.
#   the edge cached a 404        -> origin has it; the CDN is holding a negative
#                                   entry from a probe made before publication.
#                                   Wait for it to expire, or purge that URL.
#
# They are told apart by asking for the same path under a URL the edge has never
# seen. If that answers, the origin has the file and only the cache is wrong.
# The nonce must differ between runs: a fixed buster would itself get a cached
# 404 on the first pre-publication run and be useless from then on.
CACHE_BUSTER_PARAM = "drift-check-cache-bypass"
SAFE_LOCAL_SITE_PREFIXES = (".pytest_cache/",)


def _nonce() -> str:
    """A value the edge has not cached. Injectable so tests stay deterministic."""
    return str(int(time.time()))


def site_worktree_problems(root: pathlib.Path = ROOT) -> list[str]:
    """Untracked or ignored site payloads that ``cp -a site/.`` would publish."""
    command = [
        "git",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignored=matching",
        "--",
        "site/",
    ]
    completed = subprocess.run(  # noqa: S603 - fixed argv, shell=False
        command,
        cwd=root,
        capture_output=True,
        timeout=60,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        return [f"git status could not inspect site/: {detail or 'unknown error'}"]

    problems = []
    for raw in completed.stdout.split(b"\0"):
        if len(raw) < 4 or raw[2:3] != b" ":
            continue
        status = raw[:2].decode("ascii", errors="replace")
        if status not in ("??", "!!"):
            continue
        path = raw[3:].decode("utf-8", errors="surrogateescape").replace("\\", "/")
        relative = path.removeprefix("site/")
        if any(
            relative == prefix.rstrip("/") or relative.startswith(prefix)
            for prefix in SAFE_LOCAL_SITE_PREFIXES
        ):
            continue
        problems.append(f"{status} {path}")
    return problems


def compare(name: str, got: bytes, local: Path) -> str | None:
    """None when the served bytes are this repository's bytes, else what differs."""
    want = normalise(local.read_bytes())
    if got == want:
        return None
    raw_local = local.read_bytes()
    if got == raw_local:
        # Only possible if the page has no markers to strip; treat as fine.
        return None
    detail = f"{len(got)} bytes served, {len(want)} expected"
    index = next(
        (i for i in range(min(len(got), len(want))) if got[i] != want[i]),
        min(len(got), len(want)),
    )
    return (
        f"{name}: served copy differs ({detail}), first at byte {index}\n"
        f"      repo : {want[max(0, index - 50) : index + 90]!r}\n"
        f"      live : {got[max(0, index - 50) : index + 90]!r}"
    )


def probe(base: str, name: str, timeout: float, nonce: str) -> tuple[bytes | None, str, bool]:
    """Fetch a published path. Returns (bytes, error, served_only_off_cache_bypass).

    The third value is True only when the ordinary URL 404s and the cache-bypass
    URL succeeds -- that is, when visitors cannot reach a file the origin is
    serving correctly. It is still a failure; it just is not the failure the
    plain 404 message would have named.
    """
    got, error = fetch(f"{base}/{name}", timeout)
    if got is not None or error != "HTTP 404":
        return got, error, False

    bypass, _ = fetch(f"{base}/{name}?{CACHE_BUSTER_PARAM}={nonce}", timeout)
    if bypass is None:
        # Both URLs 404: the origin genuinely does not have it. Report the
        # original error, not the probe's -- the probe is an implementation
        # detail and naming it would send the reader chasing a query string.
        return None, error, False
    return bypass, "", True


# ── the other half of "is what is live what was shipped" ───────────────────
#
# Everything above compares FILES. Nothing in this project compared the running
# SERVICE against the one this repository ships, and the two go stale
# independently: the site is republished by copying a directory, the gateway
# changes only when its container is rebuilt and restarted. A deploy that does
# one and not the other leaves a repository describing a build that is not
# answering -- and, unlike a stale page, a stale gateway is missing every fix
# released since it started.
#
# This is the same defect shape as the one PUBLISHED had: the loop below had
# exactly one source of names, so it could only ever report on things somebody
# had already thought to list. No line of this tool mentioned the API at all,
# so a gateway three releases behind was not a finding anyone could have
# suppressed -- it was a question nothing here asked.
#
# The routes and their order come from site/README.md's "/api/health contract":
# the status widget on index.html tries exactly this sequence. Asking the way a
# visitor's browser asks is the point; a private route would prove something
# about a surface nobody uses.
HEALTH_ROUTES = ("/api/health", "/health", "/healthz")

# gateway/__init__.py, not pyproject.toml, for two reasons. It is what `/health`
# actually renders -- the handler builds its response from the package version --
# so this compares like with like. And it is Python, which a stdlib-only tool
# can read on the 3.10 floor the scheduled job pins; `tomllib` arrived in 3.11.
# tests/test_release_version.py already locks this value to pyproject's, so
# reading the mirror does not create a second source of truth.
GATEWAY_INIT = ROOT / "gateway" / "__init__.py"

# Three outcomes, kept apart deliberately. Folding the third into either of the
# others is the same error as counting an unrecorded cache write as zero: "the
# live build is 0.1.0" and "nothing here could tell me the live build" call for
# different actions, and only one of them is a deploy.
BUILD_MATCH = "match"
BUILD_DRIFT = "drift"
BUILD_UNKNOWN = "unknown"


def repo_gateway_version(init: Path = GATEWAY_INIT) -> str | None:
    """The version this repository's gateway would report, or None if unreadable.

    None rather than an exception: a fork that vendored only site/ still deserves
    the file comparisons above, and a build check that cannot name an expected
    value has nothing to say rather than something wrong. Exactly one string
    literal counts -- two assignments mean the last one wins at import time and
    this parse would have to guess which, and a guess here would be compared
    against production.
    """
    try:
        tree = ast.parse(init.read_text(encoding="utf-8"), filename=str(init))
    except (OSError, ValueError, SyntaxError):
        # SyntaxError is named explicitly: it descends from Exception, not from
        # ValueError, so the obvious two-name tuple lets a half-written package
        # crash a run whose real job is comparing eighteen files.
        # UnicodeDecodeError needs no entry -- it is a ValueError.
        return None
    found = [
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        )
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        and node.value.value
    ]
    if len(found) != 1:
        return None
    return found[0]


def live_build(base: str, timeout: float, expected: str) -> tuple[str, str]:
    """(status, message) for the gateway answering `base`, against `expected`.

    Unlike the browser widget, which takes the first route that RESPONDS, this
    takes the first route that yields a usable version and keeps going
    otherwise. A 200 of HTML from an edge fallback is an answer to a different
    question; stopping there would report "could not tell" while /healthz sat
    two lines below naming the build.

    An unknown is not a failure. A static-only or self-hosted deployment has no
    gateway to ask, and failing those runs would make this tool useless to the
    people most likely to run it against their own host.
    """
    attempts: list[str] = []
    for route in HEALTH_ROUTES:
        raw, error = fetch(f"{base}{route}", timeout)
        if raw is None:
            attempts.append(f"{route}: {error}")
            continue
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            attempts.append(f"{route}: answered, but not with JSON ({exc})")
            continue
        if not isinstance(payload, dict):
            attempts.append(f"{route}: answered with JSON that is not an object")
            continue
        served = payload.get("version")
        if not isinstance(served, str) or not served:
            # A bool is an int, a number is not a version, and a missing field is
            # not a version either. None of them may be coerced into one: a
            # coerced value would be compared against `expected` and could match.
            attempts.append(f"{route}: answered without a usable 'version' field")
            continue
        if served == expected:
            return BUILD_MATCH, f"live gateway reports {served}, the build this repo ships"
        return (
            BUILD_DRIFT,
            f"{route} reports version {served}; this repository ships {expected}. "
            "The service the public is using is not the build that was released.",
        )
    return (
        BUILD_UNKNOWN,
        "no health route named a build: " + "; ".join(attempts),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare the served site and the running gateway against this repository.",
        epilog="Run this from outside the origin host, or it proves nothing.",
    )
    parser.add_argument("--base", default=DEFAULT_BASE, help=f"default {DEFAULT_BASE}")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--quiet", action="store_true", help="only report problems")
    parser.add_argument(
        "--local-tree-only",
        action="store_true",
        help="check deployable site/ worktree hygiene without touching the network",
    )
    args = parser.parse_args(argv)

    worktree_problems = site_worktree_problems()
    if worktree_problems:
        print(
            "\nsite/ contains untracked or ignored payloads that cp -a would publish:\n",
            file=sys.stderr,
        )
        for problem in worktree_problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "\nRemove or commit them before deployment. .pytest_cache is the only "
            "ignored local artifact exempted.",
            file=sys.stderr,
        )
        return 1
    if args.local_tree_only:
        if not args.quiet:
            print("site/ worktree: OK — no untracked or ignored deploy payloads")
        return 0

    base = args.base.rstrip("/")
    problems: list[str] = []
    edge_cached_404: list[str] = []
    nonce = _nonce()

    for name in PUBLISHED:
        local = SITE / name
        if not local.exists():
            problems.append(f"{name}: listed as published but missing from site/")
            continue

        got, error, bypassed = probe(base, name, args.timeout, nonce)
        if got is None:
            problems.append(f"{name}: {error}")
            continue

        difference = compare(name, got, local)
        if bypassed:
            # The bytes were still compared, so a file that is BOTH unreachable
            # and wrong reports both facts rather than hiding one behind the
            # other.
            edge_cached_404.append(
                difference
                if difference
                else f"{name}: origin serves the expected bytes; only the cached 404 is stale"
            )
            continue

        if difference:
            problems.append(difference)
            continue

        if not args.quiet:
            print(f"  ok      {name}")

    expected_build = repo_gateway_version()
    if expected_build is None:
        build_status = BUILD_UNKNOWN
        build_message = (
            f"{GATEWAY_INIT.name} does not carry exactly one __version__ string, so there is "
            "no shipped version to compare the live service against"
        )
    else:
        build_status, build_message = live_build(base, args.timeout, expected_build)

    if build_status == BUILD_UNKNOWN:
        # Printed even under --quiet, which reports "only problems". An unanswered
        # question is not an answer: a run that could not check the build must never
        # look like a run that checked it and found it fine.
        print(f"\nbuild check inconclusive: {build_message}", file=sys.stderr)
    elif build_status == BUILD_MATCH and not args.quiet:
        print(f"  ok      {build_message}")

    if edge_cached_404:
        print(
            f"\n{len(edge_cached_404)} file(s) that visitors cannot reach, "
            f"but which {base} is serving correctly:\n",
            file=sys.stderr,
        )
        for item in edge_cached_404:
            print(f"  - {item}", file=sys.stderr)
        print(
            "\nEach of these 404s at its normal URL and succeeds with a cache-buster,\n"
            "which means the origin has the file and the CDN is holding a negative\n"
            "cache entry -- almost always because something requested the URL before\n"
            "it was published. THE FIX IS NOT TO REDEPLOY. Wait for the entry to\n"
            "expire, or purge that one URL at the edge. To avoid causing it: publish\n"
            "first, verify second, and never probe a public URL that does not exist\n"
            "yet.",
            file=sys.stderr,
        )

    if problems:
        print(f"\n{len(problems)} problem(s) between {base} and this repo:\n", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "\nEither the deploy did not happen, or the edge is transforming the\n"
            "response in a way EDGE_STRIPS does not describe. Do not add to\n"
            "EDGE_STRIPS to silence this without understanding what changed --\n"
            "the last time the edge rewrote a page, it corrupted the install\n"
            "command shown to every visitor.",
            file=sys.stderr,
        )

    if build_status == BUILD_DRIFT:
        # Its own block, not appended to `problems`: the remedy above is "publish
        # site/ again", and doing that would change nothing here. Naming the wrong
        # fix is how the cached-404 message sent someone to redeploy a correct site.
        print("\nthe live gateway is not the build this repo ships:\n", file=sys.stderr)
        print(f"  - {build_message}", file=sys.stderr)
        print(
            "\nCopying site/ again will NOT fix this. The pages are files; the\n"
            "gateway is a container that has to be rebuilt and restarted. Until it\n"
            "is, every fix released since that build is absent from the service\n"
            "the public is using, while this repository describes it as shipped.",
            file=sys.stderr,
        )

    if problems or edge_cached_404 or build_status == BUILD_DRIFT:
        return 1

    if not args.quiet:
        print(f"\n{len(PUBLISHED)} published files match {base}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
