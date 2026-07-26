"""Do this repository's own cross-references still point at something?

This started as a manual chore -- "re-check the links and dated claims in
docs/COMPARISON.md" -- and a manual chore is the wrong shape for it. Every
reference here rots the same way: somebody inserts a section, renames a file, or
adds a row to a table, and a pointer somewhere else quietly starts lying. Nobody
notices, because a wrong pointer renders exactly like a right one.

What the audit on 2026-07-26 actually found is that the docs were clean: 35
markdown files, every relative link resolving, every anchor resolving, all 70
`§N` cross-references landing on a real section, and all 11 external URLs
answering 200. The value of this file is not the fix -- there was nothing to fix
-- it is that the same answer is now recomputed on every run for free.

WHAT THIS CANNOT CHECK, stated so nobody reads more into a green run than it
earns: external URLs need the network, and the test suite is offline by design.
Those 11 were checked by hand on 2026-07-26 and all answered 200; that is a
point-in-time observation, not a standing guarantee.

The three checkers below are deliberately written to have no opinion about
prose. Two false-positive classes bit the first draft, and both are now
regression-tested at the bottom of this file, because a link checker that cries
wolf over a regex in a code span is a link checker somebody switches off:

  * a regex like the one in `site/README.md` looks exactly like a markdown link
  * a release-notes TEMPLATE in `RELEASING.md` links to `CHANGELOG.md#xyz---yyyy-mm-dd`,
    an anchor that is *supposed* not to exist yet
"""

from __future__ import annotations

import pathlib
import re
from collections.abc import Iterator

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

FENCE = re.compile(r"^\s*(?:```|~~~)")
INLINE_CODE = re.compile(r"`[^`\n]*`")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
NUMBERED_HEADING = re.compile(r"^#{1,6}\s+(\d+)\.\s+")
# group 1 = label, 2 = path, 3 = fragment. The path stops at `#` so a link and
# an anchor can be judged separately: a live file with a dead anchor is a
# different defect from a missing file.
MD_LINK = re.compile(r"!?\[([^\]]*)\]\(([^)\s#]*)(?:#([^)\s]*))?(?:\s+\"[^\"]*\")?\)")
# A backticked filename, or a section reference. Scanned together because the
# first tells you what the second is talking about.
MENTION_OR_SECTION = re.compile(r"`([A-Za-z0-9_./-]+\.md)`|§\s*(\d+)")

EXTERNAL = ("http://", "https://", "mailto:", "tel:")


def markdown_files() -> list[pathlib.Path]:
    """Every tracked markdown file.

    Dot-directories are skipped rather than named: `.pytest_cache` ships its own
    README, exists only on a machine that has run the suite, and would make this
    test's result depend on whether it had been run before.
    """
    return sorted(
        path
        for path in ROOT.rglob("*.md")
        if not any(part.startswith(".") for part in path.relative_to(ROOT).parts)
    )


def prose_lines(path: pathlib.Path):
    """(lineno, line) for every line OUTSIDE a fenced code block.

    Fenced blocks are where the templates and the shell snippets live, and their
    contents are illustrations, not claims about this repository.
    """
    fenced = False
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            yield lineno, line


def heading_slugs(path: pathlib.Path) -> set[str]:
    """The `#fragment` values GitHub will generate for this file's headings."""
    slugs = set()
    for _, line in prose_lines(path):
        match = HEADING.match(line)
        if not match:
            continue
        text = re.sub(r"`([^`]*)`", r"\1", match.group(2))
        text = MD_LINK.sub(r"\1", text)
        text = re.sub(r"[*_]", "", text)
        slugs.add(re.sub(r"[^\w\- ]", "", text.lower()).strip().replace(" ", "-"))
    return slugs


def numbered_sections(path: pathlib.Path) -> set[int]:
    """Section numbers from `## 4. Title` headings -- what `§4` refers to."""
    return {
        int(match.group(1))
        for _, line in prose_lines(path)
        if (match := NUMBERED_HEADING.match(line))
    }


def resolve(source: pathlib.Path, reference: str) -> pathlib.Path | None:
    """Find the file a reference names, or None.

    Two candidates, because the docs legitimately use two conventions: a link
    href is relative to the file it appears in, while a filename written for a
    human to read (`docs/OPERATING_A_PUBLIC_SERVICE.md`) is usually relative to
    the repository root. Accepting both is not laxity -- a reference that
    matches neither is still a failure.
    """
    for candidate in (source.parent / reference, ROOT / reference):
        if candidate.exists():
            return candidate
    return None


