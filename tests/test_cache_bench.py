"""Tests for the pure measurement logic in tools/cache_bench.py.

Nothing here touches the network. The HTTP call is deliberately the only thing
the module keeps out of the pure layer, so every number the benchmark prints --
the denominator guard, the warm-round selection, the token weighting, the
pass/fail decision -- is verifiable offline.
"""

from __future__ import annotations

import json

import pytest

from tools import cache_bench


def rnd(number: int, prompt: int, cached: int) -> dict:
    """A round record as usage_to_round would produce it."""
    return {"round": number, "prompt_total": prompt, "cache_read": cached}


# ------------------------------------------------------------ prefix builder ---


def test_build_prefix_is_deterministic():
    """A prefix that varies between rounds invalidates the cache under test and
    would make a broken cache look like a working one that simply missed."""
    assert cache_bench.build_prefix(30_000) == cache_bench.build_prefix(30_000)


def test_build_prefix_line_count_tracks_the_token_budget():
    for target in (3_000, 30_000, 300_000):
        expected_lines = target // cache_bench.TOKENS_PER_LINE
        text = cache_bench.build_prefix(target)
        # +1 for the header line.
        assert len(text.splitlines()) == expected_lines + 1
    assert cache_bench.build_prefix(0).splitlines()[0] == cache_bench.PREFIX_HEADER


def test_build_prefix_always_emits_at_least_one_fact_line():
    """A zero-line prefix would silently benchmark an empty cache key."""
    for target in (0, 1, 29):
        assert len(cache_bench.build_prefix(target).splitlines()) == 2


def test_build_prefix_grows_monotonically_with_the_budget():
    sizes = [len(cache_bench.build_prefix(t)) for t in (3_000, 30_000, 300_000)]
    assert sizes == sorted(sizes)
    assert sizes[0] < sizes[1] < sizes[2]


def test_build_prefix_lines_are_distinct():
    """Repeated lines compress; the prefix has to keep tokens proportional to size."""
    lines = cache_bench.build_prefix(30_000).splitlines()[1:]
    assert len(set(lines)) == len(lines)


def test_build_prefix_is_ascii_and_carries_the_do_not_summarize_marker():
    text = cache_bench.build_prefix(3_000)
    text.encode("ascii")  # raises if a non-ASCII byte crept in
    assert "do not summarize" in text.splitlines()[0]


def test_build_prefix_rejects_a_nonsensical_token_estimate():
    with pytest.raises(ValueError):
        cache_bench.build_prefix(3_000, tokens_per_line=0)


# ------------------------------------------------- prompt denominator guard ---


@pytest.mark.parametrize(
    ("inp", "cread", "expected", "why"),
    [
        (748_918, 745_400, 748_918, "Gemini via this proxy: input_tokens INCLUDES cached reads"),
        (12, 748_906, 748_918, "pure Anthropic wire: input_tokens EXCLUDES cached reads"),
        (500, 500, 500, "boundary: equal counts must not be added twice"),
        (500, 0, 500, "cold round"),
        (0, 0, 0, "no usage reported"),
    ],
)
def test_prompt_denominator_guard(inp, cread, expected, why):
    assert cache_bench.prompt_denominator(inp, cread) == expected, why


def test_prompt_denominator_cannot_produce_a_rate_above_100_percent():
    for inp in (0, 1, 500, 748_918):
        for cread in (0, 1, 500, 748_918):
            denominator = cache_bench.prompt_denominator(inp, cread)
            if denominator:
                assert cread / denominator <= 1.0


# --------------------------------------------------------- usage_to_round ---


def test_usage_to_round_normalises_a_gemini_style_usage_block():
    usage = {
        "input_tokens": 748_918,
        "cache_read_input_tokens": 745_400,
        "cache_creation_input_tokens": 0,
        "output_tokens": 12,
    }
    row = cache_bench.usage_to_round(3, usage, latency_ms=10_800, reply="OK-3")
    assert row["round"] == 3
    assert row["prompt_total"] == 748_918
    assert row["cache_read"] == 745_400
    assert row["output"] == 12
    assert row["latency_ms"] == 10_800
    assert row["ratio"] == pytest.approx(745_400 / 748_918, abs=1e-4)
    assert row["usage_keys"] == sorted(usage)


def test_usage_to_round_survives_an_upstream_that_reports_no_usage_at_all():
    row = cache_bench.usage_to_round(1, {}, latency_ms=5)
    assert row["prompt_total"] == 0
    assert row["cache_read"] == 0
    assert row["ratio"] == 0.0
    assert row["usage_keys"] == []


