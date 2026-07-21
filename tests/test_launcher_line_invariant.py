"""The invariant, not one of its axes.

``~/.yangble5/credentials`` is re-read by a launcher on every run. On Windows
the launcher tokenises it with ``for /f "tokens=1,* delims=="`` and checks it
with ``findstr``; on POSIX ``env.sh`` reads it with ``read -r`` and checks the
parsed variable with ``case``. The property that has to hold on both is:

    THE SET OF LINES CONSUMED MUST EQUAL THE SET OF LINES CHECKED.

Two releases each closed one AXIS of a violation and left the invariant broken:

  round 4  ``if /i`` consumed case-insensitively while ``findstr`` checked
           case-sensitively, so ``yangble5_api=`` was invisible to the check
           and authoritative for the launcher.
  round 5  ``for /f`` SKIPS LEADING DELIMITERS, so ``=YANGBLE5_API=payload``
           tokenises to %%A=YANGBLE5_API / %%B=payload and was consumed, while
           all nine guards are anchored ``^YANGBLE5_API=`` and never matched
           it. Measured before the fix: exit 0, marker written, and the
           effective ANTHROPIC_BASE_URL was the attacker's.

So this file does not test "a leading ``=`` is rejected". It enumerates every
way cmd's ``for /f`` is known to produce an (%%A,%%B) pair -- and every way a
line can be shaped so that ``findstr`` and ``for /f`` see different bytes --
and asserts, for all three variables and both .cmd launchers and env.sh, that
nothing executes and that the two platforms reach the same verdict.

A third divergence was found while writing it, and it is in here as
``mid-line-CR``: findstr's ``.`` and its character classes CANNOT cross a
carriage return and ``^`` only anchors at a real line start, so the anchored
guard ``^YANGBLE5_API=.*[^A-Za-z0-9:/._~-]`` was blind to everything after one,
while ``for /f`` handed the whole tail to %%B. That is the same defect as the
leading ``=`` wearing a different hat, which is why the fix is a whole-file
shape gate that runs BEFORE any line reaches the tokeniser, rather than another
pattern bolted onto the guards.

THREAT MODEL: the landing page tells people to paste a one-liner into an agent
with shell access. A prompt-injected agent appending one innocuous-looking line
to this file must not gain code execution.
"""

from __future__ import annotations

import pytest
from test_generated_launchers import (  # noqa: F401 - fixtures are re-exported
    AS_INSTALLED,
    CLEAN_CREDENTIALS,
    GOOD_KEY,
    GOOD_MODEL,
    GOOD_URL,
    effective_url,
    needs_cmd,
    needs_sh,
    posix_home,
    run_cmd_launcher,
    run_env_sh,
    win_home,
)

CR = "\r"
NUL = "\x00"
CTRL_Z = "\x1a"
BOM = "﻿"

