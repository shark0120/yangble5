"""The guard over published numbers, and the guard over that guard.

`site/README.md` tells a reader that every 3-or-more-digit figure rendered as
page text is either in the authoritative measurement record or explicitly
allow-listed.  For six commits that sentence was false: the checker's regex
carried a negative lookbehind containing ``.``, so a decimal was cut in half —
``99.53`` was seen as ``99`` (discarded, under three digits) and ``53`` was
never seen at all.  Every percentage on the site, including the headline
99.53% hit rate, was outside the only automated check over published numbers,
and the run still printed ``OK``.

These tests exist so that sentence has to stay true.  They assert both
directions on every case: a bogus figure IS reported, an authoritative figure
is NOT.  A guard that has only ever been observed to pass is not a guard.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tools import sitecheck

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "sitecheck.py"
SITE_README = ROOT / "site" / "README.md"


# ── the checker's own self-test, case by case ───────────────────────────────
# Driven through sitecheck's real tables so a case cannot be "passed" by a
# stub: `check_page` here is the same function the site run calls.

@pytest.mark.parametrize("name,payload,needle", sitecheck.MUST_FAIL,
                         ids=[c[0] for c in sitecheck.MUST_FAIL])
def test_bogus_figure_is_reported(name, payload, needle):
    """Re-introducing a wrong number must turn the check red."""
    problems = sitecheck.check_page("<t>", sitecheck._page(payload), set())
    assert any(needle in p for p in problems), (
        f"{name}: page text {payload!r} produced no problem naming {needle!r}. "
        f"A published figure that is not in the authoritative record went "
        f"unreported. Problems were: {problems!r}")


@pytest.mark.parametrize("name,payload", sitecheck.MUST_PASS,
                         ids=[c[0] for c in sitecheck.MUST_PASS])
def test_authoritative_figure_is_accepted(name, payload):
    """The guard must not be red for the record it is supposed to certify."""
    problems = sitecheck.check_page("<t>", sitecheck._page(payload), set())
    assert problems == [], f"{name}: {payload!r} was wrongly flagged: {problems!r}"


def test_selftest_passes_as_a_whole():
    assert sitecheck.selftest(verbose=False) is True


# ── the specific defect, named ─────────────────────────────────────────────

def test_decimal_is_one_figure_not_two_halves():
    """The regression test for the actual bug.

    The historical pattern split `99.53` into `99` (dropped as too short) and
    nothing else. Assert the whole decimal arrives as ONE figure.
    """
    figures, invariant = sitecheck.scan_figures("命中率 99.53% 暖輪")
    assert invariant == []
    assert ("99.53", "99.53") in figures, (
        f"a decimal must be tokenised as one figure, got {figures!r}")


def test_historical_pattern_would_have_missed_the_hit_rate():
    """Prove the defect was real, and that it is now detectable rather than
    silent: run the scanner with the pattern this file replaced and require
    the consumed-equals-checked invariant to complain."""
    text = "命中率 99.54%"
    good, good_inv = sitecheck.scan_figures(text)
    bad, bad_inv = sitecheck.scan_figures(text, sitecheck.HISTORICAL_BROKEN)

    assert ("99.54", "99.54") in good and good_inv == []
    assert not any(c == "99.54" for c, _ in bad), (
        "the historical pattern is supposed to be blind to decimals")
    assert any("INVARIANT VIOLATED" in p for p in bad_inv), (
        "narrowing the tokeniser must be reported, not silent: the set of "
        f"characters consumed must equal the set checked. Got {bad_inv!r}")


def test_995_4_is_not_reachable_from_the_record_at_any_precision():
    for table in (sitecheck.MEASURED, sitecheck.PERCENT, sitecheck.ALLOW):
        assert "99.54" not in table
    assert "99.53" in sitecheck.PERCENT


def test_percentages_are_recomputed_not_transcribed():
    """99.53 must be accepted because the arithmetic says so, not because a
    string is on a list. Perturb the record and it must stop being valid."""
    assert sitecheck.PERCENT["99.53"].startswith("round 2 hit rate = 745438/748933")
    c, p = sitecheck.CACHED[1], sitecheck.PROMPT[1]
    assert f"{100.0 * c / p:.2f}" == "99.53"
    assert f"{100.0 * (c + 40) / p:.2f}" != "99.53"


def test_unit_suffixed_figures_are_not_waved_through_as_names():
    """`749K` is a published measurement wearing a letter. The identifier rule
    must not exempt it."""
    figures, _ = sitecheck.scan_figures("~749K 前綴")
    assert ("749K", "749K") in figures
    assert sitecheck.account_figures(figures, set()) == []
    bogus, _ = sitecheck.scan_figures("~750K 前綴")
    assert any("750K" in p for p in sitecheck.account_figures(bogus, set()))


def test_adjacent_text_nodes_are_not_fused_into_an_identifier():
    """`"".join(nodes)` glued the end of one text node to the start of the
    next, turning a figure into a name. Splitting can only ever be loud."""
    src = ("<!doctype html><html lang=\"x\"><head><style>a{}</style></head>"
           "<body><b>yangble</b>99.54<script>0</script></body></html>")
    problems = sitecheck.check_page("<t>", src, set())
    assert any("99.54" in p for p in problems), (
        f"a figure abutting a previous text node was swallowed: {problems!r}")


def test_non_ascii_digits_are_reported_rather_than_ignored():
    # The full-width digits are the fixture, not a typo.
    _, invariant = sitecheck.scan_figures("命中率 ９９.５３%")  # noqa: RUF001
    assert any("non-ASCII digit" in p for p in invariant)


def test_malformed_thousands_separators_are_rejected():
    figures, _ = sitecheck.scan_figures("74,8918")
    problems = sitecheck.account_figures(figures, set())
    assert any("thousands separator" in p for p in problems)


# ── the allow-list has to describe reality ─────────────────────────────────

def test_stale_allow_list_entries_are_reported():
    stale = sitecheck.unused_allow_problems(set())
    assert len(stale) == len(sitecheck.ALLOW)


def test_every_allow_entry_carries_a_reason():
    for key, why in sitecheck.ALLOW.items():
        assert why and len(why) > 15, f"{key!r} has no stated reason"


# ── CSP hashes ─────────────────────────────────────────────────────────────

def test_csp_hashes_match_every_consumer():
    assert sitecheck.csp_problems() == []


def test_editing_an_inline_script_turns_the_csp_check_red():
    """The recipe this replaces printed the new hash and then grepped for a
    hash spelled out in the prose, so it stayed green after the script moved.
    Change a script for real and the check must name both sides."""
    pages = {f: (sitecheck.SITE / f).read_text(encoding="utf-8")
             for f in sitecheck.FILES}
    src = pages["index.html"]
    i = src.rindex("<script>") + len("<script>")
    pages["index.html"] = src[:i] + "/* edited */" + src[i:]
    problems = sitecheck.csp_problems(pages)
    assert any("stale" in p for p in problems)
    assert any("missing" in p for p in problems)


def test_csp_uses_a_parser_not_a_regex():
    """A literal '<script' inside a string must not be hashed."""
    page = "<html><style>/* mentions script */</style><script>1</script></html>"
    assert sitecheck.csp_hashes(page) == sitecheck.csp_hashes(
        "<html><script>1</script></html>")


# ── the real pages ─────────────────────────────────────────────────────────

def test_site_pages_are_clean_and_the_run_is_not_empty():
    used: set[str] = set()
    problems: list[str] = []
    for name in sitecheck.FILES:
        problems += [f"{name}: {p}" for p in sitecheck.check_page(
            name, (sitecheck.SITE / name).read_text(encoding="utf-8"), used)]
    problems += sitecheck.unused_allow_problems(used)
    assert problems == [], "\n".join(problems)
    # "0 problems" is also what a checker that examined nothing prints.
    assert "99.53" in used and "0.00" in used and "749K" in used, (
        f"the guard did not actually see the headline figures; used={sorted(used)}")


def test_cli_exits_zero_and_reports_the_allow_list():
    r = subprocess.run(  # noqa: S603 - fixed argv, interpreter is sys.executable
        [sys.executable, str(SCRIPT)],
        capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "self-test: OK" in r.stdout
    assert "index.html: OK" in r.stdout and "verify.html: OK" in r.stdout
    assert "allow-list: OK" in r.stdout


def test_cli_runs_from_any_working_directory(tmp_path):
    """CI must not be able to green it by running it somewhere it finds no
    pages. Path resolution is anchored to the file, not the cwd."""
    r = subprocess.run(  # noqa: S603 - fixed argv, interpreter is sys.executable
        [sys.executable, str(SCRIPT), "--self-test"],
        capture_output=True, text=True, encoding="utf-8", cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr


# ── the README may not drift away from the file ────────────────────────────

def test_readme_code_excerpts_are_verbatim_from_the_real_file():
    """The previous checker existed ONLY as a fenced block in site/README.md,
    which is why nothing ever ran it. Now the file is authoritative and every
    excerpt the README shows must be a literal substring of it."""
    source = SCRIPT.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", SITE_README.read_text(encoding="utf-8"), re.S)
    assert blocks, "site/README.md no longer shows the checker at all"
    for block in blocks:
        excerpt = block.strip("\n")
        assert excerpt in source, (
            "site/README.md shows code that is not in tools/sitecheck.py "
            f"verbatim:\n{excerpt[:400]}")


@pytest.mark.parametrize(
    "args", [["--self-test"], ["--quiet"], ["--inventory", "--quiet"]],
    ids=["self-test", "run", "inventory"])
def test_readme_quotes_real_output_verbatim(args):
    """site/README.md presents these blocks as "Real output". Make that true.

    Pasted output is a claim about the program, and it rots exactly like any
    other claim -- with the added problem that a reader cannot tell a stale
    transcript from a current one.
    """
    r = subprocess.run(  # noqa: S603 - fixed argv, interpreter is sys.executable
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
    out = r.stdout.rstrip("\n")
    assert out, "the command printed nothing"
    assert out in SITE_README.read_text(encoding="utf-8"), (
        f"site/README.md no longer quotes the real output of "
        f"`sitecheck.py {' '.join(args)}`; re-paste it. Current output:\n{out}")


def test_readme_points_at_the_committed_checker():
    text = SITE_README.read_text(encoding="utf-8")
    assert "tools/sitecheck.py" in text
    assert SCRIPT.exists()


def test_ci_runs_the_checker():
    """A guard that is not wired in is documentation."""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "tools/sitecheck.py" in ci, "sitecheck is not wired into CI"


# ── the coverage invariant: what the guard is allowed NOT to look at ───────
# `FILES` was a literal tuple, so coverage was opt-in by filename and no test
# asserted it was exhaustive over site/. Two 75 KB installers that publish
# `99.53%` and `748,918` sat outside the only automated guard over published
# figures, and the run printed OK. These tests make the file set total: every
# file under site/ is a page, a text file, or exempt with a written reason.


@pytest.fixture
def site_copy(tmp_path):
    """A throwaway copy of site/, so a negative control never mutates the tree."""
    dst = tmp_path / "site"
    shutil.copytree(sitecheck.SITE, dst)
    return dst


def _run_site(site: Path):
    return subprocess.run(  # noqa: S603 - fixed argv, interpreter is sys.executable
        [sys.executable, str(SCRIPT), "--quiet", "--site", str(site)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
    )


def test_the_page_set_is_discovered_not_typed():
    """The literal tuple is gone: the pages are whatever site/ contains."""
    on_disk = sorted(
        p.relative_to(sitecheck.SITE).as_posix()
        for p in sitecheck.SITE.rglob("*")
        if p.is_file() and p.suffix.lower() in sitecheck.PAGE_SUFFIXES
    )
    assert list(sitecheck.FILES) == on_disk, (
        "the checked page set is not the set of pages in site/; a page is "
        "either unguarded or the tuple names one that no longer exists"
    )
    assert on_disk, "no HTML pages discovered — the guard would certify nothing"


def test_every_file_under_site_is_classified():
    """The coverage invariant, on the real tree."""
    pages, texts, problems = sitecheck.classify()
    assert problems == [], "\n".join(problems)
    on_disk = {
        p.relative_to(sitecheck.SITE).as_posix()
        for p in sitecheck.SITE.rglob("*")
        if p.is_file()
    }
    assert set(pages) | set(texts) | set(sitecheck.EXEMPT) == on_disk, (
        f"classified {sorted(set(pages) | set(texts) | set(sitecheck.EXEMPT))} "
        f"but site/ holds {sorted(on_disk)}"
    )
    assert texts, "no text-bearing files classified — site/README.md alone should be one"


def test_a_file_the_guard_does_not_understand_is_a_finding(site_copy):
    """The whole defect in one test: a new file must not be able to appear in
    site/ outside the guard with the run still green."""
    (site_copy / "notes.rst").write_text("warm rounds hit 99.61%\n", encoding="utf-8")
    _pages, _texts, problems = sitecheck.classify(site_copy)
    assert any("notes.rst" in p and "neither checked nor exempt" in p for p in problems), (
        f"an unclassified file was waved through: {problems!r}"
    )
    r = _run_site(site_copy)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "notes.rst" in r.stdout


def test_a_new_text_file_is_guarded_the_moment_it_appears(site_copy):
    """An llms.txt or AGENTS.md restating the headline figure is exactly the
    file this guard was blind to. It is now checked without anyone opting in."""
    (site_copy / "llms.txt").write_text(
        "yangble5 caches: warm-round hit rate 99.61%, prefix 748,919 tokens.\n",
        encoding="utf-8",
    )
    r = _run_site(site_copy)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "llms.txt" in r.stdout
    assert "99.61" in r.stdout and "748919" in r.stdout, r.stdout


def test_an_untouched_copy_of_the_site_is_clean(site_copy):
    """The negative controls above only mean something if the control case is
    green: every text file in site/ as it stands is already accounted for."""
    r = _run_site(site_copy)
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_file_that_cannot_be_decoded_is_a_finding_not_a_traceback(site_copy):
    """A UnicodeDecodeError escaping to the top would abandon every remaining
    page mid-run, and an unchecked site would be reported as a crash instead of
    as the coverage hole it is."""
    (site_copy / "blob.txt").write_bytes(b"\xff\xfe\x00 not utf-8")
    r = _run_site(site_copy)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "Traceback" not in r.stderr, r.stderr
    assert "blob.txt" in r.stdout and "cannot be read as UTF-8" in r.stdout, r.stdout


def test_a_stale_exemption_is_reported(site_copy, monkeypatch):
    monkeypatch.setitem(sitecheck.EXEMPT, "gone.bin", "a file that is not there")
    _pages, _texts, problems = sitecheck.classify(site_copy)
    assert any("gone.bin" in p for p in problems), problems


def test_the_obvious_machine_readable_formats_are_covered():
    """An AGENTS.md, an llms.txt or a JSON manifest restating the headline
    figure is the concrete case this guard was blind to. None of them may
    require anyone to remember to add a filename."""
    assert not set(sitecheck.PAGE_SUFFIXES) & set(sitecheck.TEXT_SUFFIXES)
    for suffix in (".html", ".md", ".txt", ".json"):
        assert suffix in sitecheck.PAGE_SUFFIXES + sitecheck.TEXT_SUFFIXES, suffix


def test_exempt_entries_carry_a_reason_and_are_not_pages():
    for name, why in sitecheck.EXEMPT.items():
        assert why and len(why) > 20, f"{name!r} is exempt with no stated reason"
        assert Path(name).suffix.lower() not in sitecheck.PAGE_SUFFIXES, (
            f"{name!r} is an HTML page exempted from the page audit"
        )


# ── the guard over files that are not HTML pages ──────────────────────────


@pytest.mark.parametrize(
    "name,fname,payload,needle",
    sitecheck.TEXT_MUST_FAIL,
    ids=[c[0] for c in sitecheck.TEXT_MUST_FAIL],
)
def test_bogus_figure_in_a_text_file_is_reported(name, fname, payload, needle):
    problems = sitecheck.check_text(fname, payload, set())
    assert any(needle in p for p in problems), (
        f"{name}: {payload!r} in {fname} produced no problem naming {needle!r}. "
        f"A published figure outside the authoritative record went unreported. "
        f"Problems were: {problems!r}"
    )


@pytest.mark.parametrize(
    "name,fname,payload",
    sitecheck.TEXT_MUST_PASS,
    ids=[c[0] for c in sitecheck.TEXT_MUST_PASS],
)
def test_authoritative_figure_in_a_text_file_is_accepted(name, fname, payload):
    problems = sitecheck.check_text(fname, payload, set())
    assert problems == [], f"{name}: {payload!r} in {fname} was wrongly flagged: {problems!r}"


def test_a_per_file_allowance_does_not_leak():
    """site/README.md may print `99.54%` because it documents the negative
    control. site/install.sh may not, and neither may a page."""
    assert sitecheck.check_text(sitecheck.GUARD_DOC, "CI plants `99.54%`", set()) == []
    assert any("99.54" in p for p in sitecheck.check_text("install.sh", "99.54%", set()))
    page = sitecheck.check_page("<t>", sitecheck._page("命中率 99.54%"), set())
    assert any("99.54" in p for p in page), (
        "the text allow-list leaked into the page audit; CI's negative control "
        f"would stop being able to go red. Got {page!r}"
    )


def test_the_text_guard_actually_reads_the_installers():
    """"0 problems" is also what a checker that examined nothing prints. The
    installers are the files that publish 99.53% outside any HTML page."""
    used: set[tuple[str, str]] = set()
    _pages, texts, _problems = sitecheck.classify()
    assert {"install.sh", "install.ps1", sitecheck.GUARD_DOC} <= set(texts)
    for name in texts:
        assert sitecheck.check_text(
            name, (sitecheck.SITE / name).read_text(encoding="utf-8"), used
        ) == [], name
    assert sitecheck.unused_text_allow_problems(used) == []
    for name in ("install.sh", "install.ps1"):
        body = (sitecheck.SITE / name).read_text(encoding="utf-8")
        seen = {m.group(1) for m in sitecheck.TEXT_PERCENT_RE.finditer(body)}
        assert "99.53" in seen, f"{name} no longer publishes the hit rate, or the scanner is blind"


def test_the_four_place_provenance_is_recomputed_not_transcribed():
    """README quotes `= 99.5333%`, which the page tables deliberately do not
    accept. It is allowed because the arithmetic says so, not by allow-list."""
    assert "99.5333" not in sitecheck.PERCENT
    assert "99.5333" in sitecheck.TEXT_PERCENT
    c, p = sitecheck.CACHED[1], sitecheck.PROMPT[1]
    assert f"{100.0 * c / p:.4f}" == "99.5333"
    assert "99.5334" not in sitecheck.TEXT_PERCENT


def test_text_allow_entries_carry_a_reason():
    for name, entries in sitecheck.TEXT_ALLOW.items():
        for token, why in entries.items():
            assert why and len(why) > 15, f"{name}: {token!r} has no stated reason"


def test_stale_text_allow_entries_are_reported():
    stale = sitecheck.unused_text_allow_problems(set())
    expected = sum(len(v) for v in sitecheck.TEXT_ALLOW_EXPLICIT.values())
    assert len(stale) == expected


def test_the_documentation_may_only_quote_figures_the_guard_rejects():
    """README's licence to print `99.54%` is derived from MUST_FAIL, not typed.
    Retype it and the coupling that makes it safe is gone."""
    derived = sitecheck._fixture_figures()
    assert "99.54" in derived and "750K" in derived
    for figure in derived:
        assert figure not in sitecheck.TEXT_PERCENT, (
            f"{figure} is both a must-fail fixture and an accepted measurement"
        )


# ── the silent self-test cases must not be decorative ─────────────────────


@pytest.mark.parametrize("break_it", ["must-fail", "must-pass", "no-pages"])
def test_the_text_cases_can_turn_the_self_test_red(break_it, monkeypatch):
    """The non-HTML cases print nothing when they pass, so this is the only
    thing standing between them and being ornamental. Break one, require red."""
    assert sitecheck.selftest(verbose=False) is True
    if break_it == "must-fail":
        # Accept a figure a must-fail case relies on being rejected.
        monkeypatch.setitem(sitecheck.TEXT_PERCENT, "99.6", "planted by a test")
    elif break_it == "must-pass":
        # Reject a figure a must-pass case relies on being accepted.
        monkeypatch.delitem(sitecheck.TEXT_PERCENT, "99.53")
    else:
        monkeypatch.setattr(sitecheck, "PAGE_SUFFIXES", (".nothing",))
    assert sitecheck.selftest(verbose=False) is False, (
        f"breaking {break_it!r} left the self-test green, so it was proving nothing"
    )


def test_coverage_mode_lists_every_file_and_exits_zero():
    r = subprocess.run(  # noqa: S603 - fixed argv, interpreter is sys.executable
        [sys.executable, str(SCRIPT), "--coverage", "--quiet"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    for p in sitecheck.SITE.rglob("*"):
        if p.is_file():
            assert p.relative_to(sitecheck.SITE).as_posix() in r.stdout, r.stdout


def test_ci_runs_the_coverage_check():
    """The scope of the guard is only visible if something prints it."""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "--coverage" in ci, "nothing in CI prints which files the guard covers"
    assert "notes.rst" in ci or "unclassified" in ci, (
        "CI has no negative control proving an unclassified file turns the job red"
    )


# ══════════════════════════════════════════════════════════════════════════
# THE SURFACE AN AI AGENT LANDS ON
# ══════════════════════════════════════════════════════════════════════════
# site/AGENTS.md, site/llms.txt, site/sitemap.xml and site/.well-known/ are
# read by agents, not by people, and an agent acts on what it reads. They are
# also the exact shape of file the figure guard was once blind to: not HTML,
# not obviously "content", added long after anyone last opened the checker.
# Everything below exists to make "these files are covered" a thing that has
# been observed to fail rather than a thing someone believes.

NEW_SURFACE = ("AGENTS.md", "llms.txt", "sitemap.xml", ".well-known/security.txt")


@pytest.mark.parametrize("name", NEW_SURFACE)
def test_the_agent_facing_files_exist_and_are_classified(name):
    """None of them may sit in site/ outside the guard, and a dotted
    directory must not fall out of discovery — `.well-known` is exactly the
    kind of path a glob quietly skips."""
    assert (sitecheck.SITE / name).is_file(), f"site/{name} is missing"
    _pages, texts, problems = sitecheck.classify()
    assert problems == [], "\n".join(problems)
    assert name in texts, (
        f"site/{name} is not in the checked text-file set, so every figure and "
        f"every claim in it is unguarded"
    )


@pytest.mark.parametrize("name", NEW_SURFACE)
def test_a_bogus_number_in_an_agent_facing_file_turns_the_build_red(name, site_copy):
    """The negative control, once per new file, through the SAME command CI
    runs. `99.61%` and `748,919` are a plausible hit rate and a plausible
    prompt total that the measurement record does not contain."""
    target = site_copy / name
    target.write_text(
        target.read_text(encoding="utf-8")
        + "\nwarm-round cache hit rate 99.61%, prefix 748,919 tokens\n",
        encoding="utf-8",
        newline="\n",
    )
    r = _run_site(site_copy)
    assert r.returncode == 1, (
        f"a bogus measurement in site/{name} left the build green:\n{r.stdout}{r.stderr}"
    )
    assert name in r.stdout, f"the run failed but never named {name}:\n{r.stdout}"
    assert "99.61" in r.stdout and "748919" in r.stdout, r.stdout


@pytest.mark.parametrize("name", NEW_SURFACE)
def test_a_forbidden_claim_in_an_agent_facing_file_turns_the_build_red(name, site_copy):
    """A file can be wrong with no number in it at all. `#` opens a comment in
    two of these formats and `<!--` in a third, and none of that matters: a
    sentence a crawler or an agent can read is published."""
    target = site_copy / name
    target.write_text(
        target.read_text(encoding="utf-8") + "\nyangble5 is a fast model.\n",
        encoding="utf-8",
        newline="\n",
    )
    r = _run_site(site_copy)
    assert r.returncode == 1, (
        f"a forbidden claim in site/{name} left the build green:\n{r.stdout}{r.stderr}"
    )
    assert name in r.stdout and "forbidden claim" in r.stdout, r.stdout


def test_the_new_files_are_reached_by_the_real_run_not_only_by_a_copy():
    """`--site` is how the negative controls stay non-destructive, but a rule
    that only ever fires on a temporary directory proves nothing about what CI
    checks. Run the checker's own functions over the files as committed."""
    used: set[tuple[str, str]] = set()
    for name in NEW_SURFACE:
        body = (sitecheck.SITE / name).read_text(encoding="utf-8")
        assert sitecheck.check_text(name, body, used) == [], name
        assert sitecheck.whole_file_problems(name, body) == [], name


