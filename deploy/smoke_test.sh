#!/usr/bin/env bash
#
# yangble5 — POST-DEPLOY smoke test. Run this from OUTSIDE the server
# (your laptop), against the public URL, after install.sh has brought the
# stack up. Running it on the VPS itself will still pass the health checks but
# tells you nothing about whether the internet can reach you, and it will make
# the "management surface is not exposed" check meaningless.
#
# "OUTSIDE" NOW MEANS SOMETHING STRONGER THAN "not on the VPS". Checks 11 and 12
# compare the bytes a VISITOR receives against the bytes in this repository, and
# a visitor's bytes come through the CDN. Resolving the name to the origin — an
# /etc/hosts entry, a split-horizon resolver, running on the box, or a `curl
# --resolve` habit — skips the edge, which is the half of the path that has
# actually corrupted a published file here. Check 11 therefore FAILS if the peer
# that answered is loopback, an RFC1918 address, or an address on this machine.
# Do not "fix" that by pointing it at the origin; the origin was right the whole
# time, once.
#
# What that test can and cannot see, stated plainly rather than implied: it
# catches the cases where the request never left this network. It cannot tell a
# CDN's public address from an origin's public address, and it does not try to —
# if a deployment has no edge in front of it, then the origin IS what a visitor
# receives and comparing it to the repo is the right comparison.
#
# WHAT IT CHECKS
#   1. TLS terminates and the certificate is valid
#   2. GET /health                    — the gateway is alive
#   3. GET /pool/status               — capacity signal the landing page reads
#   4. anonymous + garbage keys are REJECTED (an open proxy is the failure
#      mode that costs money, so the negative case is tested first)
#   5. an authenticated NON-STREAMING round trip through gateway -> engine
#   6. an authenticated STREAMING round trip, with a buffering heuristic —
#      this is the one that breaks in production, because Cloudflare, Caddy
#      and any corporate middlebox are all happy to buffer text/event-stream
#      into a single blob and leave the agent looking frozen
#   7. /v0/management/* is NOT reachable from the internet
#   8. the engine's port is not published to the internet
#   9. the eight security response headers are ACTUALLY SERVED, by value —
#      this one is here because its absence shipped a hole: the old test
#      passed on the presence of any Strict-Transport-Security header, and
#      Cloudflare adds one of those itself, so a deployment serving no CSP,
#      no nosniff, no X-Frame-Options and no Referrer-Policy tested green
#  10. registration mode, against the "no pooled personal OAuth behind a
#      public endpoint" rule in docs/OPERATING_A_PUBLIC_SERVICE.md §1
#  11. IS WHAT WE DEPLOYED WHAT WE WROTE — every published file fetched through
#      the edge and compared against this repository, via tools/drift_check.py.
#      Two real incidents live behind this one check and neither was caught by
#      anything else in this repo:
#        * the deployed pages were a full DAY older than the repo. Every check
#          above was green throughout, because they all ask "does the service
#          answer correctly", and a stale page answers correctly.
#        * Cloudflare's Email Address Obfuscation rewrote the install command
#          inside a <pre>, so the command shown to every visitor was broken
#          while the ORIGIN served the correct bytes the entire time. Anything
#          that checks the origin, or that compares a file to itself, reports
#          success while this is happening.
#      This is a hard FAIL, and it fails when it cannot run. A check that
#      quietly turns itself off is the failure it was written to prevent.
#  12. the published .sha256 verifies the published payload — the exact
#      verification the download page tells a visitor to perform, performed
#      end-to-end over the network. Independent of 11: it needs no repository
#      and no Python, and it catches an edge that rewrites a payload or a deploy
#      that shipped a digest and its file from different commits.
#
# CHECKS 5 AND 6 SPEND TOKENS on whatever upstream account you configured.
# Two requests: check 5 asks for 16 tokens, check 6 asks for 120. Check 6 is
# larger on purpose - progressive delivery cannot be measured in a reply that
# fits in one TCP write, and a 16-token answer was being reported as BUFFERED.
# Pass --no-spend to skip both.
# CHECKS 11 AND 12 spend nothing. There is no flag to skip them.
#
# SECRETS: the API key is passed to curl through a config file on stdin
# (`curl -K -`), never as a command-line argument, so it does not appear in
# `ps`, in your shell history, or in any process listing on a shared box.
# No secret is ever written to disk, and nothing this script downloads is
# written to disk at all. The key is redacted from all output.
#
# USAGE
#   export YANGBLE5_API_KEY=yb5_...                  # or --api-key-file
#   bash deploy/smoke_test.sh --base-url https://api.example.com
#   bash deploy/smoke_test.sh --no-spend             # free checks only
#
# EXIT CODES
#   0  every check passed (warnings allowed)
#   1  at least one check FAILED
#   2  usage error
#
set -uo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
readonly VERSION="1.1.0"

BASE_URL="${YANGBLE5_BASE_URL:-}"
SITE_URL=""        # where the STATIC site is published; defaults to BASE_URL
API_KEY="${YANGBLE5_API_KEY:-}"
MODEL="${YANGBLE5_SMOKE_MODEL:-yangble5}"
DIALECT="anthropic"
TIMEOUT=120
NO_SPEND=0
JSON=0
KEY_ON_CLI=0
TLS_REACHABLE=0   # set by check_tls; later checks skip rather than repeat a dead probe
REMOTE_IP=""      # set by check_tls; check 11 refuses to run if this is the origin

# ── output ─────────────────────────────────────────────────────────────────
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YLW=$'\033[33m'
    C_BLU=$'\033[36m'; C_DIM=$'\033[2m'; C_BLD=$'\033[1m'; C_OFF=$'\033[0m'
else
    C_RED=""; C_GRN=""; C_YLW=""; C_BLU=""; C_DIM=""; C_BLD=""; C_OFF=""
fi

R_STATUS=(); R_NAME=(); R_DETAIL=()
N_PASS=0; N_WARN=0; N_FAIL=0; N_SKIP=0

record() { R_STATUS+=("$1"); R_NAME+=("$2"); R_DETAIL+=("$3"); }
pass() { record PASS "$1" "$2"; N_PASS=$((N_PASS+1)); printf '%s  PASS%s  %-30s %s\n' "$C_GRN" "$C_OFF" "$1" "$2"; }
fail() { record FAIL "$1" "$2"; N_FAIL=$((N_FAIL+1)); printf '%s  FAIL%s  %-30s %s\n' "$C_RED" "$C_OFF" "$1" "$2"; }
warn() { record WARN "$1" "$2"; N_WARN=$((N_WARN+1)); printf '%s  WARN%s  %-30s %s\n' "$C_YLW" "$C_OFF" "$1" "$2"; }
skip() { record SKIP "$1" "$2"; N_SKIP=$((N_SKIP+1)); printf '%s  SKIP%s  %-30s %s\n' "$C_DIM" "$C_OFF" "$1" "$2"; }
note() { printf '        %s%s%s\n' "$C_DIM" "$1" "$C_OFF"; }
step() { printf '\n%s%s-- %s%s\n' "$C_BLD" "$C_BLU" "$1" "$C_OFF"; }
die()  { printf '\n%sABORT:%s %s\n\n' "$C_RED" "$C_OFF" "$1" >&2; exit 2; }
have() { command -v "$1" >/dev/null 2>&1; }

