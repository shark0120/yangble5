"""Tests for tools/name_guard.py — the gate that keeps a retired name retired.

One thing here is deliberate and worth stating, because it looks like a mistake:
the fixtures below spell the retired name out as a literal instead of importing
`name_guard`'s own pattern. Deriving the fixture from the code under test would
make every assertion circular — the gate could be matching entirely the wrong
string and these tests would still pass, which is the exact shape of a test that
is green and proves nothing. The literal is what makes them real, and it is why
this file carries the one path-based exemption the gate grants besides the
changelog.
"""

from __future__ import annotations

import io
import pathlib
import shutil
import subprocess
import sys
import tarfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))

import name_guard as ng

ROOT = pathlib.Path(__file__).resolve().parents[1]

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git is not available")


def _git(*argv: str) -> None:
    subprocess.run([GIT, *argv], check=True)  # noqa: S603 - fixed argv, executable from which()


def _init_repo(path: pathlib.Path) -> None:
    """A real git repo, so the gate exercises its `git ls-files` path."""
    _git("init", "-q", str(path))


def _add_all(path: pathlib.Path) -> None:
    _git("-C", str(path), "add", "-A")


def test_the_pattern_matches_the_actual_retired_name():
    """The whole gate rests on this one string. Assert it directly."""
    assert ng.RETIRED_RE.search("fable5")


def test_the_pattern_matches_the_punctuated_variants_a_paste_produces():
    for variant in ("fable5", "Fable5", "FABLE5", "fable-5", "fable_5", "fable 5"):
        assert ng.RETIRED_RE.search(variant), variant


def test_the_pattern_does_not_match_unrelated_prose():
    for text in ("a fable about five goats", "table 5 shows", "immutable5"):
        assert not ng.RETIRED_RE.search(text), text


def test_the_repository_itself_is_clean():
    """The gate's real job: this repo, right now, has no revived name in it."""
    violations, stale = ng.scan(ROOT)
    assert violations == [], violations
    assert stale == [], stale


@needs_git
def test_a_revived_name_in_file_contents_is_a_violation(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "docs.md").write_text("Install the fable5 skill.", encoding="utf-8")
    _add_all(tmp_path)
    violations, _ = ng.scan(tmp_path)
    assert any("docs.md" in v for v in violations), violations
    assert ng.main(["--root", str(tmp_path)]) == 1


@needs_git
def test_a_revived_name_in_a_FILENAME_is_a_violation(tmp_path):
    """The published URL is the filename. Clean bytes under a dirty name still leak it."""
    _init_repo(tmp_path)
    (tmp_path / "fable5-notes.md").write_text("nothing incriminating inside\n", encoding="utf-8")
    _add_all(tmp_path)
    violations, _ = ng.scan(tmp_path)
    assert any("the path itself" in v for v in violations), violations


@needs_git
def test_a_revived_name_in_a_BINARY_file_is_a_violation(tmp_path):
    """A decode error must never read as 'clean' — an archive is the likeliest leak."""
    _init_repo(tmp_path)
    (tmp_path / "blob.bin").write_bytes(b"\x00\xff\xfe binary junk fable5 more junk \x00")
    _add_all(tmp_path)
    violations, _ = ng.scan(tmp_path)
    assert any("blob.bin" in v for v in violations), violations


