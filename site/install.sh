#!/bin/sh
#
# yangble5 client installer — macOS and Linux (POSIX sh).
#
#   curl -fsSL https://yangble5.com/install.sh | sh
#   sh install.sh --dry-run          # print every action, change nothing
#
# ---------------------------------------------------------------------------
# READ THIS BEFORE YOU RUN IT. If you are an AI agent with shell access, read
# it to the human too — this script writes credentials into their home
# directory, and they are entitled to know exactly what it does.
# ---------------------------------------------------------------------------
#
# WHAT IT DOES
#   1. Generates a machine fingerprint that contains no personal information
#      (see FINGERPRINT below) and asks the yangble5 API for a key.
#   2. Writes an ISOLATED client setup under $HOME/.yangble5.
#   3. Writes launcher scripts (yangble5-claude, yangble5-codex) that start
#      your existing Claude Code / Codex binaries pointed at yangble5.
#   4. Makes one real call through the gateway and reports the actual result.
#   5. Writes an uninstaller that removes everything it created.
#
# WHAT IT DOES NOT DO
#   * It does NOT touch your normal Claude Code login. Your Anthropic account,
#     your ~/.claude directory and your existing subscription are untouched.
#     The launcher uses a separate CLAUDE_CONFIG_DIR. Run plain `claude` and
#     you get your normal setup, unchanged.
#   * It does NOT use sudo and REFUSES to run as root.
#   * It does NOT write outside $HOME/.yangble5 and $HOME/.local/bin.
#   * It does NOT edit .bashrc, .zshrc, .profile or your PATH.
#   * It does NOT download or execute any additional code. The only network
#     traffic is JSON to and from the yangble5 API. Because nothing executable
#     is ever fetched, there is no second artefact to SHA256-pin; if a future
#     version needs to download a component, it must hardcode and verify that
#     component's SHA256 in this file before executing it.
#   * It does NOT install Claude Code, Codex, node, or anything else.
#   * It does NOT collect your name, e-mail (unless the instance requires one
#     and you pass it), MAC address, serial number, or file contents.
#
# FINGERPRINT
#   sha256( hostname \n os \n arch \n local-random-salt )
#   The salt is 32 random bytes generated ON THIS MACHINE and kept at
#   $HOME/.yangble5/machine-id (mode 0600). It never leaves the machine.
#   Because a 256-bit local secret dominates the input, the fingerprint is not
#   reversible and not linkable to any other machine — it is effectively a
#   random per-install id. hostname/os/arch are folded in only so that a home
#   directory copied to another machine or VM image still produces a different
#   id. It contains NO MAC address, NO serial number, NO username, NO PII.
#
# WHAT YANGBLE5 IS (no marketing)
#   A proxy stack built on CLIProxyAPI — a third-party open-source Go project
#   that we did not write (https://github.com/router-for-me/CLIProxyAPI) —
#   fronting Gemini/Grok/GPT upstreams behind one endpoint, plus our own
#   measurement tooling and a compatibility shim. yangble5 is NOT a model. It
#   is not a Taiwanese-trained LLM. There is no yangble5 LLM.
#
# HONEST LIMITS (repeated at the end of the run, on purpose)
#   * No live web search through the proxy. Asked for the current year on
#     2026-07-21, the Gemini upstream answered "2024" and Grok "2025". Treat
#     every answer as parametric recall.
#   * The 99.53% prompt-cache hit rate we publish is WARM-ROUND ONLY. The first
#     request of every session is a cold 0% cache write.
#   * Those numbers are one machine, one run, 2026-07-21. Not a benchmark suite.
#   * Capacity of any shared pool is small and funded by the operator
#     personally. Nothing here is unlimited, and it may say no.
#
# SECURITY MODEL
#   * set -eu, umask 077, every variable quoted.
#   * Refuses to run as root or under sudo.
#   * No `eval`, and nothing the server sends is ever executed. The API key is
#     validated against ^yb5_[0-9a-f]{16}_[A-Za-z0-9_-]+$ before it is written
#     anywhere; anything else aborts the install.
#   * The key is never passed on a command line (it would be visible to every
#     local user via `ps`). curl reads it from a 0600 config file instead.
#   * Any file that would be overwritten is first copied to
#     <file>.bak-<timestamp>.
#   * Re-running is safe and does not mint a second key.
#
# EXIT CODES
#   0  success
#   1  bad arguments
#   2  refused: running as root / under sudo
#   3  missing prerequisite (curl, sha256, /dev/urandom)
#   4  unsupported platform
#   5  the API could not be reached at all
#   6  installed in BYOK mode with no key yet — NOT usable until you supply one
#   7  could not write configuration
#   8  installed, but the live verification call failed (details printed)
#
# SPDX-License-Identifier: MIT
#

set -eu
umask 077

YB5_INSTALLER_VERSION="1.0.0"

# ── exit codes ─────────────────────────────────────────────────────────────
EX_OK=0
EX_USAGE=1
EX_ROOT=2
EX_PREREQ=3
EX_PLATFORM=4
EX_NETWORK=5
EX_REGISTER=6
EX_CONFIG=7
EX_VERIFY=8

# ── defaults (all overridable by flag or environment) ──────────────────────
YB5_API="${YANGBLE5_API:-https://yangble5.com}"
YB5_MODEL="${YANGBLE5_MODEL:-yangble5}"
YB5_HOME="${HOME:-}/.yangble5"
YB5_BIN="${YB5_HOME}/bin"
YB5_LINK_DIR="${HOME:-}/.local/bin"
YB5_EMAIL="${YANGBLE5_EMAIL:-}"
YB5_INVITE="${YANGBLE5_INVITE:-}"
YB5_BYOK_KEY="${YANGBLE5_API_KEY:-}"
YB5_MAX_OUTPUT="${YANGBLE5_MAX_OUTPUT_TOKENS:-65536}"
YB5_CONTEXT="${YANGBLE5_MAX_CONTEXT_TOKENS:-1000000}"
YB5_TIMEOUT_MS="${YANGBLE5_TIMEOUT_MS:-600000}"

DRY_RUN=0
DO_LIVE_TEST=1
FORCE_REGISTER=0
REINSTALL=0
LINK_BIN=1
PRINT_KEY=1

HTTP_STATUS=""
HTTP_TIME=""
HTTP_BODY=""
TMPD=""
BACKUPS=""
MODE="registered"      # registered | reused | byok | byok-empty

# ── output ─────────────────────────────────────────────────────────────────
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_RED=$(printf '\033[31m'); C_GRN=$(printf '\033[32m')
    C_YLW=$(printf '\033[33m'); C_BLU=$(printf '\033[36m')
    C_BLD=$(printf '\033[1m');  C_OFF=$(printf '\033[0m')
