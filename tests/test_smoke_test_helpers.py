"""The guard over ``deploy/smoke_test.sh``'s own correctness.

On 2026-07-23 the smoke test reported this against a healthy origin::

    FAIL  hdr/X-Content-Type-Options   present but wrong:
          expected to contain 'nosniff', got 'nosniff'

— eight times, once per security header, on a deployment that was serving all
eight correctly.  The cause was not the server.  ``check_security_headers`` used
``grep -qiF`` to ask "does this value contain that substring", and GNU grep 3.0
— the build Git Bash ships, which is the build this project's operator runs the
script under — **aborts** when ``-i`` and ``-F`` are combined::

    $ printf nosniff | grep -qiF nosniff; echo $?
    134                     # 128 + SIGABRT

Either flag alone is fine.  A crashed grep exits non-zero, so every comparison
said "no match", and the failure message printed the two identical strings side
by side without anything noticing they were identical.

This matters more than a cosmetic bug.  ``smoke_test.sh`` is the gate between a
deployment and an announcement — ``GO_LIVE.md`` says do not announce while it is
red.  Eight false reds train an operator to read ``FAIL hdr/…`` as "oh, that's
the grep thing", and *that* is how the real eight-missing-headers outage went
unnoticed for a day.  A check that cannot pass is the same class of defect as a
check that cannot fail.

The replacement, ``contains_ci``, is a ``case`` with a quoted expansion: no
regex, no fork, and nothing that varies between platforms.  It carries its own
``--self-test``, which is what these tests run.  A repository cannot prove
anything about a live origin, but it can prove the tool that asks the questions
still knows how to say both yes and no.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SMOKE = ROOT / "deploy" / "smoke_test.sh"
SHELL_SCRIPTS = sorted(ROOT.glob("**/*.sh"))

# `-i` with `-F`, in either order and in any bundle of short flags. Written so
# this file's own bytes do not spell the string it hunts, the way the secret
# scan in .github/workflows/ci.yml is, so the check does not flag itself.
_BAD_GREP = re.compile(r"\bgrep\s+(?:-\w*\s+)*-\w*(?:i\w*F|F\w*i)\w*\b")


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _sh() -> str:
    """bash, or skip. The suite runs on Windows agents without one."""
    import shutil

    found = shutil.which("bash")
    if not found:
        pytest.skip("bash not available")
    return found


def test_self_test_passes() -> None:
    proc = subprocess.run(  # noqa: S603 - fixed argv, interpreter from shutil.which
        [_sh(), str(SMOKE), "--self-test"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=ROOT,
    )
    assert proc.returncode == 0, (
        "deploy/smoke_test.sh --self-test failed. Its substring helper decides "
        "every security-header verdict this project publishes, so a failure here "
        f"means the smoke test's answers cannot be trusted.\n{proc.stdout}\n{proc.stderr}"
    )


def test_self_test_covers_both_directions() -> None:
    """A table of only-negative cases would have stayed green through the bug.

    This is not hypothetical.  With ``grep -qiF`` reinstated, the self-test's
    six "must NOT contain" rows all still passed — a crashed grep returns
    non-zero, which is the right answer to a negative question by accident.
    Only the positive rows caught it.  So both directions are required to be
    present, not just a healthy-looking count of cases.
    """
    body = SMOKE.read_text(encoding="utf-8")
    table = body[body.index("self_test()") :]
    table = table[: table.index("\nEOF")]
    rows = [r for r in table.splitlines() if r.count("|") == 3]
    positives = [r for r in rows if r.rsplit("|", 1)[1].strip() == "1"]
    negatives = [r for r in rows if r.rsplit("|", 1)[1].strip() == "0"]
    assert len(positives) >= 4, (
        f"only {len(positives)} positive cases; those are the ones that caught the bug"
    )
    assert len(negatives) >= 4, f"only {len(negatives)} negative cases"


def test_self_test_can_fail() -> None:
    """The self-test itself must be able to go red, or it certifies nothing.

    Run against a copy whose helper always answers yes.  Every negative row
    must then fail; anything less means the table is not actually consulted.
    """
    import tempfile

    original = SMOKE.read_text(encoding="utf-8")
    broken = original.replace(
        '        *)           return 1 ;;\n',
        '        *)           return 0 ;;\n',
    )
    assert broken != original, "could not find the branch to break; this test is scanning nothing"

    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "smoke_broken.sh"
        probe.write_text(broken, encoding="utf-8", newline="")
        proc = subprocess.run(  # noqa: S603 - fixed argv, interpreter from shutil.which
            [_sh(), str(probe), "--self-test"], capture_output=True, text=True, timeout=120
        )
    assert proc.returncode != 0, (
        "a contains_ci that answers yes to everything still passed --self-test. "
        "The self-test is decorative and would not have caught the grep abort "
        f"either.\n{proc.stdout}"
    )


@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_no_shell_script_combines_grep_i_and_f(script: Path) -> None:
    """``-i`` and ``-F`` together abort on GNU grep 3.0 (Git Bash, MSYS2).

    The crash is silent in the sense that matters: the exit status is
    indistinguishable from "no match", so a script gets a wrong answer rather
    than an error.  Every use in this repository is a case-insensitive
    *substring* test, and the shell does those natively — see ``contains_ci``.
    """
    hits = [
        f"{i}: {line.strip()}"
        for i, line in enumerate(_lines(script), 1)
        if _BAD_GREP.search(line.split("#", 1)[0])
    ]
    assert not hits, (
        f"{script.relative_to(ROOT).as_posix()} combines grep's -i and -F, which "
        "ABORTS on GNU grep 3.0 (the build Git Bash ships) and returns an exit "
        "status a caller reads as 'no match':\n  " + "\n  ".join(hits) + "\n\n"
        "For a case-insensitive substring test use the shell: lowercase both "
        "sides with tr and compare with `case \"$hay\" in *\"$needle\"*)`. The "
        "quoted expansion matches literally, so glob metacharacters in the "
        "needle stay literal."
    )


# ── the managed-robots.txt check ───────────────────────────────────────────


def test_the_managed_robots_check_exists_and_is_called() -> None:
    """A check nothing calls is a comment.

    ``site/robots.txt`` exists because the live ``/robots.txt`` was a Cloudflare
    managed default that said ``Disallow: /`` for ClaudeBot, GPTBot,
    Google-Extended and six others — a decision nobody made, on a project whose
    stated direction is to be a resource AI agents read.  Copying the file into
    the webroot does not fix it: the managed copy is injected at the edge and
    *prepends* itself, so the origin looks correctly deployed and the file in
    git is inert.  Only an off-host fetch can tell, which is why the check lives
    here and not in the test suite.
    """
    body = SMOKE.read_text(encoding="utf-8")
    assert "check_managed_robots() {" in body, "the check was removed"
    calls = [ln for ln in body.splitlines() if ln.strip() == "check_managed_robots"]
    assert calls, "check_managed_robots is defined but never called, so it never runs"


def test_the_managed_robots_check_ignores_comment_lines() -> None:
    """The bug the first version of this check shipped with.

    ``site/robots.txt``'s header comment QUOTES the obvious tells — the phrase
    "Cloudflare Managed content", the words "Content Signals", and
    ``ai-train=no`` — while explaining what the file replaces.  A substring
    search over the whole body therefore flagged the *correct*, locally served,
    unmodified file as the injected one.  Stripping ``#`` lines before comparing
    is the entire fix, so it is the thing pinned.
    """
    body = SMOKE.read_text(encoding="utf-8")
    block = body[body.index("check_managed_robots() {") :]
    block = block[: block.index("\n}\n")]
    assert "grep -vE '^[[:space:]]*(#|$)'" in block, (
        "the robots check no longer strips comment lines before comparing. "
        "site/robots.txt documents the very strings this looks for, so without "
        "that the correct file is reported as the injected one — which is how "
        "the check shipped the first time."
    )
    robots = (ROOT / "site" / "robots.txt").read_text(encoding="utf-8")
    commented = [ln for ln in robots.splitlines() if ln.lstrip().startswith("#")]
    assert any("ai-train=no" in ln for ln in commented), (
        "site/robots.txt no longer quotes 'ai-train=no' in a comment. That is "
        "fine, but it means this test is no longer proving anything about the "
        "false positive it was written for — re-point it at whatever tell the "
        "file now discusses, or delete it."
    )
