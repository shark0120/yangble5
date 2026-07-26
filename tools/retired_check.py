#!/usr/bin/env python3
"""Prove that site directories retired from Git history still return 404.

The paths are derived at runtime from deleted ``site/`` entries. They are not
copied into this file: a retired name belongs in history, not in current source.
The check needs a complete Git history and refuses to certify a shallow clone.

Usage:
    python tools/retired_check.py
    python tools/retired_check.py --base https://host

Exit status is 0 only when both the slash and no-slash form of every derived
directory return 404 twice: once normally and once with a fresh cache-buster.
403 and network/server failures are inconclusive, never evidence that a path is
gone.
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_BASE = "https://yangble5.com"
CACHE_BUSTER_PARAM = "retired-check-cache-bypass"


class HistoryUnavailable(RuntimeError):
    """The checkout cannot prove which site directories were deleted."""


def _git(root: pathlib.Path, *args: str) -> str:
    completed = subprocess.run(  # noqa: S603 - fixed executable, shell=False
        ["git", *args],  # noqa: S607 - Git has no portable absolute path
        cwd=root,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise HistoryUnavailable(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout


def retired_directories(root: pathlib.Path = ROOT) -> tuple[str, ...]:
    """Return shortest deleted site directories absent from the current tree."""
    shallow = _git(root, "rev-parse", "--is-shallow-repository").strip()
    if shallow != "false":
        raise HistoryUnavailable(
            "retired-path discovery needs complete Git history; checkout with fetch-depth: 0"
        )

    deleted = {
        line.strip()
        for line in _git(
            root,
            "log",
            "--all",
            "--diff-filter=D",
            "--name-only",
            "--pretty=format:",
            "--",
            "site/",
        ).splitlines()
        if line.strip().startswith("site/")
    }
    tracked = {
        line.strip()
        for line in _git(root, "ls-files", "--", "site/").splitlines()
        if line.strip().startswith("site/")
    }
    if not deleted:
        raise HistoryUnavailable("complete history contains no deleted site paths to verify")

    retired: set[str] = set()
    for deleted_path in deleted:
        parts = pathlib.PurePosixPath(deleted_path).parts[1:]
        for depth in range(1, len(parts)):
            candidate = "/".join(parts[:depth])
            prefix = f"site/{candidate}/"
            if not any(path == prefix[:-1] or path.startswith(prefix) for path in tracked):
                retired.add(candidate)
                break

    if not retired:
        raise HistoryUnavailable(
            "deleted site paths exist, but no fully retired directory could be derived"
        )

    shortest = {
        candidate
        for candidate in retired
        if not any(candidate != other and candidate.startswith(f"{other}/") for other in retired)
    }
    return tuple(sorted(shortest))


def _nonce() -> str:
    return str(time.time_ns())


def fetch_status(url: str, timeout: float) -> tuple[int | None, str]:
    scheme = urllib.parse.urlsplit(url).scheme
    if scheme not in ("http", "https"):
        return None, f"refusing to fetch a {scheme or 'schemeless'} URL"
    request = urllib.request.Request(  # noqa: S310 - scheme allowlisted above
        url,
        headers={
            "User-Agent": "yangble5-retired-check",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, ""
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, f"unreachable: {exc}"


def _url(base: str, path: str, trailing_slash: bool, nonce: str | None = None) -> str:
    encoded = urllib.parse.quote(path.strip("/"), safe="/")
    suffix = "/" if trailing_slash else ""
    url = f"{base.rstrip('/')}/{encoded}{suffix}"
    if nonce is not None:
        url = f"{url}?{CACHE_BUSTER_PARAM}={urllib.parse.quote(nonce, safe='')}"
    return url


def _classification(status: int | None) -> str:
    if status == 404:
        return "GONE"
    if status is None or status == 403 or (status is not None and status >= 500):
        return "INCONCLUSIVE"
    return "PRESENT"


def check_directory(
    base: str,
    path: str,
    timeout: float,
    nonce: str,
) -> list[tuple[str, str, str]]:
    """Return (URL, classification, detail) for slash and no-slash forms."""
    results = []
    for trailing_slash in (False, True):
        plain = _url(base, path, trailing_slash)
        status, error = fetch_status(plain, timeout)
        classification = _classification(status)
        detail = error or (f"HTTP {status}" if status is not None else "no response")
        if classification == "GONE":
            bypass = _url(base, path, trailing_slash, nonce)
            bypass_status, bypass_error = fetch_status(bypass, timeout)
            classification = _classification(bypass_status)
            detail = bypass_error or (
                "HTTP 404 twice"
                if bypass_status == 404
                else f"plain HTTP 404; cache-bypass HTTP {bypass_status}"
            )
        results.append((plain, classification, detail))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Require every site directory retired in Git history to remain HTTP 404.",
    )
    parser.add_argument("--base", default=DEFAULT_BASE, help=f"default {DEFAULT_BASE}")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    try:
        retired = retired_directories()
    except HistoryUnavailable as exc:
        print(f"retired_check: CANNOT CHECK — {exc}")
        return 2

    nonce = _nonce()
    failures = []
    for path in retired:
        for url, classification, detail in check_directory(args.base, path, args.timeout, nonce):
            print(f"{classification:12} {url} ({detail})")
            if classification != "GONE":
                failures.append((url, classification, detail))

    if failures:
        print("\nretired_check: FAIL — a retired path is present or could not be proven gone")
        return 1
    noun = "directory" if len(retired) == 1 else "directories"
    print(f"\nretired_check: OK — {len(retired)} retired {noun}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
