"""The error table an agent reads must still describe the errors it will get.

There are three rosters of the same fact in this repository, and until this file
existed only two of them were held to anything:

  A. ``gateway/app.py``          -- the ``_error(status, kind, ...)`` calls.  The
                                    source of truth; everything else is a claim
                                    about it.
  B. ``GET /auth/register``      -- ``error_types``, the machine-readable
                                    contract.  Locked to A in both directions by
                                    ``test_gateway_agent_contract.py`` -- the
                                    probe test for raised-but-undocumented, and
                                    an AST walk for documented-but-unraisable.
  C. ``docs/AGENT_INTERVIEW.md`` -- the markdown table at ``## 5. When it
                                    refuses``.  Fifteen rows, five columns, hand
                                    written.  Checked by nothing.

C is not the least important of the three.  It is the one with the widest
audience: ``site/llms.txt`` and ``site/AGENTS.md`` both send agents to it by
name, and it is the only roster that says what to *do* about each error in
sentences a model will act on.  B tells an agent that ``invite_invalid`` exists.
C is where it learns never to try a second code, because attempts are counted
before the invite check and a retry buys a lockout instead of a key.

So the failure this file exists to prevent is not "the docs are slightly out of
date".  It is an agent reading a row that is no longer true and confidently
doing the wrong thing to somebody's install -- or, on the other side, meeting an
``error.type`` the table never mentions and falling through to whatever its
generic handler does, which is usually to retry.

That failure was already live when this file was written.  The
``too_many_auth_failures`` row said "it can fire on a registration because of
something that happened elsewhere on the network", and registration is
deliberately exempt from that backoff -- ``gateway/app.py`` says so at the
``error_types`` dict, and ``test_gateway_agent_contract.py`` asserts a locked-out
address can still register, because the endpoint the lockout would block is the
one ``key_superseded`` tells the caller to use to escape it.  An agent that
believed the row would have told a human their registration was rate-limited
when it was not, and sat out a ``Retry-After`` instead of registering.

Five checks: two directions, the rule that makes the reverse direction honest
(in both of its own directions), and a number in a sentence.

Two notes on what this file deliberately does NOT do.

It does not re-derive "which errors are reachable from registration".  B already
carries that answer and is already proven against A, so C is measured against B.
A second, competing reachability walk would be a second thing to get wrong, and
when the two disagreed the cheapest fix would be to loosen whichever one was
noisier -- which is how a guard stops guarding.

It does not check the prose columns.  What an agent should tell a human is a
judgement, it changes for good reasons, and a test that pins it would be edited
away the first time somebody improved a sentence.  The one exception is the
marker below, which is not prose: it is a machine-checked claim that happens to
be written where the reader can see it.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
INTERVIEW = ROOT / "docs" / "AGENT_INTERVIEW.md"
APP = ROOT / "gateway" / "app.py"

# | `invite_required` | 400 | what it is really about | ... | ... |
_ROW = re.compile(r"^\|\s*`([a-z_]+)`\s*\|\s*(\d{3})\s*\|([^|]*)\|", re.M)

# The one sentence in the table this file does pin, because it is the difference
# between "this refuses your registration" and "this refuses your next API call".
NOT_A_REGISTRATION_REFUSAL = "**Not a registration refusal.**"


def _rows() -> dict[str, tuple[int, str]]:
    """The table, as {error.type: (HTTP status, what-it-is-really-about cell)}."""
    text = INTERVIEW.read_text(encoding="utf-8")
    rows = {m.group(1): (int(m.group(2)), m.group(3).strip()) for m in _ROW.finditer(text)}
    # A regex that stopped matching would make every check below vacuously true:
    # an empty table trivially contains no wrong rows. This is the same failure
    # the ASCII-tree parser in test_readme_layout.py guards against -- a parser
    # that silently reads a smaller document is a false green, not a pass.
    assert len(rows) >= 10, (
        f"only parsed {len(rows)} rows out of {INTERVIEW.name}; the table format "
        f"changed and every assertion in this file is now checking nothing"
    )
    # Rows are keyed by error.type, so two rows for the same type silently
    # collapse into one and the loser's advice disappears from every check
    # below -- while both remain in front of the agent, which reads whichever it
    # reaches first. Count the lines that LOOK like rows and insist they agree.
    looks_like = len([ln for ln in text.splitlines() if re.match(r"^\|\s*`[a-z_]+`\s*\|", ln)])
    assert looks_like == len(rows), (
        f"{looks_like} lines look like table rows but only {len(rows)} distinct "
        f"error types came out of them: the table documents one type twice. Both "
        f"rows are in front of the agent and only one is checked here"
    )
    return rows


def _table() -> dict[str, int]:
    """The rows of the error table, as {error.type: HTTP status}."""
    return {kind: status for kind, (status, _) in _rows().items()}


def _emitted() -> dict[str, set[int]]:
    """Every literal `_error(STATUS, "kind")` in gateway/app.py, by kind."""
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    out: dict[str, set[int]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "_error"):
            continue
        if len(node.args) < 2:
            continue
        status, kind = node.args[0], node.args[1]
        if not (isinstance(kind, ast.Constant) and isinstance(kind.value, str)):
            continue
        if isinstance(status, ast.Constant) and isinstance(status.value, int):
            out.setdefault(kind.value, set()).add(status.value)
    assert len(out) >= 10, (
        f"only found {len(out)} _error(...) kinds in {APP.name}; the helper was "
        f"renamed or the calls stopped passing literals, and this scan is blind"
    )
    return out


def _contract_error_types() -> list[str]:
    """The keys of the `error_types` dict served by GET /auth/register.

    Read from the source rather than from a live client on purpose: this file
    asks whether two *documents* agree, and booting the app would drag an engine
    key, a database and a settings object into a question that needs none of
    them.  ``test_gateway_agent_contract.py`` is where the served response is
    exercised for real.
    """
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        # ast.Dict guarantees these are parallel, so strict= costs nothing and
        # turns a future AST change into a failure rather than a silent truncation.
        for key, value in zip(node.keys, node.values, strict=True):
            if (
                isinstance(key, ast.Constant)
                and key.value == "error_types"
                and isinstance(value, ast.Dict)
            ):
                return [k.value for k in value.keys if isinstance(k, ast.Constant)]
    pytest.fail("no `error_types` dict literal in gateway/app.py; the contract moved")


# ── forward: every row is a real error, with the status the row states ──


@pytest.mark.parametrize("kind", sorted(_table()))
def test_every_documented_error_is_one_the_code_can_actually_emit(kind: str) -> None:
    """A row for an error that no longer exists is worse than a missing row.

    A missing row leaves an agent with no instructions and it falls back to
    something generic.  A phantom row leaves it with confident instructions for
    a situation it will never be in, and -- because the table is also how a
    reader decides what this service *does* -- implies a failure mode the
    service does not have.
    """
    emitted = _emitted()
    assert kind in emitted, (
        f"docs/AGENT_INTERVIEW.md documents error.type {kind!r}, but no "
        f"_error(..., {kind!r}) call exists in gateway/app.py. Either the code "
        f"stopped emitting it and the row is now fiction, or it was renamed and "
        f"every agent branching on the old name silently stopped matching."
    )


@pytest.mark.parametrize("kind,status", sorted(_table().items()))
def test_every_documented_status_matches_the_code(kind: str, status: int) -> None:
    """The HTTP column is not decoration.

    ``§5`` opens by telling an agent the status alone is ambiguous and the type
    is what to branch on -- but the installer branches on the *status*, and the
    table is where the two are reconciled.  A row whose status drifted describes
    a response neither consumer will ever see.
    """
    emitted = _emitted()
    if kind not in emitted:  # already reported by the test above
        pytest.skip(f"{kind} is not emitted at all")
    assert status in emitted[kind], (
        f"the table says {kind} answers HTTP {status}; gateway/app.py emits it "
        f"with {sorted(emitted[kind])}"
    )


# ── reverse: the table covers everything the machine contract announces ──


def test_the_table_documents_every_error_the_contract_announces() -> None:
    """B ⊆ C.

    ``GET /auth/register`` is already forced to announce every error the
    registration path can raise.  So anything it announces is something an agent
    can receive, and an agent that received it was sent here for instructions.
    A type in B and not in C is a real error with no advice attached.
    """
    table = _table()
    missing = [k for k in _contract_error_types() if k not in table]
    assert not missing, (
        f"GET /auth/register announces {missing} but the table in "
        f"docs/AGENT_INTERVIEW.md has no row for them. An agent that hits one "
        f"reads the document it was pointed at, finds nothing, and falls through "
        f"to its generic handler -- which for most agents means retrying."
    )


# ── the rule that makes the reverse direction honest ──
#
# The table is allowed to be WIDER than the machine contract, because an agent
# following this document does not stop at registration -- the moment it holds a
# key it starts making authenticated calls, and the errors on that surface are
# nowhere else in the document. Dropping such a row would leave the agent with
# no advice exactly where the wrong move (retry) is the expensive one.
#
# What it is not allowed to do is leave the reader guessing which kind of row
# they are looking at. A row for an error registration cannot return has to say
# so, in the cell, in the words below -- and then it is checked here in both
# directions: an undeclared extra row is a phantom, and a declared row that the
# contract *does* announce is a real registration error mislabelled as someone
# else's problem, which is the same lie pointing the other way.
#
# This replaces an earlier version of this file that simply hard-coded the one
# known extra name. That was weaker in the way that mattered: it asserted the
# row was fine without asserting anything the reader could see, and the row it
# blessed was, at that moment, telling agents something false.


def test_a_row_the_contract_does_not_announce_declares_itself() -> None:
    table = _rows()
    contract = set(_contract_error_types())
    undeclared = sorted(
        kind
        for kind, (_, about) in table.items()
        if kind not in contract and not about.startswith(NOT_A_REGISTRATION_REFUSAL)
    )
    assert not undeclared, (
        f"the table documents {undeclared} as registration errors, but "
        f"GET /auth/register does not announce them. Either the row is fiction "
        f"and should go, or it is a real error from another surface -- in which "
        f"case open its third column with {NOT_A_REGISTRATION_REFUSAL!r} so the "
        f"agent reading it knows not to expect it from a registration."
    )


def test_no_registration_error_is_marked_as_someone_elses_surface() -> None:
    contract = set(_contract_error_types())
    mislabelled = sorted(
        kind
        for kind, (_, about) in _rows().items()
        if kind in contract and about.startswith(NOT_A_REGISTRATION_REFUSAL)
    )
    assert not mislabelled, (
        f"{mislabelled} are announced by GET /auth/register as errors a "
        f"registration can return, but their rows say they are not. An agent "
        f"told to rule these out will meet one and have no branch for it."
    )


# ── the count in the sentence that tells an agent to branch on the type ──

_SPELLED = {
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


def test_the_ambiguity_warning_counts_the_rows_it_is_warning_about() -> None:
    """§5's opening argument is only as good as its arithmetic.

    The sentence is *why* an agent should branch on ``error.type`` rather than
    the status: "the HTTP status alone is ambiguous (three different types
    answer 429)".  Add a fourth 429 row and the sentence becomes false in the
    one direction that matters -- an agent that took it literally, enumerated
    three branches and treated the enumeration as complete now falls through on
    a real error.

    This is the third hand-written count in this repository found to drift as
    its list grew, after the README layout block and ``drift_check.py``'s note
    about how many files a red gate would take down.  The pattern is reliable
    enough to be worth checking on sight: a number spelled out in prose, beside
    a list somebody else will extend.
    """
    text = INTERVIEW.read_text(encoding="utf-8")
    claim = re.search(r"\((\w+) different types answer (\d{3})\)", text)
    assert claim, (
        "the ambiguity warning in §5 stopped naming a count; update this test or "
        "drop it, but do not leave it matching nothing"
    )
    spelled, status = claim.group(1), int(claim.group(2))
    assert spelled in _SPELLED, f"unrecognised number word {spelled!r} in the §5 warning"

    actual = sum(1 for row_status in _table().values() if row_status == status)
    assert _SPELLED[spelled] == actual, (
        f"§5 says {spelled} types answer {status}, but the table below it has "
        f"{actual} rows with that status. An agent told the list is complete will "
        f"enumerate {_SPELLED[spelled]} branches and fall through on the rest."
    )