# Same rendering helpers as preflight.sh. They are duplicated on purpose:
# each script has to be runnable on its own, copied to a bare host, with no
# shared library to forget to copy alongside it.
ascii() { local s="$1"; s="${s//—/--}"; s="${s//–/-}"; s="${s//…/...}"; printf '%s' "$s"; }
pad()   { local s="$1" w="$2" n; n=$((w - ${#s})); [ "$n" -lt 0 ] && n=0; printf '%s%*s' "$s" "$n" ''; }
clip()  { local s="$1" w="$2"; if [ "${#s}" -le "$w" ]; then printf '%s' "$s"; else printf '%s...' "${s:0:$((w-3))}"; fi; }
json_escape() { local s="$1"; s="${s//\\/\\\\}"; s="${s//\"/\\\"}"; s="${s//$'\n'/ }"; printf '%s' "$s"; }

# Never let a key reach the terminal, a log, or a pasted bug report.
redact() {
    local s="$1"
    [ -n "$API_KEY" ] && s="${s//$API_KEY/yb5_***REDACTED***}"
    printf '%s' "$s"
}

usage() {
    printf '%s' \
'usage: bash deploy/smoke_test.sh [options]

  --base-url URL       public base URL, e.g. https://api.example.com
                       (default: https://$YANGBLE5_DOMAIN from deploy/.env)
  --site-url URL       where the STATIC site (index.html, install.sh, the
                       .sha256 files) is published, if that is not --base-url.
                       Checks 11 and 12 use this. There is no flag to turn
                       them off: if this host publishes no site, say where the
                       site IS, do not say "do not look".
  --api-key-file PATH  read the key from a file (first line)
  --api-key VALUE      pass the key inline (DISCOURAGED: shell history + ps)
  --model NAME         model alias to exercise (default: yangble5)
  --dialect NAME       anthropic | openai   (default: anthropic)
  --timeout SECONDS    per-request timeout (default: 120)
  --no-spend           skip the two checks that actually call the upstream
  --json               print a JSON summary after the table
  --self-test          exercise the pure helpers and exit. No network, no key,
                       no origin. Run it if a header check ever reports
                       "present but wrong" with an expected and a got that
                       look identical -- that was a broken grep, not a broken
                       server, and this is the check that tells them apart.
  -h, --help           this text

The key may also come from $YANGBLE5_API_KEY, or be typed at a prompt.
Checks 5 and 6 spend tokens on your upstream account: 16 for check 5, 120
for check 6. Check 6 is larger because progressive delivery cannot be measured
in a reply that fits in a single TCP write.

Run this from OUTSIDE the origin, on a machine that resolves the name the way
the public does. Checks 11 and 12 compare what a VISITOR is served against this
repository, so a host entry or a split-horizon resolver that points the name at
the origin makes them prove nothing -- and check 11 fails rather than pretend.
'
    exit 0
}

# ── contains_ci: case-insensitive substring test, and why it is not grep ───
#
# GNU grep 3.0 — the build Git Bash ships, and the build this project's own
# operator runs this script under — ABORTS when -i and -F are combined:
#
#     $ printf nosniff | grep -qiF nosniff; echo $?
#     134                       # 128 + SIGABRT
#
# Either flag alone is fine; together they crash. A crashed grep exits
# non-zero, so on 2026-07-23 check 9 reported all eight security headers as
#
#     FAIL hdr/X-Content-Type-Options  present but wrong:
#          expected to contain 'nosniff', got 'nosniff'
#
# against an origin that was serving every one of them correctly.
#
# Eight red lines that are all false is worse than having no check at all.
# This script's verdict is what stands between a deployment and an
# announcement, and a gate that cries wolf on a healthy origin teaches its
# operator to read `FAIL hdr/...` as "oh, that's the grep thing" — which is
# exactly how the REAL eight-missing-headers outage survived a whole day.
#
# `case` with a QUOTED expansion compares literally: no globbing, no regex, no
# fork, and nothing that differs between one platform's grep and another's.
# `--self-test` below proves it against every pair this script actually uses,
# including the ones full of glob metacharacters.
contains_ci() {
    local haystack needle
    haystack="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
    needle="$(printf '%s' "$2" | tr '[:upper:]' '[:lower:]')"
    case "$haystack" in
        *"$needle"*) return 0 ;;
        *)           return 1 ;;
    esac
}

# events, gap-in-seconds -> streaming | buffered | inconclusive
#
# `--self-test` proves this says all three things. It is a separate function
# for that reason: the logic it replaces was inline, and it was wrong in a way
# that only ever showed up against a live origin with a real key -- the most
# expensive place to find anything.
stream_verdict() {
    awk -v e="${1:-0}" -v g="${2:-0}" 'BEGIN{
        # Bytes arriving over more than 150 ms cannot have been released in one
        # go. This is the only positive signal, and it is checked FIRST so a
        # long, genuinely progressive stream can never fall through to a rule
        # about short ones.
        if (g >= 0.15)  { print "streaming"; exit }
        # Eight or more chunks inside 150 ms is not a model generating text, it
        # is a buffer being flushed.
        if (e >= 8)     { print "buffered";  exit }
        # Everything else: too little output, too fast, to tell the difference.
        # This branch used to be unreachable whenever e >= 3 -- which is every
        # real reply -- so a short answer was reported as buffering.
        print "inconclusive"
    }'
}

# ── --self-test: the pure helpers, no network, no key, no origin ───────────
#
# Reachable before anything is configured, so CI can run it with no secrets.
# It exists because contains_ci REPLACED a check that was silently broken: a
# helper that decides whether a deployment is safe to announce has to be
# demonstrably able to say both yes and no.
self_test() {
    local failures=0 desc hay ndl want got
    # description | haystack | needle | expected (1 = contains, 0 = does not)
    while IFS='|' read -r desc hay ndl want; do
        [ -n "$desc" ] || continue
        if contains_ci "$hay" "$ndl"; then got=1; else got=0; fi
        if [ "$got" = "$want" ]; then
            printf '  ok    %s\n' "$desc"
        else
            printf '  FAIL  %s (expected %s, got %s) hay=%s ndl=%s\n' \
                   "$desc" "$want" "$got" "$hay" "$ndl"
            failures=$((failures+1))
        fi
    done <<'EOF'
hsts, the real header value|max-age=31536000; includeSubDomains|includeSubDomains|1
hsts without includeSubDomains is the Cloudflare default, and must NOT pass|max-age=31536000|includeSubDomains|0
nosniff, exact|nosniff|nosniff|1
DENY, exact|DENY|DENY|1
csp, needle contains a space and two apostrophes|default-src 'self'; frame-ancestors 'none'; base-uri 'none'|frame-ancestors 'none'|1
csp WITHOUT frame-ancestors must not pass|default-src 'self'; base-uri 'none'|frame-ancestors 'none'|0
permissions-policy, needle contains parentheses|geolocation=(), camera=(), usb=()|camera=()|1
permissions-policy without camera must not pass|geolocation=(), microphone=()|camera=()|0
case folds, header value upper|NOSNIFF|nosniff|1
case folds, needle upper|nosniff|NOSNIFF|1
a needle with a glob star is a LITERAL star, not a wildcard|max-age=31536000|max-*|0
a needle with a glob question mark is literal too|nosniff|nosnif?|0
a needle in brackets is literal, not a character class|DENY|[DE]ENY|0
EOF

    # ---- stream_verdict: events | gap seconds | expected verdict -----------
    #
    # The first row is the one that matters. On 2026-07-23 a healthy origin
    # answered a 16-token streaming request with six events in 32 ms and this
    # check reported "the stream is being BUFFERED", failing the run with
    # "Do NOT open registration or announce". The old rule was
    #
    #     if (e >= 3 && g < 0.15) buffered; else if (g < 0.05 && e >= 2) suspect;
    #
    # whose second branch cannot be reached whenever e >= 3 -- which is every
    # real reply. The "too fast to tell" escape hatch existed and was dead code.
    local want got
    while IFS='|' read -r desc ndl hay want; do
        [ -n "$desc" ] || continue
        got="$(stream_verdict "$ndl" "$hay")"
        if [ "$got" = "$want" ]; then
            printf '  ok    %s
' "$desc"
        else
            printf '  FAIL  %s (expected %s, got %s) events=%s gap=%s
'                    "$desc" "$want" "$got" "$ndl" "$hay"
            failures=$((failures+1))
        fi
    done <<'EOF'
the exact false positive: 6 events in 32 ms from a healthy origin|6|0.032|inconclusive
the exact live measurement: 10 events over 0.66 s|10|0.660|streaming
a long stream is streaming however many events it had|3|2.400|streaming
genuinely buffered: 40 events released together|40|0.010|buffered
genuinely buffered: exactly at the event threshold|8|0.000|buffered
one event tells you nothing|1|0.000|inconclusive
two events tell you nothing either|2|0.040|inconclusive
just under the delivery threshold, too few events to judge|7|0.149|inconclusive
just over the delivery threshold is streaming, not buffered|40|0.150|streaming
no events at all is not a buffering verdict|0|0.000|inconclusive
EOF

    if [ "$failures" -gt 0 ]; then
        printf '\nself-test: %s failure(s) in the pure helpers. Every verdict this\n' "$failures"
        printf 'script prints is unreliable until they are fixed.\n'
        return 1
    fi
    printf '\nself-test: contains_ci says yes and no when it should;\n'
    printf 'stream_verdict says streaming, buffered and inconclusive when it should.\n'
    return 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --self-test)    self_test; exit $? ;;
        --base-url)     BASE_URL="${2:?--base-url needs a URL}"; shift 2 ;;
        --site-url)     SITE_URL="${2:?--site-url needs a URL}"; shift 2 ;;
        --api-key-file) API_KEY="$(head -n1 -- "${2:?--api-key-file needs a path}" 2>/dev/null | tr -d ' \r\n')"; shift 2 ;;
        --api-key)      API_KEY="${2:?--api-key needs a value}"; KEY_ON_CLI=1; shift 2 ;;
        --model)        MODEL="${2:?--model needs a name}"; shift 2 ;;
        --dialect)      DIALECT="${2:?--dialect needs a name}"; shift 2 ;;
        --timeout)      TIMEOUT="${2:?--timeout needs seconds}"; shift 2 ;;
        --no-spend)     NO_SPEND=1; shift ;;
        --json)         JSON=1; shift ;;
        -h|--help)      usage ;;
        *)              die "unknown option: $1 (try --help)" ;;
    esac