def test_usage_to_round_treats_explicit_nulls_as_zero():
    row = cache_bench.usage_to_round(1, {"input_tokens": None, "cache_read_input_tokens": None}, 1)
    assert row["prompt_total"] == 0


def test_usage_to_round_truncates_the_reply_so_a_runaway_response_cannot_flood_output():
    row = cache_bench.usage_to_round(1, {}, 1, reply="x" * 500)
    assert len(row["reply"]) == 40


# --------------------------------------------------------------- rate math ---


def test_token_weighted_hit_rate_weights_by_prompt_size():
    """Mean of ratios would say 50%; the honest answer is dominated by the big round."""
    rounds = [rnd(1, 1_000_000, 1_000_000), rnd(2, 100, 0)]
    assert cache_bench.token_weighted_hit_rate(rounds) == pytest.approx(1_000_000 / 1_000_100)


def test_token_weighted_hit_rate_of_no_rounds_is_zero_not_an_error():
    assert cache_bench.token_weighted_hit_rate([]) == 0.0
    assert cache_bench.token_weighted_hit_rate([rnd(1, 0, 0)]) == 0.0


def test_totals_sums_both_columns():
    assert cache_bench.totals([rnd(1, 100, 10), rnd(2, 200, 20)]) == (30, 300)


# --------------------------------------------------------------- summarize ---


def test_summarize_excludes_the_cold_round_from_the_headline_number():
    """Round 1 is a cache write and can never be a read. Including it would make the
    headline a function of --rounds rather than of the cache."""
    rounds = [
        rnd(1, 1000, 0),  # cold
        rnd(2, 1000, 1000),
        rnd(3, 1000, 1000),
    ]
    result = cache_bench.summarize(rounds, target=0.99)
    assert result["cold_round"]["round"] == 1
    assert result["warm_round_numbers"] == [2, 3]
    assert result["prompt_tokens"] == 2000
    assert result["cached_tokens"] == 2000
    assert result["eligible_hit_rate"] == 1.0
    assert result["pass"] is True


def test_summarize_reproduces_the_shape_of_the_recorded_measurement():
    """Mirrors the published claim: cold round 0%, warm rounds ~99.5%, warm-only."""
    rounds = [
        rnd(1, 748_918, 0),
        rnd(2, 748_930, 745_400),
        rnd(3, 748_942, 745_400),
        rnd(4, 748_954, 745_400),
    ]
    result = cache_bench.summarize(rounds, target=0.99)
    assert result["cold_round"]["cache_read"] == 0
    assert len(result["warm_rounds"]) == 3
    assert result["eligible_hit_rate"] == pytest.approx(
        (745_400 * 3) / (748_930 + 748_942 + 748_954), abs=1e-4
    )
    # Folding the cold round in would drop the same data to roughly 75%.
    assert cache_bench.token_weighted_hit_rate(rounds) < 0.76


@pytest.mark.parametrize(
    ("cached", "expected_pass"),
    [(9900, True), (9901, True), (9899, False), (0, False)],
)
def test_pass_fail_boundary_is_inclusive_at_the_target(cached, expected_pass):
    rounds = [rnd(1, 10_000, 0), rnd(2, 10_000, cached)]
    assert cache_bench.summarize(rounds, target=0.99)["pass"] is expected_pass


def test_a_single_round_run_cannot_pass_and_says_why():
    """One round means one cold write and nothing to score."""
    result = cache_bench.summarize([rnd(1, 1000, 0)], target=0.99)
    assert result["warm_rounds"] == []
    assert result["eligible_hit_rate"] == 0.0
    assert result["pass"] is False
    assert any("warm" in note for note in result["notes"])


def test_summarize_of_nothing_is_a_failure_not_a_crash():
    result = cache_bench.summarize([], target=0.99)
    assert result["cold_round"] is None
    assert result["pass"] is False


def test_all_zero_cached_rounds_raise_the_diagnostic_note():
    """The failure mode where the upstream exposes no cache accounting at all --
    it must be reported as unmeasurable, not silently as 0%."""
    rounds = [rnd(1, 1000, 0), rnd(2, 1000, 0), rnd(3, 1000, 0)]
    result = cache_bench.summarize(rounds, target=0.99)
    assert result["pass"] is False
    assert any("ZERO cached tokens" in note for note in result["notes"])