# ── forbidden claims ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name,payload,needle", sitecheck.CLAIM_MUST_FAIL, ids=[c[0] for c in sitecheck.CLAIM_MUST_FAIL]
)
def test_a_forbidden_claim_is_reported(name, payload, needle):
    problems = sitecheck.claim_problems(payload)
    assert any(needle in p for p in problems), (
        f"{name}: {payload!r} produced no problem quoting {needle!r}. Problems "
        f"were: {problems!r}"
    )


@pytest.mark.parametrize(
    "name,payload", sitecheck.CLAIM_MUST_PASS, ids=[c[0] for c in sitecheck.CLAIM_MUST_PASS]
)
def test_denying_a_forbidden_claim_is_not_making_it(name, payload):
    """Every sentence here is on the site right now. A guard that fires on the
    denial is a guard that gets deleted, and then the assertion is unguarded
    too."""
    assert sitecheck.claim_problems(payload) == [], f"{name}: {payload!r} was wrongly flagged"


def test_a_negation_after_the_claim_does_not_retract_it():
    payload, needle = sitecheck.CLAIM_TRAILING_NEGATION
    assert any(needle in p for p in sitecheck.claim_problems(payload)), (
        "the negation window must end at the match: a sentence may assert "
        "something forbidden and then deny something else"
    )


