"""Tests for the launchers the installers GENERATE, not for the installers.

Everything under ``tests/test_installer_validation.py`` checks what the
installer accepts at install time. This file checks the other half, which had
no coverage at all: ``~/.yangble5/credentials`` is re-read by a launcher on
*every* run, long after the installer has finished, and anything running as
this user can append a line to it in between. On the threat model that matters
here -- the landing page tells people to paste a one-liner into Claude Code or
Codex, an agent with shell access -- "anything running as this user" includes a
prompt-injected agent, and one appended line must not become code execution.

The bug these tests were written for: the generated .cmd launchers consumed the
file with ``if /i`` (case-INsensitive) but validated it with ``findstr``
(case-SENSITIVE). An uppercase ``YANGBLE5_API=`` line carrying a metacharacter
was refused with exit 6; the identical value on a lowercase ``yangble5_api=``
line was invisible to the validator, authoritative for the launcher, and ran an
arbitrary command on every launch -- while ``claude`` still started, so nothing
looked wrong. 305 test functions did not cover it, because none of them ran a
launcher.

So these tests run the real generated launchers as subprocesses and assert on a
marker file. A marker that exists is code execution, whatever the exit code
said.

Both halves are generated from the real installers -- the .cmd bodies by
extracting ``Write-Yb5Config`` from install.ps1 with the PowerShell parser, and
``env.sh`` by sourcing install.sh in library mode. Neither is transcribed here;
a transcription would only prove the transcription works.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = ROOT / "site" / "install.sh"
INSTALL_PS1 = ROOT / "site" / "install.ps1"

SH = shutil.which("sh")
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")
CMD = shutil.which("cmd.exe")

needs_cmd = pytest.mark.skipif(
    POWERSHELL is None or CMD is None,
    reason="Windows launchers need both powershell.exe (to generate) and cmd.exe (to run)",
)
needs_sh = pytest.mark.skipif(SH is None, reason="POSIX sh not available")

GOOD_URL = "https://yangble5.com"
GOOD_KEY = "yb5_0123456789abcdef_AAAAAAAAAAAAAAAA"
GOOD_MODEL = "yangble5"

CLEAN_CREDENTIALS = "\n".join(
    [
        "# yangble5 credentials",
        f"YANGBLE5_API={GOOD_URL}",
        f"YANGBLE5_API_KEY={GOOD_KEY}",
        "YANGBLE5_KEY_ID=0123456789abcdef",
        f"YANGBLE5_MODEL={GOOD_MODEL}",
        "",
    ]
)


# --------------------------------------------------------------------------
# generating the launchers from the real installers
# --------------------------------------------------------------------------
# Write-Yb5Config and everything it calls. Extracted by AST rather than by
# dot-sourcing install.ps1, because dot-sourcing it would run the installer.
_PS_FUNCS = [
    "Write-Ok",
    "Write-Info",
    "Write-Warn",
    "Write-Step",
    "Stop-Install",
    "Test-Yb5ApiUrl",
    "Test-Yb5ModelName",
    "Test-Yb5UInt",
    "Test-Yb5Key",
    "Get-SafeRemoteText",
    "Protect-Path",
    "Get-CurrentUserSid",
    "New-Yb5Directory",
    "Write-Yb5File",
    "Write-Yb5Config",
    "Write-Uninstaller",
    "Add-Yb5ToPath",
]

_PS_GENERATOR = """
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:YB5_PS1, [ref]$null, [ref]$null)
$fnType = [System.Management.Automation.Language.FunctionDefinitionAst]
foreach ($n in ($env:YB5_FUNCS -split ',')) {
    $fn = $ast.Find({ param($x)
        $x -is $fnType -and $x.Name -eq $n }, $true)
    if ($null -eq $fn) { throw "function not found: $n" }
    . ([scriptblock]::Create($fn.Extent.Text))
}
$EX_OK=0; $EX_USAGE=1; $EX_ROOT=2; $EX_PREREQ=3
$EX_PLATFORM=4; $EX_NETWORK=5; $EX_REGISTER=6; $EX_CONFIG=7; $EX_VERIFY=8
$Yb5InstallerVersion = '1.0.0'
$script:Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$script:Backups = New-Object System.Collections.ArrayList
$script:InstallMode = 'registered'
$DryRun = $false
$AddToPath = $false
$Api = $env:YB5_T_API
$Model = $env:YB5_T_MODEL
$MaxContext = '1000000'
$MaxOutput = '65536'
$TimeoutMs = '600000'
$script:ApiKey = $env:YB5_T_KEY
$script:KeyId = '0123456789abcdef'
$Yb5Home = Join-Path $env:YB5_T_HOME '.yangble5'
$Yb5Bin = Join-Path $Yb5Home 'bin'
$CredFile = Join-Path $Yb5Home 'credentials'
Write-Yb5Config | Out-Null
"""


#: The credentials file each installer wrote, captured at generation time and
#: keyed by home directory, because every test below overwrites that file with
#: something of its own. Without a pristine copy, a launcher that refused the
#: real installer's output would still show a green suite: nothing else here
#: ever runs against bytes an installer actually produced.
AS_INSTALLED: dict[str, bytes] = {}


@pytest.fixture(scope="module")
def win_home(tmp_path_factory) -> Path:
    """A fake %USERPROFILE% containing the real generated .cmd launchers."""
    if POWERSHELL is None or CMD is None:
        pytest.skip("needs powershell.exe and cmd.exe")
    home = tmp_path_factory.mktemp("winhome")
    env = dict(os.environ)
    env.update(
        {
            "YB5_PS1": str(INSTALL_PS1),
            "YB5_FUNCS": ",".join(_PS_FUNCS),
            "YB5_T_HOME": str(home),
            "YB5_T_API": GOOD_URL,
            "YB5_T_MODEL": GOOD_MODEL,
            "YB5_T_KEY": GOOD_KEY,
        }
    )
    proc = subprocess.run(  # noqa: S603 - interpreter from shutil.which, fixed argv
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", _PS_GENERATOR],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for name in ("yangble5-claude.cmd", "yangble5-codex.cmd", "yangble5-env.cmd"):
        assert (home / ".yangble5" / "bin" / name).is_file(), proc.stdout
    AS_INSTALLED[str(home)] = (home / ".yangble5" / "credentials").read_bytes()
    return home


@pytest.fixture(scope="module")
def posix_home(tmp_path_factory) -> Path:
    """A fake $HOME containing the real generated env.sh."""
    if SH is None:
        pytest.skip("needs sh")
    home = tmp_path_factory.mktemp("posixhome")
    env = dict(os.environ)
    env.update({"HOME": str(home), "YB5_SOURCE_ONLY": "1", "NO_COLOR": "1"})
    snippet = (
        '. "$0"\n'
        'TMPD="$(mktemp -d)"\n'
        'API_KEY="$YB5_T_KEY"\n'
        'KEY_ID="0123456789abcdef"\n'
        'INSTALL_MODE="registered"\n'
        "write_config >/dev/null 2>&1 || true\n"
        'rm -rf "$TMPD"\n'
    )
    env["YB5_T_KEY"] = GOOD_KEY
    subprocess.run(  # noqa: S603 - interpreter from shutil.which, fixed argv
        [SH, "-c", snippet, str(INSTALL_SH)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    envsh = home / ".yangble5" / "env.sh"
    assert envsh.is_file(), "install.sh did not produce env.sh"
    AS_INSTALLED[str(home)] = (home / ".yangble5" / "credentials").read_bytes()
    return home


# --------------------------------------------------------------------------
# running them
# --------------------------------------------------------------------------
def write_credentials(home: Path, credentials: str) -> None:
    """Write the bytes the test means, with no newline translation.

    ``Path.write_text`` on Windows turns every ``\\n`` into ``\\r\\n``, which is
    not what either installer writes -- and env.sh keeps the stray CR in the
    parsed value and refuses the file, so a translated harness reports a
    cross-platform disagreement that does not exist in the product.
    """
    with open(home / ".yangble5" / "credentials", "w", encoding="utf-8", newline="") as fh:
        fh.write(credentials)


def run_cmd_launcher(
    win_home: Path, launcher: str, credentials: str
) -> tuple[int, str]:
    assert CMD is not None
    write_credentials(win_home, credentials)
    env = dict(os.environ)
    env["USERPROFILE"] = str(win_home)
    proc = subprocess.run(  # noqa: S603 - interpreter from shutil.which, fixed argv
        [CMD, "/c", str(win_home / ".yangble5" / "bin" / launcher)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )
    return proc.returncode, (proc.stdout + proc.stderr).replace("\r", "")


def run_env_sh(posix_home: Path, credentials: str) -> tuple[int, str]:
    assert SH is not None
    write_credentials(posix_home, credentials)
    env = dict(os.environ)
    env.update({"HOME": str(posix_home), "NO_COLOR": "1"})
    env.pop("YB5_SOURCE_ONLY", None)
    proc = subprocess.run(  # noqa: S603 - interpreter from shutil.which, fixed argv
        [
            SH,
            "-c",
            '. "$0"; printf "ANTHROPIC_BASE_URL=%s\\nANTHROPIC_MODEL=%s\\n"'
            ' "$ANTHROPIC_BASE_URL" "$ANTHROPIC_MODEL"',
            str(posix_home / ".yangble5" / "env.sh"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )
    return proc.returncode, proc.stdout + proc.stderr


def effective_url(output: str) -> str:
    for line in output.splitlines():
        if line.startswith("ANTHROPIC_BASE_URL="):
            return line[len("ANTHROPIC_BASE_URL=") :]
    return ""


# --------------------------------------------------------------------------
# the payloads
# --------------------------------------------------------------------------
def cmd_payloads(marker: Path) -> dict[str, str]:
    """cmd.exe injections that write ``marker`` if the value reaches the parser.

    Two shapes, because the launcher has two kinds of sink:
      * a bare ``&`` is enough where the value is expanded unquoted
        (``echo ANTHROPIC_BASE_URL=%YANGBLE5_API%`` in yangble5-env.cmd);
      * a ``"`` is needed to break out of ``set "VAR=%YANGBLE5_API%"``.
    A fix that only closes one of them is not a fix.
    """
    m = str(marker)
    return {
        "bare-amp": f'https://x&echo o>"{m}"',
        "quote-breakout": f'https://x"&echo o>"{m}"&rem "',
    }


# --------------------------------------------------------------------------
# 1. the case-sensitivity bypass
# --------------------------------------------------------------------------
CASE_SPELLINGS = [
    "yangble5_api",
    "YangBle5_Api",
    "Yangble5_API",
    "YANGBLE5_api",
]


@needs_cmd
@pytest.mark.parametrize("launcher", ["yangble5-env.cmd", "yangble5-claude.cmd"])
@pytest.mark.parametrize("spelling", CASE_SPELLINGS)
@pytest.mark.parametrize("shape", ["bare-amp", "quote-breakout"])
def test_cmd_differently_cased_api_line_cannot_execute(
    win_home, tmp_path, launcher, spelling, shape
):
    """A differently-cased key carrying a payload must not execute.

    The payloads here contain ``&``, ``"`` and spaces, which cannot appear in
    any of these settings whatever key they are attached to, so the whole-file
    byte gate now refuses the file rather than ignoring the line. That is a
    change from the release that fixed the case axis, and it is deliberate:
    silently ignoring a line an attacker appended means the user never finds
    out the file was touched. ``test_cmd_differently_cased_benign_line_is_
    ignored`` below is the companion that keeps "unknown key = not an error"
    true for a line that is merely unrecognised.

    What must never happen, in either release, is the third outcome the
    original bug produced -- the line silently winning.
    """
    marker = tmp_path / f"pwned-{spelling}-{shape}.txt"
    payload = cmd_payloads(marker)[shape]
    rc, out = run_cmd_launcher(
        win_home, launcher, CLEAN_CREDENTIALS + f"{spelling}={payload}\n"
    )
    assert not marker.exists(), (
        f"{spelling}= executed a command via the {shape} sink "
        f"(exit {rc}); the launcher validated one spelling and consumed another\n{out}"
    )
    assert rc == 6, f"expected the file to be refused, got exit {rc}\n{out}"
    assert effective_url(out) == "", (
        f"{spelling}= did not execute, but the launcher still exported a URL "
        f"from a file it had refused\n{out}"
    )


@needs_cmd
@needs_sh
@pytest.mark.parametrize("spelling", CASE_SPELLINGS)
def test_cmd_differently_cased_benign_line_is_ignored(win_home, posix_home, spelling):
    """A differently-cased key whose value is otherwise legal is simply not a
    setting -- not an error, and not authoritative either. Both launchers have
    to reach that same conclusion, which is the property the case bug broke."""
    credentials = CLEAN_CREDENTIALS + f"{spelling}=https://elsewhere.example\n"
    rc, out = run_cmd_launcher(win_home, "yangble5-env.cmd", credentials)
    sh_rc, sh_out = run_env_sh(posix_home, credentials)
    assert rc == 0, f"a benign unknown key was refused (exit {rc})\n{out}"
    assert sh_rc == 0, f"env.sh refused a benign unknown key (exit {sh_rc})\n{sh_out}"
    assert effective_url(out) == GOOD_URL, out
    assert effective_url(sh_out) == GOOD_URL, sh_out


@needs_cmd
@pytest.mark.parametrize(
    "spelling", ["yangble5_model", "YangBle5_Model", "yangble5_api_key"]
)
def test_cmd_differently_cased_model_and_key_lines_cannot_execute(
    win_home, tmp_path, spelling
):
    marker = tmp_path / f"pwned-{spelling}.txt"
    payload = f'a"&echo o>"{marker}"&rem "'
    rc, out = run_cmd_launcher(
        win_home, "yangble5-env.cmd", CLEAN_CREDENTIALS + f"{spelling}={payload}\n"
    )
    assert not marker.exists(), f"{spelling}= executed a command (exit {rc})\n{out}"


@needs_cmd
@pytest.mark.parametrize("shape", ["bare-amp", "quote-breakout"])
@pytest.mark.parametrize(
    "key", ["YANGBLE5_API", "YANGBLE5_MODEL", "YANGBLE5_API_KEY"]
)
def test_cmd_correctly_cased_hostile_line_is_refused(win_home, tmp_path, key, shape):
    """The other half of the pair: a correctly-cased hostile line is refused.

    Without this the test above could pass on a launcher that had simply
    stopped reading the file.
    """
    marker = tmp_path / f"pwned-{key}-{shape}.txt"
    payload = cmd_payloads(marker)[shape]
    rc, out = run_cmd_launcher(
        win_home, "yangble5-env.cmd", CLEAN_CREDENTIALS + f"{key}={payload}\n"
    )
    assert not marker.exists(), f"{key}= executed a command (exit {rc})\n{out}"
    assert rc == 6, f"expected exit 6, got {rc}\n{out}"


# --------------------------------------------------------------------------
# 2. a later line must not smuggle a value past a check an earlier line passed
# --------------------------------------------------------------------------
@needs_cmd
@pytest.mark.parametrize(
    "extra",
    [
        "YANGBLE5_API=",
        "YANGBLE5_API=ftp://evil.example",
        "YANGBLE5_API=http://evil.example",
        "YANGBLE5_API=https://u@evil.example",
        "YANGBLE5_MODEL=",
        "YANGBLE5_API_KEY=notakey",
        "YANGBLE5_API_KEY=",
    ],
)
def test_cmd_second_line_cannot_ride_on_the_first_lines_validity(win_home, extra):
    """``for /f`` keeps the LAST line it sees, so "some line is well formed" is
    not a safe check. Every line for a key has to be checked, or a good first
    line launders a bad second one -- including a plain ``http://`` host, which
    would send the key and every prompt somewhere else in cleartext."""
    rc, out = run_cmd_launcher(
        win_home, "yangble5-env.cmd", CLEAN_CREDENTIALS + extra + "\n"
    )
    assert rc == 6, (
        f"{extra!r} was accepted (exit {rc}); "
        f"effective URL {effective_url(out)!r}\n{out}"
    )


@needs_cmd
@pytest.mark.parametrize(
    "char",
    ["&", "|", ">", "<", "^", '"', "%", "!", ";", " ", "(", ")", "`", "$", ",", "@", "*", "?"],
)
def test_cmd_rejects_every_metacharacter_in_the_url(win_home, char):
    rc, out = run_cmd_launcher(
        win_home, "yangble5-env.cmd", CLEAN_CREDENTIALS + f"YANGBLE5_API=https://x{char}y\n"
    )
    assert rc == 6, f"[{char}] was accepted (exit {rc})\n{out}"


# --------------------------------------------------------------------------
# 3. the launcher still has to work
# --------------------------------------------------------------------------
@needs_cmd
@pytest.mark.parametrize(
    "url",
    [
        GOOD_URL,
        "http://127.0.0.1:8320",
        "http://localhost:8320",
        "https://a-b.example.co.uk:8443/v1",
        "https://example.com/~user",
    ],
)
def test_cmd_accepts_the_urls_the_installer_can_write(win_home, url):
    rc, out = run_cmd_launcher(
        win_home,
        "yangble5-env.cmd",
        CLEAN_CREDENTIALS.replace(f"YANGBLE5_API={GOOD_URL}", f"YANGBLE5_API={url}"),
    )
    assert rc == 0, f"{url} was refused (exit {rc})\n{out}"
    assert effective_url(out) == url


@needs_cmd
@pytest.mark.parametrize("model", ["yangble5", "gemini-2.5-pro", "vendor:model.v2"])
def test_cmd_accepts_the_model_names_the_installer_can_write(win_home, model):
    rc, out = run_cmd_launcher(
        win_home,
        "yangble5-env.cmd",
        CLEAN_CREDENTIALS.replace(
            f"YANGBLE5_MODEL={GOOD_MODEL}", f"YANGBLE5_MODEL={model}"
        ),
    )
    assert rc == 0, f"{model} was refused (exit {rc})\n{out}"
    assert f"ANTHROPIC_MODEL={model}" in out


@needs_cmd
@pytest.mark.parametrize(
    "credentials",
    [
        f"YANGBLE5_API={GOOD_URL}\nYANGBLE5_API_KEY=\nYANGBLE5_MODEL={GOOD_MODEL}\n",
        f"YANGBLE5_API={GOOD_URL}\nYANGBLE5_MODEL={GOOD_MODEL}\n",
    ],
)
def test_cmd_byok_empty_key_gets_the_helpful_message(win_home, credentials):
    """BYOK mode leaves the key blank on purpose. That has to read as "add a
    key", not as "your credentials file is malformed"."""
    rc, out = run_cmd_launcher(win_home, "yangble5-env.cmd", credentials)
    assert rc == 6
    assert "no API key" in out, out