def test_the_zero_cache_note_is_absent_once_any_round_reports_a_cache_read():
    rounds = [rnd(1, 1000, 0), rnd(2, 1000, 1)]
    notes = cache_bench.summarize(rounds, target=0.99)["notes"]
    assert not any("ZERO cached tokens" in note for note in notes)


def test_summary_is_json_serialisable_for_the_json_flag():
    rounds = [rnd(1, 1000, 0), rnd(2, 1000, 990)]
    payload = json.loads(json.dumps(cache_bench.summarize(rounds)))
    assert payload["target"] == cache_bench.DEFAULT_TARGET
    assert payload["eligible_hit_rate"] == 0.99


# ------------------------------------------------------- response decoding ---


def test_extract_text_concatenates_text_blocks_and_ignores_the_rest():
    response = {
        "content": [
            {"type": "text", "text": "OK-"},
            {"type": "tool_use", "name": "bash", "input": {}},
            {"type": "text", "text": "2"},
        ]
    }
    assert cache_bench.extract_text(response) == "OK-2"


@pytest.mark.parametrize("response", [{}, {"content": None}, {"content": []}, {"content": ["x"]}])
def test_extract_text_handles_missing_or_odd_content(response):
    assert cache_bench.extract_text(response) == ""


# -------------------------------------------------------- config resolution ---


def test_api_key_must_come_from_the_environment():
    with pytest.raises(SystemExit) as excinfo:
        cache_bench.resolve_api_key(env={})
    message = str(excinfo.value)
    assert cache_bench.API_KEY_ENV in message
    # The error has to be actionable on both platforms the tool supports.
    assert "export" in message
    assert "$env:" in message


def test_api_key_is_read_and_stripped():
    env = {cache_bench.API_KEY_ENV: "  test-key-not-a-real-secret \n"}
    assert cache_bench.resolve_api_key(env=env) == "test-key-not-a-real-secret"


def test_whitespace_only_api_key_is_treated_as_missing():
    with pytest.raises(SystemExit):
        cache_bench.resolve_api_key(env={cache_bench.API_KEY_ENV: "   "})


def test_base_url_precedence_is_flag_then_env_then_loopback_default():
    env = {cache_bench.BASE_URL_ENV: "http://10.0.0.5:9000"}
    assert cache_bench.resolve_base_url("http://flag:1/", env) == "http://flag:1"
    assert cache_bench.resolve_base_url(None, env) == "http://10.0.0.5:9000"
    assert cache_bench.resolve_base_url(None, {}) == cache_bench.DEFAULT_BASE_URL


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8318",
        "https://proxy.example.com",
        # Schemes are case-insensitive per RFC 3986 and urlopen honours that, so
        # the guard must not reject a URL the tool could actually have used.
        "HTTP://127.0.0.1:8318",
        "HttpS://proxy.example.com",
    ],
)
def test_require_http_url_accepts_http_and_https_in_any_case(url):
    assert cache_bench.require_http_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",  # urllib would read and print a local file
        "ftp://example.com/x",
        "127.0.0.1:8318",  # bare host:port -- urlopen cannot use it
        "",
        "gopher://example.com",
    ],
)
def test_require_http_url_rejects_schemes_urlopen_should_never_follow(url):
    """A mistyped base URL must stop the run, not silently become a file read."""
    with pytest.raises(SystemExit) as excinfo:
        cache_bench.require_http_url(url)
    assert "http://" in str(excinfo.value)


def test_resolve_base_url_applies_the_scheme_guard_to_environment_values():
    env = {cache_bench.BASE_URL_ENV: "file:///etc/passwd"}
    with pytest.raises(SystemExit):
        cache_bench.resolve_base_url(None, env)


def test_no_api_key_flag_exists():
    """argv is visible to other processes and lands in shell history."""
    dests = {action.dest for action in cache_bench.build_parser()._actions}
    assert "api_key" not in dests
    assert {"model", "prefix_tokens", "rounds", "interval", "timeout", "json"} <= dests


def test_parser_defaults_match_the_documented_behaviour():
    args = cache_bench.build_parser().parse_args([])
    assert args.target == 0.99
    assert args.rounds == 5
    assert args.interval == 2.0
    assert args.json is False
    assert args.base_url is None  # so the env var can win


def test_main_rejects_a_zero_round_run_before_making_any_request(monkeypatch):
    """Argument validation must happen before the key lookup and before any I/O."""
    monkeypatch.delenv(cache_bench.API_KEY_ENV, raising=False)
    with pytest.raises(SystemExit) as excinfo:
        cache_bench.main(["--rounds", "0"])
    assert "--rounds" in str(excinfo.value)