def test_claims_are_checked_on_pages_and_on_text_files_alike():
    """`curl https://yangble5.com/install.sh` is read by more people than the
    landing page is. A claim in a shell comment is published just as hard."""
    page = sitecheck.check_page("<t>", sitecheck._page("yangble5 是一個模型。"), set())
    assert any("forbidden claim" in p for p in page), page
    text = sitecheck.check_text("install.sh", "yangble5 is a fast model.", set())
    assert any("forbidden claim" in p for p in text), text


def test_a_forbidden_claim_cannot_be_allow_listed_away():
    """TEXT_ALLOW exists because a figure can be legitimate in one file and
    meaningless in another. A false sentence is false everywhere, so the
    per-file escape hatch must not reach it."""
    assert any(
        "forbidden claim" in p
        for p in sitecheck.check_text(sitecheck.GUARD_DOC, "yangble5 is a fast model.", set())
    )


def test_every_claim_rule_carries_a_reason_and_is_exercised():
    for label, pattern, why in sitecheck.CLAIMS:
        assert why and len(why) > 40, f"{label!r} is forbidden with no stated reason"
        assert any(
            pattern.search(payload) for _n, payload, _needle in sitecheck.CLAIM_MUST_FAIL
        ), f"no must-fail fixture exercises the {label!r} pattern"


