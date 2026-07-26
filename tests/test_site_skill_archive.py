"""The published skill archive must match everything the site says about it.

WHY THIS FILE EXISTS
--------------------
The download page asks the reader to do three things before they trust the
archive: compare a SHA256 against a digest printed in the prose, list the
archive and confirm every path sits under one named top-level directory, then
extract into `~/.claude/skills/` and run a selftest from a named subdirectory.
Every one of those is a claim the page makes *about a binary it ships beside
itself*, and nothing in the repo previously checked that the claim was true.

That gap is not hypothetical. Renaming the skill broke all three at once, and
two of the breakages were silent:

  * `index.html` went on printing the withdrawn archive's digest. A reader
    following the instructions exactly would have got a mismatch and been told,
    by the page's own rules, to stop and not extract -- on the genuine file.
  * A mechanical rename rewrote "the archive contains a top-level `X/`" to a
    value taken from the *site path* rather than the *archive*. The tar-slip
    check in the agent-facing sheet then instructed the agent to refuse the
    real archive as tampered with, "whatever the hash said".

Both are the same defect class: the artifact and the documentation drift apart,
and the documentation is what a security-conscious reader is checking against.
A wrong instruction here does not read as a typo -- it reads as evidence of
compromise, which is the most expensive possible way to be wrong.

So the agreement is a test. These assertions all derive their expected value
from the archive itself and compare it to what the docs say, which is the only
direction that works: a test that read both sides out of the docs would stay
green while the archive was anything at all.
"""

from __future__ import annotations

import hashlib
import re
import tarfile

import pytest

from tools import sitecheck

SKILL_DIR = sitecheck.SITE / "skill"
INDEX = SKILL_DIR / "index.html"
SHEET = SKILL_DIR / "YANGBLE5-SKILL.md"


@pytest.fixture(scope="module")
def archive():
    """The one published archive. Its absence is a failure, not a skip.

    The download page links to it by name. If it is not in the tree, the
    deploy publishes a page whose primary call to action 404s -- and because
    the deploy is additive (`cp -a`, no `--delete`), a stale archive left on
    the server would keep serving happily under the new page. Skipping here
    would let exactly that ship.
    """
    found = sorted(SKILL_DIR.glob("*.tar.gz"))
    assert len(found) == 1, (
        f"expected exactly one published archive in {SKILL_DIR}, found "
        f"{[p.name for p in found]}. Two archives means the download page's "
        f"single named link leaves the other one reachable but undocumented; "
        f"zero means the link 404s."
    )
    return found[0]


@pytest.fixture(scope="module")
def members(archive):
    with tarfile.open(archive, "r:gz") as tf:
        return tf.getnames()


@pytest.fixture(scope="module")
def top(members):
    """The archive's single top-level directory, read out of the archive."""
    roots = {name.split("/", 1)[0] for name in members}
    assert len(roots) == 1, (
        f"the archive has {len(roots)} top-level entries ({sorted(roots)}). "
        f"The published extraction step is `tar -xzf … -C ~/.claude/skills/`, "
        f"which with more than one root scatters files directly into the "
        f"user's skills directory instead of creating one folder there."
    )
    return roots.pop()


@pytest.fixture(scope="module")
def index_src():
    return INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def sheet_src():
    return SHEET.read_text(encoding="utf-8")


# ── the digest the reader is told to compare against ───────────────────────

def test_the_sidecar_matches_the_archive_bytes(archive):
    """`shasum -a 256 -c <file>.sha256` is step two of the published install."""
    sidecar = archive.with_name(archive.name + ".sha256")
    assert sidecar.is_file(), (
        f"{sidecar.name} is not published. The install instructions run "
        f"`shasum -a 256 -c` against it, which fails outright without the file."
    )
    real = hashlib.sha256(archive.read_bytes()).hexdigest()
    line = sidecar.read_text(encoding="utf-8").strip()
    assert real in line, (
        f"the published sidecar does not carry the archive's digest.\n"
        f"  archive: {real}\n  sidecar: {line}\n"
        f"A reader following the instructions gets a mismatch on the genuine "
        f"file and is told, correctly by the page's own rules, not to extract."
    )
    assert archive.name in line, (
        f"the sidecar names a different file than the archive beside it "
        f"({line!r}); `shasum -c` reads the filename from that line and will "
        f"look for something that is not there."
    )