done

have curl || die "curl is required"

# ── resolve the base URL ───────────────────────────────────────────────────
if [ -z "$BASE_URL" ] && [ -r "$SCRIPT_DIR/.env" ]; then
    d="$(sed -n 's/^YANGBLE5_DOMAIN=//p' "$SCRIPT_DIR/.env" 2>/dev/null | head -1 | tr -d '"'"'"' \r')"
    [ -n "$d" ] && BASE_URL="https://$d"
fi
[ -n "$BASE_URL" ] || die "no --base-url given and no YANGBLE5_DOMAIN in deploy/.env"
BASE_URL="${BASE_URL%/}"
case "$BASE_URL" in
    https://*) : ;;
    http://*)  printf '%sWARNING:%s testing over plain http — the key you are about to send is in cleartext\n' "$C_YLW" "$C_OFF" ;;
    *)         die "--base-url must start with http:// or https:// (got '$BASE_URL')" ;;
esac
case "$DIALECT" in
    anthropic|openai) : ;;
    *) die "--dialect must be 'anthropic' or 'openai'" ;;
esac

SITE_URL="${SITE_URL:-$BASE_URL}"
SITE_URL="${SITE_URL%/}"
case "$SITE_URL" in
    http://*|https://*) : ;;
    *) die "--site-url must start with http:// or https:// (got '$SITE_URL')" ;;
esac

# ── obtain the key ─────────────────────────────────────────────────────────
if [ "$KEY_ON_CLI" -eq 1 ]; then
    printf '%sWARNING:%s --api-key puts the key in your shell history and in `ps` output.\n' "$C_YLW" "$C_OFF"
    printf '         Prefer YANGBLE5_API_KEY=... or --api-key-file.\n'
fi
if [ -z "$API_KEY" ] && [ "$NO_SPEND" -eq 0 ]; then
    if [ -t 0 ]; then
        printf 'yangble5 API key (input hidden, not echoed, not stored): '
        read -rs API_KEY
        printf '\n'
    fi
fi
if [ -z "$API_KEY" ] && [ "$NO_SPEND" -eq 0 ]; then
    die "no API key. Set YANGBLE5_API_KEY, use --api-key-file, or run with --no-spend."
fi
case "${API_KEY:-yb5_x_x}" in
    yb5_*) : ;;
    *) printf '%sWARNING:%s that key does not start with yb5_ — the gateway will reject it as malformed\n' "$C_YLW" "$C_OFF" ;;
esac

printf '%s%syangble5 smoke test v%s%s\n' "$C_BLD" "$C_BLU" "$VERSION" "$C_OFF"
printf '%starget: %s   dialect: %s   model: %s%s\n' "$C_DIM" "$BASE_URL" "$DIALECT" "$MODEL" "$C_OFF"
[ "$SITE_URL" != "$BASE_URL" ] && printf '%ssite:   %s   (checks 11 and 12)%s\n' "$C_DIM" "$SITE_URL" "$C_OFF"
[ "$NO_SPEND" -eq 1 ] && note "--no-spend: the two upstream round trips will be skipped"

# ===========================================================================
# curl wrappers
# ===========================================================================
# The key goes in via a config file read from stdin, so it never appears in
# argv. curl's config syntax is `header = "..."`; a key containing a literal
# double quote would break it, which is why generated keys are [A-Za-z0-9_].
auth_config() {
    printf 'header = "Authorization: Bearer %s"\n' "$API_KEY"
    if [ "$DIALECT" = "anthropic" ]; then
        printf 'header = "x-api-key: %s"\n' "$API_KEY"
        printf 'header = "anthropic-version: 2023-06-01"\n'
    fi
}

# Unauthenticated GET. Prints "<body>|<http_code>".
get_plain() {
    local path="$1" extra_timeout="${2:-15}"
    curl -sS -o - -w '|%{http_code}' --max-time "$extra_timeout" \
        -H 'Accept: application/json' \
        "${BASE_URL}${path}" 2>/dev/null || printf '|000'
}

# Body for the inference request. The minimal form below is deliberately the
# intersection of the two dialects — model/messages/max_tokens/stream mean the
# same thing in both — so only the ROUTE and the HEADERS differ per dialect.
# max_tokens is capped at 16 because this is a liveness probe, not a demo, and
# every token is billed to the operator.
request_body() {
    printf '{"model":"%s","max_tokens":16,"stream":%s,"messages":[{"role":"user","content":"Reply with exactly one word: pong"}]}' \
        "$MODEL" "$1"
}

# The STREAMING probe needs a different body from the round-trip probe, and the
# reason is the whole point of check 6.
#
# You cannot measure progressive delivery with a reply that fits in one TCP
# write. `max_tokens: 16` produced a six-event answer that arrived in 32
# milliseconds -- correctly, from a healthy origin -- and check 6 called that
# BUFFERED and failed the run with "Do NOT open registration or announce".
# There was nothing wrong with the service; there was nothing to measure.
#
# 120 tokens of counting is enough to span several flushes. Measured against
# the live service on 2026-07-23: 10-11 `data:` events spread over 0.66-0.74 s,
# both through Cloudflare and direct to the origin.
#
# It costs more than 16 tokens, and that is stated in the header rather than
# hidden: a check that spends nothing and proves nothing is the more expensive
# of the two.
stream_request_body() {
    printf '{"model":"%s","max_tokens":120,"stream":true,"messages":[{"role":"user","content":"Count slowly from 1 to 40, one number per line."}]}' \
        "$MODEL"
}


inference_path() {
    if [ "$DIALECT" = "anthropic" ]; then printf '/v1/messages'
    else printf '/v1/chat/completions'; fi
}

# ===========================================================================
# 1. TLS
# ===========================================================================
check_tls() {
    step "1. TLS"

    case "$BASE_URL" in http://*) skip "tls/handshake" "base URL is http://"; return ;; esac

    local out code verify
    # stderr is discarded here rather than merged. curl's human-readable error
    # text ("curl: (6) Could not resolve host") would otherwise be concatenated
    # with the -w output, and parsing positionally would then read a word of
    # English prose as the HTTP status. The -w fields already encode failure
    # (http_code becomes 000), and the prose is fetched separately below only
    # when it is actually needed.
    out="$(curl -sS -o /dev/null \
        -w '%{http_code} %{ssl_verify_result} %{http_version} %{remote_ip}' \
        --max-time 20 "${BASE_URL}/health" 2>/dev/null)"
    set -- $out
    code="${1:-000}"; verify="${2:-}"
    # Kept for check 11: the address that actually answered. If it is the origin
    # rather than a CDN, every byte comparison below the API checks is testing a
    # path no visitor takes.
    REMOTE_IP="${4:-}"

    if [ "$code" = "000" ]; then
        # `2>&1 >/dev/null` keeps stderr and throws stdout away — the opposite
        # of `2>&1`, and the reason the diagnostic is a separate call.
        local why
        why="$(curl -sS -o /dev/null --max-time 20 "${BASE_URL}/health" 2>&1 >/dev/null | head -1)"
        fail "tls/handshake" "no connection: ${why:-timed out}"
        note "if the certificate is still being issued, wait a minute and retry"
        note "check the origin:  docker compose logs --tail=50 caddy"
        TLS_REACHABLE=0
        return
    fi
    TLS_REACHABLE=1
    if [ "$verify" = "0" ]; then
        pass "tls/handshake" "verified, HTTP/${3:-?}, peer ${4:-?}"
    else
        fail "tls/handshake" "certificate did NOT verify (curl ssl_verify_result=$verify)"
    fi

    # Expiry. Only if openssl is around; not worth a hard dependency.
    if [ "$TLS_REACHABLE" -ne 1 ]; then
        skip "tls/expiry" "host unreachable"
    elif have openssl; then
        local host port enddate days
        host="${BASE_URL#https://}"; host="${host%%/*}"; port=443
        enddate="$(printf '' | openssl s_client -servername "$host" -connect "$host:$port" 2>/dev/null \
                   | openssl x509 -noout -enddate 2>/dev/null | sed 's/notAfter=//')"
        if [ -n "$enddate" ]; then
            local end now
            end="$(date -d "$enddate" +%s 2>/dev/null)"
            now="$(date +%s)"
            if [ -n "$end" ]; then
                days=$(( (end - now) / 86400 ))
                if [ "$days" -lt 0 ];      then fail "tls/expiry" "certificate EXPIRED $(( -days )) days ago"
                elif [ "$days" -lt 10 ];   then warn "tls/expiry" "expires in $days days — renewal should have happened by now"
                else                            pass "tls/expiry" "valid for $days more days"
                fi
            fi
        fi
    else
        skip "tls/expiry" "openssl not installed"
    fi

    # HSTS is checked properly in check_security_headers (9), against the value
    # this repo actually sets. It is NOT checked here any more, and that is a
    # deliberate removal: the old check passed on the presence of ANY
    # Strict-Transport-Security header, and on a Cloudflare-proxied zone CF
    # supplies one of its own (max-age only, no includeSubDomains). So the
    # check went green on a deployment whose origin set no headers at all.
    :
}