# ── a measurement may not be published without its scope ──────────────────


@pytest.mark.parametrize(
    "name,payload,needle",
    sitecheck.DISCLOSURE_MUST_FAIL,
    ids=[c[0] for c in sitecheck.DISCLOSURE_MUST_FAIL],
)
def test_a_naked_hit_rate_is_reported(name, payload, needle):
    problems = sitecheck.disclosure_problems(payload)
    assert any(needle in p for p in problems), f"{name}: {payload!r} produced {problems!r}"


@pytest.mark.parametrize(
    "name,payload",
    sitecheck.DISCLOSURE_MUST_PASS,
    ids=[c[0] for c in sitecheck.DISCLOSURE_MUST_PASS],
)
def test_a_hit_rate_with_its_scope_is_accepted(name, payload):
    assert sitecheck.disclosure_problems(payload) == [], f"{name}: {payload!r} was wrongly flagged"


def test_the_disclosure_rule_is_a_whole_file_rule_not_a_fragment_rule():
    """It must NOT run inside check_page/check_text: those are handed single
    sentences by the self-test, and demanding that every sentence restate the
    whole measurement record is how a guard becomes noise and gets removed."""
    fragment = "暖輪 99.53% 命中"
    assert sitecheck.check_page("<t>", sitecheck._page(fragment), set()) == []
    assert sitecheck.check_text("install.sh", "99.53% — warm rounds only", set()) == []
    assert sitecheck.disclosure_problems(fragment) != []


