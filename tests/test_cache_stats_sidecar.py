"""Tests for the stats arithmetic in tools/cache_stats_sidecar.py.

These are the numbers that end up on a dashboard and in a README claim, so the
tests assert the specific values, not just "it runs". Everything here is offline:
no proxy, no sockets except the explicitly guarded lock test.
"""

from __future__ import annotations

import json
import socket

import pytest

from tools import cache_bench
from tools import cache_stats_sidecar as sidecar


def record(
    *,
    inp: int = 0,
    cached: int = 0,
    out: int = 0,
    total: int | None = None,
    failed: bool = False,
    alias: str = "yangble5",
    source: str = "test",
    latency_ms: int = 0,
) -> dict:
    tokens = {"input_tokens": inp, "output_tokens": out, "cached_tokens": cached}
    if total is not None:
        tokens["total_tokens"] = total
    return {
        "alias": alias,
        "source": source,
        "failed": failed,
        "latency_ms": latency_ms,
        "tokens": tokens,
    }


def fresh(window: int = 100) -> dict:
    return sidecar.fresh_stats(window)


# ------------------------------------------------- prompt denominator guard ---


@pytest.mark.parametrize(
    ("inp", "cached", "expected", "why"),
    [
        (1000, 900, 1000, "Gemini convention: input already includes the cached read"),
        (1000, 1000, 1000, "boundary: equal counts must not double"),
        (100, 900, 1000, "Anthropic convention: input excludes the cached read"),
        (0, 900, 900, "fully cached round reported with input 0"),
        (900, 0, 900, "cold round"),
        (0, 0, 0, "empty record"),
        (-5, 900, 900, "negative input clamped, never widens the denominator"),
        (900, -5, 900, "negative cached clamped"),
    ],
)
def test_prompt_denominator_guard(inp, cached, expected, why):
    assert sidecar.prompt_denominator(inp, cached) == expected, why


def test_prompt_denominator_never_yields_a_rate_above_100_percent():
    """The whole point of the guard: cached/denominator must stay <= 1."""
    for inp in (0, 1, 7, 100, 999, 748_918):
        for cached in (0, 1, 7, 100, 999, 748_918):
            denominator = sidecar.prompt_denominator(inp, cached)
            if denominator:
                assert cached / denominator <= 1.0


def test_both_tools_implement_the_same_guard():
    """cache_bench.py and the sidecar each carry their own copy so each file can be
    handed over on its own. This test is what stops the two copies from drifting."""
    for inp in range(0, 200, 7):
        for cached in range(0, 200, 11):
            assert sidecar.prompt_denominator(inp, cached) == cache_bench.prompt_denominator(
                inp, cached
            )


# ------------------------------------------------------------ ingest math ---


def test_ingest_accumulates_tokens_and_computes_a_token_weighted_rate():
    stats = fresh()
    sidecar.ingest(stats, record(inp=1000, cached=990, out=10, total=1010))
    sidecar.ingest(stats, record(inp=2000, cached=1980, out=20, total=2020))
    sidecar.recompute_rates(stats)

    assert stats["requests"] == 2
    assert stats["failures"] == 0
    assert stats["tokens"]["prompt"] == 3000
    assert stats["tokens"]["cached"] == 2970
    assert stats["tokens"]["output"] == 30
    assert stats["tokens"]["total"] == 3030
    assert stats["hit_rate"] == pytest.approx(0.99)


def test_hit_rate_is_token_weighted_not_a_mean_of_ratios():
    """One huge cached request plus one tiny cold one.

    Mean of per-request ratios would be (1.0 + 0.0)/2 = 50%. The honest,
    token-weighted answer is 99.99%.
    """
    stats = fresh()
    sidecar.ingest(stats, record(inp=1_000_000, cached=1_000_000))
    sidecar.ingest(stats, record(inp=100, cached=0))
    sidecar.recompute_rates(stats)
    assert stats["hit_rate"] == pytest.approx(1_000_000 / 1_000_100, abs=1e-4)
    assert stats["hit_rate"] > 0.999


