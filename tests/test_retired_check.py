"""Tests for the history-derived must-404 check."""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))

import retired_check as rc

ROOT = pathlib.Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"


def test_real_history_derives_a_nonempty_retired_roster_without_source_literals():
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],  # noqa: S607 - fixed Git argv
        cwd=ROOT,
        capture_output=True,
        timeout=60,
    )
    if head.returncode != 0:
        pytest.skip("archive clone simulation has no commit history")
    retired = rc.retired_directories(ROOT)
    assert retired, "the real complete history produced no retired directories"
    source = (ROOT / "tools" / "retired_check.py").read_text(encoding="utf-8")
    for path in retired:
        assert path not in source, f"retired path {path!r} was copied into current source"


def test_ci_jobs_that_execute_or_test_history_discovery_fetch_full_history():
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    for job_name in ("test", "live-site-drift"):
        checkouts = [
            step
            for step in jobs[job_name]["steps"]
            if step.get("uses") == "actions/checkout@v4"
        ]
        assert len(checkouts) == 1, f"{job_name} must have exactly one checkout step"
        assert checkouts[0].get("with", {}).get("fetch-depth") == 0, (
            f"{job_name} must fetch complete history for retired-path discovery"
        )


def test_shortest_absent_parent_is_derived_from_deleted_paths(monkeypatch):
    def fake_git(_root, *args):
        if args[:2] == ("rev-parse", "--is-shallow-repository"):
            return "false\n"
        if args and args[0] == "log":
            return "site/retired/deep/file.txt\nsite/kept/old.txt\n"
        if args[:1] == ("ls-files",):
            return "site/kept/current.txt\n"
        raise AssertionError(args)

    monkeypatch.setattr(rc, "_git", fake_git)
    assert rc.retired_directories(ROOT) == ("retired",)


def test_shallow_history_is_refused_instead_of_certifying_an_empty_roster(monkeypatch):
    monkeypatch.setattr(rc, "_git", lambda _root, *_args: "true\n")
    with pytest.raises(rc.HistoryUnavailable, match="fetch-depth: 0"):
        rc.retired_directories(ROOT)


def test_both_slash_forms_and_both_cache_bypass_forms_are_probed(monkeypatch):
    calls = []

    def all_gone(url, _timeout):
        calls.append(url)
        return 404, ""

    monkeypatch.setattr(rc, "fetch_status", all_gone)
    results = rc.check_directory("https://example.test", "former/location", 1, "nonce")

    assert len(results) == 2, (
        f"expected no-slash and trailing-slash results, found {len(results)}"
    )
    assert [result[1] for result in results] == ["GONE", "GONE"]
    assert calls == [
        "https://example.test/former/location",
        "https://example.test/former/location?retired-check-cache-bypass=nonce",
        "https://example.test/former/location/",
        "https://example.test/former/location/?retired-check-cache-bypass=nonce",
    ]


def test_a_cache_bypassed_success_means_the_path_is_present(monkeypatch):
    def cached_404(url, _timeout):
        return (200, "") if "cache-bypass" in url else (404, "")

    monkeypatch.setattr(rc, "fetch_status", cached_404)
    results = rc.check_directory("https://example.test", "former", 1, "nonce")
    assert [result[1] for result in results] == ["PRESENT", "PRESENT"]
    assert all("plain HTTP 404; cache-bypass HTTP 200" in result[2] for result in results)


def test_a_403_is_inconclusive_and_is_not_reprobed(monkeypatch):
    calls = []

    def forbidden(url, _timeout):
        calls.append(url)
        return 403, ""

    monkeypatch.setattr(rc, "fetch_status", forbidden)
    results = rc.check_directory("https://example.test", "former", 1, "nonce")
    assert [result[1] for result in results] == ["INCONCLUSIVE", "INCONCLUSIVE"]
    assert len(calls) == 2
    assert all("cache-bypass" not in url for url in calls)
