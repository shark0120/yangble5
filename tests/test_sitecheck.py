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