def test_failed_requests_are_counted_but_excluded_from_denominators():
    stats = fresh()
    sidecar.ingest(stats, record(inp=1000, cached=1000))
    sidecar.ingest(stats, record(inp=5_000_000, cached=0, failed=True))
    sidecar.recompute_rates(stats)

    assert stats["requests"] == 2
    assert stats["failures"] == 1
    # The failure contributed nothing to the token totals...
    assert stats["tokens"]["prompt"] == 1000
    assert stats["tokens"]["input"] == 1000
    # ...so it cannot drag a working cache down to 0.02%.
    assert stats["hit_rate"] == pytest.approx(1.0)
    assert stats["rolling"]["hit_rate"] == pytest.approx(1.0)


def test_failures_still_show_up_per_alias_so_the_error_rate_stays_visible():
    stats = fresh()
    sidecar.ingest(stats, record(alias="grok", inp=10, cached=0, failed=True, latency_ms=40))
    sidecar.ingest(stats, record(alias="grok", inp=100, cached=90, latency_ms=60))
    entry = stats["by_alias"]["grok"]
    assert entry["n"] == 2
    assert entry["fail"] == 1
    assert entry["inp"] == 100  # failed request's tokens excluded
    assert entry["cached"] == 90
    assert entry["latSum"] == 100  # latency counted for both


def test_total_tokens_falls_back_to_input_plus_output_when_absent():
    stats = fresh()
    sidecar.ingest(stats, record(inp=30, out=12))
    assert stats["tokens"]["total"] == 42


def test_by_source_counts_every_request_including_failures():
    stats = fresh()
    sidecar.ingest(stats, record(source="claude-code", inp=10, out=1, total=11))
    sidecar.ingest(stats, record(source="claude-code", inp=10, out=1, total=11, failed=True))
    sidecar.ingest(stats, record(source="codex", inp=5, out=5, total=10))
    assert stats["by_source"]["claude-code"]["n"] == 2
    assert stats["by_source"]["codex"] == {"n": 1, "tok": 10}


def test_alias_falls_back_to_model_then_to_question_mark():
    stats = fresh()
    sidecar.ingest(stats, {"model": "gemini-pro", "tokens": {}})
    sidecar.ingest(stats, {"tokens": {}})
    assert "gemini-pro" in stats["by_alias"]
    assert "?" in stats["by_alias"]


def test_ingest_applies_the_denominator_guard_per_record():
    """Mixed upstreams in one queue drain: one Gemini-style, one Anthropic-style."""
    stats = fresh()
    sidecar.ingest(stats, record(inp=1000, cached=900))  # inclusive -> prompt 1000
    sidecar.ingest(stats, record(inp=100, cached=900))  # exclusive -> prompt 1000
    sidecar.recompute_rates(stats)
    assert stats["tokens"]["prompt"] == 2000
    assert stats["hit_rate"] == pytest.approx(0.9)


# ---------------------------------------------------------- rolling window ---


def test_rolling_window_only_covers_the_last_n_successful_requests():
    stats = fresh(window=10)
    for _ in range(20):  # old, cold traffic
        sidecar.ingest(stats, record(inp=100, cached=0))
    for _ in range(10):  # recent, fully cached traffic
        sidecar.ingest(stats, record(inp=100, cached=100))
    sidecar.recompute_rates(stats, window=10)

    assert stats["rolling"]["window"] == 10
    assert stats["rolling"]["input"] == 1000
    assert stats["rolling"]["cached"] == 1000
    assert stats["rolling"]["hit_rate"] == pytest.approx(1.0)
    # Cumulative still remembers the cold history: 1000 cached out of 3000 prompt.
    assert stats["hit_rate"] == pytest.approx(1000 / 3000, abs=1e-4)