def test_the_naked_headline_figure_turns_the_build_red(site_copy):
    """End to end, through the command CI runs. site/sitemap.xml is used
    because it carries none of the disclosures; the other files already do,
    which is the point of the rule but makes them useless as a fixture."""
    target = site_copy / "sitemap.xml"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n<!-- cache hit rate 99.53% -->\n",
        encoding="utf-8",
        newline="\n",
    )
    r = _run_site(site_copy)
    assert r.returncode == 1, r.stdout + r.stderr
    for needle in ("sitemap.xml", "which rounds", "cold request hit zero", "scope of the run"):
        assert needle in r.stdout, f"{needle!r} missing from:\n{r.stdout}"


def test_the_naked_headline_figure_turns_the_build_red_on_a_page_too(tmp_path):
    """The same rule down the OTHER code path. Pages and text files are read
    by different halves of main(), and wiring a rule into one of them is
    exactly the kind of half-done change that looks finished.

    A minimal site rather than a copy of the real one: site/index.html carries
    every disclosure already — which is the rule working — so making it fail
    would mean deleting Chinese phrases out of a 100 KB page by substring,
    and a fixture that fragile stops testing the thing it was written for.
    """
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text(
        sitecheck._page("命中率 99.53%"), encoding="utf-8", newline="\n"
    )
    r = _run_site(site)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "index.html" in r.stdout and "which rounds" in r.stdout, r.stdout


def test_the_disclosure_rule_is_not_vacuous_on_the_real_files():
    """"0 problems" is also what a rule that matched nothing prints. Some
    committed file must actually publish the hit rate and satisfy the rule."""
    publishers = [
        name
        for name in sorted(sitecheck.classify()[1])
        if sitecheck.CACHE_FIGURE_RE.search((sitecheck.SITE / name).read_text(encoding="utf-8"))
    ]
    assert "AGENTS.md" in publishers, (
        "site/AGENTS.md no longer quotes the hit rate, so nothing proves the "
        "disclosure rule was evaluated against it"
    )
    for name in publishers:
        body = (sitecheck.SITE / name).read_text(encoding="utf-8")
        assert sitecheck.disclosure_problems(body) == [], name


def test_the_hit_rate_pattern_is_derived_from_the_record():
    """Perturb the record and the set of figures that demand a scope moves
    with it. A transcribed literal would not."""
    assert "99.53" in sitecheck.CACHE_FIGURES and "99.5333" in sitecheck.CACHE_FIGURES
    assert "0.00" not in sitecheck.CACHE_FIGURES, "zero is not a claim that needs a scope"
    assert not sitecheck.CACHE_FIGURE_RE.search("99.54%")
    # The alternation must report the figure that is actually on the page, not
    # the shortest prefix of it that happens to be in the table.
    assert sitecheck.CACHE_FIGURE_RE.search("= 99.5333%").group(1) == "99.5333"
    assert sitecheck.CACHE_FIGURE_RE.search("99.53%").group(1) == "99.53"


# ── the sitemap describes the site, in both directions ────────────────────


def test_the_real_sitemap_is_consistent_with_the_real_site():
    assert sitecheck.sitemap_problems() == []
    assert sitecheck.wellknown_problems() == []


@pytest.mark.parametrize("name", ("AGENTS.md", "llms.txt"))
def test_the_agent_facing_documents_are_indexed(name):
    text = (sitecheck.SITE / sitecheck.SITEMAP).read_text(encoding="utf-8")
    assert f"{sitecheck.SITE_ORIGIN}{name}" in text, (
        f"site/{name} is published but not in the sitemap, so it is invisible "
        f"to anything that starts from the index"
    )


def test_a_new_document_that_nothing_indexes_is_reported(site_copy):
    """The direction nobody notices: nothing is broken, the page is simply
    invisible."""
    (site_copy / "guide.md").write_text("a document\n", encoding="utf-8", newline="\n")
    problems = sitecheck.sitemap_problems(site_copy)
    assert any("guide.md" in p and "does not list" in p for p in problems), problems


def test_a_sitemap_entry_for_a_file_that_is_gone_is_reported(site_copy):
    """The direction that breaks: a renamed page leaves the index advertising
    a 404, and the person who renamed it never opened this file."""
    (site_copy / "verify.html").unlink()
    problems = sitecheck.sitemap_problems(site_copy)
    assert any("verify.html" in p and "404" in p for p in problems), problems


def test_a_sitemap_entry_pointing_off_site_is_reported(site_copy):
    path = site_copy / "sitemap.xml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "<loc>https://yangble5.com/verify.html</loc>",
            "<loc>https://evil.example/verify.html</loc>",
        ),
        encoding="utf-8",
        newline="\n",
    )
    problems = sitecheck.sitemap_problems(site_copy)
    assert any("evil.example" in p for p in problems), problems


def test_a_future_lastmod_is_reported(site_copy):
    import datetime

    path = site_copy / "sitemap.xml"
    # Rewrite whatever the first date happens to be, rather than a literal:
    # the dates move every time a listed document is edited, and a fixture
    # pinned to one of them turns red for a reason that has nothing to do
    # with the rule it is testing.
    path.write_text(
        re.sub(r"<lastmod>[^<]+</lastmod>", "<lastmod>2999-01-01</lastmod>",
               path.read_text(encoding="utf-8"), count=1),
        encoding="utf-8",
        newline="\n",
    )
    problems = sitecheck.sitemap_problems(site_copy, today=datetime.date(2026, 7, 23))
    assert any("2999-01-01" in p and "future" in p for p in problems), problems


def test_a_stale_sitemap_exclusion_is_reported(site_copy, monkeypatch):
    monkeypatch.setitem(sitecheck.SITEMAP_EXCLUDED, "gone.md", "a file that is not there")
    problems = sitecheck.sitemap_problems(site_copy)
    assert any("gone.md" in p and "stale" in p for p in problems), problems


def test_every_sitemap_exclusion_carries_a_reason():
    for name, why in sitecheck.SITEMAP_EXCLUDED.items():
        assert why and len(why) > 20, f"{name!r} is excluded from the index with no stated reason"


def test_an_exclusion_that_could_never_fire_is_reported(site_copy, monkeypatch):
    """An entry for a suffix the index does not cover documents a decision
    nobody is making. Unreachable entries are how the checker this file
    replaced hid the fact that it had never seen a percentage."""
    monkeypatch.setitem(sitecheck.SITEMAP_EXCLUDED, "install.sh", "not a document anyway")
    problems = sitecheck.sitemap_problems(site_copy)
    assert any("install.sh" in p and "never fire" in p for p in problems), problems


def test_a_loc_that_walks_out_of_the_webroot_is_not_resolved(site_copy):
    """`../README.md` exists in the repository root. Resolving it would let a
    <loc> the web server can never serve be reported as fine."""
    path = site_copy / "sitemap.xml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "<loc>https://yangble5.com/verify.html</loc>",
            "<loc>https://yangble5.com/../README.md</loc>",
        ),
        encoding="utf-8",
        newline="\n",
    )
    problems = sitecheck.sitemap_problems(site_copy)
    assert any("README.md" in p for p in problems), problems
    assert sitecheck._loc_to_relative("https://yangble5.com/../README.md") is None