else
    C_RED=""; C_GRN=""; C_YLW=""; C_BLU=""; C_BLD=""; C_OFF=""
fi

ok()   { printf '%s  ok  %s %s\n' "$C_GRN" "$C_OFF" "$1"; }
info() { printf '       %s\n' "$1"; }
warn() { printf '%s  warn%s %s\n' "$C_YLW" "$C_OFF" "$1" >&2; }
step() { printf '\n%s%s-- %s%s\n' "$C_BLD" "$C_BLU" "$1" "$C_OFF"; }
fail() {
    printf '\n%s%sFAILED:%s %s\n' "$C_BLD" "$C_RED" "$C_OFF" "$1" >&2
    printf '        exit code %s\n\n' "$2" >&2
    exit "$2"
}

cleanup() {
    if [ -n "$TMPD" ] && [ -d "$TMPD" ]; then
        rm -rf "$TMPD"
    fi
}
trap cleanup EXIT HUP INT TERM

usage() {
    cat <<'USAGE'
usage: sh install.sh [options]

  --dry-run              print every action, write nothing, call nothing
  --api URL              yangble5 endpoint (default $YANGBLE5_API or
                         https://yangble5.com)
  --model NAME           model alias to configure (default yangble5)
  --email ADDR           e-mail, if the instance requires one to register
  --invite CODE          invite code, if the instance is invite-only
  --no-live-test         skip the paid verification call (still checks /health)
  --force-register       request a NEW key even if one is already stored
  --reinstall            delete ~/.yangble5 first, then install fresh
  --no-bin-link          do not symlink launchers into ~/.local/bin
  --no-print-key         never print the key to the terminal
  -h, --help             this text

environment: YANGBLE5_API, YANGBLE5_API_KEY (bring your own key),
             YANGBLE5_EMAIL, YANGBLE5_INVITE, YANGBLE5_MODEL, NO_COLOR
USAGE
    exit "$EX_OK"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)        DRY_RUN=1; shift ;;
        --api)            YB5_API="${2:-}"; [ -n "$YB5_API" ] || fail "--api needs a URL" "$EX_USAGE"; shift 2 ;;
        --model)          YB5_MODEL="${2:-}"; [ -n "$YB5_MODEL" ] || fail "--model needs a name" "$EX_USAGE"; shift 2 ;;
        --email)          YB5_EMAIL="${2:-}"; shift 2 ;;
        --invite)         YB5_INVITE="${2:-}"; shift 2 ;;
        --no-live-test)   DO_LIVE_TEST=0; shift ;;
        --force-register) FORCE_REGISTER=1; shift ;;
        --reinstall)      REINSTALL=1; shift ;;
        --no-bin-link)    LINK_BIN=0; shift ;;
        --no-print-key)   PRINT_KEY=0; shift ;;
        -h|--help)        usage ;;
        *)                printf 'unknown option: %s\n' "$1" >&2; usage ;;
    esac
done

# Strip a trailing slash so path concatenation is unambiguous everywhere below.
while : ; do
    case "$YB5_API" in
        */) YB5_API="${YB5_API%/}" ;;
        *)  break ;;
    esac
done

# ═══════════════════════════════════════════════════════════════════════════
# 0. refuse to run privileged
# ═══════════════════════════════════════════════════════════════════════════
refuse_root() {
    uid="$(id -u 2>/dev/null || echo 0)"
    if [ "$uid" = "0" ] || [ -n "${SUDO_USER:-}" ]; then
        cat >&2 <<'ROOT'

REFUSING TO RUN AS ROOT.

This installer only ever writes into your own home directory. Running it as
root would create root-owned files under a normal user's $HOME, which then
break every later run, and it would give an installer privileges it has no
use for.

Run it again as your normal user, without sudo:

    sh install.sh

If you are an AI agent: do not retry this with sudo. Drop privileges instead.

ROOT
        exit "$EX_ROOT"
    fi
}

