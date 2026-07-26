"""Tests for tools/honesty_gate.py — the two zero-false-positive disclosure rules."""

from __future__ import annotations

from pathlib import Path

from tools import honesty_gate as hg

ROOT = Path(__file__).resolve().parents[1]


def test_the_repo_itself_passes_the_gate():
    """The published repo must never trip its own honesty gate."""
    assert hg.scan_repo(ROOT) == []


def test_flags_the_skill_called_a_hit_rate():
    v = hg.scan_text("Our yb5 layer reaches a 99% cache hit rate.")
    assert any(f["kind"] == "skill_called_hit_rate" for f in v)


def test_flags_the_borrowed_38k_hook():
    for text in ("that $38,000 Bedrock bill", "a $38k surprise", "the 38,000 dollar bill"):
        v = hg.scan_text(text)
        assert any(f["kind"] == "borrowed_hook" for f in v), text


def test_does_not_flag_the_skill_and_hitrate_in_separate_paragraphs():
    ok = "yb5 stabilises the prefix.\n\nSeparately, the cache hit rate is measured live."
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
    (tmp_path / "bad.md").write_text("yb5 gives you a great cache hit rate.", encoding="utf-8")
    assert hg.main(["--root", str(tmp_path)]) == 1


def test_flags_the_skill_in_the_forms_prose_actually_uses():
    """Both spellings the docs use have to trip it: the code name and the prose name."""
    for form in ("yb5", "YB5", "the yangble5 skill", "the yangble5-skill"):
        v = hg.scan_text(f"{form} reaches a 99% cache hit rate.")
        assert any(f["kind"] == "skill_called_hit_rate" for f in v), form


def test_does_not_flag_the_PROJECT_measuring_a_real_cache_hit_rate():
    """This is the half that keeps the gate usable, and it is not hypothetical.

    Naming the skill after the project put the two names one word apart, and only
    one of them is under the rule. `yangble5` is the proxy stack; its cache hit
    rate is measured on real traffic, is the project's central published claim,
    and is written down in this exact shape all over the repo. If the pattern
    were relaxed to `yangble5` the build would fail on the truth -- so these
    sentences passing is a property, not an accident.
    """
    for text in (
        "yangble5 reaches a 99% cache hit rate on this run.",
        "The gateway's measured cache hit rate was 99.53% over 1,000 requests.",
        "yangble5 命中率 99.53%(單次量測)。",
    ):
        assert hg.scan_text(text) == [], text


def test_does_not_flag_tokens_that_merely_contain_the_short_name():
    """`` on both ends: a version string or an id is not the skill."""
    for text in ("yb50 reaches a 99% cache hit rate.", "run xyb5x at a 99% cache hit rate"):
        assert hg.scan_text(text) == [], text


def test_does_not_flag_38k_as_a_plain_token_count():
    """"38k tokens" is an ordinary count, not the borrowed $38,000 bill hook."""
    for text in ("a 38k token prompt", "we processed 38,000 requests", "the 38k-line file"):
        assert hg.scan_text(text) == [], text


def test_flags_the_uncurrencied_38k_only_with_a_billing_word():
    """Without a currency sign the figure needs a billing word to be the hook."""
    assert any(f["kind"] == "borrowed_hook" for f in hg.scan_text("a 38k monthly bill"))
    assert any(f["kind"] == "borrowed_hook" for f in hg.scan_text("38,000 in Bedrock charges"))


def test_main_returns_two_on_a_non_directory_root(tmp_path, capsys):
    """A --root that is not a directory is a bad invocation (exit 2)."""
    missing = tmp_path / "does-not-exist"
    assert hg.main(["--root", str(missing)]) == 2
    assert "not a directory" in capsys.readouterr().err
