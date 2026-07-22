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

Usage:
    python tools/drift_check.py                      # against yangble5.com
    python tools/drift_check.py --base https://host  # against a staging host

Exit status is 0 only when every published file matches. Run it from a machine
that is NOT the origin: resolving the name to the origin skips the edge, which
is the thing being tested.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

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
)

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
            # A cached copy would make this check pass on a stale deploy, which
            # is the failure it exists to catch.
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare the served site against this repository.",
        epilog="Run this from outside the origin host, or it proves nothing.",
    )
    parser.add_argument("--base", default=DEFAULT_BASE, help=f"default {DEFAULT_BASE}")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--quiet", action="store_true", help="only report problems")
    args = parser.parse_args(argv)

    base = args.base.rstrip("/")
    problems: list[str] = []

    for name in PUBLISHED:
        local = SITE / name
        if not local.exists():
            problems.append(f"{name}: listed as published but missing from site/")
            continue

        want = normalise(local.read_bytes())
        got, error = fetch(f"{base}/{name}", args.timeout)
        if got is None:
            problems.append(f"{name}: {error}")
            continue

        if got == want:
            if not args.quiet:
                print(f"  ok      {name}")
            continue

        raw_local = local.read_bytes()
        detail = f"{len(got)} bytes served, {len(want)} expected"
        if got == raw_local:
            # Only possible if the page has no markers to strip; treat as fine.
            if not args.quiet:
                print(f"  ok      {name}")
            continue
        index = next(
            (i for i in range(min(len(got), len(want))) if got[i] != want[i]),
            min(len(got), len(want)),
        )
        problems.append(
            f"{name}: served copy differs ({detail}), first at byte {index}\n"
            f"      repo : {want[max(0, index - 50) : index + 90]!r}\n"
            f"      live : {got[max(0, index - 50) : index + 90]!r}"
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
        return 1

    if not args.quiet:
        print(f"\n{len(PUBLISHED)} published files match {base}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
