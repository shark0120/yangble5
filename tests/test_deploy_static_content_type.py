"""The guard over ``default_type text/plain`` for the static site.

On 2026-07-23 https://yangble5.com answered ``/install.sh`` with
``Content-Type: application/octet-stream``.  nginx's ``mime.types`` has no entry
for ``.sh``, ``.ps1``, ``.sha256`` or ``.md``, so all four fall through to
``default_type``, which the panel config sets to ``application/octet-stream`` at
http level.  Combined with the ``X-Content-Type-Options: nosniff`` this
deployment correctly sends, a browser downloads the file instead of showing it.
The landing page's whole argument — *read the script before you run it* — does
not survive that, and ``/AGENTS.md``, which the published one-liner points an AI
agent at as step one, does not survive it either.

``deploy/nginx/yangble5.com.conf.example`` PART 3d had the fix from the
beginning.  It is a block an operator is told to paste into an existing
``server { }``, and it was not pasted — the same way, and for the same reason,
that PART 2j (the security headers) was not pasted.  The answer that worked
there works here: ``deploy/nginx/static-content-type.conf``, one ``include``
that either applies in full or fails ``nginx -t``.

What this file asserts is the *assumption* that fix rests on.
``default_type text/plain`` is only correct while the webroot holds nothing but
text.  One ``.png`` in ``site/`` would be served as ``text/plain`` and render as
garbage, and nothing else in this repository would notice.  So the invariant is
tested rather than left in a comment: a binary added to ``site/`` turns this red
with the reason attached.

These tests read the shipped files.  They cannot prove the running config on any
host is right — nothing in a repository can.  ``deploy/smoke_test.sh`` is what
checks the live origin, and its ``AGENTS.md/content-type`` case is asserted here
to still exist.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
INCLUDE = ROOT / "deploy" / "nginx" / "static-content-type.conf"
SNIPPET = ROOT / "deploy" / "nginx" / "yangble5.com.conf.example"
SMOKE = ROOT / "deploy" / "smoke_test.sh"

# Extensions the include is allowed to assume are text.  Deliberately a short
# allowlist and not "anything that happens to decode": a `.json` or a `.svg`
# added tomorrow has a real mime type nginx would have used, and serving it as
# text/plain is a bug even though it decodes fine.
TEXT_SUFFIXES = {".html", ".md", ".txt", ".xml", ".sh", ".ps1", ".sha256"}


def _site_files() -> list[Path]:
    return sorted(p for p in SITE.rglob("*") if p.is_file())


def test_site_holds_only_text_because_default_type_says_so() -> None:
    """``default_type text/plain`` is a lie the moment a binary lands here."""
    offenders: list[str] = []
    for path in _site_files():
        rel = path.relative_to(SITE).as_posix()
        if path.suffix.lower() not in TEXT_SUFFIXES:
            offenders.append(f"{rel}: suffix {path.suffix or '(none)'} is not in TEXT_SUFFIXES")
            continue
        raw = path.read_bytes()
        if b"\x00" in raw:
            offenders.append(f"{rel}: contains NUL bytes")
            continue
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            offenders.append(f"{rel}: not valid UTF-8 ({exc})")

    assert not offenders, (
        "deploy/nginx/static-content-type.conf serves this whole webroot with\n"
        "`default_type text/plain`, which is only safe while every file in it is\n"
        "text. These are not:\n  " + "\n  ".join(offenders) + "\n\n"
        "Serving a binary as text/plain renders it as garbage in a browser. If\n"
        "site/ genuinely needs a non-text asset, give it a real mime type in the\n"
        "include (a `types { }` block, or a `location ~ \\.png$`) and add the\n"
        "suffix here in the same commit — do not just widen TEXT_SUFFIXES."
    )


def test_site_files_are_not_empty() -> None:
    """A zero-byte published file is a deploy accident, not a document."""
    empty = [p.relative_to(SITE).as_posix() for p in _site_files() if p.stat().st_size == 0]
    assert not empty, f"published but empty: {empty}"


# ── the include itself ─────────────────────────────────────────────────────


def _uncommented(path: Path) -> str:
    """The file with ``#`` comments removed.

    Every one of these configs is more comment than directive on purpose, and
    the comments quote the very directives being searched for.  Matching against
    the raw text would let a *documented* `add_header` pass for a *declared*
    one.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(line.split("#", 1)[0] for line in lines)


