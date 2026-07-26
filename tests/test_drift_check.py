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
import re
import subprocess
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


# ── the published set is COMPLETE ──────────────────────────────────────────
#
# The test above checks one direction only: everything named is real. Nothing
# checked the other one, and the other one is where the damage is. The publish
# command copies site/ wholesale, so a file added to that directory is SERVED
# whether or not anyone adds it to PUBLISHED -- and a file that is served but
# not in PUBLISHED is a live URL that drift_check never requests. It cannot be
# reported as missing, because nothing knows to look for it. That is not a hole
# somebody eventually notices; it is a hole that is invisible by construction.
#
# So every tracked file in site/ has to land in exactly one of three buckets:
#
#   PUBLISHED                              served, and the bytes are checked
#   UNCHECKED_BECAUSE_THE_EDGE_REWRITES_IT served, bytes deliberately not checked
#   NOT_DEPLOYED                           must never reach the webroot at all
#
# The arithmetic is the point. Any new file forces a decision into one of the
# three, and there is no fourth answer available -- including the answer nobody
# means to give, which is silence.


def _tracked_site_files() -> list[str]:
    """Paths under site/ as the repository ships them, relative to site/.

    `git ls-files` rather than `rglob`, because the question is what this
    repository publishes, not what happens to be in one checkout. An untracked
    scratch file in site/ would be a real deploy hazard -- `cp -a` takes it too
    -- but failing the suite on somebody's local file is the kind of noise that
    gets a test deleted, and the hazard belongs to a live probe, not to this.
    """
    listing = subprocess.run(
        # S607: `git` off PATH. There is no portable absolute path for it and
        # this runs on ubuntu and on windows. Fixed argv, shell=False.
        ["git", "ls-files", "-z", "site"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout
    return [p[len("site/") :] for p in listing.decode().split("\0") if p]


@pytest.fixture(scope="module")
def tracked() -> list[str]:
    try:
        files = _tracked_site_files()
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        pytest.skip(f"git unusable here: {exc}")
    # A green that came from an empty listing would certify nothing.
    assert len(files) > 10, files
    return files


def test_the_published_set_accounts_for_the_whole_tree(tracked):
    """Every file the repository ships under site/ is in exactly one bucket."""
    buckets = (
        set(dc.PUBLISHED)
        | set(dc.UNCHECKED_BECAUSE_THE_EDGE_REWRITES_IT)
        | set(dc.NOT_DEPLOYED)
    )
    unaccounted = sorted(set(tracked) - buckets)
    assert unaccounted == [], (
        f"{unaccounted} would be copied to the webroot by the publish command "
        f"and never asked about again. Add each to PUBLISHED, or to "
        f"NOT_DEPLOYED with the reason it must not be served."
    )


def test_no_bucket_names_a_file_that_is_gone(tracked):
    """A name left behind after the file moves is cover for the next file that
    lands on it -- and in NOT_DEPLOYED it is worse, because the runbook keeps
    deleting a path that no longer exists while the real file goes out."""
    known = set(tracked)
    stale = sorted(
        name
        for name in set(dc.UNCHECKED_BECAUSE_THE_EDGE_REWRITES_IT) | set(dc.NOT_DEPLOYED)
        if name not in known
    )
    assert stale == [], stale


def test_the_buckets_do_not_overlap():
    pub, edge, hidden = (
        set(dc.PUBLISHED),
        set(dc.UNCHECKED_BECAUSE_THE_EDGE_REWRITES_IT),
        set(dc.NOT_DEPLOYED),
    )
    assert pub & edge == set()
    assert pub & hidden == set(), "a file cannot be both checked and unpublished"
    assert edge & hidden == set()


def test_every_not_deployed_file_says_why():
    """`NOT_DEPLOYED` is the one bucket whose entries delete production files.
    An entry without a reason is an instruction to `rm` something for reasons
    nobody can reconstruct, which is how a wrong entry survives review."""
    for name, why in dc.NOT_DEPLOYED.items():
        assert len(why) > 80, f"{name}: {why!r}"


_SPELLED = {
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}


def test_the_robots_note_counts_the_files_it_would_take_down():
    """The note explaining why robots.txt is excluded argues from a count: a
    permanently red gate would be switched off, taking the other N files with
    it. The argument is only as good as N, and N was written when the list was
    shorter -- it said fourteen while PUBLISHED held eighteen, having missed
    the four skill files added when the skill was renamed."""
    source = pathlib.Path(dc.__file__).read_text(encoding="utf-8")
    spelled = re.search(r"take the other (\w+) files", source)
    assert spelled, "the note stopped naming a count; update or drop this test"
    assert _SPELLED[spelled.group(1)] == len(dc.PUBLISHED), (
        f"the note says {spelled.group(1)} but PUBLISHED holds {len(dc.PUBLISHED)}"
    )


# ── the runbook and the buckets agree ──────────────────────────────────────

RUNBOOKS = ("deploy/GO_LIVE.md", "deploy/README.md")


@pytest.mark.parametrize("runbook", RUNBOOKS)
def test_the_publish_runbook_removes_every_not_deployed_file(runbook):
    """`cp -a site/.` copies the whole directory, so NOT_DEPLOYED is only a
    decision until the runbook acts on it. Both publish procedures have to
    delete every such file by name, in the same block that does the copy -- a
    removal three sections later is one an operator skips."""
    text = (ROOT / runbook).read_text(encoding="utf-8")
    blocks = [b for b in re.findall(r"```sh\n(.*?)```", text, re.S) if "cp -a site/." in b]
    assert len(blocks) == 1, f"{runbook}: expected one publish block, found {len(blocks)}"
    for name in dc.NOT_DEPLOYED:
        assert name in blocks[0], (
            f"{runbook} copies site/ wholesale and never removes {name}, which "
            f"tools/drift_check.py says must not be served"
        )


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
    # The substituted address sits under the reserved `.example` TLD: CI asserts
    # every committed address is unassignable, and a fixture that names a domain
    # somebody could actually register is the exact thing it stops.
    result = dc.compare("page.html", b"install --email evil@attacker.example", local)
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