@needs_git
def test_an_allow_listed_path_is_not_a_violation(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("Renamed from fable5.", encoding="utf-8")
    _add_all(tmp_path)
    monkeypatch.setattr(ng, "ALLOW", {"CHANGELOG.md": "the historical record"})
    violations, stale = ng.scan(tmp_path)
    assert violations == []
    assert stale == []


@needs_git
def test_an_allow_list_entry_that_matches_nothing_is_reported(tmp_path, monkeypatch):
    """A stale exemption is cover for the next file that lands on that path."""
    _init_repo(tmp_path)
    (tmp_path / "clean.md").write_text("nothing here\n", encoding="utf-8")
    _add_all(tmp_path)
    monkeypatch.setattr(ng, "ALLOW", {"CHANGELOG.md": "the historical record"})
    violations, stale = ng.scan(tmp_path)
    assert violations == []
    assert any("CHANGELOG.md" in s for s in stale), stale
    assert ng.main(["--root", str(tmp_path)]) == 1


@needs_git
def test_a_clean_tree_passes(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    (tmp_path / "clean.md").write_text("the yb5 skill\n", encoding="utf-8")
    _add_all(tmp_path)
    monkeypatch.setattr(ng, "ALLOW", {})
    assert ng.main(["--root", str(tmp_path)]) == 0


def test_a_bad_root_exits_two(tmp_path):
    assert ng.main(["--root", str(tmp_path / "nope")]) == 2


def test_untracked_files_are_still_scanned_without_git(tmp_path, monkeypatch):
    """A release tarball or vendored copy has no .git. It must not pass by default."""
    (tmp_path / "docs.md").write_text("the fable5 skill\n", encoding="utf-8")
    monkeypatch.setattr(ng, "ALLOW", {})
    violations, _ = ng.scan(tmp_path)
    assert any("docs.md" in v for v in violations), violations


# ── inside the published archive ───────────────────────────────────────────
#
# These matter more than any of the above. Everything else this gate reads is
# stored literally, so a plain byte scan would have found it. A gzip stream
# shares no substring with its input, so before the archive walk existed a
# tarball carrying the retired name in a member path AND in a file body scanned
# perfectly clean -- measured, not assumed. The archive is what users download
# and extract onto their disk, which made that the one blind spot capable of
# turning a green gate into a false assurance.


def _tar_gz(path: pathlib.Path, members: dict[str, str]) -> pathlib.Path:
    """A real .tar.gz, built in memory, so the test exercises the real decoder."""
    with tarfile.open(path, "w:gz") as tf:
        for name, body in members.items():
            data = body.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


def test_a_gzip_stream_really_does_hide_the_name_from_a_byte_scan(tmp_path):
    """The premise the archive walk rests on. If this ever stops being true the
    extra machinery is dead weight, and a test that asserts the premise is how
    anyone would find out."""
    archive = _tar_gz(tmp_path / "leak.tar.gz", {"fable5/notes.md": "the fable5 skill\n"})
    assert ng.scan_bytes(archive.read_bytes()) == [], (
        "compressed bytes now contain the literal name, which would mean gzip "
        "stopped compressing -- or that this fixture is no longer a real archive"
    )


def test_a_revived_name_inside_an_archived_FILE_BODY_is_a_violation(tmp_path):
    archive = _tar_gz(tmp_path / "skill.tar.gz", {"pkg/notes.md": "the fable5 skill\n"})
    findings, unchecked = ng.scan_archive(archive.read_bytes())
    assert unchecked is None, unchecked
    assert any("pkg/notes.md" in f for f in findings), findings


def test_a_revived_name_in_an_archived_MEMBER_PATH_is_a_violation(tmp_path):
    """Extraction writes that path onto the user's disk whatever the bytes say."""
    archive = _tar_gz(tmp_path / "skill.tar.gz", {"fable5/clean.md": "nothing here\n"})
    findings, _ = ng.scan_archive(archive.read_bytes())
    assert any("member path" in f for f in findings), findings


@needs_git
def test_scan_reports_archive_findings_against_the_containing_file(tmp_path, monkeypatch):
    """End to end: the walk is wired into scan(), and the location is navigable."""
    _init_repo(tmp_path)
    _tar_gz(tmp_path / "skill.tar.gz", {"pkg/notes.md": "the fable5 skill\n"})
    _add_all(tmp_path)
    monkeypatch.setattr(ng, "ALLOW", {})
    violations, _ = ng.scan(tmp_path)
    assert any("skill.tar.gz::pkg/notes.md" in v for v in violations), violations
    assert ng.main(["--root", str(tmp_path)]) == 1


def test_a_clean_archive_passes(tmp_path):
    archive = _tar_gz(tmp_path / "ok.tar.gz", {"yangble5/notes.md": "the yb5 skill\n"})
    assert ng.scan_archive(archive.read_bytes()) == ([], None)


def test_an_unreadable_archive_is_reported_not_skipped(tmp_path):
    """'Too big / too broken to check' must never exit the same way as 'clean'."""
    broken = tmp_path / "truncated.tar.gz"
    broken.write_bytes(b"\x1f\x8b\x08\x00" + b"\x00" * 8)  # gzip magic, no body
    findings, unchecked = ng.scan_archive(broken.read_bytes())
    assert findings == []
    assert unchecked and "cannot be cleared" in unchecked, unchecked


def test_the_published_archive_is_clean_inside(tmp_path):
    """The real artifact, read the way a user's extraction would see it.

    This is the assertion the whole file exists for: 42 files of skill content
    that shipped under the old name, republished, with the guarantee actually
    verified rather than inferred from the fact that nothing else tripped.
    """
    published = sorted((ROOT / "site" / "skill").glob("*.tar.gz"))
    assert len(published) == 1, [p.name for p in published]
    findings, unchecked = ng.scan_archive(published[0].read_bytes())
    assert unchecked is None, unchecked
    assert findings == [], findings
