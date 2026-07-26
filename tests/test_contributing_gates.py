"""CONTRIBUTING's roster of non-pytest gates must still be CI's roster.

``### The gates that are not pytest`` is where a contributor learns what will
block their pull request for a reason ``pytest -q`` will never tell them about.
It is hand-maintained prose, and it has already rotted once: it said **two**
checks run outside pytest while CI ran **four**.  The instructive part is that
the sentence was *correct when it was written*.  ``de14474`` wrote "two" when
two was the truth; ``0027dd0`` added ``honesty_gate.py`` and ``d9539c7`` added
``name_guard.py``, and neither commit had any reason to open ``CONTRIBUTING.md``.
The prose was stale for 35 commits before ``86e1c26`` corrected it -- and it was
corrected then only because that commit was already putting README's map under
test and noticed the neighbouring rot.  Nobody was careless.  Adding a gate is a
``run:`` line in ``.github/workflows/ci.yml``, and the person best placed to
notice the omission -- whoever gets the surprise red -- is by then already
surprised.

This is the same defect as the one ``test_readme_layout.py`` guards, and it is
worth stating why a prose roster is worse than a stale README tree.  A missing
line on a map is an omission: the reader looks for something and does not find
it.  A missing gate here is an *instruction that was never given*.  The
contributor runs everything CONTRIBUTING lists, gets a clean local run, pushes,
and is told by CI that a check they had never heard of has failed -- on a repo
whose own pitch is that its claims are checkable rather than believed.

Two directions, and they are not the same check:

* **Forward** -- every gate CI runs is named in the section.  This is the one
  that rotted.
* **Reverse** -- every ``tools/*.py`` the section names is really run by CI.  A
  roster that lists a gate CI dropped is worse than one that omits a gate: it
  spends a contributor's time on a check that cannot block anything, and the
  next stale entry is indistinguishable from it.

The word "gate" is doing real work and the distinction is this file's only
judgement call.  ``tools/`` also holds ``cache_bench.py``, ``cache_stats_sidecar.py``
and ``claude_shim.py``, which CI runs -- but only as ``--help``, to prove they
still start.  Those are tools a *user* runs, not gates over the repository's
content, and a rule of "every script under ``tools/`` must appear in
CONTRIBUTING" would go red today, on an unchanged repo, for three files that
belong nowhere near a contributor's pre-push checklist.  A gate here is a
``tools/*.py`` invocation whose non-zero exit fails the build because of what it
*read*; a bare ``--help`` probe is excluded, and that exclusion is what keeps
this test honest rather than merely strict.

The roster is a set of *scripts*, not of invocations, and that is the right
granularity rather than a convenience.  Two of the six ``sitecheck.py`` lines
(``ci.yml`` 532 and 558) are negative controls where a non-zero exit is the
required pass, and the two ``drift_check.py`` lines (749, 755) are one logical
gate retried, so only the second one's exit is decisive.  Per-invocation
classification would have to get all four of those backwards-looking cases
right to answer a question nobody asked.  What a contributor needs to know is
which scripts they should have run before pushing, and that is a set of names.

The reverse direction is scoped to the section for the same reason.
``CONTRIBUTING.md`` line 13 opens the whole document with a
``python tools/cache_bench.py`` command -- the benchmark a reader is invited to
run against their own account.  Matching ``tools/*.py`` across the whole file
would drag that into the roster and fail immediately.

What this file deliberately does **not** lock: the section also says three of
the four gates are offline and the last needs a network.  That split is not
derivable from the repository with the rigour the rest of this file has.  The
obvious proxy -- does the script import ``urllib`` -- is sound only if a gate
cannot reach the network any other way, and ``name_guard.py`` shells out through
``subprocess``, so the proxy is already not airtight.  A lock that is *nearly*
right about a claim is worse than no lock, because a red it cannot justify is a
red somebody deletes.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CI = ROOT / ".github" / "workflows" / "ci.yml"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"

HEADING = "### The gates that are not pytest"

# The one invocation form this file's parser understands. Every gate in ci.yml
# is written this way today; `test_no_gate_is_invoked_in_a_form_this_parser_cannot_see`
# below fails loudly if a second spelling ever appears, because the alternative
# -- a parser that quietly matches nothing -- is how a roster check becomes a
# roster check that certifies nothing.
INVOCATION = re.compile(r"python\s+tools/(\w+)\.py([^\n]*)")

# Interpreter spellings that would run a gate and be invisible to INVOCATION.
# `python -c "import tools.cache_bench"` is not here on purpose: importing a
# module proves it parses, which is the same class of thing as `--help`.
UNSEEN_FORMS = {
    "python3 tools/": re.compile(r"\bpython3\s+tools/"),
    "py -3 tools/": re.compile(r"\bpy\s+-3\s+tools/"),
    "python -m tools.": re.compile(r"-m\s+tools\."),
    "./tools/ (executable bit)": re.compile(r"(?<![\w.])\./tools/\w+\.py"),
}

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

# The two countable claims in the section's opening sentence. Both are read, and
# a section with neither is a failure rather than a pass -- see
# `test_the_section_still_states_a_count`.
COUNT_CLAIMS = (
    re.compile(r"\b(\w+) more checks run outside pytest", re.IGNORECASE),
    re.compile(r"\ball (\w+) guard\b", re.IGNORECASE),
)


def _strip_yaml_comment(line: str) -> str:
    """Drop a trailing ``#`` comment.

    Not cosmetic. ``ci.yml`` line 178 contains the exact text
    ``python tools/sitecheck.py`` while explaining the stdlib-only import rule --
    and it is a *Python* comment, inside a ``python - <<'PY'`` heredoc, inside a
    YAML block scalar. Three levels of nesting, which is why this strips ``#`` to
    end-of-line generally rather than trying to know which language it is in.
    It happens to name a script that is a gate anyway, so a parser that missed
    this would get today's answer right for the wrong reason -- and would start
    inventing gates the first time somebody writes a comment about a tool that
    is not one.
    """
    return re.sub(r"(^|\s)#.*$", "", line)


def _ci_text() -> str:
    lines = CI.read_text(encoding="utf-8").splitlines()
    return "\n".join(_strip_yaml_comment(line) for line in lines)


@pytest.fixture(scope="module")
def roster() -> set[str]:
    """The ``tools/*.py`` scripts CI runs as gates, by basename."""
    gates = set()
    for name, args in INVOCATION.findall(_ci_text()):
        if args.strip() != "--help":
            gates.add(name)
    return gates


@pytest.fixture(scope="module")
def section() -> str:
    """``### The gates that are not pytest``, up to the next heading."""
    lines = CONTRIBUTING.read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == HEADING]
    assert len(starts) == 1, (
        f"expected exactly one {HEADING!r} heading in CONTRIBUTING.md, found {len(starts)}. "
        "This test reads that section by name; if it was renamed, rename it here too "
        "rather than leaving the roster unguarded."
    )
    start = starts[0]
    end = next(
        (i for i in range(start + 1, len(lines)) if re.match(r"^#{1,3} ", lines[i])),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _named_in(section_text: str) -> set[str]:
    return set(re.findall(r"tools/(\w+)\.py", section_text))


def test_the_parser_found_something_to_check(roster, section):
    """The check every other test here depends on.

    A regex that matches nothing reports no drift, which is also what a repo in
    perfect order reports. `test_readme_layout.py` guards its own parser the
    same way and for the same reason: reformat `ci.yml`, and without this the
    whole file would go quietly, permanently green.
    """
    assert len(roster) >= 3, (
        f"only parsed {len(roster)} gates out of {CI}: {sorted(roster)}. "
        "Either the workflow was reformatted into a shape INVOCATION no longer "
        "matches, or gates were removed. Both need a human."
    )
    assert "honesty_gate" in roster, "the parser is not reading plain `run:` steps"
    assert "sitecheck" in roster, "the parser is not reading multi-step jobs"
    assert "drift_check" in roster, (
        "the parser is not reading invocations inside shell blocks -- drift_check "
        "is run with redirections in the live-site-drift job"
    )
    assert len(_named_in(section)) >= 3, (
        "the section was located but names almost no tools/*.py scripts, so the "
        "reverse check below has nothing to compare against"
    )


def test_a_help_probe_is_not_mistaken_for_a_gate(roster):
    """The file's one judgement call, asserted rather than assumed.

    `cache_bench.py`, `cache_stats_sidecar.py` and `claude_shim.py` are run by
    CI only as `--help`. They are tools a user runs. If the exclusion in
    `roster` ever stops working, this test goes red *before*
    `test_every_gate_ci_runs_is_in_the_contributing_roster` does -- which is the
    difference between "your exclusion broke" and three baffling demands to
    document a benchmark as a gate.
    """
    text = _ci_text()
    for tool in ("cache_bench", "cache_stats_sidecar", "claude_shim"):
        assert f"python tools/{tool}.py --help" in text, (
            f"tools/{tool}.py is no longer run as a --help probe in ci.yml. If it "
            "became a gate, document it in CONTRIBUTING; if it was dropped, remove "
            "it from this test."
        )
        assert tool not in roster, (
            f"tools/{tool}.py is a --help startup probe, not a gate, but the parser "
            "counted it as one. It would now demand a place in a contributor's "
            "pre-push checklist it does not belong in."
        )


def test_no_gate_is_invoked_in_a_form_this_parser_cannot_see():
    """A parser that does not understand a line must say so, not skip it.

    This is the failure mode that matters. B16 exists because a gate was added
    and nothing pointed at the documentation; a check that silently ignores
    `python3 tools/newgate.py` reproduces exactly that, while reporting green.
    """
    text = _ci_text()
    for description, pattern in UNSEEN_FORMS.items():
        match = pattern.search(text)
        assert match is None, (
            f"ci.yml invokes a tools/ script as {description!r} "
            f"({match.group(0)!r}), and INVOCATION in this file only understands "
            "`python tools/<name>.py`. Teach the regex the new form -- do not "
            "leave a gate that this roster check cannot see."
        )


def test_every_gate_ci_runs_is_in_the_contributing_roster(roster, section):
    """Forward. The direction that rotted."""
    undocumented = sorted(roster - _named_in(section))
    assert undocumented == [], (
        "CI fails a pull request on these gates, and CONTRIBUTING never mentions them:\n  "
        + "\n  ".join(f"tools/{name}.py" for name in undocumented)
        + f"\n\nAdd each to {HEADING!r} in CONTRIBUTING.md, with the sentence a "
        "contributor needs: what it reads, and what a red one means. A gate nobody "
        "was told about is discovered by being surprised by it."
    )


def test_every_gate_contributing_names_is_really_run(roster, section):
    """Reverse. A roster entry CI dropped costs a contributor real time."""
    phantom = sorted(_named_in(section) - roster)
    assert phantom == [], (
        f"{HEADING!r} tells contributors to run these, but ci.yml does not run them "
        "as gates:\n  " + "\n  ".join(f"tools/{name}.py" for name in phantom)
        + "\n\nEither the gate was dropped from CI -- in which case drop it here too "
        "-- or it is now invoked in a form this file's parser does not understand, "
        "which is the more dangerous of the two."
    )


def test_the_section_still_states_a_count(section):
    """"Four more checks" is the sentence that was wrong. Keep it countable.

    A count claim rewritten out of existence is not a fix: it removes the
    statement a reader uses to know whether they have run everything. If the
    prose genuinely no longer counts anything, this test should be changed
    deliberately rather than pass by finding nothing to read.
    """
    found = [pattern.search(section) for pattern in COUNT_CLAIMS]
    assert any(found), (
        f"no countable claim left in {HEADING!r}. This test locks the sentence that "
        "once said two gates ran outside pytest while four did. If it was reworded, "
        "point COUNT_CLAIMS at the new wording."
    )


@pytest.mark.parametrize("pattern", COUNT_CLAIMS, ids=["opening-sentence", "all-N-guard"])
def test_the_stated_count_matches_the_roster(pattern, roster, section):
    match = pattern.search(section)
    if match is None:
        pytest.skip("this phrasing is not present; test_the_section_still_states_a_count "
                    "fails if none of them is")
    word = match.group(1).lower()
    assert word in NUMBER_WORDS, (
        f"{match.group(0)!r} does not state a number this test can read. Write the "
        f"count as a word ({', '.join(sorted(NUMBER_WORDS))}) so it stays checkable."
    )
    assert NUMBER_WORDS[word] == len(roster), (
        f"CONTRIBUTING says {match.group(0)!r}, but ci.yml runs {len(roster)} gates "
        f"outside pytest: {', '.join(sorted(roster))}.\n\n"
        "This exact sentence has been wrong before -- it said two while four ran."
    )
