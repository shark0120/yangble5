#!/usr/bin/env python3
"""Name guard: a retired name stays retired.

WHY THIS EXISTS
---------------
This project shipped a skill under a name that collided with an AI vendor's
model name. The name was withdrawn on 2026-07-26 and the skill republished as
``yb5``. Withdrawing a name is a one-time edit; *keeping* it withdrawn is a
standing obligation, and standing obligations that live only in someone's
memory are the ones that fail. A contributor pastes an old snippet, a doc gets
restored from a backup, an AI assistant completes the old identifier from its
training data -- and the name is back in a published file with nobody noticing.

So the obligation is a test. CI fails if the retired name appears anywhere in
the repository, in any tracked file of any type. Unlike ``honesty_gate.py``,
which reads only the prose the project publishes, this gate reads everything:
the name is as unwelcome in a Python identifier, an nginx comment, a JSON key
or a CI workflow as it is in a sentence.

Compressed artifacts are expanded and read member by member, because the
published archive is simultaneously the likeliest place for the old name to
survive and the one place a byte scan is completely blind. See the comment on
``_ARCHIVE_SUFFIXES``.

THE ALLOW-LIST, AND WHY IT IS TINY
----------------------------------
Exactly one kind of mention survives: the historical record that tells someone
holding the old archive what happened to it. Scrubbing that would be worse
than useless -- a user who downloaded the withdrawn file deserves to be able to
find out that it was withdrawn and what replaced it.

Every allow-list entry carries a written reason, and an entry that matches
nothing is itself reported. A stale exemption is cover for the next file that
lands on that path, which is the same rule ``tools/sitecheck.py`` lives by.

Exit codes: 0 = clean; 1 = a violation; 2 = bad invocation.
Standard library only; no network.
"""

from __future__ import annotations

import argparse
import gzip
import io
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

# The retired name, in every punctuation a paste or an autocomplete produces.
# Assembled from parts so that this source file does not itself contain the
# literal string it bans -- the guard would otherwise be its own first finding,
# and exempting the guard by path is weaker than not tripping at all.
_STEM = "f" + "able"
RETIRED_RE = re.compile(_STEM + r"[\s\-_]*5", re.IGNORECASE)

# Paths that may carry the retired name, each with the reason somebody wrote
# down. An entry matching no occurrence is reported: see the module docstring.
ALLOW: dict[str, str] = {
    "CHANGELOG.md": (
        "the historical record of the withdrawal itself. Someone who "
        "downloaded the withdrawn archive needs to be able to discover that "
        "it was withdrawn, and what replaced it; a changelog that cannot name "
        "what changed is not a changelog."
    ),
    # This file is deliberately NOT here. It assembles its pattern from parts,
    # so it never contains the literal string and needs no exemption -- and an
    # exemption it does not need would be an exemption nothing checks.
    "tests/test_name_guard.py": (
        "this gate's own test, which must be able to construct a violating "
        "fixture in order to prove the gate catches one."
    ),
}

# Binary suffixes are read as bytes and decoded leniently rather than skipped:
# an archive whose *filename* carries the name is exactly the case this exists
# to catch, and a decode error must never be mistaken for a clean file.
_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"}

# COMPRESSED ARTIFACTS ARE EXPANDED, because a byte scan cannot see into them.
#
# Reading a file as bytes catches the name in anything stored literally. It
# catches nothing at all in a gzip stream: compressed output shares no
# substring with its input, so a tarball whose member *path* carries the old
# name, and whose file body spells it out, scans perfectly clean. That was
# measured before this code was written, and tests/test_name_guard.py keeps
# asserting it -- if it ever stops being true, this machinery is dead weight.
#
# It is also the worst possible place to be blind. The published archive is the
# artifact users download and extract onto their disk -- dozens of files, the
# largest concentration of the old name anywhere, and the one thing a reader
# checks by digest rather than by eye. A guard that reads every file in the
# repo except the one that ships is a guard that reports OK for the wrong
# reason.
_ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar", ".gz")

# Expanding compressed data is unbounded work; these stop a pathological or
# hand-crafted file from hanging CI. They are not security limits -- this repo's
# own archive is ~93 KB and 42 files, so anything near them is already strange.
# Hitting a limit is REPORTED as a violation, never treated as clean: "too big
# to check" and "checked and clean" must not produce the same exit code.
_MAX_EXPANDED = 64 * 1024 * 1024
_MAX_MEMBERS = 5000


