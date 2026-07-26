"""Lock each normative interview answer to one runnable verification command.

The commands do not certify editorial judgement. They verify the implementation
premise underneath each answer: the contract shape, installer boundary, consent
behaviour, registration branch or documentation roster. The prose may improve
without changing a command; a renamed or empty test selection may not.
"""

from __future__ import annotations

import os
import pathlib
import re
import shlex

DEFAULT_ROOT = pathlib.Path(__file__).resolve().parent.parent
ROOT = pathlib.Path(os.environ.get("YB5_AGENT_INTERVIEW_ROOT", DEFAULT_ROOT))
INTERVIEW = ROOT / "docs" / "AGENT_INTERVIEW.md"

ANSWER_HEADINGS = {
    "contract-authority": "**Rule 1 — the contract is live; this file is not.**",
    "installer-integrity-boundary": (
        "**Rule 2 — the published hash pins the script, not the command line.**"
    ),
    "transcript-secrets": "## 1. What you must never put in your own transcript",
    "derived-facts": "## 2. What you derive before you open your mouth",
    "machine-id": "### `machine_id` — **DERIVE**, and never ask",
    "email": "### `email` — **ASK, once, as an opt-in — never as a requirement**",
    "invite-code": (
        "### `invite_code` — **DERIVE whether it is needed; ASK for the value only if it is**"
    ),
    "label": (
        "### `label` — **do not ask today.** This is a gap in the installer, "
        "not in the contract"
    ),
    "mandatory-consent": "### 4.1 The one mandatory question",
    "recommended-sequence": "### 4.2 Recommended sequence",
    "wrong-questions": "### 4.3 Questions that are always wrong",
    "refusal-errors": "## 5. When it refuses: every error type",
    "registration-throttled": (
        "### 5.1 `registration_throttled` in full — the branch where the obvious advice is wrong"
    ),
    "already-registered": "### 5.2 `already_registered` in full",
    "success-outcomes": "## 6. When it works: what you must tell them",
    "new-key": "### 6.1 `201` — a new key",
    "reused-key": (
        '### 6.2 `200` with `"reused": true` — a re-run on a machine that already registered'
    ),
    "byok-only-instance": (
        "### 6.3 `404` or `501` — this instance does not offer registration"
    ),
    "shortest-interview": "## 7. The shortest correct interview",
    "known-gaps": "## 8. Known gaps",
}

CONTAINER_HEADINGS = {
    "## 0. The two rules that outrank the rest of this document",
    "## 3. Every field the API accepts",
    "## 4. The interview",
    "## See also",
}

MARKER = re.compile(r"<!-- interview-answer:([a-z0-9-]+) -->")
VERIFY_LINE = re.compile(r"^\*\*Verify:\*\* `([^`\r\n]+)`$", re.MULTILINE)


def _text() -> str:
    return INTERVIEW.read_text(encoding="utf-8")


def _answer_blocks() -> dict[str, str]:
    text = _text()
    matches = list(MARKER.finditer(text))
    assert matches, f"{INTERVIEW} has no interview-answer markers"
    blocks = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        answer_id = match.group(1)
        assert answer_id not in blocks, f"duplicate interview answer marker {answer_id!r}"
        blocks[answer_id] = text[match.end() : end]
    return blocks


def _commands() -> dict[str, str]:
    commands = {}
    for answer_id, block in _answer_blocks().items():
        found = VERIFY_LINE.findall(block)
        assert len(found) == 1, (
            f"answer {answer_id!r} has {len(found)} verification commands; "
            "every answer must carry exactly one"
        )
        commands[answer_id] = found[0]
    return commands


