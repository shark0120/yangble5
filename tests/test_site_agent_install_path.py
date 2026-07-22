"""What an AI agent actually sees when it reads yangble5.com.

An agent does not run JavaScript. It fetches the HTML and reads the text. So
every one of these tests looks at the page the way `tools/sitecheck.py` does --
through the same HTML parser, with `<script>` and `<style>` content dropped --
and asserts things about *that* text, not about the file as a whole.

The defects these lock down all had the same shape: the page was correct for a
human in a browser and silently wrong for the reader it was explicitly written
for.

  * The Windows command existed only inside a JS object literal, so the
    script-less text offered a Windows agent the POSIX `curl … | sh` line.
  * The tab labelled 'paste into Claude Code / Codex' emitted the identical
    `curl … | sh` and told the agent to just run it; `--dry-run` appeared
    nowhere on the page.
  * The 'verify it yourself' block computed a hash, compared it to nothing,
    and then ran the installer anyway.
  * The advertised one-liners discard argv, and no page text showed the forms
    that do not.
  * The capacity widget read only /health, whose accepting_requests is blind
    to the daily pool and the operator reserve.
  * The page claimed 'no third-party requests' while the edge attached NEL
    headers pointing at a.nel.cloudflare.com.
"""

from __future__ import annotations

import re

import pytest

from tools import sitecheck

PAGES = ("index.html", "verify.html")


def _raw(name: str) -> str:
    return (sitecheck.SITE / name).read_text(encoding="utf-8")


def _text(src: str) -> str:
    """The page as an agent reads it: parsed, entities resolved, no script."""
    d = sitecheck.Doc()
    d.feed(src)
    d.close()
    return "\n".join(d.text)


def _script(src: str) -> str:
    """Only the inline script bodies."""
    p = sitecheck._InlineScripts()
    p.feed(src)
    p.close()
    return "\n".join(p.out)


def _code(src: str) -> str:
    """The inline script with comments removed.

    Searching the raw script is how a test passes on prose: the comment that
    explains why `reserve_engaged` matters contains the string
    `reserve_engaged`, so an assertion over the raw text stays green after the
    line that reads the field is deleted. Only executable text counts.
    """
    js = re.sub(r"/\*.*?\*/", " ", _script(src), flags=re.S)
    return re.sub(r"(?m)//.*$", " ", js)


@pytest.fixture(scope="module")
def text():
    return {n: _text(_raw(n)) for n in PAGES}


@pytest.fixture(scope="module")
def raw():
    return {n: _raw(n) for n in PAGES}


# ── F2: the Windows installer must exist in script-less text ───────────────

def test_windows_installer_is_visible_without_javascript(text):
    """`install.ps1` used to appear 0 times in the page's script-less text."""
    assert "install.ps1" in text["index.html"], (
        "index.html's script-stripped text does not mention install.ps1. A "
        "Windows agent reading the served HTML is handed the POSIX "
        "`curl … | sh` line, which fails in PowerShell and, in Git Bash, runs "
        "and dies at the installer's own platform check.")
    assert "irm https://yangble5.com/install.ps1" in text["index.html"]
    assert "curl -fsSL https://yangble5.com/install.sh" in text["index.html"]


def test_no_shell_command_lives_only_inside_the_script(raw):
    """The class rule: a command string must never be reachable only via JS.

    `CMD[state.os]` was the whole defect. Rendering happens by hiding rows that
    are already in the markup, so the script may not carry command text at all.
    """
    forbidden = ("curl -fsSL", "irm https://", "| sh", "| iex", "shasum")
    for name in PAGES:
        js = _code(raw[name])
        for needle in forbidden:
            assert needle not in js, (
                f"{name}: the inline script contains the command fragment "
                f"{needle!r}. Anything only the script can produce is invisible "
                f"to an agent that does not execute JavaScript.")


# ── F3: the agent path must differ from the terminal path ──────────────────

def _rows(src: str) -> dict[tuple[str, str], str]:
    """(target, os) -> command text, straight out of the static markup."""
    out = {}
    for m in re.finditer(
        r'<div class="cmd-row" data-cmd-target="([a-z]+)" data-cmd-os="([a-z]+)">'
        r"\s*<pre[^>]*>(.*?)</pre>", src, re.S
    ):
        out[(m.group(1), m.group(2))] = _text(m.group(3))
    return out


def test_hiding_a_row_actually_hides_it(raw):
    """`.cmd-row` sets `display:flex`, which beats the UA sheet's
    `[hidden]{display:none}`. Without an explicit override the script's
    row-hiding is a no-op and all four blocks stack up on the homepage."""
    src = raw["index.html"]
    assert re.search(r"\.cmd-row\[hidden\]\s*\{[^}]*display\s*:\s*none", src), (
        "no rule makes [hidden] win over .cmd-row{display:flex}")
    js = _code(src)
    assert re.search(r"row\.hidden\s*=", js), "render() no longer hides rows"
    assert "data-cmd-target" in js and "data-cmd-os" in js, (
        "render() must key off both axes; hiding on one axis leaves the wrong "
        "OS or the wrong paste-target visible")