#: Every line shape that could put a value in front of the launcher without the
#: guards having inspected it. ``{k}`` is the key, ``{p}`` the payload.
#:
#: The comment on each entry records what ``for /f`` actually does with it,
#: measured on Windows 11 26200 -- not what the documentation implies. Two of
#: those measurements contradict the folklore and are the reason this table is
#: written from observation:
#:   * ``for /f`` does NOT strip leading whitespace once ``delims`` is ``=``,
#:     because space stops being a delimiter;
#:   * ``eol=;`` skips a line that STARTS with ``;`` but does NOT truncate one
#:     that merely contains a ``;``.
LINE_SHAPES = {
    # -- leading delimiters: for /f skips them, so %%A is the bare key --------
    "leading-=": "={k}={p}",
    "leading-==": "=={k}={p}",
    "leading-=====": "====={k}={p}",
    # -- leading whitespace: NOT stripped (space is not a delimiter here), so
    #    %%A keeps it and the line is inert -- but it is refused anyway, because
    #    "inert today" is what round 4 assumed about case ---------------------
    "leading-space": " {k}={p}",
    "leading-tab": "\t{k}={p}",
    "leading-=-then-space": "= {k}={p}",
    "leading-=-then-tab": "=\t{k}={p}",
    # -- eol: a line starting with ';' is SKIPPED entirely by for /f ----------
    "leading-semicolon": ";{k}={p}",
    "leading-=-then-semicolon": "=;{k}={p}",
    "semicolon-in-value": "{k}=https://ok;{p}",
    # -- delimiter runs: for /f collapses '==' and %%B starts after them ------
    "repeated-=": "{k}=={p}",
    "space-before-=": "{k} ={p}",
    "space-after-=": "{k}= {p}",
    "tab-before-=": "{k}\t={p}",
    # -- values that are not plain text --------------------------------------
    "quoted-value": '{k}="{p}"',
    "single-quoted-value": "{k}='{p}'",
    # -- no pair at all, or a degenerate one ---------------------------------
    "no-delimiter": "{k}{p}",
    "bare-key": "{k}",
    "only-=": "=",
    "empty-value": "{k}=",
    # -- carriage returns: findstr cannot see past one, for /f can -----------
    "mid-line-CR": "{k}=https://ok" + CR + "{p}",
    "trailing-CR": "{k}={p}" + CR,
    "CR-then-another-key": "{k}=https://ok" + CR + "{k}={p}",
    "CR-alone-then-key": CR + "{k}={p}",
    # -- bytes that stop one reader but not the other ------------------------
    "NUL-in-value": "{k}=https://ok" + NUL + "{p}",
    "ctrl-z-in-value": "{k}=https://ok" + CTRL_Z + "{p}",
    "BOM-before-key": BOM + "{k}={p}",
    "high-byte-in-value": "{k}=https://okÿ{p}",
    # -- keys that are almost the key ----------------------------------------
    "key-with-suffix": "{k}X={p}",
    "key-with-prefix": "X{k}={p}",
}

KEYS = ["YANGBLE5_API", "YANGBLE5_MODEL", "YANGBLE5_API_KEY"]
CMD_LAUNCHERS = ["yangble5-env.cmd", "yangble5-claude.cmd"]


def _cmd_line(shape: str, key: str, marker) -> str:
    """A payload that writes ``marker`` if any of it reaches cmd's parser.

    A bare ``&`` is enough where the value is expanded unquoted (the ``echo``
    in yangble5-env.cmd); the ``"`` breaks out of ``set "VAR=%VALUE%"`` in
    yangble5-claude.cmd. Both are in one string so a single table covers both
    sinks -- a fix that closes only one of them is not a fix.
    """
    payload = f'https://x"&echo o>"{marker}"&echo o>"{marker}"&rem "'
    return LINE_SHAPES[shape].format(k=key, p=payload)


def _sh_line(shape: str, key: str, marker) -> str:
    payload = f"https://x$(touch '{marker}')`touch '{marker}'`"
    return LINE_SHAPES[shape].format(k=key, p=payload)


# --------------------------------------------------------------------------
# 1. nothing in the table may execute, on either .cmd launcher
# --------------------------------------------------------------------------
@needs_cmd
@pytest.mark.parametrize("launcher", CMD_LAUNCHERS)
@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("shape", sorted(LINE_SHAPES))
def test_cmd_no_line_shape_can_execute(win_home, tmp_path, launcher, key, shape):
    marker = tmp_path / "pwned.txt"
    line = _cmd_line(shape, key, marker)
    rc, out = run_cmd_launcher(win_home, launcher, CLEAN_CREDENTIALS + line + "\n")
    assert not marker.exists(), (
        f"{shape} on {key} executed a command through {launcher} (exit {rc}).\n"
        f"line: {line!r}\n"
        f"The launcher consumed a line no findstr guard inspected.\n{out}"
    )