def test_every_normative_answer_heading_has_one_marker_and_one_command():
    text = _text()
    assert "Every **Verify** line below runs from a repository checkout" in text
    assert "not substitutes for fetching the live contract" in text
    blocks = _answer_blocks()
    discovered_headings = set(re.findall(r"^#{2,3} .+$", text, re.MULTILINE))
    discovered_headings.update(
        re.findall(r"^\*\*Rule [12] — .+\*\*$", text, re.MULTILINE)
    )
    classified_headings = set(ANSWER_HEADINGS.values()) | CONTAINER_HEADINGS
    assert discovered_headings == classified_headings, (
        "interview heading classification drift: "
        f"unclassified={sorted(discovered_headings - classified_headings)}, "
        f"gone={sorted(classified_headings - discovered_headings)}. "
        "A normative answer needs a marker and command; a structural heading "
        "needs an explicit CONTAINER_HEADINGS exemption."
    )
    assert set(blocks) == set(ANSWER_HEADINGS), (
        f"answer marker drift: added={sorted(set(blocks) - set(ANSWER_HEADINGS))}, "
        f"missing={sorted(set(ANSWER_HEADINGS) - set(blocks))}"
    )
    for answer_id, heading in ANSWER_HEADINGS.items():
        assert f"<!-- interview-answer:{answer_id} -->\n{heading}" in text, (
            f"answer marker {answer_id!r} is no longer attached to {heading!r}"
        )
    commands = _commands()
    assert set(commands) == set(blocks)
    assert len(VERIFY_LINE.findall(text)) == len(blocks), (
        "a **Verify:** command exists outside an answer block, or an answer has more than one"
    )


def test_every_answer_command_selects_real_tests_without_shell_indirection():
    for answer_id, command in _commands().items():
        assert not any(char in command for char in ";&|><\r\n"), (
            f"answer {answer_id!r} uses shell composition; verification must be one command"
        )
        argv = shlex.split(command, posix=True)
        assert argv[:4] == ["python", "-m", "pytest", "-q"], (
            f"answer {answer_id!r} must use the portable `python -m pytest -q` form"
        )
        assert len(argv) in (5, 7), f"answer {answer_id!r} has an unknown pytest command shape"
        target = argv[4]
        assert target.startswith("tests/") and target.endswith(".py")
        source_path = ROOT / target
        assert source_path.is_file(), f"answer {answer_id!r} targets missing {target}"
        names = re.findall(
            r"^def (test_[a-zA-Z0-9_]+)\s*\(",
            source_path.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        assert names, f"answer {answer_id!r} targets a file with no top-level tests"
        if len(argv) == 7:
            assert argv[5] == "-k"
            terms = [term.strip() for term in argv[6].split(" or ")]
            assert terms and all(re.fullmatch(r"[a-zA-Z0-9_]+", term) for term in terms)
            for term in terms:
                assert any(term in name for name in names), (
                    f"answer {answer_id!r} selects {term!r}, but no test in {target} "
                    "contains that name. The command would collect nothing for that branch."
                )


def test_shortest_interview_answer_stays_one_question():
    section = _answer_blocks()["shortest-interview"]
    assert "the entire human-facing interview is **one question**" in section
    assert section.count("Shall I go ahead?") == 1
    assert "Optionally followed by the e-mail offer" in section


def test_known_gaps_answer_stays_true():
    section = _answer_blocks()["known-gaps"]
    shell = (ROOT / "site" / "install.sh").read_text(encoding="utf-8")
    powershell = (ROOT / "site" / "install.ps1").read_text(encoding="utf-8")
    shell_label_mentions = [
        line
        for line in shell.splitlines()
        if "--label" in line and not line.lstrip().startswith("#")
    ]
    assert shell_label_mentions == [], "install.sh now has an executable --label path"
    assert "-Label" not in powershell, "install.ps1 now exposes -Label"

    site_test = (ROOT / "tests" / "test_site_agent_install_path.py").read_text(encoding="utf-8")
    match = re.search(r'^CANONICAL_LINE = "([^"]+)"$', site_test, re.MULTILINE)
    assert match is not None
    assert match.group(1) not in {line.strip() for line in _text().splitlines()}, (
        "AGENT_INTERVIEW now prints the canonical one-liner it says must have one authority"
    )
    assert "The lock covers exact answers, not editorial judgement." in section
