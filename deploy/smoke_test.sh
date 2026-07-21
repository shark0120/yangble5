#!/usr/bin/env bash
#
# yangble5 — POST-DEPLOY smoke test. Run this from OUTSIDE the server
# (your laptop), against the public URL, after install.sh has brought the
# stack up. Running it on the VPS itself will still pass the health checks but
# tells you nothing about whether the internet can reach you, and it will make
# the "management surface is not exposed" check meaningless.
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
#
# CHECKS 5 AND 6 SPEND TOKENS on whatever upstream account you configured.
# Two requests, max_tokens=16. Pass --no-spend to skip them.
#
# SECRETS: the API key is passed to curl through a config file on stdin
# (`curl -K -`), never as a command-line argument, so it does not appear in
# `ps`, in your shell history, or in any process listing on a shared box.
# Nothing is ever written to disk. The key is redacted from all output.
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
readonly VERSION="1.0.0"

BASE_URL="${YANGBLE5_BASE_URL:-}"
API_KEY="${YANGBLE5_API_KEY:-}"
MODEL="${YANGBLE5_SMOKE_MODEL:-yangble5}"
DIALECT="anthropic"
TIMEOUT=120
NO_SPEND=0
JSON=0
KEY_ON_CLI=0
TLS_REACHABLE=0   # set by check_tls; later checks skip rather than repeat a dead probe

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
  --api-key-file PATH  read the key from a file (first line)
  --api-key VALUE      pass the key inline (DISCOURAGED: shell history + ps)
  --model NAME         model alias to exercise (default: yangble5)
  --dialect NAME       anthropic | openai   (default: anthropic)
  --timeout SECONDS    per-request timeout (default: 120)
  --no-spend           skip the two checks that actually call the upstream
  --json               print a JSON summary after the table
  -h, --help           this text

The key may also come from $YANGBLE5_API_KEY, or be typed at a prompt.
Checks 5 and 6 spend tokens on your upstream account (max_tokens=16 each).
'
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --base-url)     BASE_URL="${2:?--base-url needs a URL}"; shift 2 ;;
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

    # HSTS is set by the Caddyfile; its absence means the config did not load.
    if [ "$TLS_REACHABLE" -ne 1 ]; then
        skip "tls/hsts" "host unreachable"
        return
    fi
    local hsts
    hsts="$(curl -sSI --max-time 15 "${BASE_URL}/health" 2>/dev/null | grep -i '^strict-transport-security:' | tr -d '\r')"
    if [ -n "$hsts" ]; then pass "tls/hsts" "$(printf '%s' "$hsts" | cut -c1-60)"
    else warn "tls/hsts" "no Strict-Transport-Security header — is the Caddyfile the one from this repo?"; fi
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
    body="$(request_body true)"

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
    ctype="$(printf '%s' "$meta" | awk '{print $6}')"
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

    # Buffering heuristic. awk because the shell has no floats.
    local gap verdict
    gap="$(awk -v a="${total:-0}" -v b="${ttfb:-0}" 'BEGIN{printf "%.3f", a-b}')"
    verdict="$(awk -v g="$gap" -v e="${events:-0}" 'BEGIN{
        if (e >= 3 && g < 0.15) print "buffered";
        else if (g < 0.05 && e >= 2) print "suspect";
        else print "streaming"; }')"

    case "$verdict" in
        streaming) pass "api/streaming" "$events events, first byte ${ttfb}s, last ${total}s (delivered over ${gap}s)" ;;
        buffered)  fail "api/streaming" "$events events but all arrived within ${gap}s of each other — the stream is being BUFFERED"
                   note "something is collecting the whole response before releasing it. In order of likelihood:"
                   note "  1. a Cloudflare setting that transforms the body (Rocket Loader / auto-minify / compression)"
                   note "  2. a reverse proxy in front of caddy that is not this repo's Caddyfile"
                   note "  3. flush_interval not set to -1 on the gateway upstream"
                   note "see deploy/cloudflare.md 'Response buffering'" ;;
        *)         warn "api/streaming" "$events events, delivered over ${gap}s — too fast to tell buffering from a short reply; re-run with a longer prompt" ;;
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