# --------------------------------------------------------------------------
# 2. ...and none of them may quietly become the value the launcher uses
# --------------------------------------------------------------------------
@needs_cmd
@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("shape", sorted(LINE_SHAPES))
def test_cmd_no_line_shape_can_take_effect(win_home, tmp_path, key, shape):
    """Not executing is only half of it. The failure mode that hid for two
    releases was the launcher *working normally* on an attacker's value, so a
    line that does not execute still has to either be refused or be inert."""
    marker = tmp_path / "pwned.txt"
    line = _cmd_line(shape, key, marker)
    rc, out = run_cmd_launcher(
        win_home, "yangble5-env.cmd", CLEAN_CREDENTIALS + line + "\n"
    )
    if rc == 0:
        assert effective_url(out) == GOOD_URL, (
            f"{shape} on {key} was accepted AND changed the effective URL to "
            f"{effective_url(out)!r} -- the value used was not the value "
            f"checked.\nline: {line!r}\n{out}"
        )
        # the same claim for the other two settings: an accepted file must have
        # exported exactly what CLEAN_CREDENTIALS said, not the hostile line
        assert f"ANTHROPIC_MODEL={GOOD_MODEL}" in out, (
            f"{shape} on {key} was accepted but ANTHROPIC_MODEL is not "
            f"{GOOD_MODEL!r}\nline: {line!r}\n{out}"
        )
        assert f"ANTHROPIC_AUTH_TOKEN={GOOD_KEY[:24]}" in out, (
            f"{shape} on {key} was accepted but the exported key is not the "
            f"one in the clean file\nline: {line!r}\n{out}"
        )
    else:
        assert rc == 6, f"expected exit 0 or 6, got {rc}\nline: {line!r}\n{out}"
        assert effective_url(out) == "", (
            f"{shape} on {key} was refused but still exported a URL\n{out}"
        )


# --------------------------------------------------------------------------
# 3. the same table against env.sh
# --------------------------------------------------------------------------
@needs_sh
@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("shape", sorted(LINE_SHAPES))
def test_sh_no_line_shape_can_execute_or_take_effect(posix_home, tmp_path, key, shape):
    marker = tmp_path / "sh-pwned.txt"
    line = _sh_line(shape, key, marker)
    rc, out = run_env_sh(posix_home, CLEAN_CREDENTIALS + line + "\n")
    assert not marker.exists(), (
        f"{shape} on {key} executed a command through env.sh (exit {rc}).\n"
        f"line: {line!r}\n{out}"
    )
    if rc == 0:
        assert effective_url(out) == GOOD_URL, (
            f"{shape} on {key} was accepted AND changed the effective URL to "
            f"{effective_url(out)!r}\nline: {line!r}\n{out}"
        )


# --------------------------------------------------------------------------
# 4. and the two must reach the same verdict, shape by shape
# --------------------------------------------------------------------------
#: One shape where the two platforms provably differ, with the reason. It is
#: listed rather than quietly excluded, and the test below FAILS if a listed
#: shape stops diverging -- otherwise a carve-out written today silently
#: covers a regression introduced tomorrow.
#:
#: NUL: cmd's ``for /f`` stops reading the file at the first NUL byte and the
#: Windows byte gate refuses the file outright. POSIX ``read -r`` silently
#: DROPS the NUL before any shell code can see it, so env.sh validates
#: ``https://okhttps://elsewhere.example`` -- measured -- and accepts it. The
#: invariant still holds on both: each side validates exactly the bytes it goes
#: on to use. env.sh cannot do better without shelling out to another process
#: inside the parser, which is a worse trade than this divergence.
KNOWN_DIVERGENCES = {
    ("NUL-in-value", "YANGBLE5_API"): "read -r drops NUL; findstr refuses it",
}


