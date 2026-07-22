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

THE LINE
--------
The site now publishes a single sentence a person pastes into Claude Code or
Codex, which points the agent at an instruction document on this same origin.
Its security property is not secrecy or cleverness -- it is COMPARABILITY. The
published SHA256 pins the *script* and says nothing about the *command line*,
so a byte-identical, hash-matching install.sh invoked with a hostile `--api`
registers the user with the attacker, writes the attacker's key into
~/.yangble5/credentials, and repoints ANTHROPIC_BASE_URL -- with every
integrity check reporting success throughout. An agent is handed one-liners by
READMEs, blog posts and chat messages, so the only defence that survives is a
canonical string short enough, and invariant enough, that a non-programmer can
tell a variant apart from it at a glance.

Everything below follows from that: the line is one string (not one per OS),
it lives in the served markup rather than behind a tab or a script, it carries
no flag and no endpoint and no key, every copy of it on the site is byte
identical, and the pages state in plain language both the refusal rule and the
fact that hash verification cannot see any of this.
"""

from __future__ import annotations

import re

import pytest

from tools import sitecheck

PAGES = ("index.html", "verify.html")

# The canonical string, spelled out here rather than scraped from the page.
# A test that reads its expectation out of the artefact under test cannot fail:
# append `--api https://evil.tld` to the <pre> and a scraping test would simply
# adopt the new value and stay green. This constant is the second copy that
# makes the comparison real, and changing it is a deliberate act.
CANONICAL_LINE = "Install yangble5 by following https://yangble5.com/AGENTS.md"

# The distinctive opening, used to find every *candidate* line on a page --
# including a corrupted one. Matching on the prefix rather than the whole
# string is what lets these tests see a variant instead of missing it.
LINE_PREFIX = "Install yangble5 by following"


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


# ── Cloudflare rewrites the pages; the commands must survive it ─────────────

@pytest.mark.parametrize("page", PAGES)
def test_every_email_in_a_command_block_is_guarded(page):
    """Cloudflare Email Address Obfuscation corrupts the published commands.

    Observed on the live site 2026-07-22, after a deploy whose bytes were
    verified correct on the origin: `--email you@example.com` inside a <pre>
    was served as

        --email <a href="/cdn-cgi/l/email-protection" class="__cf_email__"
                   data-cfemail="...">[email&#160;protected]</a>

    so every visitor -- and every AI agent -- copying the install command got a
    broken one. It also injects a /cdn-cgi/ resource the CSP does not pin, and
    it makes byte-level verification of a deployed page impossible.

    `<!--email_off-->` disables the rewrite for a region. Doing it in the page
    rather than in the Cloudflare dashboard keeps the fix in version control,
    where it cannot be switched off by someone who does not know what it
    protects. The zone setting is not visible from here and a dashboard toggle
    leaves no trace in the repo, so this test is what holds the property.
    """
    src = _raw(page)
    for block in re.findall(r"<pre\b[^>]*>.*?</pre>", src, re.S):
        for addr in re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", block):
            guarded = f"<!--email_off-->{addr}<!--/email_off-->"
            assert guarded in block, (
                f"{page}: {addr!r} sits in a copyable <pre> unguarded. "
                "Cloudflare will rewrite it into an obfuscation link and the "
                f"published command becomes wrong. Wrap it: {guarded}"
            )


# ══════════════════════════════════════════════════════════════════════════
# THE LINE
# ══════════════════════════════════════════════════════════════════════════


def _candidate_lines(text: str) -> list[str]:
    """Every string on the page that *starts like* the canonical line.

    Deliberately matches the prefix and then takes the rest of the line, so a
    copy that has grown ` --api https://evil.tld` is RETURNED (and compared
    against the constant), not filtered out.
    """
    return [m.group(0).strip() for m in re.finditer(LINE_PREFIX + r"[^\n]*", text)]


# ── the string itself ──────────────────────────────────────────────────────

def test_the_line_has_no_flag_no_endpoint_and_no_key():
    """The shape is the security property, so it is asserted structurally.

    Anything an attacker would want to add -- a flag, a second URL, an
    assignment, a pipe, a substitution, a quote -- needs a character this
    pattern does not permit. A future edit that "just adds --yes-register so
    it works unattended" fails here, which is the point: that flag is exactly
    what the installer refuses to let an agent add on its own.
    """
    assert re.fullmatch(
        r"[A-Za-z0-9 ]+ https://yangble5\.com/AGENTS\.md", CANONICAL_LINE
    ), (
        f"the canonical line is no longer a plain sentence ending in the one "
        f"instruction URL: {CANONICAL_LINE!r}. Flags, endpoints, keys, pipes, "
        f"quotes, environment assignments and shell metacharacters are all "
        f"excluded on purpose -- they are what a hostile variant adds."
    )
    for forbidden in ("--", " -", "|", "$", "&", ";", "`", "'", '"', "=", "\\"):
        assert forbidden not in CANONICAL_LINE, (
            f"the canonical line contains {forbidden!r}; it must carry nothing "
            f"an attacker would benefit from varying"
        )


def test_the_line_is_short_enough_to_retype():
    """A reader who cannot copy-paste must still be able to reproduce it, and
    a reader who can must be able to eyeball the whole thing at once. Both
    fail once it wraps."""
    assert len(CANONICAL_LINE) <= 72, (
        f"the canonical line is {len(CANONICAL_LINE)} characters. Past ~72 it "
        f"wraps in a terminal and in most chat clients, and a string a person "
        f"cannot see all of in one row is a string nobody compares."
    )


def test_the_line_names_no_host_but_the_instruction_host():
    urls = re.findall(r"https?://[^\s]+", CANONICAL_LINE)
    assert urls == ["https://yangble5.com/AGENTS.md"], (
        f"the canonical line must name exactly one URL, the instruction "
        f"document on the origin that also serves the installer and its "
        f"digest. Found {urls}."
    )


# ── how it appears on the pages ────────────────────────────────────────────

def test_the_line_is_in_the_served_text_of_both_pages(text):
    """An agent does not run JavaScript, and a cautious reader lands on
    verify.html rather than the homepage. Both have to carry it, because
    comparison is impossible against a string you have to navigate to find."""
    for name in PAGES:
        assert CANONICAL_LINE in text[name], (
            f"{name}: the canonical agent line is missing from the "
            f"script-less text. Expected verbatim:\n  {CANONICAL_LINE}"
        )


@pytest.mark.parametrize("page", PAGES)
def test_every_copy_of_the_line_is_byte_identical(page, text):
    """Two copies that differ silently defeat the whole mechanism.

    This is the test that would have caught the realistic failure: somebody
    updates the homepage and not verify.html, and the page whose entire job is
    'compare what you were handed against this' publishes the stale one.
    """
    found = _candidate_lines(text[page])
    assert found, f"{page}: nothing on the page starts with {LINE_PREFIX!r}"
    for got in found:
        assert got == CANONICAL_LINE, (
            f"{page}: a copy of the agent line differs from the canonical "
            f"string.\n  canonical: {CANONICAL_LINE}\n  on page:   {got}\n"
            f"Every published copy must be byte identical; a reader compares "
            f"against whichever one they happened to open."
        )


def test_there_is_exactly_one_line_across_the_whole_site(text):
    """One string, not one per OS, not one per audience. The tabbed card is
    allowed to fork by OS because those are shell commands a reader runs; a
    canonical string that forks is a canonical string with two right answers,
    and then 'this is not ours' stops being checkable."""
    variants = {ln for name in PAGES for ln in _candidate_lines(text[name])}
    assert variants == {CANONICAL_LINE}, (
        f"the site publishes more than one agent line: {sorted(variants)}"
    )


def test_the_line_is_not_hidden_behind_a_tab(raw):
    """`.cmd-row`s are hidden by the script until their tab is chosen. A
    canonical string inside one is invisible to a reader on the wrong tab and,
    worse, invisible in a screenshot somebody uses to check a variant."""
    src = raw["index.html"]
    at_line = src.index('id="agentLine"')
    first_row = src.index("data-cmd-target")
    assert at_line < first_row, (
        "the canonical line has moved inside (or below) the tabbed command "
        "card. It must sit above it, outside any row the script can hide."
    )
    block = re.search(r"<pre[^>]*id=\"agentLine\"[^>]*>(.*?)</pre>", src, re.S)
    assert block, "the <pre id=\"agentLine\"> block is gone"
    assert "hidden" not in block.group(0), "the canonical line is hidden"


def test_the_line_is_not_a_shell_command(raw):
    """It is handed to an agent, not to an interpreter. Nothing in the block
    may look runnable: the moment it does, somebody appends to it."""
    block = re.search(r"<pre[^>]*id=\"agentLine\"[^>]*>(.*?)</pre>", raw["index.html"], re.S)
    body = _text(block.group(1)).strip()
    assert body == CANONICAL_LINE, (
        f"the homepage block is not exactly the canonical line:\n{body!r}"
    )
    for shellish in ("curl", "irm", "|", "sh -s", "iex", "&&"):
        assert shellish not in body, (
            f"the canonical line block contains {shellish!r}; it must not be, "
            f"or resemble, something an agent pipes into an interpreter"
        )


# ── what the page has to SAY about variants ────────────────────────────────

def test_the_page_states_the_canonical_rule_in_plain_language(text):
    """'Compare it character for character' is the entire user-facing defence.
    If the page does not say it, the string is just decoration."""
    t = text["index.html"]
    for needle in ("逐字", "不是我們的"):
        assert needle in t, (
            f"index.html never tells the reader {needle!r}: that a line which "
            f"is not character-for-character identical to the published one is "
            f"not ours. Without that sentence the canonical string has no "
            f"instructions for use."
        )


@pytest.mark.parametrize(
    "flag",
    ["--api", "--allow-nondefault-endpoint", "YANGBLE5_API", "--yes-register"],
)
def test_the_refusable_flags_are_named_on_the_page(text, flag):
    """An agent cannot refuse a category it was never told about. These four
    are the ones that change WHERE the user's prompts go, or that consent to
    an account on the user's behalf -- install.sh refuses to let an agent add
    the last three itself, and says so in its own --help."""
    assert flag in text["index.html"], (
        f"{flag!r} appears nowhere in index.html's script-less text. The page "
        f"must name the flags an agent is required to refuse, by their exact "
        f"spelling, or the refusal rule is unactionable."
    )


def test_the_page_tells_the_agent_to_say_why_it_refused(text):
    """A silent decline is indistinguishable from 'it did not work', so the
    user never learns that someone tried to redirect their prompts."""
    t = text["index.html"]
    assert "默默不做" in t, (
        "the page does not tell the agent to state its reason out loud when it "
        "refuses a variant. Refusing quietly hides the attack from the only "
        "person who can act on it."
    )


# ── the honesty the whole design turns on ──────────────────────────────────

@pytest.mark.parametrize("page", PAGES)
@pytest.mark.parametrize(
    "needle",
    ["--api", "--allow-nondefault-endpoint", "ANTHROPIC_BASE_URL",
     "~/.yangble5/credentials"],
)
def test_both_pages_disclose_that_the_digest_does_not_pin_the_command(page, text, needle):
    """The single most important true thing this site can say.

    A page that publishes a SHA256 and stops there teaches the reader that a
    matching digest means safe. It does not: the digest pins the script and is
    blind to the command line. These four tokens can only co-occur in a
    passage that spells the mechanism out -- the flag, the consent flag it
    needs, the variable that redirects every future session, and the file the
    attacker's key lands in.
    """
    assert needle in text[page], (
        f"{page} no longer names {needle!r}. Both pages must explain that a "
        f"hash-matching installer invoked with a hostile endpoint registers "
        f"the user with the attacker and repoints every later session, while "
        f"every published integrity check keeps reporting success."
    )


def test_verify_page_does_not_call_the_homepage_line_a_pipeline(text):
    """verify.html used to open with '首頁那一行是 curl … | sh'. That became
    false the moment the homepage led with a sentence for an agent, and a
    verification page whose first paragraph is wrong about what it is
    verifying is worse than no page."""
    assert "首頁那一行是" not in text["verify.html"], (
        "verify.html still describes the homepage as offering a single "
        "`curl … | sh` line. The homepage now leads with the agent line and "
        "keeps the pipeline as the alternative; say both."
    )


# ── the demonstration of the attack must not be usable AS the attack ───────

def test_the_attack_demonstration_is_inert(raw):
    """Showing the shape is necessary -- a reader who has never seen it cannot
    recognise it. Publishing a working one is not."""
    src = raw["index.html"]
    blocks = [m.group(0) for m in re.finditer(r"<pre\b[^>]*>.*?</pre>", src, re.S)
              if "--api http" in _text(m.group(0))]
    assert len(blocks) == 1, (
        f"expected exactly one <pre> demonstrating a hostile --api, found "
        f"{len(blocks)}. Each additional one is another string somebody can "
        f"copy by accident."
    )
    block = blocks[0]
    body = _text(block)

    assert "不要執行" in body, (
        "the hostile-endpoint demonstration does not carry a do-not-run "
        "warning INSIDE the block. The warning has to travel with the text, "
        "because what gets copied is the block, not the paragraph above it."
    )
    hosts = re.findall(r"--api\s+https?://([^\s]+)", body)
    assert hosts and all(h.endswith(".example") for h in hosts), (
        f"the demonstration points at {hosts}. It must use a .example host: "
        f"RFC 2606 reserves that suffix so the string can never resolve, no "
        f"matter who reads the page or how carelessly they paste it."
    )
    assert 'data-copy-target="attackShape"' not in src, (
        "the hostile-endpoint demonstration has a copy button. Every other "
        "command block on this page exists to be copied; this one exists to "
        "be recognised, and giving it the same affordance is how it ends up "
        "in somebody's terminal."
    )


# ── secrets must never be routed through an agent transcript ───────────────

_SECRET_PRINTERS = (
    r"\b(?:cat|less|more|head|tail|type)\b[^\n]*(?:credentials|machine-id)",
    r"Get-Content[^\n]*(?:credentials|machine-id)",
    r"\becho\b[^\n]*(?:YANGBLE5_API_KEY|FINGERPRINT)",
)


@pytest.mark.parametrize("page", PAGES)
def test_no_command_block_prints_a_secret(page, raw):
    """stdout is the agent's transcript.

    ~/.yangble5/credentials holds the API key. ~/.yangble5/machine-id holds the
    32-byte salt the fingerprint is derived from, and the fingerprint is itself
    a bearer credential: POST /auth/register accepts it with no other
    authentication and hands back the same key. A block that pages either of
    them into a conversation has leaked the account.
    """
    for block in re.findall(r"<pre\b[^>]*>.*?</pre>", raw[page], re.S):
        body = _text(block)
        for pattern in _SECRET_PRINTERS:
            m = re.search(pattern, body, re.I)
            assert not m, (
                f"{page}: a copyable block prints a secret into what, for an "
                f"AI agent, is the conversation transcript: {m.group(0)!r}"
            )


def test_the_page_explains_why_the_machine_id_is_truncated(text):
    """The 12-character truncation looks like formatting. It is not, and a
    reader who thinks it is will happily ask the agent for the full value."""
    t = text["index.html"]
    assert "truncated" in t, (
        "the page never shows what the installer actually prints, so the "
        "truncation reads as an accident"
    )
    assert "憑證" in t and "machine id" in t, (
        "the page does not say that the full machine id IS a credential -- "
        "POST /auth/register takes it alone and returns the account key -- so "
        "nothing explains why only the first characters are printed."
    )
    assert "~/.yangble5/machine-id" in t, (
        "the page names the credentials file but not machine-id, which holds "
        "the salt the fingerprint is derived from and is equally fatal to leak"
    )


# ── the instruction document must be on the origin that serves the installer ─

def test_the_instruction_document_is_same_origin(text):
    """The reason the line can be this short is that trust is not split.

    AGENTS.md, llms.txt, install.sh and install.sh.sha256 all come from
    yangble5.com, so the reader has exactly one thing to check and it is the
    thing in their address bar. An instruction document on a third-party host
    -- a gist, a docs site, a shortener -- reintroduces every problem the
    canonical string was built to remove.
    """
    t = text["index.html"]
    assert "https://yangble5.com/AGENTS.md" in t
    assert "https://yangble5.com/llms.txt" in t, (
        "the machine-readable pointer is not published, so an agent that "
        "prefers llms.txt has to guess a URL"
    )
    for hostile in ("gist.github.com", "bit.ly", "tinyurl", "raw.githubusercontent.com"):
        assert hostile not in t, (
            f"index.html points an agent at {hostile}; the instruction "
            f"document must be same-origin with the installer and its digest"
        )


# ── the page quotes the installer; the quotes must still be in the installer ─

def _flatten(src: str) -> str:
    """Shell text with comment markers and line wrapping removed.

    A sentence in install.sh's header is wrapped across lines and prefixed
    with `#   `, so a naive substring search for a quotation would fail on a
    quote that is in fact perfectly accurate. Normalising both sides is what
    makes the comparison test the claim rather than the formatting.
    """
    lines = [re.sub(r"^\s*#\s?", "", ln) for ln in src.splitlines()]
    return re.sub(r"\s+", " ", " ".join(lines))


def test_every_english_quotation_is_really_in_the_installer(raw):
    """index.html tells the reader 'do not trust us, the script says so too'.

    That argument is worth exactly as much as the quotation is accurate, and
    the quoted file is not this page's to edit -- site/install.sh is
    digest-pinned and maintained elsewhere, so a rewrite there turns a
    verifiable citation into an invented one with nothing to notice it. An
    earlier draft of this section claimed a sentence appeared in `--help`
    when it is actually printed by the registration refusal; that is the
    class of error this catches.
    """
    installer = _flatten((sitecheck.SITE / "install.sh").read_text(encoding="utf-8"))
    quotes = re.findall(r"<em>[“\"]([^”\"<]+)[”\"]</em>", raw["index.html"])
    assert quotes, (
        "no quotation of the installer is left on the page. The refusal rules "
        "are only checkable because the page shows they are the script's own."
    )
    for q in quotes:
        assert re.sub(r"\s+", " ", q).strip() in installer, (
            f"index.html quotes install.sh as saying:\n  {q!r}\n"
            f"That string is not in site/install.sh. Either the installer was "
            f"reworded and the page now misquotes it, or the quotation was "
            f"never accurate. Fix the page -- install.sh is digest-pinned and "
            f"is not this file's to change."
        )