def test_all_four_command_rows_are_static(raw):
    rows = _rows(raw["index.html"])
    assert set(rows) == {("agent", "unix"), ("agent", "win"),
                         ("term", "unix"), ("term", "win")}, sorted(rows)


@pytest.mark.parametrize("os_", ["unix", "win"])
def test_agent_row_is_not_the_terminal_row(raw, os_):
    rows = _rows(raw["index.html"])
    agent, term = rows[("agent", os_)], rows[("term", os_)]
    assert agent.strip() != term.strip(), (
        f"the agent tab and the terminal tab emit identical text for {os_}. "
        f"The site's designated AI path is then unreviewable remote code "
        f"execution: fetch a remote script and pipe it into an interpreter.")
    assert "| sh\n" not in agent + "\n" and "| iex" not in agent, (
        f"the {os_} agent row still pipes a remote script into an "
        f"interpreter:\n{agent}")


@pytest.mark.parametrize(
    "os_,needles",
    [("unix", ("install.sh.sha256", "shasum -a 256 -c", "--dry-run")),
     ("win", ("install.ps1.sha256", "Get-FileHash", "-DryRun", "throw"))],
)
def test_agent_row_verifies_then_dry_runs(raw, os_, needles):
    agent = _rows(raw["index.html"])[("agent", os_)]
    for n in needles:
        assert n in agent, (
            f"the {os_} agent sequence is missing {n!r}; it must download, "
            f"check the published digest, and dry-run before anything is "
            f"installed. Got:\n{agent}")


def test_dry_run_is_visible_in_the_page_text(text):
    """The single safest action an agent can take must not be invisible."""
    assert "--dry-run" in text["index.html"] and "-DryRun" in text["index.html"]
    assert "--dry-run" in text["verify.html"] and "-DryRun" in text["verify.html"]


def test_no_page_text_tells_an_agent_to_just_run_the_one_liner(text):
    """The old hint said 'paste this line straight to your agent, it will
    execute it for you and complete the setup'."""
    assert "它會替你執行並完成設定" not in text["index.html"]


# ── F4: computing a hash is not checking a hash ────────────────────────────

def test_every_hashing_block_actually_checks(raw):
    """`shasum -a 256 install.sh` followed by `sh install.sh` is a hash
    computation and then an unconditional execution."""
    for name in PAGES:
        for m in re.finditer(r"<pre[^>]*>(.*?)</pre>", raw[name], re.S):
            block = _text(m.group(1))
            if "shasum" not in block:
                continue
            assert "shasum -a 256 -c" in block, (
                f"{name}: a block computes a SHA256 but never compares it to "
                f"the published digest, so an agent that follows it will "
                f"report 'verified' having verified nothing:\n{block}")


def test_no_block_pipes_a_pager(raw):
    """`less` in a non-tty agent shell either dumps the whole file into the
    transcript or blocks forever waiting for a keypress."""
    for name in PAGES:
        assert not re.search(r"\bless install\.(sh|ps1)\b", _text(raw[name])), (
            f"{name}: still tells the reader to page the installer with less")


def test_the_homepage_verify_block_is_the_checking_form(raw):
    block = re.search(r'<pre id="verifyCmd">(.*?)</pre>', raw["index.html"], re.S)
    assert block, "the homepage 'check it yourself' block is gone"
    body = _text(block.group(1))
    assert "install.sh.sha256" in body and "shasum -a 256 -c" in body
    assert "--dry-run" in body
    assert not re.search(r"^sh install\.sh$", body, re.M), (
        "the block still ends by executing the installer unconditionally")


# ── F5: the installers take arguments; the page must say how ───────────────

@pytest.mark.parametrize(
    "needle",
    ["sh -s --", "--email", "--invite", "scriptblock]::Create",
     "-Email", "-Invite", "YANGBLE5_EMAIL", "YANGBLE5_INVITE"],
)
def test_argument_passing_forms_are_published(text, needle):
    assert needle in text["index.html"], (
        f"{needle!r} appears nowhere in index.html's script-less text. "
        f"`curl … | sh` gives the script no argv and `irm … | iex` gives the "
        f"scriptblock no parameters, so an agent that interviews the user has "
        f"no documented way to pass the answers on.")


def test_the_page_says_the_installer_never_asks(text):
    assert "不會問你任何問題" in text["index.html"] or "不會停下來問" in text["index.html"]


# ── F6: the capacity widget must ask the endpoint that knows ───────────────

