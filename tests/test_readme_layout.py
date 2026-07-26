"""The map in README.md must still describe the territory.

``## Repository layout`` is an ASCII tree, and for most readers it is the first
and only survey of what this repository contains.  On 2026-07-26 it was wrong in
two different ways.

The mild way: it claimed ``tools/`` held seven files.  It held eight.  The
missing one was ``name_guard.py`` -- the gate that keeps a retired brand name
from coming back, added hours after the previous commit to fix this same
omission for two other gates.  ``docs/AGENT_INTERVIEW.md`` and ``byok/README.md``
were absent the same way.

The serious way: two top-level directories were not on the map **at all**, and
both are named by copy-pasteable commands in this same README.  ``evidence/``
holds the recorded run that README's headline offers as proof you can check
without an API key; the map showed ``docs/evidence/``, which is a different
directory holding something else.  ``examples/`` holds the prompts the
``cache_guard`` walkthrough scans.  A skeptical reader's cheapest first move is
to confirm a cited file is really in the repo before cloning anything -- they
scroll to the one section that says what is in the repo, do not find it, and
conclude the command is wrong.  That is worse than an omission: the map does not
merely fail to mention the file, it actively contradicts the claim the project
rests on.

What makes all of this worth a test rather than five one-line edits: **a line
that drifted and a line that did not are indistinguishable by reading**.  The
block is hand-maintained prose with column-aligned annotations, so the only way
to notice an absence is to hold the tree next to ``git ls-files`` and diff them
by eye, which nobody does and nobody should.

Three directions, and they are not the same check:

* **Forward** -- every path the map names is really shipped.  A rename or a
  deletion leaves a line pointing at nothing, and a map with a phantom entry is
  worse than one with a gap, because the reader goes looking.
* **Reverse** -- every file in a directory the map *enumerates* is on the map.
* **Whole directories** -- every top-level directory a clone receives appears
  somewhere on the map.  This is the check that would have caught ``evidence/``,
  and neither of the other two can: forward only looks at what is written down,
  and reverse only descends into directories already on the map.  A directory
  nobody listed is invisible to both.

Reverse cannot be asked of every directory, and the repository's own numbers say
where the line falls.  Measured against the tree, the listed fraction per
directory is bimodal with nothing in the middle:

    assets/  scripts/  docs/evidence/  docs/diagrams/   100%   enumerated
    docs/                                                90%   enumerated
    tools/                                               87%   enumerated
    byok/                                                66%   enumerated
    ------------------------------------------------- the gap -------------
    site/                                                35%   curated
    gateway/                                             22%   curated
    deploy/                                               4%   curated
    tests/  docs/launch/                                  0%   named only

The second group is curated on purpose: ``deploy/`` lists the one config file a
reader needs and ``gateway/`` lists two entries whose annotation carries a
sentence about becoming an operator.  Demanding all 22 of ``deploy/`` would turn
this file into noise, and a noisy gate is one somebody switches off -- taking
the real defect with it.  ``EXHAUSTIVE`` below is therefore hand-curated, the
same deliberate choice ``test_deploy_env_completeness.py`` makes and for the
same reason.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
HEADING = "## Repository layout"

# Directories whose listing in the block is a complete enumeration, so a file
# that exists and is not listed is drift. Everything else in the tree is a
# curated selection -- see the table in the module docstring for the measured
# split that puts each directory on one side or the other.
EXHAUSTIVE = (
    "tools/",
    "byok/",
    "scripts/",
    "assets/",
    "docs/",
    "docs/evidence/",
    "docs/diagrams/",
    "evidence/",
    "examples/",
)

# Top-level directories a clone receives that the map deliberately does not
# show. Keep this list short and argued: every entry is a directory a reader is
# told nothing about, so the bar is that no README instruction sends them there.
TOP_LEVEL_EXEMPT = {
    # CI's own plumbing. Nothing in the README points a reader at it, and the
    # workflows are not an artifact anyone is asked to run or inspect.
    ".github/",
}

# `__init__.py` makes a directory importable; it is not something a reader of
# the map is looking for, and `tools/` is documented as files you copy one at a
# time, which this is not.
IGNORED = {"__init__.py"}

# A branch in the tree: leading spaces and `│` for depth, then `├─ ` or `└─ `.
# Annotation continuation lines have no branch glyph and are skipped by not
# matching at all.
BRANCH = re.compile(r"^(?P<indent>[\s│]*)(?:├─|└─)\s(?P<rest>\S.*)$")

# The name field: one path, optionally followed by ` / `-separated alternates.
# Stopping at the first plain space is what separates the name from the
# annotation, which is usually column-aligned but collapses to a single space
# when the name is long (`deploy/engine/config.example.yaml`).
NAME_FIELD = re.compile(r"^(\S+(?:\s/\s\S+)*)")

INDENT_PER_LEVEL = 3  # "│  " and "   " are both three columns


def names_in(field: str) -> list[str]:
    """The one or more names an entry line declares.

    ``install.sh / install.ps1`` is two files on one line, and
    ``uninstall.sh / .ps1`` is the same thing written with the extension alone.
    Reading only the first token would quietly drop three real files from the
    forward check -- an under-count that looks exactly like a passing test.
    """
    names, previous = [], None
    for fragment in field.split(" / "):
        if fragment.startswith(".") and previous and "." in previous:
            fragment = previous.rsplit(".", 1)[0] + fragment
        names.append(fragment)
        previous = fragment
    return names


def declared_paths() -> set[str]:
    """Every path the layout block names, as a repo-relative path.

    Directories keep their trailing slash, which is how the block writes them
    and how `EXHAUSTIVE` refers to them.
    """
    text = README.read_text(encoding="utf-8")
    start = text.index(HEADING)
    fence = text[start : text.index("\n## ", start + len(HEADING))].split("```")[1]

    open_at_depth: dict[int, str] = {}
    paths: set[str] = set()
    for line in fence.splitlines():
        match = BRANCH.match(line)
        if match is None:
            continue
        depth = len(match.group("indent")) // INDENT_PER_LEVEL
        for name in names_in(NAME_FIELD.match(match.group("rest")).group(1)):
            path = open_at_depth.get(depth - 1, "") + name
            paths.add(path)
            if name.endswith("/"):
                open_at_depth[depth] = path
    return paths


def shipped() -> tuple[set[str], set[str]] | None:
    """(files, directories) a fresh clone receives, or None without git.

    Git stores no directory objects, so the directories are derived from the
    file paths. The map names several directories that hold no file of their own
    (`docs/launch/`), and they are real entries a clone does get.
    """
    try:
        listing = subprocess.run(
            # S607: `git` off PATH, because there is no portable absolute path
            # for it -- CI runs this on ubuntu and on windows. Fixed argv,
            # shell=False, nothing here an input could steer.
            ["git", "ls-files", "-z"],  # noqa: S607
            cwd=ROOT,
            capture_output=True,
            check=True,
            timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    files = {name for name in listing.decode("utf-8").split("\0") if name}
    directories = {
        path[: index + 1] for path in files for index, char in enumerate(path) if char == "/"
    }
    return files, directories


@pytest.fixture
def tree() -> tuple[set[str], set[str]]:
    result = shipped()
    if result is None:
        pytest.skip("no usable git checkout; the shipped tree cannot be listed")
    assert result[0], "git reported no tracked files, so these tests would prove nothing"
    return result


def sibling_groups() -> dict[str, list[tuple[str, str]]]:
    """Entries grouped by the directory they sit in, as (glyph, name) pairs.

    Keyed by resolved PARENT PATH, never by indent depth. `docs/` is itself the
    last root entry, so it is drawn with `└─` and its children indent with three
    spaces where every other subtree uses `│  `. Grouping by depth therefore
    pools `docs/`'s children with `docs/diagrams/`'s, and reports two violations
    that are not there.
    """
    text = README.read_text(encoding="utf-8")
    start = text.index(HEADING)
    fence = text[start : text.index("\n## ", start + len(HEADING))].split("```")[1]

    open_at_depth: dict[int, str] = {}
    groups: dict[str, list[tuple[str, str]]] = {}
    for line in fence.splitlines():
        match = BRANCH.match(line)
        if match is None:
            continue
        depth = len(match.group("indent")) // INDENT_PER_LEVEL
        glyph = "└─" if line.lstrip("│ ").startswith("└─") else "├─"
        parent = open_at_depth.get(depth - 1, "")
        field = NAME_FIELD.match(match.group("rest")).group(1)
        # One entry per LINE, not per name. `install.sh / install.ps1` is two
        # files but a single branch of the tree, and counting it twice makes a
        # correctly drawn group look like it has two terminators.
        groups.setdefault(parent, []).append((glyph, field))
        names = names_in(field)
        if names[0].endswith("/"):
            open_at_depth[depth] = parent + names[0]
    return groups


def test_the_tree_is_drawn_correctly():
    """`└─` marks the last child, and adding an entry moves it.

    This is the mistake the additions themselves invite: appending a line under
    a directory means retyping the previous last child's `└─` as `├─`, and
    forgetting leaves a tree that renders with a dangling stub or an early
    terminator. It is a visual defect, so every other check here passes over it
    happily, and a reader sees a malformed map.
    """
    problems = []
    for parent, entries in sorted(sibling_groups().items()):
        where = parent or "the repository root"
        closers = [name for glyph, name in entries if glyph == "└─"]
        if len(closers) != 1:
            problems.append(f"{where}: {len(closers)} entries drawn with `└─`, expected exactly 1")
        elif entries[-1][0] != "└─":
            problems.append(
                f"{where}: `└─` is on {closers[0]}, but the last entry is {entries[-1][1]}"
            )
    assert problems == [], "\n".join(problems)


def test_the_block_parses_into_something_recognisable():
    """A parser that silently matches nothing makes every test below vacuous.

    That failure mode -- a gate that passes while checking not one thing -- is
    the one this repository keeps rediscovering, so it is asserted directly
    rather than assumed. The bounds are deliberately loose: this is here to
    catch a parser that broke, not to pin the tree's size.
    """
    paths = declared_paths()
    assert len(paths) > 30, f"only {len(paths)} paths parsed out of the layout block"
    assert "tools/cache_bench.py" in paths, "nesting is not being resolved into full paths"
    assert "docs/evidence/claude-code-e2e.md" in paths, "depth 2 is not being reached"
    assert "site/uninstall.ps1" in paths, "` / ` alternates are being dropped"


@pytest.mark.parametrize("directory", EXHAUSTIVE)
def test_every_enumerated_directory_is_in_the_block(directory: str):
    """If a directory is renamed, its `EXHAUSTIVE` entry stops matching anything
    and the check below would pass by finding nothing to compare."""
    assert directory in declared_paths(), (
        f"{directory} is listed as exhaustively enumerated but does not appear in "
        f"{HEADING!r}. Either the tree renamed it -- in which case rename it here "
        "too -- or its check has been silently doing nothing."
    )


def test_no_top_level_directory_is_missing_from_the_map(tree):
    """The check the other two cannot make.

    Forward only inspects paths that are written down; reverse only descends
    into directories already on the map. A whole directory that nobody ever
    listed is invisible to both -- which is exactly how `evidence/` stayed off
    the map while three separate places in this README told the reader to run a
    command against a file inside it.
    """
    files, _ = tree
    top_level = {path.split("/")[0] + "/" for path in files if "/" in path}
    absent = sorted(top_level - TOP_LEVEL_EXEMPT - declared_paths())
    assert absent == [], (
        "these directories ship, but README's layout block never mentions them:\n  "
        + "\n  ".join(absent)
        + "\n\nA reader checking whether a path this README cites actually exists "
        "looks here, does not find it, and concludes the instruction is wrong. "
        "Add a line to the block, or -- if a reader is genuinely never sent there "
        "-- add it to TOP_LEVEL_EXEMPT in this file with the reason."
    )


def test_every_path_on_the_map_is_really_shipped(tree):
    files, directories = tree
    phantom = sorted(p for p in declared_paths() if p not in files and p not in directories)
    assert phantom == [], (
        "README's layout block names paths that no clone contains:\n  "
        + "\n  ".join(phantom)
        + "\n\nA reader follows these. An entry pointing at nothing sends them "
        "looking for something that was renamed or deleted."
    )


@pytest.mark.parametrize("directory", EXHAUSTIVE)
def test_an_enumerated_directory_hides_nothing(directory: str, tree):
    files, directories = tree
    depth = directory.count("/")
    present = {
        path[len(directory) :]
        for path in files
        if path.startswith(directory) and "/" not in path[len(directory) :]
    } | {
        path[len(directory) :]
        for path in directories
        if path.startswith(directory) and path[len(directory) :].count("/") == 1
    }
    present -= IGNORED

    declared = declared_paths()
    listed = {
        path[len(directory) :]
        for path in declared
        if path.startswith(directory) and path != directory and path.count("/") <= depth + 1
    }

    unlisted = sorted(present - listed)
    assert unlisted == [], (
        f"{directory} is enumerated in README's layout block, but these ship without "
        f"appearing on it:\n  " + "\n  ".join(unlisted) + "\n\n"
        "Add a line to the block, keeping the column alignment of its neighbours. "
        "If this file genuinely does not belong on a reader's map, the honest fix is "
        f"to drop {directory!r} from EXHAUSTIVE in this file and say why -- not to "
        "leave the map quietly wrong."
    )
