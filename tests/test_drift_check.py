"""Tests for tools/drift_check.py — is the served site the repository's site?

This tool had no tests until 2026-07-26, which is a strange gap for the one
thing in the project that looks at production. It went untested because it needs
the network, and the answer to that is the same as everywhere else here: test
the DECISION, not the socket. `fetch` is the only part that touches the network,
so every test below replaces it and exercises the logic that reads its result.

The bulk of the file is about one distinction the tool used to get wrong. A 404
was reported as "either the deploy did not happen, or the edge is transforming
the response" -- and on 2026-07-26 that sentence was printed about a file the
origin was serving perfectly, whose bytes matched the repository exactly, and
which was unreachable only because the CDN had cached a 404 from a probe made
minutes before publication. Acting on that message means redeploying a correct
site and watching it stay broken. The remedy for a negative cache entry is to
wait for it or purge it, and nothing about a redeploy does either.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))

import drift_check as dc

ROOT = pathlib.Path(__file__).resolve().parents[1]


class FakeEdge:
    """A stand-in for `fetch` that answers per-URL and records what was asked.

    Recording the calls is what makes the "did not re-probe" assertions
    possible: a tool that quietly fires a second request on every error would
    still pass a test that only looked at return values.
    """

    def __init__(self, responses: dict[str, tuple[bytes | None, str]]):
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: float) -> tuple[bytes | None, str]:
        self.calls.append(url)
        for suffix, result in self.responses.items():
            if suffix in url:
                return result
        return None, "HTTP 404"


# ── the published set is real ──────────────────────────────────────────────


def test_every_published_path_exists_in_site():
    """A name in PUBLISHED that is not on disk means the list drifted from the
    tree, and the tool would report it against production as a live problem."""
    missing = [name for name in dc.PUBLISHED if not (dc.SITE / name).exists()]
    assert missing == [], missing


def test_the_site_directory_is_this_repository():
    assert dc.SITE == ROOT / "site"


# ── compare(): the byte comparison, with the edge's allowed rewrites ────────


def test_identical_bytes_are_not_a_difference(tmp_path):
    local = tmp_path / "page.html"
    local.write_bytes(b"<p>hello</p>")
    assert dc.compare("page.html", b"<p>hello</p>", local) is None


def test_an_edge_stripped_marker_is_not_a_difference(tmp_path):
    """Cloudflare consumes the email_off comments. Their removal is expected;
    their absence from the REPO copy would be the bug."""
    local = tmp_path / "page.html"
    local.write_bytes(b"<!--email_off-->you@example.com<!--/email_off-->")
    assert dc.compare("page.html", b"you@example.com", local) is None


def test_an_untouched_copy_is_not_a_difference(tmp_path):
    """The edge is allowed to leave the markers alone, so the raw copy passes too."""
    raw = b"<!--email_off-->you@example.com<!--/email_off-->"
    local = tmp_path / "page.html"
    local.write_bytes(raw)
    assert dc.compare("page.html", raw, local) is None


def test_a_changed_byte_is_reported_with_its_offset(tmp_path):
    local = tmp_path / "page.html"
    local.write_bytes(b"install --email you@example.com")
    result = dc.compare("page.html", b"install --email evil@attacker.com", local)
    assert result is not None
    assert "page.html" in result
    assert "first at byte 16" in result, result
    assert "repo :" in result and "live :" in result


def test_a_truncated_response_is_reported(tmp_path):
    """A shorter served copy has no differing byte to point at; the offset must
    still land somewhere real rather than raising."""
    local = tmp_path / "page.html"
    local.write_bytes(b"abcdefgh")
    result = dc.compare("page.html", b"abcd", local)
    assert result is not None
    assert "4 bytes served, 8 expected" in result


# ── probe(): telling a missing deploy apart from a cached 404 ───────────────


def test_a_normal_response_is_returned_without_a_second_request(monkeypatch):
    edge = FakeEdge({"/llms.txt": (b"body", "")})
    monkeypatch.setattr(dc, "fetch", edge)
    got, error, bypassed = dc.probe("https://x", "llms.txt", 1.0, "N")
    assert (got, error, bypassed) == (b"body", "", False)
    assert len(edge.calls) == 1, edge.calls


def test_a_genuine_404_stays_a_404(monkeypatch):
    """Both URLs 404 -> the origin really does not have it. Redeploying IS the fix."""
    edge = FakeEdge({})  # everything 404s, buster included
    monkeypatch.setattr(dc, "fetch", edge)
    got, error, bypassed = dc.probe("https://x", "skill/index.html", 1.0, "N")
    assert got is None
    assert error == "HTTP 404"
    assert bypassed is False
    assert len(edge.calls) == 2, "a 404 must be re-probed before it is believed"


def test_a_cached_404_is_detected_and_the_real_bytes_are_returned(monkeypatch):
    """The defect this whole change exists for.

    The plain URL 404s; the cache-bypass URL serves the file. That is a CDN
    negative cache entry, not a failed deploy, and the caller needs the bytes so
    it can still verify they are the right ones.
    """
    edge = FakeEdge({dc.CACHE_BUSTER_PARAM: (b"the real file", "")})
    monkeypatch.setattr(dc, "fetch", edge)
    got, error, bypassed = dc.probe("https://x", "skill/pkg.tar.gz", 1.0, "N")
    assert got == b"the real file"
    assert error == ""
    assert bypassed is True


def test_the_reprobe_uses_a_url_the_edge_has_not_seen(monkeypatch):
    edge = FakeEdge({dc.CACHE_BUSTER_PARAM: (b"x", "")})
    monkeypatch.setattr(dc, "fetch", edge)
    dc.probe("https://x", "skill/pkg.tar.gz", 1.0, "NONCE-123")
    assert edge.calls[0] == "https://x/skill/pkg.tar.gz", edge.calls
    assert edge.calls[1] == f"https://x/skill/pkg.tar.gz?{dc.CACHE_BUSTER_PARAM}=NONCE-123"


def test_errors_other_than_404_are_not_reprobed(monkeypatch):
    """A 500 or a timeout says nothing about caching. A second request would be
    noise, and on a struggling origin it would be load."""
    for error in ("HTTP 500", "HTTP 403", "unreachable: timed out"):
        edge = FakeEdge({"/llms.txt": (None, error)})
        monkeypatch.setattr(dc, "fetch", edge)
        got, seen, bypassed = dc.probe("https://x", "llms.txt", 1.0, "N")
        assert (got, seen, bypassed) == (None, error, False)
        assert len(edge.calls) == 1, (error, edge.calls)


def test_the_nonce_changes_between_runs(monkeypatch):
    """A fixed buster would collect its own cached 404 on the first
    pre-publication run and be dead weight from then on."""
    monkeypatch.setattr(dc.time, "time", lambda: 1000.0)
    first = dc._nonce()
    monkeypatch.setattr(dc.time, "time", lambda: 2000.0)
    assert dc._nonce() != first


# ── main(): which diagnosis the operator actually reads ────────────────────


@pytest.fixture
def one_published(monkeypatch):
    """Narrow the run to a single real file so stderr is readable."""
    monkeypatch.setattr(dc, "PUBLISHED", ("llms.txt",))
    monkeypatch.setattr(dc, "_nonce", lambda: "N")
    return dc.SITE / "llms.txt"


def test_a_matching_site_exits_zero(monkeypatch, one_published):
    body = one_published.read_bytes()
    monkeypatch.setattr(dc, "fetch", FakeEdge({"/llms.txt": (body, "")}))
    assert dc.main(["--quiet"]) == 0


def test_a_cached_404_does_not_tell_the_operator_to_redeploy(
    monkeypatch, one_published, capsys
):
    """The regression guard. Before this change the message below was the one
    printed for a correctly deployed file, which sends someone to redeploy a
    site that is already right."""
    body = one_published.read_bytes()
    monkeypatch.setattr(dc, "fetch", FakeEdge({dc.CACHE_BUSTER_PARAM: (body, "")}))

    assert dc.main(["--quiet"]) == 1, "unreachable files must still fail the gate"
    err = capsys.readouterr().err
    assert "visitors cannot reach" in err
    assert "THE FIX IS NOT TO REDEPLOY" in err
    assert "only the cached 404 is stale" in err
    assert "the deploy did not happen" not in err, err


def test_a_genuine_404_still_tells_the_operator_to_redeploy(
    monkeypatch, one_published, capsys
):
    monkeypatch.setattr(dc, "fetch", FakeEdge({}))
    assert dc.main(["--quiet"]) == 1
    err = capsys.readouterr().err
    assert "HTTP 404" in err
    assert "the deploy did not happen" in err
    assert "THE FIX IS NOT TO REDEPLOY" not in err, err


def test_a_file_that_is_unreachable_AND_wrong_reports_both(
    monkeypatch, one_published, capsys
):
    """Reporting only "the cache is stale" for a file whose bytes are also wrong
    would hide the more serious of the two problems behind the milder one."""
    monkeypatch.setattr(dc, "fetch", FakeEdge({dc.CACHE_BUSTER_PARAM: (b"wrong", "")}))
    assert dc.main(["--quiet"]) == 1
    err = capsys.readouterr().err
    assert "served copy differs" in err
    assert "only the cached 404 is stale" not in err, err


def test_a_published_file_missing_from_the_tree_is_a_problem(monkeypatch, capsys):
    monkeypatch.setattr(dc, "PUBLISHED", ("no/such/file.txt",))
    monkeypatch.setattr(dc, "fetch", FakeEdge({}))
    assert dc.main(["--quiet"]) == 1
    assert "missing from site/" in capsys.readouterr().err


# ── fetch(): the one place that touches the network ────────────────────────


def test_non_http_schemes_are_refused():
    """`--base file:///etc` would otherwise "pass" this check by reading local
    files, which is the opposite of testing what the internet is served."""
    for url in ("file:///etc/passwd", "ftp://host/x", "/no/scheme"):
        got, error = dc.fetch(url, 1.0)
        assert got is None
        assert "refusing to fetch" in error, url