def test_pool_status_is_tried_before_health(raw):
    m = re.search(r"var POOL_ENDPOINTS = \[(.*?)\]", raw["index.html"], re.S)
    assert m, "POOL_ENDPOINTS is gone"
    eps = re.findall(r'"([^"]+)"', m.group(1))
    assert eps[0] == "/pool/status", (
        f"/health's accepting_requests is computed from the monthly cap alone: "
        f"it is true while the daily pool is spent, while the operator reserve "
        f"is engaged, and while the single upstream account is quota'd. "
        f"/pool/status ANDs in the daily verdict and reports reserve_engaged, "
        f"so it must be asked first. Order was {eps}")
    assert "/health" in eps, "the /health fallback was dropped"


def test_the_widget_reads_reserve_engaged(raw):
    js = _code(raw["index.html"])
    assert "d.reserve_engaged" in js, (
        "reserve_engaged is never read, so the page shows a green 'accepting' "
        "light while every non-operator key is being refused with "
        "operator_reserve_engaged")
    assert re.search(r"accepting\s*=[^;]*reserv", js), (
        "reserve_engaged is read but does not feed the accepting verdict")


def test_the_widget_reads_both_reset_spellings(raw):
    js = _code(raw["index.html"])
    for spelling in ("reset_at", "resets_at"):
        assert re.search(r"\.\s*" + spelling + r"\b", js), (
            f"nothing reads .{spelling}; the gateway spells it reset_at and "
            f"README.md documented resets_at, so reading one spelling renders "
            f"a present timestamp as 未提供")


def test_the_page_discloses_what_the_green_light_cannot_see(text):
    t = text["index.html"]
    assert "/pool/status" in t, "the page never names the endpoint it reads"
    for needle in ("上游", "當日池", "保留額"):
        assert needle in t, (
            f"the status section does not disclose {needle!r}: a visitor is "
            f"entitled to know that 'accepting' does not cover it")


# ── F1: the third-party-request claim must match the served headers ────────

def test_no_unqualified_no_third_party_requests_claim(text):
    """The edge attaches Report-To / NEL headers naming a.nel.cloudflare.com.
    Any page that claims otherwise is falsifiable with one devtools open --
    on a page whose whole argument is 'verify us rather than trust us'."""
    for name in PAGES:
        t = text[name]
        if "第三方請求" not in t:
            continue
        assert "a.nel.cloudflare.com" in t, (
            f"{name} makes a claim about third-party requests without "
            f"disclosing the Cloudflare NEL reporting endpoint the edge "
            f"attaches to every response.")
        assert "Nel" in t and "Report-To" in t, (
            f"{name} does not name the actual headers, so a reader cannot "
            f"check the claim against what the browser shows them.")


def test_the_disclosure_says_it_is_the_edge_not_the_file(text):
    t = text["index.html"]
    assert "Cloudflare" in t
    assert "邊緣" in t, (
        "the disclosure must distinguish what the HTML does from what the CDN "
        "edge adds; otherwise it reads as an admission the page tracks you")


# ── the page and the gateway must agree on what remaining_pct MEANS ────────

def test_capacity_reader_and_gateway_agree_on_the_unit():
    """`remaining_pct` is a fraction on both sides, and the page scales it.

    This has to read BOTH files, because neither side is wrong on its own.
    The gateway documents 0.0-1.0 and a full pool sends 1.0; the page used to
    accept anything in [0,100] and render it unscaled, so a full pool showed
    "剩餘 1%" in red under a green "服務運作中". No test of one file could
    see it, and no input could reveal it either -- 1.0 is legal under both
    readings. The invariant is the AGREEMENT, so that is what is asserted.
    """
    app = (sitecheck.ROOT / "gateway" / "app.py").read_text(encoding="utf-8")
    assert re.search(r"remaining_pct:\s*float\s*#\s*0\.0-1\.0", app), (
        "gateway/app.py no longer declares remaining_pct as a 0.0-1.0 "
        "fraction; site/index.html readPct() scales by 100 on that basis"
    )

    reader = re.search(r"function readPct\(d\)\s*\{.*?\n  \}", _raw("index.html"), re.S)
    assert reader, "readPct() is gone from index.html"
    body = reader.group(0)

    assert "v <= 1)" in body, (
        "readPct accepts values above 1, so it cannot be reading a fraction; "
        "a full pool (1.0) would render as 1% and be painted red"
    )
    assert "v * 100" in body, "readPct returns the fraction unscaled"
    assert "remaining_percent" not in body, (
        "readPct accepts a `..._percent` field alongside `..._pct`. Those are "
        "different units and the value cannot distinguish them, which is how "
        "the original defect survived"
    )


# ── the pages still pass every existing static check ───────────────────────

def test_pages_remain_structurally_clean():
    used: set[str] = set()
    problems: list[str] = []
    for name in PAGES:
        problems += [f"{name}: {p}" for p in sitecheck.check_page(
            name, _raw(name), used)]
    assert problems == [], "\n".join(problems)