def test_no_sitemap_is_not_a_finding(site_copy):
    """Not publishing an index is a legitimate choice. Publishing one that
    lies is not, and only the second is this checker's business."""
    (site_copy / "sitemap.xml").unlink()
    assert sitecheck.sitemap_problems(site_copy) == []


# ── security.txt has to still be true ─────────────────────────────────────


def test_an_expired_security_txt_is_reported(site_copy):
    """RFC 9116 makes Expires mandatory because a stale security contact is
    worse than none: a finder either wastes the report or goes public."""
    import datetime

    problems = sitecheck.wellknown_problems(site_copy, today=datetime.date(2099, 1, 1))
    assert any("Expires" in p and "has passed" in p for p in problems), problems


def test_a_security_txt_without_a_contact_is_reported(site_copy):
    path = site_copy / ".well-known" / "security.txt"
    path.write_text(
        "\n".join(
            ln for ln in path.read_text(encoding="utf-8").splitlines()
            if not ln.startswith("Contact:")
        ),
        encoding="utf-8",
        newline="\n",
    )
    problems = sitecheck.wellknown_problems(site_copy)
    assert any("Contact" in p for p in problems), problems


def test_a_commented_out_field_does_not_count_as_present(site_copy):
    """The file is mostly comment. A `# Contact:` line explaining the field
    must not satisfy the requirement for the field."""
    path = site_copy / ".well-known" / "security.txt"
    path.write_text(
        "# Contact: https://example.invalid/report\n# Expires: 2099-01-01T00:00:00.000Z\n",
        encoding="utf-8",
        newline="\n",
    )
    problems = sitecheck.wellknown_problems(site_copy)
    assert len(problems) == 2, problems


def test_the_published_expiry_is_still_in_the_future():
    """The one clock-dependent rule in this file, asserted directly so the
    failure names the cause instead of arriving as a mysterious red build."""
    assert sitecheck.wellknown_problems() == [], (
        "site/.well-known/security.txt has expired. Renew the date ONLY if the "
        "promise behind it is still true; if nobody is reading the advisory "
        "queue, delete the file instead."
    )


# ── the agent-facing instructions may not drift from the site ─────────────


def _agent_block(marker: str) -> list[str]:
    """The command lines of one agent block on the landing page, as an agent
    reads them: parsed, entities resolved, comments dropped."""
    text = sitecheck.page_text((sitecheck.SITE / "index.html").read_text(encoding="utf-8"))
    start = text.index(marker)
    block = text[start : text.index("\n#", start)]
    return [ln for ln in block.split("\n") if ln.strip()]


@pytest.mark.parametrize(
    "marker",
    (
        "curl -fsSL https://yangble5.com/install.sh -o install.sh",
        "irm https://yangble5.com/install.ps1 -OutFile install.ps1",
    ),
    ids=("unix", "windows"),
)
def test_agents_md_publishes_the_same_command_as_the_landing_page(marker):
    """AGENTS.md is the document an agent acts on, and section 2 tells it to
    refuse any variant of the command carrying flags it did not read there.
    That instruction is only safe while the command in AGENTS.md IS the
    command on the site: a stale copy here would teach an agent to refuse the
    real one. The `throw` message is excluded because it is localised — the
    site is Traditional Chinese and this file is English.
    """
    agents = (sitecheck.SITE / "AGENTS.md").read_text(encoding="utf-8")
    for line in _agent_block(marker):
        if "throw" in line:
            assert "if ($actual -ine $expected) { throw" in agents
            continue
        assert line in agents, (
            f"site/AGENTS.md no longer carries the published command line "
            f"{line!r}. An agent told to refuse anything that differs from "
            f"this file would refuse the real installer."
        )


def test_agents_md_quotes_the_canonical_line_exactly_as_the_pages_do():
    """AGENTS.md tells the reader to compare what it was handed against the
    canonical sentence character by character. That instruction is worth
    nothing if the copy here has drifted from the one on the pages that
    publish it — the agent would be comparing against the wrong string and
    would reject the real one. The page is authoritative; this asserts the
    document agrees with it rather than defining a second source of truth.
    """
    pages = {
        name: sitecheck.page_text((sitecheck.SITE / name).read_text(encoding="utf-8"))
        for name in sitecheck.FILES
    }
    # A sentence ending in the URL, not the bare URL: both appear on the
    # pages, and only the first is the thing a reader is told to compare.
    sentence = re.compile(r"[A-Za-z0-9 ]+ https://yangble5\.com/AGENTS\.md")
    lines = {
        line.strip()
        for text in pages.values()
        for line in text.splitlines()
        if sentence.fullmatch(line.strip())
    }
    assert len(lines) == 1, (
        f"the pages publish {len(lines)} different sentences ending in the "
        f"instruction URL, so there is no canonical line to agree with: {lines}"
    )
    canonical = lines.pop()
    agents = (sitecheck.SITE / "AGENTS.md").read_text(encoding="utf-8")
    assert canonical in agents, (
        f"the pages publish {canonical!r} but site/AGENTS.md does not quote it. "
        f"An agent told to compare its instructions against the canonical "
        f"sentence would be comparing against a string nobody publishes."
    )


def test_agents_md_never_tells_an_agent_to_reveal_the_credential():
    """POST /auth/register accepts a machine id with no other authentication
    and returns the account key, so the full value is a bearer credential and
    stdout is a transcript. The file must not contain an instruction to print
    it, or the paths that hold it."""
    agents = (sitecheck.SITE / "AGENTS.md").read_text(encoding="utf-8")
    for phrase in ("Never print the full machine id", "credentials", "machine-id"):
        assert phrase in agents, f"AGENTS.md no longer warns about {phrase!r}"
    for forbidden in ("cat ~/.yangble5/credentials", "Get-Content ~/.yangble5/credentials"):
        assert forbidden not in agents, (
            f"AGENTS.md contains {forbidden!r} — an agent copies commands out of "
            f"this file, and that one puts the API key into its transcript"
        )


def test_agents_md_refuses_the_endpoint_takeover():
    """The published SHA-256 pins the script, not the invocation. A genuine
    hash-matching installer with a hostile --api is the whole threat, and this
    file is the only place an agent is told about it."""
    agents = (sitecheck.SITE / "AGENTS.md").read_text(encoding="utf-8")
    assert "--allow-nondefault-endpoint" in agents
    assert "never yours to add" in agents.lower()
    assert "pins the" in agents and "not the" in agents


# ── the new rules must be able to turn the self-test red ──────────────────


@pytest.mark.parametrize("break_it", ["claim", "disclosure"])
def test_the_new_silent_cases_can_turn_the_self_test_red(break_it, monkeypatch):
    """These cases print nothing when they pass, exactly like the text cases,
    and for the same reason: site/README.md quotes this program's transcript
    verbatim. This is the only thing standing between them and ornament."""
    assert sitecheck.selftest(verbose=False) is True
    if break_it == "claim":
        monkeypatch.setattr(sitecheck, "CLAIMS", ())
    else:
        monkeypatch.setattr(sitecheck, "DISCLOSURES", ())
    assert sitecheck.selftest(verbose=False) is False, (
        f"emptying {break_it.upper()}S left the self-test green, so it was proving nothing"
    )