# ===========================================================================
# 2. /health
# ===========================================================================
check_health() {
    step "2. /health"

    local r body code
    r="$(get_plain /health 20)"
    code="${r##*|}"; body="${r%|*}"

    case "$code" in
        200)
            local status
            status="$(printf '%s' "$body" | sed -n 's/.*"status"[[:space:]]*:[[:space:]]*"\([a-z]*\)".*/\1/p')"
            case "$status" in
                ok)       pass "health" "HTTP 200, status=ok" ;;
                degraded) warn "health" "HTTP 200, status=degraded — the global budget cap has tripped; the service is up but refusing paid work" ;;
                *)        warn "health" "HTTP 200 but no recognisable status field: $(printf '%s' "$body" | head -c 100)" ;;
            esac
            local reg
            reg="$(printf '%s' "$body" | sed -n 's/.*"registration"[[:space:]]*:[[:space:]]*"\([a-z]*\)".*/\1/p')"
            [ -n "$reg" ] && note "registration mode: $reg"
            ;;
        000) fail "health" "no response at all (HTTP 000) — DNS, TLS or the edge is down" ;;
        502|503|504) fail "health" "HTTP $code — caddy is up but the gateway container is not answering"
                     note "docker compose ps ; docker compose logs --tail=100 gateway" ;;
        521|522|523|524) fail "health" "HTTP $code — Cloudflare cannot reach your origin (see deploy/cloudflare.md)" ;;
        *) fail "health" "HTTP $code" ;;
    esac
}

# ===========================================================================
# 3. /pool/status
# ===========================================================================
check_pool_status() {
    step "3. /pool/status"

    local r body code
    r="$(get_plain /pool/status 20)"
    code="${r##*|}"; body="${r%|*}"

    case "$code" in
        200)
            pass "pool/status" "HTTP 200"
            local accepting
            accepting="$(printf '%s' "$body" | sed -n 's/.*"accepting"[[:space:]]*:[[:space:]]*\(true\|false\).*/\1/p')"
            [ -n "$accepting" ] && note "accepting new work: $accepting"
            # An unauthenticated endpoint must not leak the operator's spend.
            if printf '%s' "$body" | grep -qiE '"(cost_usd|usd|spend|balance|budget_usd)"'; then
                fail "pool/status-privacy" "the public capacity endpoint is exposing a dollar figure"
            else
                pass "pool/status-privacy" "no dollar amounts in the public payload"
            fi
            ;;
        404) warn "pool/status" "HTTP 404 — this build of the gateway does not expose /pool/status" ;;
        000) fail "pool/status" "no response (HTTP 000)" ;;
        *)   fail "pool/status" "HTTP $code" ;;
    esac
}

# ===========================================================================
# 4. Authentication is actually enforced
# ===========================================================================
# Tested BEFORE the happy path on purpose. A proxy that answers without a key
# is an open relay billed to the operator, and that is a far worse outcome
# than a proxy that does not answer at all.
check_auth_enforced() {
    step "4. Authentication is enforced"

    local path body code out
    path="$(inference_path)"
    body="$(request_body false)"

    out="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 \
        -X POST -H 'Content-Type: application/json' \
        --data "$body" "${BASE_URL}${path}" 2>/dev/null)"
    code="${out:-000}"
    case "$code" in
        401|403) pass "auth/anonymous-rejected" "HTTP $code for a request with no credentials" ;;
        200)     fail "auth/anonymous-rejected" "HTTP 200 WITHOUT A KEY — this is an open proxy billed to you. Take it offline now (see GO_LIVE.md 'kill switch')." ;;
        429)     warn "auth/anonymous-rejected" "HTTP 429 — rate-limited before auth could be judged; re-run in a minute" ;;
        000)     fail "auth/anonymous-rejected" "no response (HTTP 000)" ;;
        *)       warn "auth/anonymous-rejected" "HTTP $code — expected 401; not obviously open, but not the documented behaviour" ;;
    esac

    out="$(printf 'header = "Authorization: Bearer yb5_smoketest_notarealkey"\n' \
        | curl -sS -K - -o /dev/null -w '%{http_code}' --max-time 30 \
          -X POST -H 'Content-Type: application/json' \
          --data "$body" "${BASE_URL}${path}" 2>/dev/null)"
    code="${out:-000}"
    case "$code" in
        401|403) pass "auth/bad-key-rejected" "HTTP $code for a well-formed but invalid key" ;;
        429)     warn "auth/bad-key-rejected" "HTTP 429 — the auth backoff is engaging (that is the intended behaviour under probing)" ;;
        200)     fail "auth/bad-key-rejected" "HTTP 200 for an invalid key" ;;
        000)     fail "auth/bad-key-rejected" "no response (HTTP 000)" ;;
        *)       warn "auth/bad-key-rejected" "HTTP $code — expected 401" ;;
    esac
}

# ===========================================================================
# 5. Non-streaming round trip
# ===========================================================================
check_roundtrip() {
    step "5. Authenticated round trip (non-streaming)"

    if [ "$NO_SPEND" -eq 1 ]; then skip "api/round-trip" "--no-spend"; return; fi

    local path body raw code ttfb total
    path="$(inference_path)"
    body="$(request_body false)"

    raw="$(auth_config | curl -sS -K - \
        -w '\n__M__ %{http_code} %{time_starttransfer} %{time_total} %{size_download}' \
        --max-time "$TIMEOUT" -X POST -H 'Content-Type: application/json' \
        --data "$body" "${BASE_URL}${path}" 2>&1)"

    local meta; meta="$(printf '%s' "$raw" | sed -n 's/^__M__ //p' | tail -1)"
    local payload; payload="$(printf '%s' "$raw" | sed '/^__M__ /d')"
    code="$(printf '%s' "$meta" | awk '{print $1}')"
    ttfb="$(printf '%s' "$meta" | awk '{print $2}')"
    total="$(printf '%s' "$meta" | awk '{print $3}')"

    case "$code" in
        200)
            if printf '%s' "$payload" | grep -qiE '"(content|choices|completion)"'; then
                pass "api/round-trip" "HTTP 200 in ${total}s (engine reached, model replied)"
            else
                warn "api/round-trip" "HTTP 200 in ${total}s but the body has no content field: $(redact "$(printf '%s' "$payload" | head -c 120)")"
            fi ;;
        400) fail "api/round-trip" "HTTP 400 — $(redact "$(printf '%s' "$payload" | head -c 200)")"
             note "if this says 'Request contains an invalid argument', you have hit the"
             note "role:\"system\" translator bug: engine < 7.2.93 needs tools/claude_shim.py" ;;
        401|403) fail "api/round-trip" "HTTP $code — the key was rejected; is it suspended, or from a previous pepper?" ;;
        402|429) warn "api/round-trip" "HTTP $code — a budget or rate limit refused the request (the gateway is working)" ;;
        502|503) fail "api/round-trip" "HTTP $code — gateway reached, engine did not answer. docker compose logs engine" ;;
        504|524) fail "api/round-trip" "HTTP $code — timeout. See deploy/cloudflare.md section 4 (the 100-second problem)" ;;
        000) fail "api/round-trip" "no response within ${TIMEOUT}s" ;;
        *)   fail "api/round-trip" "HTTP $code: $(redact "$(printf '%s' "$payload" | head -c 160)")" ;;
    esac
    [ -n "${ttfb:-}" ] && note "time to first byte ${ttfb}s, total ${total}s"
}