def test_rolling_window_skips_failures_when_selecting_the_last_n():
    stats = fresh(window=2)
    sidecar.ingest(stats, record(inp=100, cached=100))
    sidecar.ingest(stats, record(inp=100, cached=100))
    sidecar.ingest(stats, record(inp=999, cached=0, failed=True))
    sidecar.ingest(stats, record(inp=999, cached=0, failed=True))
    sidecar.recompute_rates(stats, window=2)
    # The two failures must not push the two good requests out of the window.
    assert stats["rolling"]["input"] == 200
    assert stats["rolling"]["hit_rate"] == pytest.approx(1.0)


def test_rolling_window_of_zero_reports_zero_rather_than_dividing_by_zero():
    stats = fresh(window=0)
    sidecar.ingest(stats, record(inp=100, cached=100))
    sidecar.recompute_rates(stats, window=0)
    assert stats["rolling"] == {"window": 0, "hit_rate": 0.0, "input": 0, "cached": 0}


def test_empty_stats_report_zero_not_nan():
    stats = fresh()
    sidecar.recompute_rates(stats)
    assert stats["hit_rate"] == 0.0
    assert stats["rolling"]["hit_rate"] == 0.0
    assert stats["updated_at"]


def test_recent_ring_log_is_capped_and_keeps_the_newest():
    stats = fresh()
    for i in range(50):
        sidecar.ingest(stats, record(inp=i, cached=0), recent_keep=10)
    assert len(stats["recent"]) == 10
    assert [r["inp"] for r in stats["recent"]] == list(range(40, 50))


def test_apply_records_ignores_non_dict_rows_and_reports_the_count():
    stats = fresh()
    ingested = sidecar.apply_records(stats, [record(inp=10, cached=5), "junk", None, 42])
    assert ingested == 1
    assert stats["requests"] == 1
    assert sidecar.apply_records(stats, []) == 0


# ---------------------------------------------------------- persistence ---


def test_save_stats_is_atomic_and_leaves_a_valid_readable_file(tmp_path):
    path = tmp_path / "nested" / "stats.json"
    stats = fresh()
    sidecar.ingest(stats, record(inp=1000, cached=990))
    sidecar.recompute_rates(stats)
    sidecar.save_stats(path, stats)

    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8")) == stats
    # No temp file survives a successful write.
    assert list(path.parent.glob("*.tmp")) == []


def test_save_stats_overwrites_in_place_without_a_truncation_window(tmp_path):
    path = tmp_path / "stats.json"
    first = fresh()
    sidecar.ingest(first, record(inp=1, cached=1))
    sidecar.save_stats(path, first)

    second = fresh()
    for _ in range(5):
        sidecar.ingest(second, record(inp=1000, cached=1000))
    sidecar.recompute_rates(second)
    sidecar.save_stats(path, second)

    reloaded = json.loads(path.read_text(encoding="utf-8"))
    assert reloaded["requests"] == 5
    assert list(path.parent.glob("*.tmp")) == []


def test_stats_survive_a_restart_round_trip(tmp_path):
    path = tmp_path / "stats.json"
    stats = fresh()
    sidecar.ingest(stats, record(inp=1000, cached=990))
    sidecar.recompute_rates(stats)
    sidecar.save_stats(path, stats)

    reloaded = sidecar.load_stats(path)
    assert reloaded["tokens"]["cached"] == 990
    sidecar.ingest(reloaded, record(inp=1000, cached=990))
    sidecar.recompute_rates(reloaded)
    assert reloaded["requests"] == 2
    assert reloaded["tokens"]["cached"] == 1980


@pytest.mark.parametrize(
    "content",
    ["", "not json at all", "[]", '{"schema": 1, "requests": 999}', '{"requests": 999}'],
)
def test_corrupt_or_stale_stats_files_start_over_instead_of_reporting_garbage(tmp_path, content):
    path = tmp_path / "stats.json"
    path.write_text(content, encoding="utf-8")
    stats = sidecar.load_stats(path)
    assert stats["schema"] == sidecar.SCHEMA
    assert stats["requests"] == 0


