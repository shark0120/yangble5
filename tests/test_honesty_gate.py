"""Tests for tools/honesty_gate.py — the two zero-false-positive disclosure rules."""

from __future__ import annotations

from pathlib import Path

from tools import honesty_gate as hg

ROOT = Path(__file__).resolve().parents[1]


def test_the_repo_itself_passes_the_gate():
    """The published repo must never trip its own honesty gate."""
    assert hg.scan_repo(ROOT) == []


def test_flags_fable5_called_a_hit_rate():
    v = hg.scan_text("Our fable5 layer reaches a 99% cache hit rate.")
    assert any(f["kind"] == "fable5_called_hit_rate" for f in v)


def test_flags_the_borrowed_38k_hook():
    for text in ("that $38,000 Bedrock bill", "a $38k surprise", "the 38,000 dollar bill"):
        v = hg.scan_text(text)
        assert any(f["kind"] == "borrowed_hook" for f in v), text


def test_does_not_flag_fable5_and_hitrate_in_separate_paragraphs():
    ok = "fable5 stabilises the prefix.\n\nSeparately, the cache hit rate is measured live."
    assert hg.scan_text(ok) == []


def test_prose_only_strips_code_diagrams_headings_and_tables():
    text = (
        "# 99.53% heading\n\n"
        "```\ncode with 99.53% inside\n```\n\n"
        "| 99.53% | table |\n\n"
        "<svg><text>99.53%</text></svg>\n\n"
        "Real prose here."
    )
    stripped = hg.prose_only(text)
    assert "99.53" not in stripped  # heading, code fence, table row, svg all removed
    assert "Real prose here." in stripped


def test_a_bare_headline_number_is_intentionally_not_a_violation():
    """The headline-qualifier rule is left to human review; see the module docstring.
    A bare number alone must not fail the gate, or it false-positives a clean repo."""
    assert hg.scan_text("The result was 99.53% on this run.") == []


def test_main_returns_zero_on_the_clean_repo(capsys):
    assert hg.main(["--root", str(ROOT)]) == 0
    assert "OK" in capsys.readouterr().out


def test_main_returns_one_when_a_violation_is_present(tmp_path):
    (tmp_path / "bad.md").write_text("fable5 gives you a great cache hit rate.", encoding="utf-8")
    assert hg.main(["--root", str(tmp_path)]) == 1