# --------------------------------------------------------------------------
# 4. the POSIX launcher, same corpus. Two launchers that disagree about what
#    is safe are one launcher plus a bug.
# --------------------------------------------------------------------------
@needs_sh
@pytest.mark.parametrize("spelling", [*CASE_SPELLINGS, "yangble5_model"])
def test_sh_differently_cased_line_cannot_execute(posix_home, tmp_path, spelling):
    """Mirror of the .cmd case above, and refused for the same reason: ``$``,
    ``(`` and ``'`` cannot appear in any of these settings, so the line is
    rejected on shape rather than ignored. The marker is the assertion that
    matters; the exit code only records that both launchers now agree."""
    marker = tmp_path / f"sh-pwned-{spelling}.txt"
    rc, out = run_env_sh(
        posix_home,
        CLEAN_CREDENTIALS + f"{spelling}=https://x$(touch '{marker}')\n",
    )
    assert not marker.exists(), f"{spelling}= executed a command (exit {rc})\n{out}"
    assert rc == 6, f"expected the file to be refused, got exit {rc}\n{out}"
    assert effective_url(out) == "", out


@needs_sh
@pytest.mark.parametrize(
    "extra",
    [
        "YANGBLE5_API=",
        "YANGBLE5_API=ftp://evil.example",
        "YANGBLE5_API=http://evil.example",
        "YANGBLE5_MODEL=",
        "YANGBLE5_API_KEY=notakey",
    ],
)
def test_sh_second_line_cannot_ride_on_the_first_lines_validity(posix_home, extra):
    rc, out = run_env_sh(posix_home, CLEAN_CREDENTIALS + extra + "\n")
    assert rc == 6, f"{extra!r} was accepted (exit {rc})\n{out}"