# ===========================================================================
# 6. Streaming round trip  <-- the one that breaks in production
# ===========================================================================
# Two different failures hide here and they need different fixes:
#
#   a) streaming does not work at all       -> no SSE events, or a 5xx
#   b) streaming "works" but is BUFFERED    -> all events arrive at once, at
#      the end. The response is correct, so every naive test passes, but the
#      agent shows a frozen cursor for the whole generation and any request
#      slower than Cloudflare's 100s idle limit dies with a 524.
#
# (b) is detected by comparing time-to-first-byte with total time: an
# un-buffered stream starts delivering long before it finishes, so
# time_total - time_starttransfer is large. If several events arrive but the
# gap is ~0, something between here and the engine collected the whole stream
# before releasing a single byte.
check_streaming() {
    step "6. Authenticated round trip (STREAMING)"

    if [ "$NO_SPEND" -eq 1 ]; then skip "api/streaming" "--no-spend"; return; fi

    local path body raw meta payload code ttfb total events ctype
    path="$(inference_path)"
    body="$(stream_request_body)"

    raw="$(auth_config | curl -sS -K - --no-buffer \
        -w '\n__M__ %{http_code} %{time_starttransfer} %{time_total} %{size_download} %{content_type}' \
        --max-time "$TIMEOUT" -X POST \
        -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
        --data "$body" "${BASE_URL}${path}" 2>&1)"

    meta="$(printf '%s' "$raw" | sed -n 's/^__M__ //p' | tail -1)"
    payload="$(printf '%s' "$raw" | sed '/^__M__ /d')"
    code="$(printf '%s' "$meta"  | awk '{print $1}')"
    ttfb="$(printf '%s' "$meta"  | awk '{print $2}')"
    total="$(printf '%s' "$meta" | awk '{print $3}')"
    # $5, not $6. The -w format is
    #   %{http_code} %{time_starttransfer} %{time_total} %{size_download} %{content_type}
    # and `__M__ ` is stripped before this runs, so content_type is the FIFTH
    # field. Reading $6 returned the empty string on every run ever made, which
    # the case below rendered as "unset - expected text/event-stream". The
    # origin has been sending `text/event-stream` correctly the whole time; this
    # assertion had simply never been able to pass.
    ctype="$(printf '%s' "$meta" | awk '{print $5}')"
    events="$(printf '%s' "$payload" | grep -c '^data:')"

    if [ "$code" != "200" ]; then
        case "$code" in
            524|504) fail "api/streaming" "HTTP $code — the stream was cut by a timeout, not by the model"
                     note "this is the classic Cloudflare 100s idle kill; deploy/cloudflare.md section 4" ;;
            000)     fail "api/streaming" "no response within ${TIMEOUT}s" ;;
            *)       fail "api/streaming" "HTTP $code: $(redact "$(printf '%s' "$payload" | head -c 160)")" ;;
        esac
        return
    fi

    if [ "${events:-0}" -eq 0 ]; then
        fail "api/streaming" "HTTP 200 but zero 'data:' events (content-type: ${ctype:-unknown})"
        note "the request succeeded without streaming — check that stream:true survived the proxy"
        return
    fi

    case "$ctype" in
        *event-stream*) pass "api/streaming-content-type" "$ctype" ;;
        *) warn "api/streaming-content-type" "${ctype:-unset} — expected text/event-stream" ;;
    esac

    # awk because the shell has no floats. The rules, and why they live in a
    # function with a self-test, are at stream_verdict.
    local gap verdict
    gap="$(awk -v a="${total:-0}" -v b="${ttfb:-0}" 'BEGIN{printf "%.3f", a-b}')"
    verdict="$(stream_verdict "${events:-0}" "$gap")"

    case "$verdict" in
        streaming) pass "api/streaming" "$events events, first byte ${ttfb}s, last ${total}s (delivered over ${gap}s)" ;;
        buffered)  fail "api/streaming" "$events events all arrived within ${gap}s — the stream is being BUFFERED"
                   note "something is collecting the whole response before releasing it. In order of likelihood:"
                   note "  1. a Cloudflare setting that transforms the body (Rocket Loader / auto-minify / compression)"
                   note "  2. a reverse proxy in front of caddy that is not this repo's Caddyfile"
                   note "  3. proxy_buffering not turned off on the /v1/ location (nginx), or"
                   note "     flush_interval not set to -1 (caddy) on the gateway upstream"
                   note "see deploy/cloudflare.md 'Response buffering'" ;;
        *)         warn "api/streaming" "$events events over ${gap}s — too little output, too fast, to tell buffering from a short reply"
                   note "the model returned less than this check needs in order to measure"
                   note "delivery. That is a gap in the EVIDENCE, not a verdict about the"
                   note "service: re-run when the upstream is answering at normal length." ;;
    esac
}

# ===========================================================================
# 7. The management surface must not be reachable
# ===========================================================================
# CLIProxyAPI's /v0/management/* can list, mint and export upstream
# credentials. The Caddyfile answers 404 for the whole /v0/* prefix before the
# request can reach the engine. A 401 here is NOT a pass: it would mean the
# request reached the engine and only its own key stood in the way.
check_management_blocked() {
    step "7. Management surface is not exposed"

    local p r code
    for p in /v0/management/api-keys /v0/management/config /v0 /v0/management; do
        r="$(get_plain "$p" 15)"
        code="${r##*|}"; local body="${r%|*}"
        case "$code" in
            404|405|000)
                pass "block${p}" "HTTP $code" ;;
            401|403)
                fail "block${p}" "HTTP $code — the request REACHED THE ENGINE. The edge block is not in effect."
                note "your Caddyfile is not the one in this repo, or something is bypassing caddy" ;;
            200)
                fail "block${p}" "HTTP 200 — THE MANAGEMENT API IS PUBLIC. Shut the service down now."
                note "kill switch: docker compose -f /opt/yangble5/app/deploy/docker-compose.yml down"
                note "then rotate every upstream credential (deploy/SECRETS_SETUP.md)"
                if printf '%s' "$body" | grep -qiE 'api-?key|credential|token'; then
                    note "the response body mentions keys/credentials — treat them all as leaked"
                fi ;;
            *)
                warn "block${p}" "HTTP $code — expected 404" ;;
        esac
    done

    # Other surfaces the Caddyfile also blocks.
    for p in /.env /.git/config /debug/pprof/ /metrics; do
        r="$(get_plain "$p" 10)"; code="${r##*|}"
        case "$code" in
            404|403|000) pass "block${p}" "HTTP $code" ;;
            200)         fail "block${p}" "HTTP 200 — this path should not be served" ;;
            *)           warn "block${p}" "HTTP $code" ;;
        esac
    done
    return 0
}

# ===========================================================================
# 8. Engine port is not published
# ===========================================================================
check_engine_port_closed() {
    step "8. Engine port is not published"

    local host code
    host="${BASE_URL#*://}"; host="${host%%/*}"

    # Deliberately short: a filtered port is expected to hang, and a hang is
    # the answer we want. Reaching the engine on 8318 from the internet would
    # bypass the gateway entirely — no auth, no budget, no rate limit.
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 6 \
        "http://${host}:8318/v1/models" 2>/dev/null)"
    case "${code:-000}" in
        000) pass "engine/port-8318" "no response — port is closed or filtered, as intended" ;;
        401|403) fail "engine/port-8318" "HTTP $code — the ENGINE IS DIRECTLY REACHABLE on 8318 (its own key is the only thing stopping a caller)"
                 note "docker-compose.yml publishes no port for the engine; something else opened it. Check ufw and any provider-level firewall." ;;
        *) fail "engine/port-8318" "HTTP $code — something is answering on the engine's port from the internet" ;;
    esac
}