# ── the clock the checker reads ────────────────────────────────────────────
#
# On 2026-07-23 this repository was green on a workstation in UTC+8 and red on
# every one of the ten CI runners, which are UTC. Nobody had touched a file.
# `site/sitemap.xml` carried `<lastmod>2026-07-23</lastmod>`, stamped by
# someone for whom that was today, read by a runner for whom it was tomorrow —
# and `sitemap_problems` asked the machine what day it was with
# `datetime.date.today()`, which is LOCAL time.
#
# Four tests failed on ten platforms over identical bytes. A gate whose verdict
# depends on the committer's longitude is not a gate, and the failure mode is
# nasty in the specific way that matters here: it is green where the change is
# written and red where it is reviewed, which is the direction that wastes the
# most time before anyone suspects the clock.


def test_the_checker_never_asks_the_machine_what_day_it_is():
    """No local-time call anywhere under tools/.

    Static, and over the whole directory rather than the one function that
    broke: the next date-dependent rule will be written by someone who never
    read this and will reach for `date.today()` because that is what it is
    called.
    """
    import ast

    offenders = []
    for path in sorted((sitecheck.ROOT / "tools").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            attr = node.func.attr
            if attr == "today":
                offenders.append(f"{path.name}:{node.lineno}: .today() is local time")
            elif attr in ("now", "fromtimestamp") and not (node.args or node.keywords):
                offenders.append(f"{path.name}:{node.lineno}: .{attr}() with no tz is local time")
    assert not offenders, (
        "local-time clock reads under tools/:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse tools/sitecheck.py's _utc_today(), or pass "
        "datetime.timezone.utc explicitly. These functions answer differently "
        "on a workstation and on a UTC runner, so the check they feed is green "
        "where it is written and red where it is reviewed."
    )


def test_utc_today_is_actually_utc():
    """The helper itself, in case someone 'simplifies' it back."""
    import datetime as _dt

    assert sitecheck._utc_today() == _dt.datetime.now(_dt.timezone.utc).date()


def test_the_shipped_sitemap_is_clean_on_a_utc_runner():
    """What CI will compute, asserted here rather than discovered there.

    `sitemap_problems()` with no argument already uses UTC, so this is the same
    question CI asks — but naming it means a future stamp fails with *this*
    sentence attached instead of as four unrelated red tests.
    """
    problems = sitecheck.sitemap_problems(today=sitecheck._utc_today())
    assert problems == [], (
        "site/sitemap.xml is not clean against UTC's today. If a <lastmod> is "
        "the future by more than the one-day grace, it was stamped from a "
        "calendar rather than from the change: write the date the file actually "
        "changed.\n  " + "\n  ".join(problems)
    )


@pytest.mark.parametrize(
    ("stamp", "reported"),
    [
        ("2026-07-23", False),  # exactly today, from the runner's point of view
        ("2026-07-24", False),  # tomorrow in UTC == today for a committer in +08
        ("2026-07-25", True),   # two days out; no offset on Earth reaches this
    ],
)
def test_the_one_day_grace_on_lastmod(site_copy, stamp, reported):
    """The grace exists, and it is exactly one day wide.

    A date-only `<lastmod>` denotes a DAY, not an instant, and days do not line
    up across zones. Without the grace, every commit made during Asian working
    hours stamps a date the UTC runner reads as tomorrow and the build is red
    for nine hours a day. With more than a day of it, a genuinely wrong date
    stops being caught. Both edges are pinned because a tolerance nobody tests
    is a tolerance that quietly becomes infinite.
    """
    import datetime

    path = site_copy / "sitemap.xml"
    path.write_text(
        re.sub(r"<lastmod>[^<]+</lastmod>", f"<lastmod>{stamp}</lastmod>",
               path.read_text(encoding="utf-8"), count=1),
        encoding="utf-8",
        newline="\n",
    )
    problems = sitecheck.sitemap_problems(site_copy, today=datetime.date(2026, 7, 23))
    hit = any(stamp in p and "future" in p for p in problems)
    assert hit is reported, (stamp, problems)


# ── robots.txt: the paths it names have to be reachable ────────────────────
#
# site/robots.txt carries a "what a crawler will actually find" list, and every
# AI agent that touches this domain reads it. It had rotted: it named
# `/README.md`, and https://yangble5.com/README.md has always answered 404.
# Nothing noticed, because the entry is a comment and comments are not
# directives.


def test_the_shipped_robots_txt_names_only_reachable_paths():
    assert sitecheck.robots_problems() == []


def test_the_published_set_is_actually_available():
    """The check must not be able to quietly become a weaker one.

    `robots_problems` compares against `PUBLISHED` in tools/drift_check.py --
    the list of what a visitor can really fetch -- and falls back to "is the
    file in site/?" if that import fails. The fallback PASSES the exact defect
    this was written for: `site/README.md` is on disk and simply never
    deployed. So a failed import turns a working check into a green one that
    proves nothing, which is the failure mode this whole module exists to
    prevent.

    The import is spelled two ways because this module is loaded two ways --
    `python tools/sitecheck.py` puts tools/ on sys.path, pytest imports
    `tools.sitecheck` from the root -- and only one spelling works under each.
    """
    published = sitecheck._published()
    assert published is not None, (
        "tools/sitecheck.py could not import PUBLISHED from tools/drift_check.py, "
        "so robots_problems has silently downgraded to a check that passes on a "
        "file which exists in site/ but is never deployed"
    )
    assert "index.html" in published and "AGENTS.md" in published


@pytest.mark.parametrize(
    ("mutation", "expect"),
    [
        # The exact rot that was live. README.md IS in site/; it is not deployed.
        (("# /verify.html", "# /README.md   the long-form documentation\n# /verify.html"),
         "not published"),
        (("# /AGENTS.md", "# /nope.md"), "not published"),
        (("Sitemap: https://yangble5.com/sitemap.xml", ""), "no `Sitemap:` line advertises it"),
        (("https://yangble5.com/sitemap.xml", "https://example.invalid/sitemap.xml"),
         "not a URL on this site"),
    ],
)
def test_robots_problems_reports_each_way_it_can_rot(site_copy, mutation, expect):
    old, new = mutation
    path = site_copy / "robots.txt"
    text = path.read_text(encoding="utf-8")
    assert old in text, f"the fixture no longer contains {old!r}; this test is mutating nothing"
    path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")

    problems = sitecheck.robots_problems(site_copy)
    assert any(expect in p for p in problems), (expect, problems)


# ── the Windows execution policy, which blocks the verified path only ──────
#
# `Restricted` is the out-of-box default on Windows client editions and it
# blocks `powershell -File script.ps1` -- the last line of AGENTS.md section 5,
# reached only AFTER the download and the SHA-256 check have both succeeded.
# Measured 2026-07-23: `-File` exits 1 with "running scripts is disabled on this
# system"; the same code via `-Command`, and via `irm ... | iex`, both run fine.
#
# That asymmetry is the danger. The policy blocks the path that can be
# verified against a published digest and waves through the path that cannot,
# so an agent that improvises its way past the error lands on the unverified
# one -- which is the single outcome this whole document exists to prevent.


def test_agents_md_names_the_execution_policy_failure():
    agents = (sitecheck.SITE / "AGENTS.md").read_text(encoding="utf-8")
    assert "running scripts is disabled on this system" in agents, (
        "AGENTS.md no longer quotes the exact error an agent will see on a stock "
        "Windows box. Without the literal string it cannot match what it is looking at"
    )
    assert "It is not a hash failure" in agents, (
        "the guidance no longer distinguishes this from a digest mismatch, which is "
        "the other reason section 5 tells an agent to stop"
    )


def test_agents_md_forbids_the_fallback_that_drops_the_digest_check():
    """`iex` runs under Restricted. That is why it must be named and refused."""
    agents = (sitecheck.SITE / "AGENTS.md").read_text(encoding="utf-8")
    lowered = agents.lower()
    assert "do not fall back to the `irm" in lowered, (
        "AGENTS.md no longer names the iex fallback as the thing not to do"
    )
    assert "never checked and cannot be" in agents, (
        "AGENTS.md no longer says WHY the iex fallback is wrong. 'Do not' without "
        "'because' is the kind of rule that gets optimised away"
    )


def test_agents_md_does_not_claim_the_project_never_uses_the_bypass_flag():
    """It did, and the project's own installer contradicted it.

    The rule against adding `-ExecutionPolicy Bypass` is right; the REASON given
    was false. `site/install.sh` prints that exact flag to Windows users, and
    `site/install.ps1` uses it in the uninstall line. An agent that reads the
    justification, then reads install.sh, learns that this file states things
    that are not so -- which is corrosive to every other rule in it.

    Checked against install.sh rather than pinned as a string, so that if the
    installer ever stops printing the flag, this test says the wording may now
    be safe again instead of silently guarding a fact that changed.
    """
    agents = (sitecheck.SITE / "AGENTS.md").read_text(encoding="utf-8")
    install_sh = (sitecheck.SITE / "install.sh").read_text(encoding="utf-8")
    install_ps1 = (sitecheck.SITE / "install.ps1").read_text(encoding="utf-8")

    flag = "-ExecutionPolicy Bypass"
    project_uses_it = flag in install_sh or flag in install_ps1
    assert project_uses_it, (
        "neither published installer mentions -ExecutionPolicy Bypass any more. "
        "AGENTS.md's careful wording about 'this project's own install.sh prints "
        "it' is now stale -- re-read section 8 and simplify it."
    )
    assert "the published Windows command does not" not in agents, (
        "AGENTS.md is claiming the published Windows command never uses "
        "-ExecutionPolicy Bypass. site/install.sh prints it at least once, so the "
        "claim is false and an agent can catch this file being wrong"
    )


def test_agents_md_uninstall_covers_windows():
    """§11 used to give two POSIX commands and nothing else.

    Verified by running the real Windows installer on 2026-07-23: it writes
    ``~/.yangble5/uninstall.ps1``. ``yangble5-uninstall`` is a shell launcher
    that install.sh writes and install.ps1 does not, and ``sh`` is not on a
    stock Windows box either. A compliant agent on Windows had no uninstall
    path at all -- and the one the installer's own closing screen prints
    carries ``-ExecutionPolicy Bypass``, which §8 forbids the agent to add.
    """
    agents = (sitecheck.SITE / "AGENTS.md").read_text(encoding="utf-8")
    section = agents[agents.index("## 11. Uninstall") :]
    assert "uninstall.ps1" in section, "§11 still has no Windows uninstall path"
    assert "yangble5-uninstall" in section, "§11 lost the POSIX launcher"

    ps1 = (sitecheck.SITE / "install.ps1").read_text(encoding="utf-8")
    assert "uninstall.ps1" in ps1, (
        "install.ps1 no longer writes uninstall.ps1, so §11's Windows command "
        "names a file that is never created -- which is the defect this test "
        "was written for, in the other direction"
    )


def test_agents_md_uninstall_does_not_lead_with_the_flag_section_8_forbids():
    """§11's headline was `yangble5-uninstall --yes`, framed as *the* command.

    §8 says never run the uninstaller with ``--yes`` unprompted: run it bare,
    show the list, wait. §11 mentioned that as something to do "if the user
    wants to see the list" -- optional, in the section an agent copies from.
    Two rules, one mandatory and one optional, about the same destructive act.
    """
    agents = (sitecheck.SITE / "AGENTS.md").read_text(encoding="utf-8")
    section = agents[agents.index("## 11. Uninstall") :]
    first_block = section[section.index("```") + 3 :]
    first_block = first_block[: first_block.index("```")]
    lines = [ln.strip() for ln in first_block.splitlines() if ln.strip()]
    assert lines, "§11 has no command block"
    assert "--yes" not in lines[0] and "-Yes" not in lines[0], (
        f"§11's first command is {lines[0]!r}, which skips the confirmation §8 "
        "makes mandatory. The bare form has to come first: it is the one that "
        "prints the list of paths before anything is deleted."
    )


# ── the figure guard must see <meta>, not just the body ────────────────────
#
# The meta description and og:/twitter: cards are the strings that travel
# DETACHED from their evidence: into a search result, into another model's
# summary of this site. A fabricated "99.53%" or a bare "1M" there is the worst
# place for an unguarded number -- and the checker was blind to all of them,
# because their text lives in a `content` attribute that tag-stripping dropped.


def test_page_text_includes_meta_and_og_content():
    src = (
        '<!doctype html><html><head>'
        '<meta name="description" content="body says one thing, meta says 91.11%">'
        '<meta property="og:description" content="and og says 3,141,592 tokens">'
        "</head><body>nothing numeric here</body></html>"
    )
    text = sitecheck.page_text(src)
    assert "91.11" in text, "meta description is invisible to the figure scanner"
    assert "3,141,592" in text, "og:description is invisible to the figure scanner"


def test_a_bogus_figure_in_meta_description_is_caught():
    """The whole point: an unaccounted number in meta must fail like one in body."""
    src = (
        '<!doctype html><html><head>'
        '<meta name="description" content="measured cache hit rate 91.11%">'
        "</head><body>ordinary copy</body></html>"
    )
    problems = sitecheck.check_text("index.html", src, set())
    assert any("91.11" in p for p in problems), (
        "a fabricated percentage placed only in the meta description passed the "
        "figure guard. That is the string search engines and other models quote, "
        "so it is the one that most needs guarding."
    )


def test_the_real_meta_description_carries_its_evidence():
    """The live meta description states 748,918; the guard now sees it, so it
    must also be able to account for it -- i.e. the shipped page is clean with
    meta included, not merely clean because meta was ignored."""
    src = (sitecheck.SITE / "index.html").read_text(encoding="utf-8")
    # In the meta description specifically, not merely somewhere on the page.
    import re

    metas = re.findall(r'<meta[^>]+content="([^"]*)"', src)
    meta_blob = " ".join(metas)
    assert "748,918" in meta_blob, (
        "the meta description no longer carries the 748,918 evidence figure; if it "
        "was replaced by a bare '1M' or similar, that is the drift this guards"
    )
    assert "748,918" in sitecheck.page_text(src), "page_text dropped the meta figure"
    # And the whole checker, run the way main() runs it, is still green -- proven
    # by the separate CLI self-test; here we assert the figure is under guard,
    # not ignored.