def test_load_stats_returns_fresh_document_when_file_is_missing(tmp_path):
    stats = sidecar.load_stats(tmp_path / "does-not-exist.json")
    assert stats["requests"] == 0
    assert stats["recent"] == []


# ---------------------------------------------------------------- locking ---


def test_lock_port_zero_disables_the_singleton_lock():
    assert sidecar.acquire_singleton_lock(0) is None
    assert sidecar.acquire_singleton_lock(-1) is None


def test_second_instance_on_the_same_lock_port_is_rejected():
    """Loopback only -- no external network. Skipped if the sandbox forbids binding."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    except OSError as exc:  # pragma: no cover - sandbox without loopback sockets
        probe.close()
        pytest.skip(f"cannot bind a loopback socket here: {exc}")
    probe.close()

    try:
        first = sidecar.acquire_singleton_lock(port)
    except (OSError, sidecar.AlreadyRunning) as exc:  # pragma: no cover
        pytest.skip(f"loopback port {port} unusable: {exc}")
    assert first is not None
    try:
        with pytest.raises(sidecar.AlreadyRunning):
            sidecar.acquire_singleton_lock(port)
    finally:
        first.close()


# ------------------------------------------------------------ key handling ---


def test_management_key_must_come_from_the_environment():
    with pytest.raises(SystemExit) as excinfo:
        sidecar.resolve_mgmt_key(env={})
    assert sidecar.MGMT_KEY_ENV in str(excinfo.value)
    env = {sidecar.MGMT_KEY_ENV: "  key-from-env  "}
    assert sidecar.resolve_mgmt_key(env=env) == "key-from-env"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x/y", "127.0.0.1:8318", ""])
def test_require_http_url_rejects_non_http_schemes(url):
    """This process polls unattended for days holding a management key; a mistyped
    base URL has to fail at startup rather than quietly poll the wrong thing."""
    with pytest.raises(SystemExit):
        sidecar.require_http_url(url)


def test_require_http_url_matches_the_cache_bench_copy():
    """Second deliberate duplication between the two standalone tools -- pinned so
    the two copies cannot drift apart."""
    for url in (
        "http://127.0.0.1:8318",
        "https://proxy.example.com",
        "HTTP://UPPER.example.com",
        "file:///etc/passwd",
        "127.0.0.1:8318",
        "",
    ):
        try:
            mine = sidecar.require_http_url(url)
        except SystemExit:
            mine = "rejected"
        try:
            theirs = cache_bench.require_http_url(url)
        except SystemExit:
            theirs = "rejected"
        assert mine == theirs, url


def test_no_management_key_flag_exists():
    """A management key on argv is readable by every other process on the box."""
    actions = {action.dest for action in sidecar.build_parser(env={})._actions}
    assert "mgmt_key" not in actions
    assert "key" not in actions


def test_parser_defaults_come_from_the_environment():
    parser = sidecar.build_parser(
        env={
            sidecar.BASE_URL_ENV: "http://10.0.0.5:9000/",
            sidecar.STATS_PATH_ENV: "/var/lib/yangble5/stats.json",
            sidecar.POLL_SECONDS_ENV: "2.5",
            sidecar.ROLLING_WINDOW_ENV: "250",
            sidecar.LOCK_PORT_ENV: "0",
        }
    )
    args = parser.parse_args([])
    assert args.base_url == "http://10.0.0.5:9000"
    assert args.stats_path == "/var/lib/yangble5/stats.json"
    assert args.poll_seconds == 2.5
    assert args.window == 250
    assert args.lock_port == 0


def test_malformed_numeric_environment_values_fall_back_to_defaults():
    parser = sidecar.build_parser(env={sidecar.POLL_SECONDS_ENV: "soon", sidecar.LOCK_PORT_ENV: ""})
    args = parser.parse_args([])
    assert args.poll_seconds == sidecar.DEFAULT_POLL_SECONDS
    assert args.lock_port == sidecar.DEFAULT_LOCK_PORT