# ===========================================================================
# 9. Security response headers
# ===========================================================================
# THIS CHECK EXISTS BECAUSE ITS ABSENCE SHIPPED A HOLE.
#
# deploy/nginx/yangble5.com.conf.example PART 2j (and deploy/Caddyfile) declare
# eight response headers as mandatory. On 2026-07-22 the live deployment served
# exactly one of them, and the smoke test was green anyway: the only header it
# looked for was Strict-Transport-Security, and Cloudflare adds an HSTS header
# of its own on a proxied zone. A presence test for "any security header"
# cannot distinguish "origin configured" from "CDN default".
#
# So this check asserts the VALUE, not the presence, wherever the value is
# ours, and it FAILS rather than warns. A CSP that is not being served is not a
# CSP: site/index.html and site/verify.html each run one inline <script> that
# the policy pins by sha256 precisely so that a page whose job is to convince
# someone to pipe a URL into a shell cannot be made to run injected script.
#
# It is checked on `/` — the static page — because that is where the CSP
# matters and where a webroot `location /` block with its own add_header would
# have silently dropped the lot.
check_security_headers() {
    step "9. Security response headers"

    if [ "$TLS_REACHABLE" -ne 1 ]; then
        skip "headers" "host unreachable"
        return
    fi

    local hdrs
    hdrs="$(curl -sSI --max-time 20 "${BASE_URL}/" 2>/dev/null | tr -d '\r')"
    if [ -z "$hdrs" ]; then
        fail "headers" "no response headers from ${BASE_URL}/"
        return
    fi

    # header name -> substring that must appear in its value. The substring is
    # the part that is ours; matching the whole line would break on harmless
    # ordering or spacing differences between nginx and Caddy.
    hdr_value() { printf '%s' "$hdrs" | grep -i "^$1:" | head -1 | cut -d: -f2- | sed 's/^ *//'; }

    local name want got missing=0
    while IFS='|' read -r name want; do
        [ -n "$name" ] || continue
        got="$(hdr_value "$name")"
        if [ -z "$got" ]; then
            fail "hdr/$name" "ABSENT — the origin is not serving it, and Cloudflare will not add it for you"
            missing=$((missing+1))
        elif [ -n "$want" ] && ! contains_ci "$got" "$want"; then
            fail "hdr/$name" "present but wrong: expected to contain '$want', got '$(printf '%s' "$got" | cut -c1-45)'"
            missing=$((missing+1))
        else
            pass "hdr/$name" "$(printf '%s' "$got" | cut -c1-45)"
        fi
    done <<'EOF'
Strict-Transport-Security|includeSubDomains
X-Content-Type-Options|nosniff
X-Frame-Options|DENY
Referrer-Policy|no-referrer
Content-Security-Policy|frame-ancestors 'none'
Cross-Origin-Opener-Policy|same-origin
Cross-Origin-Resource-Policy|same-origin
Permissions-Policy|camera=()
EOF

    if [ "$missing" -gt 0 ]; then
        note "nginx: PART 2j of deploy/nginx/yangble5.com.conf.example is not in the"
        note "running config. Prefer the include — deploy/nginx/security-headers.conf —"
        note "then 'nginx -t && nginx -s reload' and re-run this from OFF the VPS."
        note "Also check no location block declares an add_header of its own: one is"
        note "enough to drop every server-level add_header on that location."
    fi

    # The CSP pins two inline scripts by hash. An unpinned script-src is worse
    # than no CSP here, because the page then claims a protection it lacks.
    local csp
    csp="$(hdr_value Content-Security-Policy)"
    if [ -n "$csp" ]; then
        if printf '%s' "$csp" | grep -qi "script-src[^;]*unsafe-inline"; then
            fail "hdr/csp-unsafe-inline" "script-src allows 'unsafe-inline' — the sha256 pinning is decorative"
        else
            pass "hdr/csp-unsafe-inline" "script-src does not allow 'unsafe-inline'"
        fi
    fi

    # `default_type text/plain` — PART 3d, or the include that replaced it,
    # deploy/nginx/static-content-type.conf. nginx's mime.types knows neither
    # .sh nor .md, so both land on default_type, which panel configs set to
    # application/octet-stream. With nosniff (correctly) set, a browser then
    # DOWNLOADS the file instead of showing it.
    #
    # The two are not equally serious, so they are not reported equally:
    #
    #   install.sh  — a warning. The landing page tells visitors to read the
    #                 script before running it and a download defeats that, but
    #                 `curl | sh` is unaffected and some operators serve it as
    #                 a download deliberately.
    #   AGENTS.md   — a failure. The published one-liner sends an AI agent to
    #                 this URL as step one of the entire install path. Fetchers
    #                 that parse text/* and refuse everything else are common,
    #                 so octet-stream here does not degrade the experience, it
    #                 removes it: the agent cannot read its instructions and
    #                 there is no second route.
    local ctype
    ctype="$(curl -sSI --max-time 20 "${BASE_URL}/install.sh" 2>/dev/null | tr -d '\r' \
             | grep -i '^content-type:' | head -1 | cut -d: -f2- | sed 's/^ *//')"
    case "$ctype" in
        text/plain*) pass "install.sh/content-type" "$ctype — readable in a browser" ;;
        '')          warn "install.sh/content-type" "no Content-Type at all" ;;
        *)           warn "install.sh/content-type" "$ctype — a browser downloads this instead of showing it (default_type)" ;;
    esac

    ctype="$(curl -sSI --max-time 20 "${BASE_URL}/AGENTS.md" 2>/dev/null | tr -d '\r' \
             | grep -i '^content-type:' | head -1 | cut -d: -f2- | sed 's/^ *//')"
    case "$ctype" in
        text/*) pass "AGENTS.md/content-type" "$ctype" ;;
        '')     fail "AGENTS.md/content-type" "no Content-Type — the document the published one-liner points an agent at"
                note "Either /AGENTS.md is not deployed or the static-content-type include"
                note "is not in the running config. Until it is, the one-liner's first step"
                note "fails for any agent that will not parse a non-text body." ;;
        *)      fail "AGENTS.md/content-type" "$ctype — an agent that only parses text/* cannot read its own instructions"
                note "Apply deploy/nginx/static-content-type.conf at server level, then"
                note "'nginx -t && nginx -s reload'. mime.types has no entry for .md." ;;
    esac
}

# ===========================================================================
# 10. Open registration vs. what is backing the pool
# ===========================================================================
# docs/OPERATING_A_PUBLIC_SERVICE.md §1 states the hard rule: pooled PERSONAL
# OAuth credentials must never back a public service. This script cannot see
# what credentials the engine holds, so it cannot decide that for you — it can
# only make the combination impossible to ship without noticing.
check_registration_exposure() {
    step "10. Registration mode"

    local r body code mode
    r="$(get_plain /health 20)"; code="${r##*|}"; body="${r%|*}"
    [ "$code" = "200" ] || { skip "registration/mode" "/health did not answer 200"; return; }

    mode="$(printf '%s' "$body" | sed -n 's/.*"registration"[[:space:]]*:[[:space:]]*"\([a-z]*\)".*/\1/p')"
    case "$mode" in
        open)
            warn "registration/mode" "open — anyone on the internet can mint a key against your pool"
            note "CONFIRM, before announcing, that every upstream credential behind this"
            note "endpoint is licensed for serving third parties. Personal OAuth accounts"
            note "are not (docs/OPERATING_A_PUBLIC_SERVICE.md §1). The suspension lands on"
            note "the Google/xAI/OpenAI ACCOUNT, not on yangble5, and takes out everything"
            note "else that account is used for."
            note "If a tier is served by a single credential, its suspension is a total"
            note "outage for that tier with no failover. Say so publicly, or close it:"
            note "  YANGBLE5_REGISTRATION_MODE=closed  (existing keys keep working)"
            ;;
        invite)  pass "registration/mode" "invite — new keys need a code you minted" ;;
        closed)  pass "registration/mode" "closed — no new keys" ;;
        '')      skip "registration/mode" "/health did not report a registration mode" ;;
        *)       warn "registration/mode" "unrecognised mode '$mode'" ;;
    esac
}

