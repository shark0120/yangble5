"""Offline tests for tools/cache_guard.py — prompt cache-hostility linter."""

from __future__ import annotations

import json
from pathlib import Path

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
    for name in ("good_prompt.jsonl", "bad_prompt.jsonl"):
        text = (EXAMPLES / name).read_text(encoding="utf-8")
        assert "@" not in text
        assert "sk-" not in text