def label(path: pathlib.Path) -> str:
    """A short name for messages. Falls back for the synthetic documents the
    self-tests below build outside the repository."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return path.name


def hoist_hrefs(line: str) -> str:
    """Move a link's href in front of its label.

    `[`docs/X.md` §1](../docs/X.md)` names its target twice -- once for the
    reader, once for the renderer -- with the `§` sitting between them. A
    left-to-right scan would bind the section number to the display text and
    resolve it against the wrong directory. Hoisting makes the href win, which
    is the copy the renderer actually follows.
    """

    def replace(match: re.Match[str]) -> str:
        label, href = match.group(1), match.group(2)
        return (f"`{href}` " if href.endswith(".md") else "") + label

    return MD_LINK.sub(replace, line)


# ── the three checkers ─────────────────────────────────────────────────────


def broken_links(path: pathlib.Path) -> Iterator[str]:
    for lineno, line in prose_lines(path):
        for match in MD_LINK.finditer(INLINE_CODE.sub("``", line)):
            target = match.group(2)
            if not target or target.startswith(EXTERNAL):
                continue
            if resolve(path, target) is None:
                yield f"{label(path)}:{lineno}: no such file: {target}"


def broken_anchors(path: pathlib.Path) -> Iterator[str]:
    for lineno, line in prose_lines(path):
        for match in MD_LINK.finditer(INLINE_CODE.sub("``", line)):
            target, fragment = match.group(2), match.group(3)
            if not fragment or target.startswith(EXTERNAL):
                continue
            document = path if not target else resolve(path, target)
            # A missing file is broken_links()'s finding, not this one's.
            if document is None or document.suffix != ".md":
                continue
            if fragment not in heading_slugs(document):
                yield (
                    f"{label(path)}:{lineno}: "
                    f"{document.name} has no heading '#{fragment}'"
                )


def broken_section_refs(path: pathlib.Path) -> Iterator[str]:
    """`§N` references, resolved against the nearest filename in the paragraph.

    Paragraph scope, not line scope: the filename and the `§` are routinely on
    different lines of the same wrapped sentence. A blank line ends the scope,
    and a `§` with no filename before it refers to the current document.
    """
    named: str | None = None
    for lineno, line in prose_lines(path):
        if not line.strip():
            named = None
            continue
        for token in MENTION_OR_SECTION.finditer(hoist_hrefs(line)):
            mention, number = token.group(1), token.group(2)
            if mention:
                named = mention
                continue
            document = resolve(path, named) if named else path
            if document is None:
                yield f"{label(path)}:{lineno}: §{number} in missing {named}"
                continue
            if int(number) not in numbered_sections(document):
                yield (
                    f"{label(path)}:{lineno}: "
                    f"{document.name} has no section {number}"
                )


# ── the repository itself ──────────────────────────────────────────────────


def test_there_are_markdown_files_to_check():
    """Without this, a bug in markdown_files() makes every test below vacuous."""
    assert len(markdown_files()) > 20


def test_every_relative_link_points_at_a_file_that_exists():
    problems = [problem for path in markdown_files() for problem in broken_links(path)]
    assert problems == [], "\n".join(problems)


def test_every_anchor_points_at_a_heading_that_exists():
    problems = [problem for path in markdown_files() for problem in broken_anchors(path)]
    assert problems == [], "\n".join(problems)


def test_every_section_reference_points_at_a_section_that_exists():
    problems = [problem for path in markdown_files() for problem in broken_section_refs(path)]
    assert problems == [], "\n".join(problems)


def test_section_references_are_actually_being_found():
    """The suite's own tripwire.

    All three checkers above pass by finding nothing. If `hoist_hrefs` or the
    token regex broke, they would keep passing while checking almost nothing, so
    assert the corpus is still being seen. The floor is well under the count
    observed on 2026-07-26 (70) -- this is a canary for a scan that collapsed,
    not a lock on the docs' contents.
    """
    found = sum(
        1
        for path in markdown_files()
        for _, line in prose_lines(path)
        for token in MENTION_OR_SECTION.finditer(hoist_hrefs(line))
        if token.group(2)
    )
    assert found >= 40, found


# ── a self-referential claim that would rot silently ───────────────────────

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15,
}
RED = "\U0001f534"  # the losing marker in COMPARISON.md's tables


def test_the_comparison_table_still_loses_the_number_of_rows_it_claims():
    """COMPARISON.md counts its own losses in prose, one paragraph below the
    table that produces the number. Add a row and the sentence is wrong -- in
    the flattering direction, which is the direction this project has spent the
    most effort not drifting in.
    """
    text = (ROOT / "docs" / "COMPARISON.md").read_text(encoding="utf-8")
    section = text.split("## Where yangble5 loses")[1].split("\n---")[0]
    rows = [row for row in section.splitlines() if row.startswith("| **")]
    losses = [row for row in rows if row.split("|")[2].strip().startswith(RED)]

    claim = re.search(r"That's \*\*(\w+) rows where yangble5 is the worst", text)
    assert claim is not None, "the prose that counts the losing rows is gone"
    assert NUMBER_WORDS[claim.group(1)] == len(losses), (
        f"prose says {claim.group(1)}, table has {len(losses)} losing rows"
    )
    assert rows, "the losses table did not parse"


# ── the checkers detect what they claim to detect ──────────────────────────
#
# Every test above passes by finding nothing, which is also what a checker that
# silently does nothing returns. These build the mutation proof into the suite:
# each feeds a document with a known defect and asserts it is caught.


@pytest.fixture
def doc(tmp_path):
    def write(name: str, body: str) -> pathlib.Path:
        path = tmp_path / name
        path.write_text(body, encoding="utf-8")
        return path

    return write


def test_a_broken_relative_link_is_detected(doc):
    path = doc("a.md", "See [the thing](no-such-file.md) for details.\n")
    assert list(broken_links(path))


def test_a_working_relative_link_is_not_reported(doc):
    doc("b.md", "# Target\n")
    path = doc("a.md", "See [the thing](b.md).\n")
    assert not list(broken_links(path))


def test_a_broken_anchor_is_detected(doc):
    doc("b.md", "# Actual heading\n")
    path = doc("a.md", "See [the thing](b.md#not-a-heading).\n")
    assert list(broken_anchors(path))


def test_a_working_anchor_is_not_reported(doc):
    doc("b.md", "## 4. The denominator problem\n")
    path = doc("a.md", "See [it](b.md#4-the-denominator-problem).\n")
    assert not list(broken_anchors(path))


def test_a_broken_section_reference_is_detected(doc):
    doc("b.md", "## 1. Only section\n")
    path = doc("a.md", "Read `b.md` §9 before you start.\n")
    assert list(broken_section_refs(path))


def test_a_section_reference_resolves_across_a_line_break(doc):
    """The filename and the `§` are routinely in different lines of one
    sentence; line-scoped resolution reported six false positives here."""
    doc("b.md", "## 1. Only section\n")
    path = doc("a.md", "This is the shape [`b.md`](b.md)\n§1 warns you about.\n")
    assert not list(broken_section_refs(path))


def test_a_blank_line_ends_the_filename_scope(doc):
    """Otherwise a filename mentioned once binds every `§` in the rest of the
    file, and a same-document reference silently resolves against a stranger."""
    doc("b.md", "## 9. Ninth\n")
    path = doc("a.md", "Read `b.md` §9.\n\nUnrelated paragraph, see §9.\n")
    assert list(broken_section_refs(path))


def test_a_section_reference_in_a_link_label_uses_the_href(doc):
    """`[`docs/b.md` §1](../docs/b.md)` names the target twice. Binding to the
    display text resolves against the wrong directory."""
    nested = doc("a.md", "x").parent / "docs"
    nested.mkdir()
    (nested / "b.md").write_text("## 1. First\n", encoding="utf-8")
    path = doc("a.md", "the shape [`docs/b.md` §1](docs/b.md) forbids\n")
    assert not list(broken_section_refs(path))


def test_a_link_inside_a_code_fence_is_not_checked(doc):
    """RELEASING.md ships a release-notes template whose anchor is a
    placeholder. It is an illustration; treating it as a claim makes the gate
    permanently red over a file that is behaving correctly."""
    path = doc(
        "a.md",
        "Copy this:\n\n```markdown\nSee [CHANGELOG.md](CHANGELOG.md#xyz---yyyy-mm-dd).\n```\n",
    )
    assert not list(broken_links(path))
    assert not list(broken_anchors(path))


def test_a_regex_in_a_code_span_is_not_mistaken_for_a_link(doc):
    """`site/README.md` documents a regex that is character-for-character the
    shape of a markdown link. The first draft reported it as broken."""
    path = doc("a.md", "The pattern is `[0-9A-Za-z_.,](?:[0-9A-Za-z_.,]*)` here.\n")
    assert not list(broken_links(path))


def test_an_external_url_is_not_treated_as_a_path(doc):
    """They cannot be checked offline; silently passing them is the honest
    behaviour, and reporting them as missing files would be a lie."""
    path = doc("a.md", "See [upstream](https://example.invalid/some/page.md).\n")
    assert not list(broken_links(path))
    assert not list(broken_anchors(path))