# ===========================================================================
# 11. Is what we DEPLOYED what we WROTE?
# ===========================================================================
# Checks 1-10 all ask "does the service behave correctly". A site that is a day
# out of date behaves perfectly correctly. So does a site whose install command
# the CDN has rewritten into something that does not work. Both of those have
# happened to this project, and on both occasions every check above was green.
#
# tools/drift_check.py is the answer: it fetches each published file THROUGH THE
# EDGE and compares it against the repository copy, with the edge transformations
# this project has agreed to enumerated in one list. Read its docstring before
# adding anything to that list.
#
# Three things this check refuses to do, each because doing them is how the
# equivalent check died last time:
#
#   * It does not skip when the tool is missing. "SKIP" in a green summary is
#     indistinguishable from "PASS" to the person reading the summary at 3am.
#   * It does not pass when the peer that answered is the origin. Comparing the
#     origin against the repo is a comparison that has never once failed here,
#     including on the day the CDN was serving a corrupted install command.
#   * It has no off switch. If the host under test publishes no static site,
#     --site-url says where the site is; there is no flag that says "do not ask".
check_site_drift() {
    step "11. Deployed site == this repository"

    local tool py candidate
    tool="$REPO_ROOT/tools/drift_check.py"

    if [ ! -f "$tool" ]; then
        fail "site/drift" "tools/drift_check.py not found next to this script"
        note "expected at: $tool"
        note "This check compares the SERVED site against a repository, so it"
        note "cannot run from a copy of smoke_test.sh alone. Run it from a"
        note "checkout of the commit you deployed -- on your laptop, not the VPS."
        return
    fi

    # The interpreter is version-checked, not just found. `python` is still
    # Python 2 on some hosts, and a Python 2 syntax error exits non-zero exactly
    # like a real difference does -- reporting "the site does not match" when the
    # truth is "nothing looked" is the worst outcome this check can produce.
    py=""
    for candidate in python3 python; do
        have "$candidate" || continue
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
            py="$candidate"; break
        fi
    done
    if [ -z "$py" ]; then
        fail "site/drift" "no Python >= 3.10 on PATH, so the comparison cannot run"
        note "drift_check.py is stdlib-only and needs no install or virtualenv."
        note "Check 12 below still runs and still catches a rewritten payload, but it"
        note "cannot see a page that is merely STALE. Do not treat it as a substitute."
        return
    fi

    # Which host answered. If it is loopback or an RFC1918 address then the name
    # resolved to something inside this network -- the origin, a sidecar, a
    # tunnel -- and the CDN is not in the path being tested.
    local peer
    peer="$REMOTE_IP"
    if [ "$SITE_URL" != "$BASE_URL" ] || [ -z "$peer" ]; then
        peer="$(curl -sS -o /dev/null -w '%{remote_ip}' --max-time 20 "${SITE_URL}/" 2>/dev/null)"
    fi
    # An empty remote_ip means curl never completed a connection. Treating that
    # as "not a private address" and passing is the exact shape of false green
    # this check exists to remove: it would print PASS for a site that is down.
    if [ -z "${peer:-}" ]; then
        fail "site/drift-vantage" "nothing answered at ${SITE_URL}, so there is no vantage to judge"
        note "curl reported no peer address. Either the host is unreachable, or"
        note "curl is too old to report %{remote_ip} (7.29, 2013). Until this says"
        note "which host served the page, the comparison below cannot be trusted"
        note "even if it passes -- that is why this is a FAIL and not a warning."
        return
    fi

    case "$peer" in
        127.*|::1|0.0.0.0|10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*|169.254.*|fd*:*|fe80:*)
            fail "site/drift-vantage" "${SITE_URL} resolved to $peer — that is not the public path"
            note "A private or loopback peer means this machine reached the origin"
            note "directly: a hosts entry, a split-horizon resolver, a tunnel, or you"
            note "are on the VPS. The comparison would then pass on exactly the"
            note "deployment that broke last time, because the ORIGIN bytes were"
            note "correct throughout and it was the EDGE that rewrote them."
            note "Re-run from a machine that resolves the name the way a visitor does."
            return ;;
    esac

    # The script's own local addresses, best effort. Being served by your own
    # PUBLIC IP is the same problem as loopback and is not caught by the case
    # above: it is what happens when someone runs the "post-deploy" step over ssh
    # on the box they just deployed to.
    #
    # Every source that is present, not the first one that exists. `hostname -I`
    # is Linux-only and macOS's hostname prints a usage error instead, so an
    # if/elif chain that reaches `hostname` first quietly returns nothing on
    # macOS and this check stops checking without saying so.
    local mine=""
    have ip       && mine="$mine$(ip -o addr show 2>/dev/null | awk '{print $4}' | cut -d/ -f1)"$'\n'
    have ifconfig && mine="$mine$(ifconfig 2>/dev/null | awk '/inet /{print $2}' | sed 's/^addr://')"$'\n'
    have hostname && mine="$mine$(hostname -I 2>/dev/null | tr ' ' '\n')"$'\n'
    if printf '%s\n' "$mine" | grep -qxF -- "$peer"; then
        fail "site/drift-vantage" "${SITE_URL} answered from $peer, which is an address on THIS machine"
        note "You are running the post-deploy check on the machine you deployed."
        note "Nothing below it can tell you what the internet receives."
        return
    fi
    pass "site/drift-vantage" "served by $peer, which is neither this host nor a private address"

    # PYTHONUNBUFFERED because the per-file results go to stdout and the problem
    # report to stderr. Merged into one stream, a buffered stdout arrives after
    # stderr, so the "ok" lines print BELOW the failure they precede and the
    # report reads as if the files listed as ok were the ones that failed.
    local out rc
    out="$(PYTHONUNBUFFERED=1 "$py" "$tool" --base "$SITE_URL" 2>&1)"
    rc=$?

    if [ "$rc" -eq 0 ]; then
        local matched
        matched="$(printf '%s\n' "$out" | grep -c '^  ok ')"
        pass "site/drift" "$matched published files byte-identical to this checkout"
        return
    fi

    # A crash and a real difference are both a non-zero exit. Saying which is
    # the difference between "deploy the current commit" and "the checker is
    # broken", and those are not the same emergency.
    if printf '%s' "$out" | grep -q 'Traceback (most recent call last)'; then
        fail "site/drift" "drift_check.py CRASHED — this says nothing about the site either way"
        printf '%s\n' "$out" | tail -20 | while IFS= read -r line; do note "$line"; done
        note "Fix the tool, then re-run. Do not read this as 'the site is fine'."
        return
    fi

    fail "site/drift" "the served site is NOT this commit (drift_check.py exit $rc)"
    printf '%s\n' "$out" | head -40 | while IFS= read -r line; do note "$line"; done
    note "Two things this means, and they need opposite responses:"
    note "  * a file differs      -> the deploy did not happen, or did not finish."
    note "                           Deploy this commit, then re-run. Do NOT edit"
    note "                           the repo to match what is live."
    note "  * a file 404s         -> either the deploy dropped it, or this host"
    note "                           does not publish the site (see --site-url)."
    note "Do not add to EDGE_STRIPS in drift_check.py to make this quiet. The last"
    note "edge transformation nobody understood corrupted the install command shown"
    note "to every visitor, and the origin looked perfect the whole time."
}

# ===========================================================================
# 12. The published digest verifies the published payload
# ===========================================================================
# site/verify.html tells a visitor to fetch install.sh, fetch install.sh.sha256,
# and compare. This performs that, over the network, on the real URLs. It is
# deliberately independent of check 11: no repository, no Python, no knowledge of
# what the file is supposed to contain -- so it still answers when 11 cannot, and
# a bug in one does not silence the other.
#
# What it catches that nothing else does: a deploy that copied install.sh and
# install.sh.sha256 from different commits. CI proves they matched in the tree;
# only this proves they still match after whatever moved them.
# ===========================================================================
# 12b. Whose robots.txt is the internet actually being served?
# ===========================================================================
# site/robots.txt exists because the live /robots.txt used to be a CLOUDFLARE
# MANAGED DEFAULT: not in version control, written by nobody here, and saying
# `Disallow: /` for ClaudeBot, GPTBot, Google-Extended and six others. This
# project is MIT-licensed and its stated direction is to be a resource AI
# agents read and quote, so blocking every AI crawler was not a decision anyone
# made — it was a platform default nobody could see.
#
# Copying the file into the webroot does NOT fix that. A managed robots.txt is
# injected at the edge and PREPENDS itself to the origin's, so the file in git
# can be shadowed while looking perfectly deployed from the server's side. Only
# an off-host fetch can tell.
#
# WARN and not FAIL, deliberately: the switch is a dashboard setting in an
# account this repository cannot see, an operator may legitimately decide the
# other way, and a red gate over something the person running it may not be
# able to change today is how a gate gets ignored. But it is reported every
# single run, because the file's own header says the check is the only thing
# that does not move when the menu does.
check_managed_robots() {
    step "12b. robots.txt — ours, or the platform's?"

    local repo="$SCRIPT_DIR/../site/robots.txt"
    if [ ! -r "$repo" ]; then
        skip "site/robots" "site/robots.txt not readable from here (run from a checkout)"
        return
    fi

    local served
    served="$(curl -sS --max-time 20 -H 'Cache-Control: no-cache' "${SITE_URL}/robots.txt" 2>/dev/null)"
    if [ -z "$served" ]; then
        warn "site/robots" "no /robots.txt served at all"
        return
    fi

    # Compare DIRECTIVE lines, not bytes and not Cloudflare-specific strings.
    #
    # Not bytes: comments and ordering are cosmetic here, and a byte compare
    # would make this fail for reasons that do not change any crawler's
    # behaviour.
    #
    # Not "does it contain 'ai-train=no'": site/robots.txt's own header comment
    # QUOTES all three of the obvious tells while explaining what it replaces,
    # so a substring search over the whole body flags the correct file as the
    # injected one. That is not hypothetical -- it is what the first version of
    # this check did, and it reported a locally-served, unmodified
    # site/robots.txt as "managed". Stripping `#` lines is what makes the
    # question answerable.
    #
    # A directive the origin never wrote can only have come from something
    # between the origin and the client, whatever that something calls itself
    # this year.
    local directives
    directives() { grep -vE '^[[:space:]]*(#|$)' | tr -d '\r' | sed 's/[[:space:]]*$//'; }

    local extra
    extra="$(printf '%s\n' "$served" | directives \
             | grep -vxF -f <(directives < "$repo") 2>/dev/null)"

    if [ -n "$extra" ]; then
        local n
        n="$(printf '%s\n' "$extra" | grep -c .)"
        warn "site/robots" "$n directive line(s) served that site/robots.txt does not contain"
        note "Something between the origin and the client is INJECTING robots"
        note "directives. On this zone that is Cloudflare's managed robots.txt,"
        note "which prepends itself above the origin's file, so site/robots.txt"
        note "looks perfectly deployed from the server's side and is inert."
        note "Turn it off: Cloudflare dashboard -> this zone -> AI Crawl Control"
        note "-> stop managing robots.txt. The menu moves; this check does not."
        # Deduplicated: the managed block is nine `Disallow: /` under nine
        # different agents, and printing it verbatim buries the one line that
        # actually states the policy under eight copies of the mechanism.
        note "The injected directives (deduplicated):"
        printf '%s\n' "$extra" | awk '!seen[$0]++' | head -10 \
            | while IFS= read -r line; do note "    $line"; done
        note "In full, with the User-agent each one sits under:"
        note "    curl -sS ${SITE_URL}/robots.txt | head -60"
        return
    fi

    pass "site/robots" "every served directive is one site/robots.txt declares"
}

