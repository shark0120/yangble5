"""Offline tests for tools/cache_guard.py — prompt cache-hostility linter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import cache_guard

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "cache_guard"


# --- prompt_text extraction -------------------------------------------------


def test_prompt_text_from_bare_string():
    assert cache_guard.prompt_text("hello") == "hello"


def test_prompt_text_from_prompt_field():
    assert cache_guard.prompt_text({"prompt": "sys text"}) == "sys text"


def test_prompt_text_folds_system_and_history_but_not_the_last_turn():
    payload = {
        "system": "SYS",
        "messages": [
            {"role": "user", "content": "old turn"},
            {"role": "assistant", "content": "old reply"},
            {"role": "user", "content": "FRESH TURN"},
        ],
    }
    text = cache_guard.prompt_text(payload)
    assert "SYS" in text and "old turn" in text and "old reply" in text
    assert "FRESH TURN" not in text  # the varying tail is excluded from the prefix


def test_prompt_text_reads_text_blocks():
    payload = {"system": [{"type": "text", "text": "block one"}, {"type": "image"}]}
    assert cache_guard.prompt_text(payload) == "block one"


def test_prompt_text_folds_openai_chat_completions_system_message():
    """OpenAI chat.completions carries the system prompt as a role:system entry
    inside messages, not a top-level field; the same fold picks it up."""
    payload = {
        "model": "gpt-5",
        "messages": [
            {"role": "system", "content": "STABLE SYSTEM"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "FRESH QUESTION"},
        ],
    }
    text = cache_guard.prompt_text(payload)
    assert "STABLE SYSTEM" in text and "old question" in text and "old answer" in text
    assert "FRESH QUESTION" not in text  # the varying tail is excluded


def test_prompt_text_reads_the_openai_responses_shape():
    """Responses API: `instructions` is the system prompt, `input` the turns."""
    payload = {
        "model": "gpt-5",
        "instructions": "STABLE INSTRUCTIONS",
        "input": [
            {"role": "user", "content": "old turn"},
            {"role": "assistant", "content": "old reply"},
            {"role": "user", "content": "FRESH TURN"},
        ],
    }
    text = cache_guard.prompt_text(payload)
    assert "STABLE INSTRUCTIONS" in text and "old turn" in text and "old reply" in text
    assert "FRESH TURN" not in text


def test_prompt_text_bare_string_responses_input_is_the_fresh_turn():
    """A bare-string `input` is the single fresh turn, so only `instructions` is
    part of the stable prefix -- mirroring a lone final message."""
    payload = {"instructions": "STABLE", "input": "the user's one-off question"}
    assert cache_guard.prompt_text(payload) == "STABLE"


def test_scan_flags_a_timestamp_in_openai_responses_instructions():
    """End to end: the guard runs on a Responses payload, not just Anthropic."""
    report = cache_guard.scan_payloads(
        [{"instructions": "Reviewer. Generated 2026-07-25T03:14:07Z.", "input": "go"}]
    )
    assert not report.clean
    assert report.high >= 1


# --- pure helpers -----------------------------------------------------------


def test_common_prefix_len_basic():
    assert cache_guard.common_prefix_len(["abcXYZ", "abcQRS"]) == 3
    assert cache_guard.common_prefix_len(["same", "same"]) == 4
    assert cache_guard.common_prefix_len(["a", "b"]) == 0
    assert cache_guard.common_prefix_len(["only"]) == 4


def test_find_volatility_flags_high_severity_patterns():
    names = {
        f["rule"]
        for f in cache_guard.find_volatility(
            "id 3f2504e0-4f89-41d3-9a0c-0305e82c3301 at 2026-07-25T03:14:07Z epoch 1784000000"
        )
    }
    assert "uuid" in names
    assert "iso8601_datetime" in names
    assert "unix_epoch_s" in names


def test_find_volatility_deoverlaps_datetime_over_bare_date():
    findings = cache_guard.find_volatility("run at 2026-07-25T03:14:07Z now")
    # the full ISO datetime wins; the bare-date rule must not also fire on it
    assert [f["rule"] for f in findings if f["rule"] == "bare_date"] == []
    assert any(f["rule"] == "iso8601_datetime" for f in findings)


def test_est_tokens_is_a_labelled_estimate():
    assert cache_guard.est_tokens(400) == 100  # ~4 chars/token


# --- scan -------------------------------------------------------------------


def test_scan_clean_prompt_passes():
    report = cache_guard.scan_payloads([{"system": "stable system prompt, no volatiles"}])
    assert report.clean
    assert report.high == 0


def test_scan_flags_a_timestamp_in_the_system_prompt():
    report = cache_guard.scan_payloads(
        [{"system": "Reviewer. Generated at 2026-07-25T03:14:07Z. Follow the guide."}]
    )
    assert not report.clean
    assert report.high >= 1


def test_scan_default_ignores_medium_but_strict_counts_it():
    payload = [{"system": "Today's date is relevant. Consider the current date carefully."}]
    assert cache_guard.scan_payloads(payload, strict=False).clean  # medium-only -> clean by default
    assert not cache_guard.scan_payloads(payload, strict=True).clean


# --- diff -------------------------------------------------------------------


def test_diff_detects_a_prefix_stability_regression_and_estimates_cost():
    before = [{"system": "A" * 400}, {"system": "A" * 400}]
    after = [{"system": "A" * 100 + "1784000001"}, {"system": "A" * 100 + "1784000002"}]
    report = cache_guard.diff_payloads(before, after)
    assert report.regressed
    assert report.before_ratio == 1.0
    assert report.after_ratio < 1.0
    assert report.churned_tokens_est > 0
    assert report.cost_delta_per_1k(3.0) > 0


def test_diff_no_regression_when_prefix_stays_stable():
    same = [{"system": "B" * 200}, {"system": "B" * 200}]
    report = cache_guard.diff_payloads(same, same)
    assert not report.regressed


def test_diff_does_not_flag_a_longer_variable_tail_as_a_regression():
    """The cacheable prefix is byte-identical; only the varying tail grew. The
    length-normalised ratio drops (bigger denominator) but nothing that touches the
    cache changed. Keying regression off the ratio here fails CI with a
    contradictory 'shrank by ~0 tokens, $0.00' -- so it must key off the prefix."""
    before = ["P" * 300, "P" * 300]
    after = ["P" * 300 + "x" * 100, "P" * 300 + "y" * 100]
    report = cache_guard.diff_payloads(before, after)
    assert report.after_ratio < report.before_ratio  # the ratio genuinely dropped
    assert not report.regressed  # ...but the stable prefix did not shrink
    assert report.churned_tokens_est == 0
    assert report.cost_delta_per_1k(3.0) == 0.0


def test_prompt_text_rejects_a_payload_that_is_neither_string_nor_object():
    for bad in (42, ["a", "list"], None, 3.14):
        try:
            cache_guard.prompt_text(bad)
        except ValueError as exc:
            assert "string or an object" in str(exc)
        else:  # pragma: no cover - the assertion below reports the miss
            raise AssertionError(f"prompt_text({bad!r}) should have raised ValueError")


# --- CLI + the shipped example fixtures stay truthful ------------------------


def test_cli_scan_good_example_exits_zero():
    assert cache_guard.main(["scan", str(EXAMPLES / "good_prompt.jsonl")]) == 0


def test_cli_scan_bad_example_exits_one():
    assert cache_guard.main(["scan", str(EXAMPLES / "bad_prompt.jsonl")]) == 1


def test_cli_diff_good_to_bad_reports_regression(capsys):
    code = cache_guard.main(
        [
            "diff",
            "--before",
            str(EXAMPLES / "good_prompt.jsonl"),
            "--after",
            str(EXAMPLES / "bad_prompt.jsonl"),
            "--json",
        ]
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["regressed"] is True
    assert payload["after_ratio"] < payload["before_ratio"]


def test_example_fixtures_have_no_credentials():
    for name in ("good_prompt.jsonl", "bad_prompt.jsonl", "openai_responses.jsonl"):
        text = (EXAMPLES / name).read_text(encoding="utf-8")
        assert "@" not in text
        assert "sk-" not in text


def test_example_ci_workflow_is_valid_and_runs_the_guard():
    """The copy-paste GitHub Actions template must be valid YAML that actually
    invokes the guard -- on pull requests, with least privilege and pinned
    actions -- so a user who drops it in gets a working gate, not a broken one."""
    yaml = pytest.importorskip("yaml")
    spec = yaml.safe_load((EXAMPLES / "cache-guard.yml").read_text(encoding="utf-8"))
    # PyYAML parses a bare `on:` key as the boolean True (YAML 1.1); accept either.
    triggers = spec.get("on")
    if triggers is None:
        triggers = spec.get(True)
    assert triggers is not None and "pull_request" in triggers, "must run on pull requests"
    assert spec["permissions"] == {"contents": "read"}, "least privilege"
    steps = spec["jobs"]["cache-guard"]["steps"]
    uses = [s.get("uses", "") for s in steps]
    assert any(u.startswith("actions/checkout@v") for u in uses), "pin actions/checkout"
    assert any(u.startswith("actions/setup-python@v") for u in uses), "pin actions/setup-python"
    run_cmds = " ".join(s.get("run", "") for s in steps)
    assert "cache_guard.py scan" in run_cmds, "the gate must actually run the guard"