@needs_cmd
@needs_sh
@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("shape", sorted(LINE_SHAPES))
def test_both_launchers_agree_on_every_line_shape(win_home, posix_home, tmp_path, key, shape):
    """This is the test that would have caught the round-4 bug from the other
    side: ``yangble5_api=`` was inert on POSIX and authoritative on Windows,
    and no single-platform test could see the difference."""
    marker = tmp_path / "agree.txt"
    # one benign payload, so the comparison is about the SHAPE and not about a
    # metacharacter that both sides would obviously reject
    line = LINE_SHAPES[shape].format(k=key, p="https://elsewhere.example")
    credentials = CLEAN_CREDENTIALS + line + "\n"
    win_rc, win_out = run_cmd_launcher(win_home, "yangble5-env.cmd", credentials)
    sh_rc, sh_out = run_env_sh(posix_home, credentials)
    assert not marker.exists()
    reason = KNOWN_DIVERGENCES.get((shape, key))
    if reason is not None:
        assert (win_rc == 6) != (sh_rc == 6), (
            f"{shape} on {key} is listed in KNOWN_DIVERGENCES ({reason}) but "
            f"the launchers now agree (cmd {win_rc}, sh {sh_rc}). Delete the "
            f"entry -- a stale carve-out hides the next regression."
        )
        # whichever side accepted it must still have used a value it checked
        if sh_rc == 0:
            assert "$" not in effective_url(sh_out)
        return
    assert (win_rc == 6) == (sh_rc == 6), (
        f"{shape} on {key}: cmd exit {win_rc}, sh exit {sh_rc}\n"
        f"line: {line!r}\n--- cmd ---\n{win_out}\n--- sh ---\n{sh_out}"
    )
    if win_rc == 0 and sh_rc == 0:
        assert effective_url(win_out) == effective_url(sh_out), (
            f"{shape} on {key}: cmd used {effective_url(win_out)!r}, "
            f"sh used {effective_url(sh_out)!r}"
        )


@needs_cmd
@needs_sh
@pytest.mark.parametrize("key", ["FOO", "yangble5_api", "YANGBLE5_APIX", "YANGBLE5_KEY_ID"])
@pytest.mark.parametrize("meta", ["&", '"', "|", ">", "^", "%", "!", " ", "(", "$", "`"])
def test_a_metacharacter_on_a_key_no_launcher_reads_is_still_refused(
    win_home, posix_home, key, meta
):
    """The job the whole-file byte gate does that the per-key guards cannot.

    ``FOO=...&...`` is inert on Windows -- ``for /f`` produces %%A=FOO, which
    matches none of the three ``if`` tests -- and inert on POSIX for the same
    reason. Neither per-key guard looks at it, because there is no guard for
    ``FOO``. Without a whole-file byte scan the two platforms then disagree
    about whether the file is acceptable at all, and "inert today" is exactly
    the assumption the case bug was built on. Measured with the byte gate
    disabled: cmd exit 0, sh exit 6.
    """
    credentials = CLEAN_CREDENTIALS + f"{key}=https://x{meta}y\n"
    win_rc, win_out = run_cmd_launcher(win_home, "yangble5-env.cmd", credentials)
    sh_rc, sh_out = run_env_sh(posix_home, credentials)
    assert win_rc == 6, (
        f"{key}= carrying {meta!r} was accepted by the .cmd launcher\n{win_out}"
    )
    assert sh_rc == 6, f"{key}= carrying {meta!r} was accepted by env.sh\n{sh_out}"


