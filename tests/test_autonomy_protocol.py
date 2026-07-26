"""Lock the public autonomy protocol to its manifest and local loop contract."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re

import pytest

DEFAULT_ROOT = pathlib.Path(__file__).resolve().parent.parent
ROOT = pathlib.Path(os.environ.get("YB5_AUTONOMY_PROTOCOL_ROOT", DEFAULT_ROOT))
MANIFEST = ROOT / "docs" / "autonomy-rules.json"
PROTOCOL = ROOT / "docs" / "launch" / "autonomous-maintenance-protocol.md"
DEFAULT_LOOP = ROOT.parent / "_AI-NAV" / "YB5-LOOP.md"
LOOP = pathlib.Path(os.environ.get("YB5_LOOP_PATH", DEFAULT_LOOP))

DOC_BEGIN = "<!-- AUTONOMY_RULES:BEGIN -->"
DOC_END = "<!-- AUTONOMY_RULES:END -->"
LOOP_BEGIN = "<!-- AUTONOMY_CONTRACT:BEGIN -->"
LOOP_END = "<!-- AUTONOMY_CONTRACT:END -->"
ROW = re.compile(r"^\| `([a-z0-9-]+)` \| (.+) \|$", re.MULTILINE)
LOOP_RULE = re.compile(r"^- autonomy-rule: `([a-z0-9-]+)`$", re.MULTILINE)
LOOP_DIGEST = re.compile(r"^autonomy-contract-sha256: `([0-9a-f]{64})`$", re.MULTILINE)


def _manifest_payload() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _manifest_digest() -> str:
    canonical = json.dumps(
        _manifest_payload(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _manifest_rules() -> list[tuple[str, str]]:
    payload = _manifest_payload()
    assert payload.get("schema_version") == 1
    rules = payload.get("rules")
    assert isinstance(rules, list) and rules, "autonomy manifest has no rules"
    parsed = []
    for rule in rules:
        assert set(rule) == {"id", "principle"}
        rule_id = rule["id"]
        principle = rule["principle"]
        assert re.fullmatch(r"[a-z0-9-]+", rule_id)
        assert isinstance(principle, str) and principle.endswith(".")
        assert "\n" not in principle and "|" not in principle
        parsed.append((rule_id, principle))
    assert len({rule_id for rule_id, _ in parsed}) == len(parsed), "duplicate autonomy rule ID"
    return parsed


def _between(text: str, begin: str, end: str) -> str:
    assert text.count(begin) == 1 and text.count(end) == 1
    return text.split(begin, 1)[1].split(end, 1)[0]


def _loop_contract(text: str) -> tuple[str, list[str]]:
    block = _between(text, LOOP_BEGIN, LOOP_END)
    digests = LOOP_DIGEST.findall(block)
    assert len(digests) == 1, "loop autonomy contract must carry exactly one manifest digest"
    rule_ids = LOOP_RULE.findall(block)
    assert rule_ids, "loop autonomy contract has no rule IDs"
    assert len(rule_ids) == len(set(rule_ids)), "loop autonomy contract repeats a rule ID"
    return digests[0], rule_ids


def test_public_protocol_table_is_exactly_the_manifest():
    protocol = PROTOCOL.read_text(encoding="utf-8")
    table = _between(protocol, DOC_BEGIN, DOC_END)
    rows = ROW.findall(table)
    assert rows, "public autonomy table parser matched nothing"
    assert rows == _manifest_rules(), (
        "the public protocol table diverged from docs/autonomy-rules.json; "
        "change the authority and rendered table together"
    )


def test_public_protocol_is_a_generic_user_publish_draft():
    text = PROTOCOL.read_text(encoding="utf-8")
    assert "> **Draft — user publishes, agents do not.**" in text
    for private_term in (
        "yangble5",
        "YB5-LOOP",
        "_AI-NAV",
        "C:\\",
        "shark0120",
    ):
        assert private_term not in text, (
            f"public protocol leaked project-local term {private_term!r}"
        )


def test_loop_contract_matches_manifest_when_the_loop_is_available():
    if not LOOP.is_file():
        pytest.skip("external loop file is not present in this checkout")
    digest, rule_ids = _loop_contract(LOOP.read_text(encoding="utf-8"))
    expected_digest = _manifest_digest()
    expected_ids = [rule_id for rule_id, _ in _manifest_rules()]
    assert digest == expected_digest, (
        f"{LOOP} pins autonomy manifest {digest}, expected {expected_digest}"
    )
    assert rule_ids == expected_ids, (
        f"{LOOP} autonomy rule IDs diverged: found {rule_ids}, expected {expected_ids}"
    )


def test_loop_contract_parser_rejects_an_empty_or_duplicate_roster():
    digest = _manifest_digest()
    empty = f"{LOOP_BEGIN}\nautonomy-contract-sha256: `{digest}`\n{LOOP_END}"
    with pytest.raises(AssertionError, match="no rule IDs"):
        _loop_contract(empty)

    duplicate = (
        f"{LOOP_BEGIN}\n"
        f"autonomy-contract-sha256: `{digest}`\n"
        "- autonomy-rule: `ci-first`\n"
        "- autonomy-rule: `ci-first`\n"
        f"{LOOP_END}"
    )
    with pytest.raises(AssertionError, match="repeats a rule ID"):
        _loop_contract(duplicate)