def test_the_digest_printed_in_the_page_prose_is_the_real_one(archive, index_src):
    """The page prints the digest in full so it can be compared by eye.

    This is the assertion that catches a rebuild: gzip embeds a timestamp and
    tar embeds mtimes, so re-creating the archive produces different bytes and
    a different digest from an identical source tree. Publishing a rebuilt
    archive without updating this line is a one-command mistake, and its
    symptom is indistinguishable from tampering.
    """
    real = hashlib.sha256(archive.read_bytes()).hexdigest()
    printed = re.findall(r"\b[0-9a-f]{64}\b", index_src)
    assert printed, "index.html no longer prints a SHA256 for the archive"
    assert set(printed) == {real}, (
        f"index.html prints a digest that is not the published archive's.\n"
        f"  archive:  {real}\n  in page:  {sorted(set(printed))}\n"
        f"Update the <code> block beside the download link."
    )


# ── the archive's shape, and the docs' description of it ───────────────────

def test_no_member_escapes_the_top_level_directory(members, top):
    """The tar-slip property the sheet tells an agent to verify by hand."""
    for name in members:
        assert not name.startswith("/"), f"absolute path in archive: {name}"
        assert ".." not in name.split("/"), f"path escapes via '..': {name}"
        assert name == top or name.startswith(top + "/"), (
            f"{name!r} sits outside the single top-level {top!r} directory the "
            f"published listing check requires."
        )


def test_the_sheet_names_the_directory_the_archive_actually_has(sheet_src, top):
    """The agent-facing sheet's tar-slip check must match the real archive.

    If these disagree the sheet instructs the agent to refuse the genuine
    archive as tampered with -- "stop and report it, that is not the published
    archive, whatever the hash said when you checked it". A false alarm with
    that wording is worse than no check: it teaches the reader that the
    project's own integrity instructions cry wolf.
    """
    quoted = re.findall(r"top-level `([^`]+)`", sheet_src)
    assert quoted, "the sheet no longer states a required top-level directory"
    for got in quoted:
        assert got.rstrip("/") == top, (
            f"the sheet requires a top-level {got!r} but the archive ships "
            f"{top + '/'!r}."
        )
    escapes = re.findall(r"escapes `([^`]+)`", sheet_src)
    for got in escapes:
        assert got.rstrip("/") == top, (
            f"the sheet's escape check names {got!r}, not the archive's "
            f"actual top-level {top + '/'!r}."
        )


def test_the_page_names_the_directory_the_archive_actually_has(index_src, top):
    """Same agreement, in the Chinese-language install step on the page."""
    quoted = re.findall(r"頂層\s*<code>([^<]+)</code>", index_src)
    assert quoted, "the install step no longer states the top-level directory"
    for got in quoted:
        assert got.rstrip("/") == top, (
            f"index.html tells the reader the archive contains a top-level "
            f"{got!r}; it actually contains {top + '/'!r}. The reader is asked "
            f"to check this before extracting, so a wrong value reads as "
            f"evidence of tampering."
        )


def test_the_selftest_path_on_the_page_exists_in_the_archive(index_src, members):
    """`cd ~/.claude/skills/<dir>/scripts` then `python <name>.py`.

    The page's final install step is the one the reader uses to decide the
    install worked. A path that does not exist in the archive turns "run this
    and look for PASS" into a file-not-found, on a correct installation.
    """
    m = re.search(r"cd ~/\.claude/skills/(\S+)\s*\n\s*python (\S+\.py)", index_src)
    assert m, "the selftest step is no longer a cd + python pair on the page"
    subdir, script = m.group(1).rstrip("/"), m.group(2)
    assert f"{subdir}/{script}" in members, (
        f"the page tells the reader to run {script} from ~/.claude/skills/"
        f"{subdir}/, but the archive has no member {subdir}/{script}. "
        f"Members near that path: "
        f"{sorted(n for n in members if n.endswith('.py'))[:5]}"
    )


# ── the filenames on the page must be the filenames that ship ──────────────

def test_every_archive_filename_on_the_page_is_the_published_one(index_src, archive):
    """One published archive means one name, spelled the same everywhere.

    The download link, the sidecar link, the `shasum -c` line, the
    `Get-FileHash` line and both `tar -xzf` lines all repeat it. Renaming the
    archive and missing one of them yields a page that verifies file A and
    extracts file B, and the reader has no way to tell which step is wrong.
    """
    named = set(re.findall(r"[\w.-]+\.tar\.gz", index_src))
    assert named == {archive.name}, (
        f"index.html names {sorted(named)} but the published archive is "
        f"{archive.name!r}. Every occurrence must be the shipped filename."
    )


def test_the_sheet_and_the_page_agree_on_the_filename(sheet_src, archive):
    named = set(re.findall(r"[\w.-]+\.tar\.gz", sheet_src))
    assert named <= {archive.name}, (
        f"the agent-facing sheet names {sorted(named - {archive.name})}, which "
        f"is not the published archive {archive.name!r}. The sheet is what an "
        f"agent follows unattended, so a stale name there fails silently."
    )