# --------------------------------------------------------------------------
# 5. duplicate keys: "some line is well formed" was never a safe check
# --------------------------------------------------------------------------
#: ``for /f`` keeps the LAST line it sees and env.sh keeps the last assignment,
#: so a good line must never launder a bad one. The two platforms get there by
#: different routes and are deliberately NOT equally strict:
#:
#:   * env.sh validates the PARSED VARIABLE, so it validates precisely the line
#:     it kept. A bad line that a later good line overrides is genuinely
#:     harmless there, and env.sh accepts the file.
#:   * the .cmd launcher cannot validate "the line for/f kept" -- it has no way
#:     to hand a variable to a matcher without putting it on a command line,
#:     where it would be re-parsed -- so it validates EVERY line for the key
#:     and refuses the file if any of them is bad.
#:
#: The property both must satisfy is therefore not "same exit code" but "the
#: value that comes out was one that passed that launcher's own checks".
DUPLICATE_ORDERS = [
    ("good-then-bad", "{good}\n{bad}"),
    ("bad-then-good", "{bad}\n{good}"),
    ("good-bad-good", "{good}\n{bad}\n{good}"),
]

BAD_SECOND_LINES = [
    "YANGBLE5_API=http://evil.example",
    "YANGBLE5_API=ftp://evil.example",
    "YANGBLE5_API=https://u@evil.example",
    "YANGBLE5_API=",
    "=YANGBLE5_API=https://evil.example",
    "YANGBLE5_MODEL=",
    "YANGBLE5_API_KEY=notakey",
]

GOOD_LINE_FOR = {
    "YANGBLE5_API": f"YANGBLE5_API={GOOD_URL}",
    "YANGBLE5_MODEL": "YANGBLE5_MODEL=yangble5",
    "YANGBLE5_API_KEY": "YANGBLE5_API_KEY=yb5_0123456789abcdef_AAAAAAAAAAAAAAAA",
}


@needs_cmd
@needs_sh
@pytest.mark.parametrize("order,tmpl", DUPLICATE_ORDERS)
@pytest.mark.parametrize("bad", BAD_SECOND_LINES)
def test_a_duplicate_key_cannot_ride_on_a_good_lines_validity(
    win_home, posix_home, order, tmpl, bad
):
    key = bad.lstrip("=").split("=", 1)[0]
    good = GOOD_LINE_FOR[key]
    credentials = CLEAN_CREDENTIALS + tmpl.format(good=good, bad=bad) + "\n"
    win_rc, win_out = run_cmd_launcher(win_home, "yangble5-env.cmd", credentials)
    sh_rc, sh_out = run_env_sh(posix_home, credentials)

    # the .cmd launcher checks every line, so any bad line refuses the file
    assert win_rc == 6, (
        f"{order}: {bad!r} was accepted by the .cmd launcher (exit {win_rc}); "
        f"effective URL {effective_url(win_out)!r}\n{win_out}"
    )
    # env.sh may accept, but only ever with the value it validated
    if sh_rc == 0:
        assert effective_url(sh_out) == GOOD_URL, (
            f"{order}: env.sh accepted the file and came out with "
            f"{effective_url(sh_out)!r}, which is not the value it checked\n{sh_out}"
        )
    else:
        assert sh_rc == 6, f"unexpected env.sh exit {sh_rc}\n{sh_out}"


@needs_sh
@pytest.mark.parametrize("bad", BAD_SECOND_LINES)
def test_sh_refuses_a_bad_line_that_is_the_one_it_keeps(posix_home, bad):
    """The half of the duplicate-key rule env.sh must enforce on its own: an
    APPENDED bad line is the last one, so it is the one env.sh keeps, and it
    has to be refused. This is the realistic shape of the attack -- an agent
    appends, it does not insert."""
    key = bad.lstrip("=").split("=", 1)[0]
    credentials = CLEAN_CREDENTIALS + GOOD_LINE_FOR[key] + "\n" + bad + "\n"
    rc, out = run_env_sh(posix_home, credentials)
    assert rc == 6, f"{bad!r} appended after a good line was accepted\n{out}"


