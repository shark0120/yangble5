"""The doc-lock article is a draft built from mechanisms that exist in this tree."""

from __future__ import annotations

import os
import pathlib
import re

DEFAULT_ROOT = pathlib.Path(__file__).resolve().parent.parent
ROOT = pathlib.Path(os.environ.get("YB5_DOC_LOCK_ARTICLE_ROOT", DEFAULT_ROOT))
ARTICLE = ROOT / "docs" / "launch" / "doc-locks.md"

EXAMPLES = {
    "released-measurements": {
        "paths": (
            "evidence/run-749k-20260721.jsonl",
            "tools/sitecheck.py",
            "tests/test_sitecheck.py",
        ),
        "proofs": {
            "tools/sitecheck.py": (
                "def load_measurement_record(",
                "PROMPT, CACHED, ROUND_MS = load_measurement_record()",
            ),
            "tests/test_sitecheck.py": (
                "LATENCY_SURFACES =",
                "test_every_latency_surface_tracks_the_evidence_record",
                "test_the_superseded_latency_transcription_is_rejected",
            ),
        },
    },
    "release-version": {
        "paths": (
            "pyproject.toml",
            "gateway/__init__.py",
            "site/README.md",
            "tests/test_release_version.py",
        ),
        "proofs": {
            "tests/test_release_version.py": (
                "def _project_version(",
                "def _gateway_version(",
                "def _documented_health_version(",
                "test_release_checklist_names_the_source_and_both_mirrors",
            ),
        },
    },
    "python-gate-roster": {
        "paths": (
            ".github/workflows/ci.yml",
            "CONTRIBUTING.md",
            "tests/test_contributing_gates.py",
        ),
        "proofs": {
            "tests/test_contributing_gates.py": (
                "INVOCATION = re.compile(",
                "test_every_gate_ci_runs_is_in_the_contributing_roster",
                "test_every_gate_contributing_names_is_really_run",
                "PYTHON_COUNT = re.compile(",
            ),
        },
    },
    "workflow-native-gates": {
        "paths": (
            ".github/workflows/ci.yml",
            "CONTRIBUTING.md",
            "tests/test_contributing_gates.py",
        ),
        "proofs": {
            "tests/test_contributing_gates.py": (
                "WORKFLOW_GATE_SIGNATURES =",
                "test_every_workflow_native_gate_is_in_the_contributing_roster",
                "test_every_workflow_native_gate_contributing_names_is_really_run",
                "WORKFLOW_COUNT = re.compile(",
            ),
        },
    },
    "ci-step-inventory": {
        "paths": (
            ".github/workflows/ci.yml",
            "tests/test_ci_step_inventory.py",
        ),
        "proofs": {
            "tests/test_ci_step_inventory.py": (
                "EXPECTED_STEP_COUNTS =",
                "test_every_ci_job_is_in_the_step_inventory",
                "test_every_ci_job_has_the_expected_total_and_named_step_count",
            ),
        },
    },
}

REQUIRED_STAGE_HEADINGS = (
    "## 1. Parse the authority; do not paste a second copy",
    "## 2. Compare both directions",
    "## 3. Lock numeric prose, including number words",
    "## 4. Prove the parser is not empty",
    "## Run a negative control in a disposable copy",
)


def _article() -> str:
    return ARTICLE.read_text(encoding="utf-8")


def _flat_article() -> str:
    without_quote_markers = re.sub(r"(?m)^>\s?", "", _article())
    return re.sub(r"\s+", " ", without_quote_markers)


def test_article_remains_a_user_publish_draft():
    text = _article()
    assert "> **Draft — user publishes, agents do not.**" in text
    assert "The user decides whether and where to publish it" in _flat_article()


def test_article_states_every_stage_of_the_method():
    text = _article()
    missing = [heading for heading in REQUIRED_STAGE_HEADINGS if heading not in text]
    assert missing == [], f"doc-lock method draft omits stages: {missing}"
    positions = [text.index(heading) for heading in REQUIRED_STAGE_HEADINGS]
    assert positions == sorted(positions), "doc-lock stages are out of order"


def test_article_has_exactly_five_machine_readable_receipts():
    markers = re.findall(r"<!-- doc-lock-example:([a-z0-9-]+) -->", _article())
    assert len(markers) == 5, "the prose says five implemented receipts"
    assert len(markers) == len(set(markers)), "a receipt marker is duplicated"
    assert set(markers) == set(EXAMPLES)


def test_every_receipt_names_real_sources_and_real_lock_code():
    article = _article()
    for example, contract in EXAMPLES.items():
        assert f"<!-- doc-lock-example:{example} -->" in article
        for relative in contract["paths"]:
            assert (ROOT / relative).is_file(), f"article cites missing file {relative}"
            assert f"`{relative}`" in article, f"receipt {example} does not name {relative}"
        for relative, needles in contract["proofs"].items():
            source = (ROOT / relative).read_text(encoding="utf-8")
            for needle in needles:
                assert needle in source, (
                    f"receipt {example} claims mechanism {needle!r}, but {relative} "
                    "no longer implements it"
                )


def test_article_discloses_the_known_limits_instead_of_selling_a_universal_checker():
    text = _flat_article()
    for limitation in (
        "manually declared in `LATENCY_SURFACES`",
        "not a repository-wide detector",
        "not imagine a wholly new one",
        "same-count replacement it cannot detect",
        "proves correspondence, not semantic truth",
    ):
        assert limitation in text