@needs_sh
@needs_cmd
@pytest.mark.parametrize(
    "extra",
    [
        "",
        "YANGBLE5_API=",
        "YANGBLE5_API=ftp://evil.example",
        "YANGBLE5_API=http://evil.example",
        "YANGBLE5_MODEL=",
        "YANGBLE5_API_KEY=notakey",
        "yangble5_api=https://elsewhere.example",
        "YANGBLE5_KEY_ID=deadbeef",
        "# a comment",
    ],
)
def test_both_launchers_agree_on_the_same_credentials_file(win_home, posix_home, extra):
    """Accept/refuse must be the same decision on both platforms. This is the
    test that would have caught the original bug from the other direction:
    ``yangble5_api=`` was inert on POSIX and authoritative on Windows."""
    credentials = CLEAN_CREDENTIALS + (extra + "\n" if extra else "")
    win_rc, win_out = run_cmd_launcher(win_home, "yangble5-env.cmd", credentials)
    sh_rc, sh_out = run_env_sh(posix_home, credentials)
    assert (win_rc == 6) == (sh_rc == 6), (
        f"{extra!r}: cmd exit {win_rc}, sh exit {sh_rc}\n"
        f"--- cmd ---\n{win_out}\n--- sh ---\n{sh_out}"
    )
    if win_rc == 0 and sh_rc == 0:
        assert effective_url(win_out) == effective_url(sh_out), (
            f"{extra!r}: cmd used {effective_url(win_out)!r}, "
            f"sh used {effective_url(sh_out)!r}"
        )