# --------------------------------------------------------------------------
# 6. the file both installers actually write still has to be accepted
# --------------------------------------------------------------------------
#: Without these, everything above is satisfied by a launcher that refuses
#: every file, which is not a fix.
ACCEPTABLE = {
    "as written by the installer": CLEAN_CREDENTIALS,
    "no comment header": (
        f"YANGBLE5_API={GOOD_URL}\n"
        "YANGBLE5_API_KEY=yb5_0123456789abcdef_AAAAAAAAAAAAAAAA\n"
        "YANGBLE5_MODEL=yangble5\n"
    ),
    "blank lines between settings": CLEAN_CREDENTIALS.replace("\n", "\n\n"),
    "comment containing punctuation": (
        "# This file is DATA. The launchers parse it as strict KEY=VALUE;\n"
        "# nothing in it is executed (nothing!). Delete it to revoke.\n"
        + CLEAN_CREDENTIALS
    ),
    "unknown key with a legal value": CLEAN_CREDENTIALS + "YANGBLE5_FUTURE=1\n",
    "key id present": CLEAN_CREDENTIALS + "YANGBLE5_EXTRA=0123456789abcdef\n",
}


# Everything else in this file is a hand-written constant, and a hand-written
# constant is only evidence that the gates accept *that constant*. If an
# installer emitted a header line or a trailing byte the gates refuse, every
# other test here would still be green while no real installation could start.
# ``AS_INSTALLED`` is filled in by the fixtures at generation time, before any
# test overwrites the file.
#
# It is a plain dict rather than a fixture that reaches for both homes, because
# ``pytest.skip`` raises a ``BaseException`` subclass: an autouse fixture that
# asked for ``win_home`` on Linux would propagate the skip and silently disable
# every test in this module, POSIX ones included.


@needs_cmd
@pytest.mark.parametrize(
    "launcher", ["yangble5-env.cmd", "yangble5-claude.cmd", "yangble5-codex.cmd"]
)
def test_no_rem_comment_carries_a_redirection_or_pipe(win_home, launcher):
    """``REM`` is not a comment, it is a command that ignores its arguments.

    On the cmd this was tested against it swallows ``>`` ``<`` and ``|``, and
    the launcher provably creates no stray files -- but the margin is one
    careless edit wide, and a comment in this file describes ``&`` and ``|``
    for a living. Asserting the property is cheaper than re-deriving that
    quirk every time someone rewords a comment.
    """
    text = (win_home / ".yangble5" / "bin" / launcher).read_text(encoding="utf-8")
    offenders = []
    for n, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if not stripped.upper().startswith("REM"):
            continue
        for i, ch in enumerate(stripped):
            if ch in "<>|" and (i == 0 or stripped[i - 1] != "^"):
                offenders.append((n, ch, stripped))
                break
    assert not offenders, (
        f"{launcher} has REM lines carrying an unescaped redirection or pipe "
        "character:\n"
        + "\n".join(f"  line {n}: [{ch}] {s}" for n, ch, s in offenders)
    )


@needs_cmd
def test_the_windows_installer_writes_a_file_its_own_launcher_accepts(win_home):
    raw = AS_INSTALLED.get(str(win_home))
    assert raw, "install.ps1 wrote no credentials file to snapshot"
    assert b"\r" not in raw, f"install.ps1 wrote a CR: {raw!r}"
    assert raw.endswith(b"\n"), f"install.ps1 wrote no final newline: {raw[-40:]!r}"
    rc, out = run_cmd_launcher(win_home, "yangble5-env.cmd", raw.decode("utf-8"))
    assert rc == 0, (
        "the gates refuse the file install.ps1 itself writes -- no fresh "
        f"installation could start (exit {rc})\n{raw.decode('utf-8')}\n{out}"
    )


@needs_sh
def test_the_posix_installer_writes_a_file_its_own_launcher_accepts(posix_home):
    raw = AS_INSTALLED.get(str(posix_home))
    assert raw, "install.sh wrote no credentials file to snapshot"
    assert b"\r" not in raw, f"install.sh wrote a CR: {raw!r}"
    assert raw.endswith(b"\n"), f"install.sh wrote no final newline: {raw[-40:]!r}"
    rc, out = run_env_sh(posix_home, raw.decode("utf-8"))
    assert rc == 0, (
        "the gates refuse the file install.sh itself writes -- no fresh "
        f"installation could start (exit {rc})\n{raw.decode('utf-8')}\n{out}"
    )