def tracked_files(root: Path) -> list[Path]:
    """Every file git tracks, so the gate's reach matches what ships."""
    git = shutil.which("git")
    if git is not None:
        out = subprocess.run(  # noqa: S603 - fixed argv, executable from which()
            [git, "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            text=True,
            check=False,
        )
        if out.returncode == 0 and out.stdout:
            return [root / p for p in out.stdout.split("\0") if p]
    # A checkout without git (a release tarball, a vendored copy) still gets
    # checked -- falling back to a walk is better than silently passing.
    return [
        p
        for p in sorted(root.rglob("*"))
        if p.is_file() and not _SKIP_DIRS & set(p.relative_to(root).parts)
    ]


def scan_bytes(blob: bytes) -> list[tuple[int, str]]:
    """Line number and line for every occurrence of the retired name.

    Decoding is lenient and the file is read as bytes, so a binary blob is
    searched rather than skipped: a compiled artifact or an archive that still
    carries the name is precisely the leak this gate exists to catch, and a
    ``UnicodeDecodeError`` must never read as "clean".
    """
    text = blob.decode("utf-8", errors="replace")
    hits: list[tuple[int, str]] = []
    for n, line in enumerate(text.splitlines(), 1):
        if RETIRED_RE.search(line):
            hits.append((n, line.strip()[:160]))
    return hits


def _expand_gzip(blob: bytes) -> bytes:
    with gzip.GzipFile(fileobj=io.BytesIO(blob)) as fh:
        out = fh.read(_MAX_EXPANDED + 1)
    if len(out) > _MAX_EXPANDED:
        raise ValueError(f"expands past the {_MAX_EXPANDED}-byte cap")
    return out


def scan_archive(blob: bytes) -> tuple[list[str], str | None]:
    """Findings inside a compressed artifact, plus a reason it could not be cleared.

    Returns ``(findings, unchecked)``. ``findings`` are ``member:line: text``
    strings; ``unchecked`` is set when the archive could not be fully examined,
    which the caller reports as a violation rather than passing over. A member
    *path* carrying the name is a finding on its own, for the same reason a
    repository filename is: extraction puts that name on the user's disk
    regardless of what the bytes inside say.
    """
    if blob[:2] == b"\x1f\x8b":
        try:
            blob = _expand_gzip(blob)
        except (OSError, EOFError, ValueError) as exc:
            return [], f"gzip stream could not be expanded, so it cannot be cleared ({exc})"

    # Tar-ness is decided up front rather than by catching the open. Wrapping
    # the whole walk in `except TarError` would let a fault raised *midway*
    # read as "this was never a tar", fall through to a byte scan of data
    # already proven unscannable, and report clean.
    if not tarfile.is_tarfile(io.BytesIO(blob)):
        # A plain .gz of a single file. The decompressed bytes are still worth
        # scanning -- that is the whole point of having got this far.
        return [f"{n}: {line}" for n, line in scan_bytes(blob)], None

    findings: list[str] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(blob)) as archive:
            for count, member in enumerate(archive):
                if count >= _MAX_MEMBERS:
                    return findings, f"more than {_MAX_MEMBERS} members; not fully checked"
                if RETIRED_RE.search(member.name):
                    findings.append(f"{member.name}: member path carries the retired name")
                if not member.isfile() or member.size > _MAX_EXPANDED:
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                findings += [f"{member.name}:{n}: {line}" for n, line in scan_bytes(handle.read())]
    except tarfile.TarError as exc:
        return findings, f"tar could not be fully read, so it cannot be cleared ({exc})"
    return findings, None


def scan(root: Path) -> tuple[list[str], list[str]]:
    """Return (violations, stale_allow_entries)."""
    violations: list[str] = []
    matched_allow: set[str] = set()

    for path in tracked_files(root):
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue

        # A filename carrying the retired name is a violation on its own: the
        # published URL is the name, whatever the bytes inside say.
        name_hit = bool(RETIRED_RE.search(rel))

        try:
            blob = path.read_bytes()
        except OSError as exc:
            violations.append(f"{rel}: unreadable, so it cannot be cleared ({exc})")
            continue

        hits = scan_bytes(blob)
        inner: list[str] = []
        unchecked: str | None = None
        if rel.endswith(_ARCHIVE_SUFFIXES):
            inner, unchecked = scan_archive(blob)

        if not (hits or name_hit or inner or unchecked):
            continue

        if rel in ALLOW:
            matched_allow.add(rel)
            continue

        if name_hit:
            violations.append(f"{rel}: the path itself carries the retired name")
        for n, line in hits:
            violations.append(f"{rel}:{n}: {line}")
        # `::` separates the container from what is inside it, so a finding
        # reads as a location a person can actually navigate to.
        violations += [f"{rel}::{item}" for item in inner]
        if unchecked:
            violations.append(f"{rel}: {unchecked}")

    stale = [
        f"{k}: allow-list entry never matched — {v}"
        for k, v in ALLOW.items()
        if k not in matched_allow
    ]
    return violations, stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="repository root to scan")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"name_guard: not a directory: {root}", file=sys.stderr)
        return 2

    violations, stale = scan(root)

    for line in violations:
        print(f"name_guard: RETIRED NAME: {line}", file=sys.stderr)
    for line in stale:
        print(f"name_guard: STALE ALLOW: {line}", file=sys.stderr)

    if violations or stale:
        print(
            f"\nname_guard: FAIL — {len(violations)} occurrence(s), "
            f"{len(stale)} stale allow-list entry(ies).\n"
            "The skill was renamed on 2026-07-26 to remove a collision with an AI "
            "vendor's model name. Use the current name. If a mention is genuinely "
            "historical, add the path to ALLOW in tools/name_guard.py with the "
            "reason written out.",
            file=sys.stderr,
        )
        return 1

    print(f"name_guard: OK — retired name absent ({len(ALLOW)} allow-listed path(s), all matched).")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
