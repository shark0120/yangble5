"""The launch handoff must remain a draft whose claims match release facts.

The package exists so the user can press publication buttons after reviewing
one place. A stale breaking-change warning, version recommendation, measurement
or artifact digest would make that convenience actively dangerous.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised by Python 3.10 CI
    import tomli as tomllib


DEFAULT_ROOT = pathlib.Path(__file__).resolve().parent.parent
ROOT = pathlib.Path(os.environ.get("YB5_RELEASE_PACK_ROOT", DEFAULT_ROOT))
PACK = ROOT / "docs" / "launch" / "RELEASE_PACK.md"
CHANGELOG = ROOT / "CHANGELOG.md"
EVIDENCE = ROOT / "evidence" / "run-749k-20260721.jsonl"
ARCHIVE = ROOT / "site" / "skill" / "yangble5-skill-v1.0.0.tar.gz"
SIDECAR = ARCHIVE.with_suffix(ARCHIVE.suffix + ".sha256")


def _pack() -> str:
    return PACK.read_text(encoding="utf-8")


def _release_candidate() -> str:
    text = CHANGELOG.read_text(encoding="utf-8")
    unreleased, remainder = text.split("## [Unreleased]", 1)[1].split(
        "\n## [0.2.0] - 2026-07-27", 1
    )
    assert not unreleased.strip(), "new work belongs under the empty Unreleased heading"
    return remainder.split("\n## [0.1.0]", 1)[0]


def _marked(text: str, name: str) -> str:
    start = f"<!-- {name}:BEGIN -->"
    end = f"<!-- {name}:END -->"
    assert text.count(start) == 1 and text.count(end) == 1
    return text.split(start, 1)[1].split(end, 1)[0]


def _records() -> tuple[dict, list[dict]]:
    lines = [json.loads(line) for line in EVIDENCE.read_text(encoding="utf-8").splitlines()]
    return lines[0]["_meta"], lines[1:]


def test_recommended_version_follows_the_projects_breaking_change_policy():
    with (ROOT / "pyproject.toml").open("rb") as handle:
        current = tomllib.load(handle)["project"]["version"]
    assert current == "0.2.0", "release-preparation tree must carry the recommended version"
    changelog = CHANGELOG.read_text(encoding="utf-8")
    release_candidate = _release_candidate()
    assert "BREAKING" in release_candidate
    assert "/pool/status" in release_candidate
    assert "BYOK stored other people's provider credentials in plaintext" in release_candidate
    assert (
        "[Unreleased]: https://github.com/shark0120/yangble5/compare/v0.2.0...HEAD"
        in changelog
    )
    assert (
        "[0.2.0]: https://github.com/shark0120/yangble5/releases/tag/v0.2.0" in changelog
    )
    pack = _pack()
    assert "recommend `v0.2.0`" in pack
    assert "pre-1.0 breaking change bumps MINOR" in pack
    assert (
        "blob/v0.2.0/CHANGELOG.md#020---2026-07-27" in pack
    ), "release draft must link to the versioned 0.2.0 changelog section"
    assert "[FINAL_RELEASE_DATE]" not in pack
    assert "[FINAL_CHANGELOG_ANCHOR]" not in pack


def test_release_draft_carries_the_product_and_both_breaking_changes():
    draft = _marked(_pack(), "RELEASE_DRAFT")
    for required in (
        "uses: shark0120/yangble5@v0.2.0",
        "tools/cache_bench.py --replay",
        "pool_ceiling_configured",
        "YANGBLE5_BYOK_ENCRYPTION_KEY",
        "stored in plaintext SQLite",
        "YANGBLE5_BYOK_ENABLED=false",
        "[UPSTREAM_ISSUE_URL]",
    ):
        assert required in draft


def test_release_measurements_come_from_the_committed_record():
    meta, rounds = _records()
    draft = _marked(_pack(), "RELEASE_DRAFT")
    expected = meta["expected_headline"]
    assert f"{expected['warm_token_weighted_hit_rate']:.2%}" in draft
    assert f"{expected['cold_round_prompt']:,} tokens" in draft
    for row in rounds:
        assert f"{row['latency_ms']:,}" in draft
    for qualifier in (
        "warm-only",
        "0%",
        "prefix-size dependent",
        "one machine and one run",
        "no repetitions or error bars",
        "did **not** reliably make requests faster",
    ):
        assert qualifier in draft


def test_optional_artifact_digest_matches_both_bytes_and_sidecar():
    digest = hashlib.sha256(ARCHIVE.read_bytes()).hexdigest()
    sidecar_digest = SIDECAR.read_text(encoding="utf-8").split()[0]
    assert digest == sidecar_digest
    assert digest in _marked(_pack(), "RELEASE_DRAFT")


def test_upstream_draft_separates_current_source_from_live_reproduction():
    pack = _pack()
    draft = _marked(pack, "UPSTREAM_ISSUE_DRAFT")
    for required in (
        "v7.2.101",
        "conductor_models.go#L80-L108",
        "openai_compat_pool_test.go#L259-L287",
        "optionally session-sticky",
        "current rotating behavior as the default",
        "did **not** run a controlled pool-versus-direct A/B",
        "not benchmark data",
    ):
        assert required in pack or required in draft
    assert "Do not claim a live v7.2.101 reproduction" in pack
    assert "Live observation: v7.1.23" in draft


def test_pack_keeps_every_public_action_user_only_and_user_receipts_unfilled():
    pack = _pack()
    assert "All five rows below are **USER ONLY**" in pack
    assert "The user—not an agent—creates/pushes the tag" in pack
    for placeholder in (
        "[FINAL_COMMIT]",
        "[FINAL_CI_RUN_URL]",
        "[UPSTREAM_ISSUE_URL]",
    ):
        assert placeholder in pack
    commands = re.findall(r"^git (?:push|tag)\b", pack, flags=re.MULTILINE)
    assert commands == [], (
        "the draft must describe public actions, not make them agent-run commands"
    )