def test_include_declares_default_type_and_charset() -> None:
    body = _uncommented(INCLUDE)
    assert re.search(r"^\s*default_type\s+text/plain\s*;", body, re.M), (
        "the include exists to set default_type text/plain and does not"
    )
    assert re.search(r"^\s*charset\s+utf-8\s*;", body, re.M), (
        "without `charset utf-8` a browser guesses the encoding of /llms.txt and "
        "/AGENTS.md from its locale, and mojibakes the em dashes and the Chinese"
    )


def test_include_declares_no_add_header() -> None:
    """The trap this repository has already fallen into once.

    nginx does not merge ``add_header`` across levels.  A single ``add_header``
    inside the ``location /`` below would drop every server-level header on the
    entire static site — including the Content-Security-Policy that pins the
    inline scripts by hash.  The page would keep working, which is what makes it
    dangerous.
    """
    body = _uncommented(INCLUDE)
    found = re.findall(r"^\s*add_header\s+(\S+)", body, re.M)
    assert not found, (
        f"static-content-type.conf declares add_header {found}. Inside a location "
        "that drops EVERY add_header inherited from the server block, CSP "
        "included. If a header really is needed here, re-include "
        "security-headers.conf inside the same location in the same commit."
    )


def test_snippet_and_include_agree_on_the_directives() -> None:
    """PART 3d and the include are two copies, so drift is what is asserted.

    An operator who pastes the snippet and an operator who applies the include
    must end up with the same behaviour, or one of them silently gets a webroot
    that downloads its own install script.
    """
    wanted = ("default_type text/plain;", "charset utf-8;", "index index.html;")
    snippet = _uncommented(SNIPPET)
    include = _uncommented(INCLUDE)
    for directive in wanted:
        collapsed_snippet = re.sub(r"\s+", " ", snippet)
        collapsed_include = re.sub(r"\s+", " ", include)
        assert directive in collapsed_snippet, f"PART 3d lost `{directive}`"
        assert directive in collapsed_include, f"the include lost `{directive}`"


@pytest.mark.parametrize("marker", ["AGENTS.md/content-type", "install.sh/content-type"])
def test_smoke_test_still_checks_the_served_content_type(marker: str) -> None:
    """Only the live origin can answer this, so the live check must survive.

    Both cases were added after the origin was found serving octet-stream. A
    refactor that drops them leaves the repository asserting a config file it
    has no evidence is deployed.
    """
    assert marker in SMOKE.read_text(encoding="utf-8"), (
        f"deploy/smoke_test.sh no longer checks `{marker}`. That check is the only "
        "thing in this project that looks at what the origin actually serves; the "
        "tests in this file only prove the config files agree with each other."
    )


def test_agents_md_content_type_is_a_failure_not_a_warning() -> None:
    """The distinction is the point, so it is pinned.

    ``install.sh`` served as a download is a degraded experience — ``curl | sh``
    still works.  ``AGENTS.md`` served as a download is the *end* of the agent
    install path: the one-liner points there and there is no second route.
    """
    text = SMOKE.read_text(encoding="utf-8")
    block = text[text.index("AGENTS.md/content-type") :]
    block = block[: block.index("\n}")]
    assert "fail \"AGENTS.md/content-type\"" in block, (
        "the AGENTS.md content-type check was downgraded to a warning. An agent "
        "that will not parse application/octet-stream cannot read step one of the "
        "install path, and a warning does not stop a release."
    )