@needs_cmd
@needs_sh
@pytest.mark.parametrize("name", sorted(ACCEPTABLE))
def test_the_gates_still_accept_a_legitimate_file(win_home, posix_home, name):
    credentials = ACCEPTABLE[name]
    win_rc, win_out = run_cmd_launcher(win_home, "yangble5-env.cmd", credentials)
    sh_rc, sh_out = run_env_sh(posix_home, credentials)
    assert win_rc == 0, f"{name} was refused by the .cmd launcher\n{win_out}"
    assert sh_rc == 0, f"{name} was refused by env.sh\n{sh_out}"
    assert effective_url(win_out) == GOOD_URL, win_out
    assert effective_url(sh_out) == GOOD_URL, sh_out


# --------------------------------------------------------------------------
# 7. line endings, stated as their own contract
# --------------------------------------------------------------------------
#: Gate 2 on Windows is a ``.$`` scan, which fires both on a CR and on a last
#: line with no newline, because findstr cannot tell those apart. env.sh
#: refuses both too, so the contract is the same on both platforms and is
#: written down here rather than left as an accident of the implementation.
@needs_cmd
@needs_sh
@pytest.mark.parametrize(
    "name,credentials",
    [
        ("CRLF throughout", CLEAN_CREDENTIALS.replace("\n", "\r\n")),
        ("one CRLF line", CLEAN_CREDENTIALS.replace(
            f"YANGBLE5_API={GOOD_URL}\n", f"YANGBLE5_API={GOOD_URL}\r\n")),
        ("no final newline", CLEAN_CREDENTIALS.rstrip("\n")),
        ("lone CR joining two lines", CLEAN_CREDENTIALS.replace(
            f"YANGBLE5_API={GOOD_URL}\n", f"YANGBLE5_API={GOOD_URL}\r")),
    ],
)
def test_only_lf_terminated_text_is_accepted(win_home, posix_home, name, credentials):
    win_rc, win_out = run_cmd_launcher(win_home, "yangble5-env.cmd", credentials)
    sh_rc, sh_out = run_env_sh(posix_home, credentials)
    assert win_rc == 6, f"{name} was accepted by the .cmd launcher\n{win_out}"
    assert sh_rc == 6, f"{name} was accepted by env.sh\n{sh_out}"


@needs_cmd
@needs_sh
def test_a_comment_may_end_without_a_newline(win_home, posix_home):
    """The last-line rule is about data lines. A trailing comment with no
    newline is filtered out before either gate sees it, and both launchers have
    to agree about that too -- this is the case that made the POSIX side need
    the partial-line flag checked AFTER the comment test rather than before."""
    credentials = CLEAN_CREDENTIALS + "# trailing note with no newline"
    win_rc, win_out = run_cmd_launcher(win_home, "yangble5-env.cmd", credentials)
    sh_rc, sh_out = run_env_sh(posix_home, credentials)
    assert win_rc == 0, win_out
    assert sh_rc == 0, sh_out


# --------------------------------------------------------------------------
# 8. what the F811 ignore for this file gives up, put back
# --------------------------------------------------------------------------
def test_no_test_name_in_this_module_is_defined_twice():
    """``F811`` is switched off for this file so pytest fixtures can be
    imported by name (see pyproject.toml). The rule's other job is catching a
    ``def test_x`` written twice, where the second silently replaces the first
    and the suite reports one fewer test than anyone counted. That job is not
    delegated to the linter here, so it is done directly."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    names = [
        n.name
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    assert not duplicates, (
        "these top-level functions are defined more than once, so the earlier "
        f"definition never runs: {duplicates}"
    )