check_published_digests() {
    step "12. Published digests verify the published payloads"

    local sha=""
    if have sha256sum;  then sha="sha256sum"
    elif have shasum;   then sha="shasum -a 256"
    elif have openssl;  then sha="openssl dgst -sha256 -r"
    fi
    if [ -z "$sha" ]; then
        fail "site/digests" "no sha256sum, shasum or openssl — the check a visitor is told to run cannot be run here"
        return
    fi

    # sha256 of zero bytes. `curl -f` writes nothing on an HTTP error, and the
    # digest of nothing is a perfectly valid-looking hex string, so "the fetch
    # failed" and "the file is empty" would otherwise be reported as a digest
    # MISMATCH -- the alarming answer to the wrong question.
    local EMPTY_SHA256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    # The payload list is DERIVED from site/ when a checkout is present, and
    # only falls back to a literal list when it is not. A hard-coded list makes
    # coverage opt-in by filename and shrinks as a share of the site every time
    # someone adds a file -- which is precisely how two installers publishing the
    # headline figures ended up outside the guard that claimed to cover them
    # (see the `published-numbers` job in .github/workflows/ci.yml). A file that
    # is in the repo and 404s on the site is a finding, not an omission.
    local payloads
    if [ -d "$REPO_ROOT/site" ]; then
        payloads="$(cd "$REPO_ROOT/site" && ls -1 -- *.sh *.ps1 2>/dev/null)"
    fi
    if [ -z "${payloads:-}" ]; then
        payloads="install.sh
install.ps1
uninstall.sh
uninstall.ps1"
        note "no site/ checkout here; falling back to the four documented payload names"
    fi

    local name url got want rc
    for name in $payloads; do
        url="${SITE_URL}/${name}"

        # Piped, never captured into a variable: command substitution strips
        # trailing newlines, and a digest over "the file minus its last newline"
        # is a mismatch that would look like an attack.
        got="$(curl -fsS --max-time 30 "$url" 2>/dev/null | $sha 2>/dev/null | awk '{print $1}')"
        rc=$?
        if [ "$rc" -ne 0 ] || [ -z "$got" ] || [ "$got" = "$EMPTY_SHA256" ]; then
            fail "digest/$name" "could not fetch $url (or it was served empty)"
            continue
        fi

        want="$(curl -fsS --max-time 20 "${url}.sha256" 2>/dev/null | awk 'NR==1{print $1}')"
        if [ -z "$want" ]; then
            fail "digest/$name" "$url is served but ${name}.sha256 is not — the documented verification step cannot be performed by a visitor"
            continue
        fi

        if [ "$got" = "$want" ]; then
            pass "digest/$name" "${got:0:16}... matches the published .sha256"
        else
            fail "digest/$name" "PUBLISHED DIGEST DOES NOT MATCH THE PUBLISHED FILE"
            note "served file : $got"
            note "served digest: $want"
            note "A visitor following site/verify.html sees this as a failure and is"
            note "told, correctly, not to run the script. Assume the deploy is"
            note "half-applied until proven otherwise; if the bytes were changed by"
            note "something other than a deploy, treat it as a compromise of the"
            note "distribution path and rotate before republishing."
        fi
    done
}

# ===========================================================================
# Run
# ===========================================================================
check_tls
check_health
check_pool_status
check_auth_enforced
check_roundtrip
check_streaming
check_management_blocked
check_engine_port_closed
check_security_headers
check_registration_exposure
check_site_drift
check_published_digests
check_managed_robots

# ── summary ────────────────────────────────────────────────────────────────
RULE="+--------+--------------------------------+---------------------------------------------------+"
printf '\n%s%s-- Summary%s\n' "$C_BLD" "$C_BLU" "$C_OFF"
printf '%s\n' "$RULE"
printf '| %s | %s | %s |\n' "$(pad STATUS 6)" "$(pad CHECK 30)" "$(pad DETAIL 49)"
printf '%s\n' "$RULE"
i=0
while [ "$i" -lt "${#R_STATUS[@]}" ]; do
    case "${R_STATUS[$i]}" in
        PASS) c="$C_GRN" ;; FAIL) c="$C_RED" ;; WARN) c="$C_YLW" ;; *) c="$C_DIM" ;;
    esac
    printf '| %s%s%s | %s | %s |\n' "$c" "$(pad "${R_STATUS[$i]}" 6)" "$C_OFF" \
        "$(pad "$(clip "$(ascii "${R_NAME[$i]}")" 30)" 30)" \
        "$(pad "$(clip "$(ascii "$(redact "${R_DETAIL[$i]}")")" 49)" 49)"
    i=$((i+1))
done
printf '%s\n' "$RULE"
printf '  %s%s pass%s   %s%s warn%s   %s%s fail%s   %s%s skip%s\n\n' \
    "$C_GRN" "$N_PASS" "$C_OFF" "$C_YLW" "$N_WARN" "$C_OFF" \
    "$C_RED" "$N_FAIL" "$C_OFF" "$C_DIM" "$N_SKIP" "$C_OFF"

if [ "$JSON" -eq 1 ]; then
    printf '{"version":"%s","base_url":"%s","dialect":"%s","model":"%s",' \
        "$VERSION" "$(json_escape "$BASE_URL")" "$DIALECT" "$(json_escape "$MODEL")"
    printf '"pass":%s,"warn":%s,"fail":%s,"skip":%s,"checks":[' "$N_PASS" "$N_WARN" "$N_FAIL" "$N_SKIP"
    i=0
    while [ "$i" -lt "${#R_STATUS[@]}" ]; do
        [ "$i" -gt 0 ] && printf ','
        printf '{"status":"%s","check":"%s","detail":"%s"}' "${R_STATUS[$i]}" \
            "$(json_escape "${R_NAME[$i]}")" "$(json_escape "$(redact "${R_DETAIL[$i]}")")"
        i=$((i+1))
    done
    printf ']}\n'
fi

if [ "$N_FAIL" -gt 0 ]; then
    printf '%s%sSMOKE TEST FAILED.%s %s check(s) failed.\n' "$C_BLD" "$C_RED" "$C_OFF" "$N_FAIL"
    printf 'Do NOT open registration or announce. See deploy/GO_LIVE.md for the rollback for each step.\n\n'
    exit 1
fi
printf '%s%sALL CHECKS PASSED.%s' "$C_BLD" "$C_GRN" "$C_OFF"
[ "$NO_SPEND" -eq 1 ] && printf ' (upstream round trips were skipped — re-run without --no-spend before announcing)'
printf '\n\n'
exit 0