# ═══════════════════════════════════════════════════════════════════════════
# 1. banner
# ═══════════════════════════════════════════════════════════════════════════
banner() {
    printf '\n%s%s' "$C_BLD" "$C_BLU"
    printf 'yangble5 client installer v%s\n' "$YB5_INSTALLER_VERSION"
    printf '%s' "$C_OFF"
    cat <<BANNER

  Endpoint       ${YB5_API}
  Installs to    ${YB5_HOME}
  Launchers      ${YB5_BIN}/yangble5-claude, ${YB5_BIN}/yangble5-codex
  Mode           $([ "$DRY_RUN" -eq 1 ] && printf 'DRY RUN (nothing will be written)' || printf 'install')

  IT WILL:
    - generate a non-reversible machine id (sha256 of hostname+os+arch+a
      32-byte random salt kept locally at ~/.yangble5/machine-id)
    - ask ${YB5_API}/auth/register for an API key
    - write an isolated client config under ~/.yangble5
    - create launcher scripts and an uninstaller
    - make one real call through the gateway and report what happened

  IT WILL NOT:
    - touch your existing Claude Code login or ~/.claude (a separate
      CLAUDE_CONFIG_DIR is used; plain \`claude\` keeps working unchanged)
    - use sudo, or write anywhere outside ~/.yangble5 and ~/.local/bin
    - modify your PATH, .bashrc, .zshrc or .profile
    - download or execute any code — the only traffic is JSON to the API
    - send your name, e-mail, MAC address, serial number or any file contents

  yangble5 is a PROXY built on the third-party CLIProxyAPI project. It is not
  a model, and there is no yangble5 LLM.

BANNER
}

# ═══════════════════════════════════════════════════════════════════════════
# 2. preflight
# ═══════════════════════════════════════════════════════════════════════════
have() { command -v "$1" >/dev/null 2>&1; }

sha256_stdin() {
    if have sha256sum; then
        sha256sum | cut -d' ' -f1
    elif have shasum; then
        shasum -a 256 | cut -d' ' -f1
    elif have openssl; then
        openssl dgst -sha256 | sed 's/^.*= *//'
    else
        return 1
    fi
}

random_hex32() {
    if have openssl; then
        openssl rand -hex 32
        return 0
    fi
    if [ -r /dev/urandom ] && have od; then
        od -An -N32 -tx1 < /dev/urandom | tr -d ' \n'
        printf '\n'
        return 0
    fi
    return 1
}

preflight() {
    step "preflight"

    [ -n "${HOME:-}" ] || fail "\$HOME is not set; refusing to guess where to install." "$EX_PREREQ"
    [ -d "$HOME" ]     || fail "\$HOME ($HOME) is not a directory." "$EX_PREREQ"

    have curl || fail "curl is required and was not found in PATH.
        macOS:  it ships with the system
        Debian: sudo apt-get install curl
        Alpine: sudo apk add curl" "$EX_PREREQ"
    ok "curl $(curl --version 2>/dev/null | head -n 1 | cut -d' ' -f2)"

    if ! printf 'x' | sha256_stdin >/dev/null 2>&1; then
        fail "no SHA-256 tool found (need one of: sha256sum, shasum, openssl)." "$EX_PREREQ"
    fi
    ok "sha256 available"

    if ! random_hex32 >/dev/null 2>&1; then
        fail "no cryptographic random source (need openssl or a readable /dev/urandom).
        Refusing to continue: a weak salt would make the machine id guessable." "$EX_PREREQ"
    fi
    ok "csprng available"

    OS_NAME="$(uname -s 2>/dev/null || echo unknown)"
    ARCH_NAME="$(uname -m 2>/dev/null || echo unknown)"
    case "$OS_NAME" in
        Linux|Darwin)  ok "platform ${OS_NAME}/${ARCH_NAME}" ;;
        FreeBSD|OpenBSD|NetBSD)
            warn "platform ${OS_NAME}/${ARCH_NAME} is untested; continuing anyway" ;;
        *)
            fail "unsupported platform: ${OS_NAME}/${ARCH_NAME}.
        This installer supports macOS and Linux. On Windows use install.ps1." "$EX_PLATFORM" ;;
    esac

    case "$YB5_API" in
        https://*) : ;;
        http://127.0.0.1*|http://localhost*)
            warn "using plaintext HTTP to a local endpoint (${YB5_API}) — fine for testing" ;;
        http://*)
            fail "refusing to send an API key over plaintext HTTP to a remote host: ${YB5_API}" "$EX_USAGE" ;;
        *)
            fail "--api must be a http(s) URL, got: ${YB5_API}" "$EX_USAGE" ;;
    esac

    TMPD="$(mktemp -d 2>/dev/null || mktemp -d -t yangble5)" || \
        fail "could not create a temporary directory" "$EX_PREREQ"
    chmod 700 "$TMPD"

    if ! have claude; then
        warn "\`claude\` is not in PATH — the launcher will be written anyway,
       but you need Claude Code installed for it to do anything:
       https://claude.com/product/claude-code"
    fi
    if ! have codex; then
        info "note: \`codex\` is not in PATH; the Codex launcher is written anyway"
    fi
}

# ═══════════════════════════════════════════════════════════════════════════
# 3. filesystem helpers (dry-run aware, backup-on-overwrite)
# ═══════════════════════════════════════════════════════════════════════════
timestamp() { date +%Y%m%d-%H%M%S; }

ensure_dir() {
    if [ "$DRY_RUN" -eq 1 ]; then
        [ -d "$1" ] || info "would create directory $1"
        return 0
    fi
    mkdir -p "$1" || fail "could not create $1" "$EX_CONFIG"
    chmod 700 "$1" 2>/dev/null || true
}

# write_file <dest> <mode> [nobackup]   — content on stdin
#
# `nobackup` is for files this installer owns outright (INSTALL_INFO), whose
# content changes every run by design. Backing those up would leave a trail of
# .bak files behind for no benefit. Everything a user might have edited is
# always backed up.
write_file() {
    wf_dest="$1"
    wf_mode="$2"
    wf_nobak="${3:-}"
    wf_tmp="${TMPD}/write.$$"
    cat > "$wf_tmp"

    if [ "$DRY_RUN" -eq 1 ]; then
        wf_bytes="$(wc -c < "$wf_tmp" | tr -d ' ')"
        if [ -f "$wf_dest" ]; then
            info "would back up and overwrite ${wf_dest} (${wf_bytes} bytes, mode ${wf_mode})"
        else
            info "would write ${wf_dest} (${wf_bytes} bytes, mode ${wf_mode})"
        fi
        rm -f "$wf_tmp"
        return 0
    fi

    if [ -f "$wf_dest" ]; then
        if cmp -s "$wf_tmp" "$wf_dest"; then
            info "unchanged ${wf_dest}"
            rm -f "$wf_tmp"
            return 0
        fi
        if [ "$wf_nobak" != "nobackup" ]; then
            wf_bak="${wf_dest}.bak-$(timestamp)"
            cp -p "$wf_dest" "$wf_bak" || fail "could not back up ${wf_dest}" "$EX_CONFIG"
            BACKUPS="${BACKUPS}${wf_bak}
"
            warn "backed up existing ${wf_dest} -> ${wf_bak}"
        fi
    fi

    ensure_dir "$(dirname "$wf_dest")"
    cp "$wf_tmp" "$wf_dest" || fail "could not write ${wf_dest}" "$EX_CONFIG"
    chmod "$wf_mode" "$wf_dest" || fail "could not chmod ${wf_dest}" "$EX_CONFIG"
    rm -f "$wf_tmp"
    ok "wrote ${wf_dest}"
}

# ═══════════════════════════════════════════════════════════════════════════
# 4. machine fingerprint  (see FINGERPRINT in the header)
# ═══════════════════════════════════════════════════════════════════════════
MACHINE_SALT=""

# Creates the salt if absent and reports what it did. Kept separate from
# machine_fingerprint() because that function's stdout IS the fingerprint —
# anything else printed there would be captured into the value.
ensure_machine_salt() {
    ems_file="${YB5_HOME}/machine-id"

    if [ -f "$ems_file" ]; then
        MACHINE_SALT="$(cat "$ems_file")"
        [ -n "$MACHINE_SALT" ] || fail "${ems_file} is empty; delete it and re-run." "$EX_PREREQ"
        return 0
    fi

    MACHINE_SALT="$(random_hex32)"
    if [ "$DRY_RUN" -eq 1 ]; then
        info "would create ${ems_file} (32-byte local random salt, mode 0600)"
        return 0
    fi
    ensure_dir "$YB5_HOME"
    printf '%s\n' "$MACHINE_SALT" > "$ems_file"
    chmod 600 "$ems_file"
}

# Pure: prints the fingerprint and nothing else.
machine_fingerprint() {
    # Order and separators are fixed so the id is stable across runs.
    printf '%s\n%s\n%s\n%s\n' \
        "$(uname -n 2>/dev/null || echo unknown)" \
        "$(uname -s 2>/dev/null || echo unknown)" \
        "$(uname -m 2>/dev/null || echo unknown)" \
        "$MACHINE_SALT" | sha256_stdin
}

# ═══════════════════════════════════════════════════════════════════════════
# 5. HTTP  (key never appears in argv — curl reads it from a 0600 config file)
# ═══════════════════════════════════════════════════════════════════════════

# http_call <method> <path> <body-file|-> <auth: key|"">
# Sets HTTP_STATUS, HTTP_TIME, HTTP_BODY. Returns 1 if curl itself failed.
http_call() {
    hc_method="$1"
    hc_path="$2"
    hc_body="$3"
    hc_key="$4"

    hc_out="${TMPD}/resp.$$"
    hc_cfg="${TMPD}/curlrc.$$"

    : > "$hc_cfg"
    chmod 600 "$hc_cfg"
    {
        # No `location` entry: curl does not follow redirects unless asked, and
        # a followed redirect would hand the API key to whatever host the
        # redirect named. Leave it off.
        printf 'silent\n'
        printf 'show-error\n'
        printf 'max-time = 120\n'
        printf 'connect-timeout = 15\n'
        printf 'request = "%s"\n' "$hc_method"
        printf 'header = "content-type: application/json"\n'
        printf 'header = "accept: application/json"\n'
        printf 'header = "user-agent: yangble5-installer/%s"\n' "$YB5_INSTALLER_VERSION"
        printf 'header = "anthropic-version: 2023-06-01"\n'
        if [ -n "$hc_key" ]; then
            # Safe to interpolate: validated against a strict charset before use.
            printf 'header = "x-api-key: %s"\n' "$hc_key"
            printf 'header = "authorization: Bearer %s"\n' "$hc_key"
        fi
        if [ "$hc_body" != "-" ]; then
            printf 'data-binary = "@%s"\n' "$hc_body"
        fi
        printf 'output = "%s"\n' "$hc_out"
        printf 'write-out = "%%{http_code} %%{time_total}"\n'
        printf 'url = "%s%s"\n' "$YB5_API" "$hc_path"
    } >> "$hc_cfg"

    HTTP_STATUS=""; HTTP_TIME=""; HTTP_BODY=""
    if ! hc_meta="$(curl --config "$hc_cfg" 2>"${TMPD}/curlerr.$$")"; then
        HTTP_BODY="$(cat "${TMPD}/curlerr.$$" 2>/dev/null || true)"
        rm -f "$hc_cfg" "$hc_out" "${TMPD}/curlerr.$$"
        return 1
    fi
    HTTP_STATUS="${hc_meta%% *}"
    HTTP_TIME="${hc_meta##* }"
    HTTP_BODY="$(cat "$hc_out" 2>/dev/null || true)"
    rm -f "$hc_cfg" "$hc_out" "${TMPD}/curlerr.$$"
    return 0
}

# json_string <field> — reads JSON on stdin, prints the first string value.
# Deliberately dumb: no eval, no shell expansion of server data, and every
# value it produces is validated by the caller before it is used.
json_string() {
    tr ',' '\n' | \
        sed -n 's/.*"'"$1"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | \
        head -n 1
}

valid_key() {
    # yb5_<16 hex>_<url-safe secret>  — see gateway/storage.py:parse_key
    printf '%s' "$1" | grep -Eq '^yb5_[0-9a-f]{16}_[A-Za-z0-9_-]{16,}$'
}

# ═══════════════════════════════════════════════════════════════════════════
# 6. existing install
# ═══════════════════════════════════════════════════════════════════════════
detect_existing() {
    step "existing install"

    if [ "$REINSTALL" -eq 1 ] && [ -d "$YB5_HOME" ]; then
        if [ "$DRY_RUN" -eq 1 ]; then
            info "would delete ${YB5_HOME} (--reinstall)"
        else
            warn "--reinstall: deleting ${YB5_HOME}"
            rm -rf "$YB5_HOME"
        fi
    fi

    if [ -f "${YB5_HOME}/INSTALL_INFO" ]; then
        de_prev="$(sed -n 's/^installer_version=//p' "${YB5_HOME}/INSTALL_INFO" | head -n 1)"
        de_when="$(sed -n 's/^installed_at=//p' "${YB5_HOME}/INSTALL_INFO" | head -n 1)"
        ok "found an existing install (v${de_prev:-?}, ${de_when:-unknown})"
        info "updating it in place; your stored key is kept and re-used"
        info "(use --force-register for a new key, --reinstall to start clean)"
    else
        ok "no previous install found"
    fi
}

# ═══════════════════════════════════════════════════════════════════════════
# 7. obtain a key: stored -> BYOK -> register -> BYOK fallthrough
# ═══════════════════════════════════════════════════════════════════════════
CRED_FILE="${YB5_HOME}/credentials"
API_KEY=""
KEY_ID=""

read_stored_key() {
    [ -f "$CRED_FILE" ] || return 1
    rs_key="$(sed -n 's/^YANGBLE5_API_KEY=//p' "$CRED_FILE" | head -n 1)"
    valid_key "$rs_key" || return 1
    API_KEY="$rs_key"
    KEY_ID="$(sed -n 's/^YANGBLE5_KEY_ID=//p' "$CRED_FILE" | head -n 1)"
    return 0
}

byok_instructions() {
    cat <<BYOK

  ${C_BLD}Bring your own key / your own upstream account${C_OFF}

  The shared pool is funded out of the operator's own pocket and is small.
  When it is full it says so instead of quietly degrading. Two ways forward:

  1. Someone gives you an invite code for this instance:
         sh install.sh --invite YOUR_CODE

  2. You run the stack yourself against your own upstream account — this is
     the path that always works and costs the operator nothing:
         https://github.com/shark0120/yangble5#quick-start
     Then point this installer at your own gateway:
         sh install.sh --api http://127.0.0.1:8320

  3. You already have a yangble5 key:
         YANGBLE5_API_KEY=yb5_... sh install.sh

BYOK
}

obtain_key() {
    step "credentials"

    if [ "$FORCE_REGISTER" -eq 0 ] && read_stored_key; then
        ok "re-using the key already stored at ${CRED_FILE}"
        info "key_id ${KEY_ID:-unknown} (re-running is idempotent; no new key minted)"
        MODE="reused"
        return 0
    fi

    if [ -n "$YB5_BYOK_KEY" ]; then
        if ! valid_key "$YB5_BYOK_KEY"; then
            fail "YANGBLE5_API_KEY is set but is not a valid yangble5 key.
        Expected the form yb5_<16 hex>_<secret>." "$EX_USAGE"
        fi
        API_KEY="$YB5_BYOK_KEY"
        KEY_ID="$(printf '%s' "$API_KEY" | cut -d_ -f2)"
        ok "using the key supplied in YANGBLE5_API_KEY (no registration needed)"
        MODE="byok"
        return 0
    fi

    ensure_machine_salt
    FINGERPRINT="$(machine_fingerprint)"
    ok "machine id ${FINGERPRINT}"
    info "= sha256(hostname + os + arch + local random salt). Not reversible."
    info "  No MAC address, no serial number, no username, no PII."

    if [ "$DRY_RUN" -eq 1 ]; then
        info "would POST ${YB5_API}/auth/register with label=installer-<machine id>"
        info "would store the returned key at ${CRED_FILE} (mode 0600)"
        API_KEY="yb5_0000000000000000_DRYRUNDRYRUNDRYRUNxx"
        KEY_ID="0000000000000000"
        MODE="registered"
        return 0
    fi

    # Validate anything that goes into the JSON body, so we never need to
    # escape it and can never inject into it.
    if [ -n "$YB5_EMAIL" ]; then
        printf '%s' "$YB5_EMAIL" | grep -Eq '^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$' || \
            fail "--email does not look like an e-mail address: ${YB5_EMAIL}" "$EX_USAGE"
    fi
    if [ -n "$YB5_INVITE" ]; then
        printf '%s' "$YB5_INVITE" | grep -Eq '^[A-Za-z0-9_-]{1,200}$' || \
            fail "--invite contains characters an invite code cannot have." "$EX_USAGE"
    fi

    reg_body="${TMPD}/register.json"
    {
        printf '{"label":"installer-%s"' "$(printf '%s' "$FINGERPRINT" | cut -c1-32)"
        if [ -n "$YB5_EMAIL" ]; then
            printf ',"email":"%s"' "$YB5_EMAIL"
        fi
        if [ -n "$YB5_INVITE" ]; then
            printf ',"invite_code":"%s"' "$YB5_INVITE"
        fi
        printf '}\n'
    } > "$reg_body"
    chmod 600 "$reg_body"
    # NOTE: the gateway's RegisterRequest accepts email / invite_code / label
    # only (gateway/app.py). The fingerprint therefore travels as `label` —
    # the one field the server actually persists — rather than as a field the
    # server would silently discard.

    info "POST ${YB5_API}/auth/register"
    if ! http_call POST /auth/register "$reg_body" ""; then
        cat >&2 <<NET

Could not reach ${YB5_API} at all.

  ${HTTP_BODY}

Troubleshooting, in order:
  curl -v --max-time 15 ${YB5_API}/health
  # DNS?      -> getent hosts yangble5.com   (or: dscacheutil -q host -a name yangble5.com)
  # Proxy?    -> echo "\$HTTPS_PROXY \$https_proxy \$NO_PROXY"
  # TLS?      -> curl -vI ${YB5_API}/health 2>&1 | grep -i 'SSL\|certificate'
NET
        exit "$EX_NETWORK"
    fi

    case "$HTTP_STATUS" in
        200|201)
            rk="$(printf '%s' "$HTTP_BODY" | json_string api_key)"
            if ! valid_key "$rk"; then
                fail "the server replied ${HTTP_STATUS} but the body did not contain a
        well-formed yangble5 key. Refusing to write anything.
        Response (first 400 chars):
        $(printf '%s' "$HTTP_BODY" | cut -c1-400)" "$EX_REGISTER"
            fi
            API_KEY="$rk"
            KEY_ID="$(printf '%s' "$HTTP_BODY" | json_string key_id)"
            case "$KEY_ID" in
                *[!0-9a-f]*|"") KEY_ID="$(printf '%s' "$API_KEY" | cut -d_ -f2)" ;;
            esac
            ok "registered — key_id ${KEY_ID}"
            MODE="registered"
            return 0
            ;;
        403|409|429|503)
            et="$(printf '%s' "$HTTP_BODY" | json_string type)"
            em="$(printf '%s' "$HTTP_BODY" | json_string message)"
            warn "the instance declined to issue a key (HTTP ${HTTP_STATUS}${et:+, ${et}})"
            [ -n "$em" ] && info "server said: ${em}"
            info "this is NOT an installer failure — falling through to BYOK mode"
            byok_instructions
            MODE="byok-empty"
            API_KEY=""
            KEY_ID=""
            return 0
            ;;
        400)
            em="$(printf '%s' "$HTTP_BODY" | json_string message)"
            warn "the instance rejected the registration request (HTTP 400)"
            [ -n "$em" ] && info "server said: ${em}"
            info "most often this means the instance requires an e-mail address:"
            info "    sh install.sh --email you@example.com"
            byok_instructions
            MODE="byok-empty"
            API_KEY=""
            KEY_ID=""
            return 0
            ;;
        *)
            fail "unexpected reply from ${YB5_API}/auth/register: HTTP ${HTTP_STATUS}
        Body (first 400 chars):
        $(printf '%s' "$HTTP_BODY" | cut -c1-400)" "$EX_REGISTER" ;;
    esac
}

# ═══════════════════════════════════════════════════════════════════════════
# 8. write the isolated client configuration
# ═══════════════════════════════════════════════════════════════════════════
write_config() {
    step "writing configuration"

    ensure_dir "$YB5_HOME"
    ensure_dir "$YB5_BIN"
    ensure_dir "${YB5_HOME}/claude"
    ensure_dir "${YB5_HOME}/codex"

    # -- credentials (0600) --------------------------------------------------
    if [ -n "$API_KEY" ]; then
        write_file "$CRED_FILE" 600 <<CRED
# yangble5 credentials — mode 0600, never commit this file.
# Delete this file (or run yangble5-uninstall) to revoke it locally.
YANGBLE5_API=${YB5_API}
YANGBLE5_API_KEY=${API_KEY}
YANGBLE5_KEY_ID=${KEY_ID}
YANGBLE5_MODEL=${YB5_MODEL}
CRED
    else
        write_file "$CRED_FILE" 600 <<CRED
# yangble5 credentials — BYOK mode, no key yet.
# Put your key on the YANGBLE5_API_KEY line below and everything starts working.
YANGBLE5_API=${YB5_API}
YANGBLE5_API_KEY=
YANGBLE5_KEY_ID=
YANGBLE5_MODEL=${YB5_MODEL}
CRED
    fi

    # -- shared environment, sourced by both launchers -----------------------
    write_file "${YB5_HOME}/env.sh" 600 <<ENV
# yangble5 launcher environment. Sourced by the yangble5-* launchers.
# Editing this file changes how the launchers behave; it affects nothing else
# on this machine.

. "\${HOME}/.yangble5/credentials"

if [ -z "\${YANGBLE5_API_KEY:-}" ]; then
    printf 'yangble5: no API key in %s\n' "\${HOME}/.yangble5/credentials" >&2
    printf 'yangble5: add one, or re-run the installer.\n' >&2
    exit 6
fi

export YANGBLE5_API YANGBLE5_API_KEY YANGBLE5_MODEL

# --- Claude Code -----------------------------------------------------------
# CLAUDE_CONFIG_DIR is what keeps your real login untouched: Claude Code keeps
# its auth and settings per-config-dir, so this session cannot see, use, or
# damage the credentials in ~/.claude.
export CLAUDE_CONFIG_DIR="\${HOME}/.yangble5/claude"
export ANTHROPIC_BASE_URL="\${YANGBLE5_API}"
export ANTHROPIC_AUTH_TOKEN="\${YANGBLE5_API_KEY}"
export ANTHROPIC_MODEL="\${YANGBLE5_MODEL}"
# Claude Code assumes a 200K window for model names it does not recognise, and
# 'yangble5' is by construction a name it has never heard of — so it would
# auto-compact early, and every compaction is a cache-destroying rewrite.
# Official env var, Claude Code v2.1.193+.
# This does NOT create context: it moves where the client decides to compact.
# We verified a 748,918-token prompt end to end. We did not verify 1,000,000.
export CLAUDE_CODE_MAX_CONTEXT_TOKENS=${YB5_CONTEXT}
export CLAUDE_CODE_MAX_OUTPUT_TOKENS=${YB5_MAX_OUTPUT}
export API_TIMEOUT_MS=${YB5_TIMEOUT_MS}
# ANTHROPIC_API_KEY would take precedence over ANTHROPIC_AUTH_TOKEN and send
# your real Anthropic key to this proxy. Removed from the launcher environment.
unset ANTHROPIC_API_KEY

# --- Codex -----------------------------------------------------------------
export CODEX_HOME="\${HOME}/.yangble5/codex"
ENV

    # -- Codex config --------------------------------------------------------
    write_file "${YB5_HOME}/codex/config.toml" 600 <<TOML
# yangble5 — isolated Codex configuration (CODEX_HOME=~/.yangble5/codex).
# Your normal ~/.codex is untouched.
model = "${YB5_MODEL}"
model_provider = "yangble5"
# See the note in env.sh: a larger window does not create context, it only
# changes where the client compacts.
model_context_window = ${YB5_CONTEXT}
model_max_output_tokens = ${YB5_MAX_OUTPUT}

[model_providers.yangble5]
name = "yangble5"
base_url = "${YB5_API}/v1"
env_key = "YANGBLE5_API_KEY"
wire_api = "chat"
TOML

    # -- Claude Code isolated config dir marker ------------------------------
    write_file "${YB5_HOME}/claude/README.txt" 600 <<CLAUDEDIR
This directory is CLAUDE_CONFIG_DIR for the yangble5-claude launcher only.

Claude Code stores its auth and settings per config directory, so anything in
here is separate from your real ~/.claude. Deleting this directory logs out
the yangble5 session and nothing else.
CLAUDEDIR

    # -- launchers -----------------------------------------------------------
    write_file "${YB5_BIN}/yangble5-claude" 700 <<'LAUNCH'
#!/bin/sh
# yangble5-launcher
# Starts your existing Claude Code binary against yangble5, in an isolated
# config dir. Your normal `claude` command is completely unaffected.
set -eu
. "${HOME}/.yangble5/env.sh"
if ! command -v claude >/dev/null 2>&1; then
    printf 'yangble5: Claude Code (`claude`) is not in PATH.\n' >&2
    printf 'yangble5: install it first: https://claude.com/product/claude-code\n' >&2
    exit 3
fi
exec claude "$@"
LAUNCH

    write_file "${YB5_BIN}/yangble5-codex" 700 <<'LAUNCH'
#!/bin/sh
# yangble5-launcher
# Starts your existing Codex binary against yangble5, in an isolated
# CODEX_HOME. Your normal `codex` command is completely unaffected.
set -eu
. "${HOME}/.yangble5/env.sh"
if ! command -v codex >/dev/null 2>&1; then
    printf 'yangble5: Codex (`codex`) is not in PATH.\n' >&2
    exit 3
fi
exec codex "$@"
LAUNCH

    write_file "${YB5_BIN}/yangble5-env" 700 <<'LAUNCH'
#!/bin/sh
# yangble5-launcher
# Prints the environment the launchers use, with the key redacted, so you can
# see exactly what is being set without exposing the secret.
set -eu
. "${HOME}/.yangble5/env.sh"
printf 'ANTHROPIC_BASE_URL=%s\n'            "${ANTHROPIC_BASE_URL}"
printf 'ANTHROPIC_MODEL=%s\n'               "${ANTHROPIC_MODEL}"
printf 'ANTHROPIC_AUTH_TOKEN=%s...redacted\n' "$(printf '%s' "${ANTHROPIC_AUTH_TOKEN}" | cut -c1-24)"
printf 'CLAUDE_CONFIG_DIR=%s\n'             "${CLAUDE_CONFIG_DIR}"
printf 'CLAUDE_CODE_MAX_CONTEXT_TOKENS=%s\n' "${CLAUDE_CODE_MAX_CONTEXT_TOKENS}"
printf 'CLAUDE_CODE_MAX_OUTPUT_TOKENS=%s\n'  "${CLAUDE_CODE_MAX_OUTPUT_TOKENS}"
printf 'API_TIMEOUT_MS=%s\n'                "${API_TIMEOUT_MS}"
printf 'CODEX_HOME=%s\n'                    "${CODEX_HOME}"
LAUNCH

    write_uninstaller

    # -- install marker ------------------------------------------------------
    write_file "${YB5_HOME}/INSTALL_INFO" 600 nobackup <<INFO
installer_version=${YB5_INSTALLER_VERSION}
installed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date)
api=${YB5_API}
model=${YB5_MODEL}
mode=${MODE}
platform=${OS_NAME}/${ARCH_NAME}
INFO

    link_launchers
}

link_launchers() {
    [ "$LINK_BIN" -eq 1 ] || { info "skipping ~/.local/bin symlinks (--no-bin-link)"; return 0; }

    if [ "$DRY_RUN" -eq 1 ]; then
        info "would symlink yangble5-claude, yangble5-codex, yangble5-env,"
        info "  yangble5-uninstall from ${YB5_LINK_DIR} -> ${YB5_BIN}"
        return 0
    fi

    mkdir -p "$YB5_LINK_DIR" 2>/dev/null || {
        warn "could not create ${YB5_LINK_DIR}; launchers stay in ${YB5_BIN}"
        return 0
    }

    for ll_name in yangble5-claude yangble5-codex yangble5-env yangble5-uninstall; do
        ll_dest="${YB5_LINK_DIR}/${ll_name}"
        if [ -e "$ll_dest" ] && [ ! -L "$ll_dest" ]; then
            warn "${ll_dest} exists and is not a symlink — leaving it alone"
            continue
        fi
        ln -sf "${YB5_BIN}/${ll_name}" "$ll_dest"
    done
    ok "linked launchers into ${YB5_LINK_DIR}"

    case ":${PATH}:" in
        *":${YB5_LINK_DIR}:"*) ok "${YB5_LINK_DIR} is already on your PATH" ;;
        *)
            warn "${YB5_LINK_DIR} is NOT on your PATH."
            info "This installer will not edit your shell rc files. Add it yourself:"
            info "    echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.profile"
            info "Or call the launcher by its full path: ${YB5_BIN}/yangble5-claude"
            ;;
    esac
}

write_uninstaller() {
    write_file "${YB5_BIN}/yangble5-uninstall" 700 <<'UNINST'
#!/bin/sh
# yangble5-launcher
# Convenience wrapper: runs the real uninstaller if it is next to this file,
# otherwise removes ~/.yangble5 and the symlinks that point into it.
set -eu
if [ -f "${HOME}/.yangble5/uninstall.sh" ]; then
    exec sh "${HOME}/.yangble5/uninstall.sh" "$@"
fi
printf 'yangble5: ~/.yangble5/uninstall.sh is missing.\n' >&2
printf 'yangble5: remove it by hand with:  rm -rf ~/.yangble5\n' >&2
exit 1
UNINST

    write_file "${YB5_HOME}/uninstall.sh" 700 <<'UNINSTALLER'
#!/bin/sh
# yangble5 uninstaller (installed copy). See site/uninstall.sh in the repo.
set -eu

YB5_HOME="${HOME}/.yangble5"
LINK_DIR="${HOME}/.local/bin"
ASSUME_YES=0
DRY_RUN=0

while [ $# -gt 0 ]; do
    case "$1" in
        -y|--yes)  ASSUME_YES=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        *)         printf 'usage: yangble5-uninstall [--yes] [--dry-run]\n' >&2; exit 1 ;;
    esac
done

printf '\nyangble5 uninstaller — this will delete:\n\n'
if [ -d "$YB5_HOME" ]; then
    printf '  %s   (whole directory, including your API key)\n' "$YB5_HOME"
else
    printf '  (nothing at %s)\n' "$YB5_HOME"
fi
for n in yangble5-claude yangble5-codex yangble5-env yangble5-uninstall; do
    l="${LINK_DIR}/${n}"
    if [ -L "$l" ]; then
        t="$(readlink "$l" 2>/dev/null || echo '')"
        case "$t" in
            "${YB5_HOME}"/*) printf '  %s -> %s\n' "$l" "$t" ;;
            *)               printf '  (skipping %s: does not point into %s)\n' "$l" "$YB5_HOME" ;;
        esac
    fi
done
printf '\nIt will NOT touch ~/.claude, ~/.codex, your shell rc files, or anything\n'
printf 'outside the paths listed above. Your key is deleted locally only; ask the\n'
printf 'operator to revoke it server-side if it may have leaked.\n\n'

if [ "$DRY_RUN" -eq 1 ]; then
    printf 'dry run — nothing deleted.\n\n'
    exit 0
fi

if [ "$ASSUME_YES" -ne 1 ]; then
    if [ -t 0 ]; then
        printf 'Type YES to confirm: '
        read -r a
        [ "$a" = "YES" ] || { printf 'aborted.\n'; exit 1; }
    else
        printf 'Refusing to delete without confirmation. Re-run with --yes.\n' >&2
        exit 1
    fi
fi

for n in yangble5-claude yangble5-codex yangble5-env yangble5-uninstall; do
    l="${LINK_DIR}/${n}"
    if [ -L "$l" ]; then
        t="$(readlink "$l" 2>/dev/null || echo '')"
        case "$t" in
            "${YB5_HOME}"/*) rm -f "$l"; printf 'removed %s\n' "$l" ;;
        esac
    fi
done
if [ -d "$YB5_HOME" ]; then
    rm -rf "$YB5_HOME"
    printf 'removed %s\n' "$YB5_HOME"
fi
printf '\nyangble5 is gone. Your normal Claude Code login was never touched.\n\n'
UNINSTALLER
}

# ═══════════════════════════════════════════════════════════════════════════
# 9. verify — one real call, honestly reported
# ═══════════════════════════════════════════════════════════════════════════
VERIFY_OK=0

troubleshooting() {
    cat <<TS

  Troubleshooting, in the order worth trying:

    1. Is the service up at all?
         curl -sS ${YB5_API}/health

    2. Is your key accepted, and what is your quota?
         curl -sS ${YB5_API}/usage -H "x-api-key: \$(sed -n 's/^YANGBLE5_API_KEY=//p' ~/.yangble5/credentials)"

    3. The exact call this installer made:
         curl -sS -w '\\nstatus %{http_code} in %{time_total}s\\n' \\
              -X POST ${YB5_API}/v1/messages \\
              -H 'content-type: application/json' \\
              -H 'anthropic-version: 2023-06-01' \\
              -H "x-api-key: \$(sed -n 's/^YANGBLE5_API_KEY=//p' ~/.yangble5/credentials)" \\
              -d '{"model":"${YB5_MODEL}","max_tokens":16,"messages":[{"role":"user","content":"ping"}]}'

    4. Common causes:
         401  the key was revoked, or KEY_PEPPER was rotated server-side
         402  the operator's monthly cap is reached — the instance is read-only
         429  your daily allowance is spent (resets 00:00 UTC), or rate limited
         404  the model alias "${YB5_MODEL}" is not configured on that instance
         502  the gateway is up but the CLIProxyAPI engine behind it is not

    5. Report it with the status code and time above:
         https://github.com/shark0120/yangble5/issues
TS
}

verify() {
    step "verification"

    if [ "$DRY_RUN" -eq 1 ]; then
        info "would GET  ${YB5_API}/health"
        info "would GET  ${YB5_API}/v1/models   (authenticated, costs nothing)"
        info "would POST ${YB5_API}/v1/messages (one real 16-token completion)"
        VERIFY_OK=1
        return 0
    fi

    # -- 1. health: unauthenticated, free -----------------------------------
    if ! http_call GET /health - ""; then
        warn "GET /health — could not connect: ${HTTP_BODY}"
        troubleshooting
        return 1
    fi
    if [ "$HTTP_STATUS" != "200" ]; then
        warn "GET /health returned HTTP ${HTTP_STATUS} in ${HTTP_TIME}s"
        troubleshooting
        return 1
    fi
    v_accepting="$(printf '%s' "$HTTP_BODY" | json_string status)"
    ok "GET /health -> 200 in ${HTTP_TIME}s (status: ${v_accepting:-unknown})"
    case "$HTTP_BODY" in
        *'"accepting_requests":false'*|*'"accepting_requests": false'*)
            warn "the instance reports it is NOT accepting requests right now"
            info "(operator budget cap reached — it will recover; this is by design)"
            ;;
    esac

    if [ -z "$API_KEY" ]; then
        warn "no API key yet (BYOK mode) — skipping the authenticated calls"
        return 1
    fi

    # -- 2. models: authenticated, non-spending ------------------------------
    if ! http_call GET /v1/models - "$API_KEY"; then
        warn "GET /v1/models — could not connect: ${HTTP_BODY}"
        troubleshooting
        return 1
    fi
    if [ "$HTTP_STATUS" != "200" ]; then
        v_msg="$(printf '%s' "$HTTP_BODY" | json_string message)"
        warn "GET /v1/models -> HTTP ${HTTP_STATUS} in ${HTTP_TIME}s"
        [ -n "$v_msg" ] && info "server said: ${v_msg}"
        troubleshooting
        return 1
    fi
    ok "GET /v1/models -> 200 in ${HTTP_TIME}s (the key is accepted)"

    if [ "$DO_LIVE_TEST" -eq 0 ]; then
        warn "skipping the live completion (--no-live-test)"
        info "the key works, but nothing has been proven end to end"
        return 0
    fi

    # -- 3. one real completion ---------------------------------------------
    v_body="${TMPD}/probe.json"
    printf '{"model":"%s","max_tokens":16,"messages":[{"role":"user","content":"Reply with the single word: pong"}]}\n' \
        "$YB5_MODEL" > "$v_body"

    info "POST /v1/messages — one real 16-token completion through the stack"
    if ! http_call POST /v1/messages "$v_body" "$API_KEY"; then
        warn "POST /v1/messages — could not connect: ${HTTP_BODY}"
        troubleshooting
        return 1
    fi

    if [ "$HTTP_STATUS" = "200" ]; then
        ok "POST /v1/messages -> 200 in ${HTTP_TIME}s"
        v_text="$(printf '%s' "$HTTP_BODY" | json_string text)"
        [ -n "$v_text" ] && info "model replied: $(printf '%s' "$v_text" | cut -c1-60)"
        info "this was a COLD request: 0% prompt-cache hit, by definition. The"
        info "99.53% figure applies to warm rounds inside one session only."
        VERIFY_OK=1
        return 0
    fi

    v_msg="$(printf '%s' "$HTTP_BODY" | json_string message)"
    warn "POST /v1/messages -> HTTP ${HTTP_STATUS} in ${HTTP_TIME}s"
    [ -n "$v_msg" ] && info "server said: ${v_msg}"
    info "the config was written, but the stack did NOT answer. Not calling this a success."
    troubleshooting
    return 1
}

# ═══════════════════════════════════════════════════════════════════════════
# 10. next steps
# ═══════════════════════════════════════════════════════════════════════════
print_key_once() {
    [ -n "$API_KEY" ] || return 0
    [ "$MODE" = "registered" ] || return 0
    [ "$PRINT_KEY" -eq 1 ] || { info "key not printed (--no-print-key); it is in ${CRED_FILE}"; return 0; }
    [ "$DRY_RUN" -eq 1 ] && return 0

    cat <<KEY

  ${C_BLD}Your yangble5 API key — shown once, and only once${C_OFF}

      ${API_KEY}

  It is stored at ${CRED_FILE} with mode 0600. The server keeps only a
  scrypt hash of it, so nobody — including the operator — can show it to you
  again. If you lose it, register a new one.

  ${C_YLW}If an AI agent ran this installer for you, that key is now in its
  transcript. Re-run with --no-print-key next time if that matters.${C_OFF}

KEY
}

next_steps() {
    step "done"
    print_key_once

    cat <<NEXT
  ${C_BLD}Launch${C_OFF}
      yangble5-claude              # Claude Code, through yangble5
      yangble5-codex               # Codex, through yangble5
      yangble5-env                 # show the env being set (key redacted)

      Your normal \`claude\` and \`codex\` commands are unchanged. This install
      cannot see or damage your existing Claude Code login — it lives in a
      separate CLAUDE_CONFIG_DIR (~/.yangble5/claude).

  ${C_BLD}Where things live${C_OFF}
      ~/.yangble5/credentials      your key, mode 0600
      ~/.yangble5/env.sh           the environment the launchers export
      ~/.yangble5/claude/          isolated CLAUDE_CONFIG_DIR
      ~/.yangble5/codex/config.toml isolated CODEX_HOME
      ~/.yangble5/bin/             the launchers
      ~/.yangble5/machine-id       your local random salt — never uploaded

  ${C_BLD}Uninstall${C_OFF}
      yangble5-uninstall --yes     # prints what it deletes, then deletes it
      (equivalently: sh ~/.yangble5/uninstall.sh --yes)

  ${C_BLD}Re-running${C_OFF}
      Safe. It re-uses the stored key, backs up anything it changes, and does
      not mint a second key unless you pass --force-register.

  ${C_BLD}What you should not expect${C_OFF}
      - No live web search. Nothing routed through this proxy searches the web.
        Measured 2026-07-21: asked the current year, Gemini said "2024" and Grok
        said "2025". Treat every answer as recall from training, not fact.
      - The 99.53% prompt-cache hit rate is WARM ROUNDS ONLY. Your first request
        in every session is a cold 0% write. One machine, one run, 2026-07-21.
      - CLAUDE_CODE_MAX_CONTEXT_TOKENS=${YB5_CONTEXT} moves where your client
        decides to compact. It does not create context. We verified a 748,918-token
        prompt end to end; we did not verify 1,000,000.
      - Shared capacity is small and paid for by the operator personally. It will
        tell you when it is out rather than pretend otherwise.
      - yangble5 is a proxy built on CLIProxyAPI, a third-party open-source Go
        project we did not write: https://github.com/router-for-me/CLIProxyAPI

NEXT
}

# ═══════════════════════════════════════════════════════════════════════════
# main
# ═══════════════════════════════════════════════════════════════════════════
main() {
    refuse_root
    banner
    preflight
    detect_existing
    obtain_key
    write_config

    if verify; then
        next_steps
        if [ "$MODE" = "byok-empty" ]; then
            printf '%s  note%s installed in BYOK mode — add a key to %s to use it.\n\n' \
                "$C_YLW" "$C_OFF" "$CRED_FILE"
            exit "$EX_REGISTER"
        fi
        exit "$EX_OK"
    fi

    next_steps
    if [ "$MODE" = "byok-empty" ]; then
        printf '\n%s%sInstalled in BYOK mode — no key yet, so nothing was verified.%s\n' \
            "$C_BLD" "$C_YLW" "$C_OFF"
        printf 'Add your key to %s and re-run: sh install.sh\n' "$CRED_FILE"
        printf 'Exit code %s. The installer did its job; the pool had nothing to give.\n\n' "$EX_REGISTER"
        exit "$EX_REGISTER"
    fi
    printf '\n%s%sInstalled, but verification FAILED — see above.%s\n' "$C_BLD" "$C_RED" "$C_OFF"
    printf 'Nothing was rolled back; the config is in place so you can retry.\n'
    printf 'Exit code %s.\n\n' "$EX_VERIFY"
    exit "$EX_VERIFY"
}

main
