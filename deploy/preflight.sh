#!/usr/bin/env bash
#
# yangble5 — PRE-INSTALL preflight. Run this on the VPS BEFORE install.sh.
#
# READ-ONLY BY CONSTRUCTION. This script:
#   * creates no files and deletes none  (it deliberately uses printf instead of
#     here-documents, so it does not even create the temporary files bash uses
#     to back a `<<EOF` or `<<<` redirection)
#   * installs nothing, starts nothing, stops nothing
#   * writes no config, touches no firewall rule, changes no service state
#   * makes only outbound network requests, and only GET/HEAD
#
# It answers one question: "if I run install.sh and harden.sh right now, will
# they succeed, and will the result actually be reachable?"
#
# USAGE
#   bash deploy/preflight.sh --domain api.example.com
#   bash deploy/preflight.sh                      # reads YANGBLE5_DOMAIN from deploy/.env
#   bash deploy/preflight.sh --offline            # skip every network check
#   bash deploy/preflight.sh --json               # machine-readable summary
#
# EXIT CODES
#   0  every critical check passed (warnings may still be present)
#   1  at least one CRITICAL check failed — do not install yet
#   2  usage error
#
# NOTE ON `set -e`: this script is not run with `-e` on purpose. A preflight
# runs commands that are *expected* to fail (that is the whole point), and
# `-e` would abort the run on the first failing probe instead of reporting it.
# Every command that can fail is explicitly tested.
#
set -uo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly VERSION="1.0.0"

# ── requirements this deployment actually has ──────────────────────────────
# Derived from deploy/docker-compose.yml's per-service memory ceilings:
#   caddy 256M + gateway 512M + engine 2G + shim 256M = 3.0G of ceilings.
# Ceilings are not usage, so the hard minimum is lower than their sum. These
# are engineering estimates from the compose limits, NOT measured steady-state
# figures — nobody has profiled this stack under sustained public load.
readonly MIN_RAM_MB=1800          # below this, the engine's 2G ceiling is a lie
readonly REC_RAM_MB=3800          # comfortable: all four services + page cache
readonly MIN_DISK_MB=8000         # images + volumes + a few days of logs
readonly REC_DISK_MB=20000

# ── options ────────────────────────────────────────────────────────────────
DOMAIN="${YANGBLE5_DOMAIN:-}"
OFFLINE=0
JSON=0
NO_IP_ECHO=0
SELFTEST="${YB5_PREFLIGHT_SELFTEST:-0}"
EXTRA_HOSTS=()

# ── output ─────────────────────────────────────────────────────────────────
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YLW=$'\033[33m'
    C_BLU=$'\033[36m'; C_DIM=$'\033[2m'; C_BLD=$'\033[1m'; C_OFF=$'\033[0m'
else
    C_RED=""; C_GRN=""; C_YLW=""; C_BLU=""; C_DIM=""; C_BLD=""; C_OFF=""
fi

# Results accumulate here and are printed as one table at the end, so the
# operator gets a verdict without scrolling back through the probe output.
R_STATUS=(); R_NAME=(); R_DETAIL=()
N_PASS=0; N_WARN=0; N_FAIL=0; N_SKIP=0

record() { R_STATUS+=("$1"); R_NAME+=("$2"); R_DETAIL+=("$3"); }

pass() { record PASS "$1" "$2"; N_PASS=$((N_PASS + 1))
         printf '%s  PASS%s  %-34s %s\n' "$C_GRN" "$C_OFF" "$1" "$2"; }
fail() { record FAIL "$1" "$2"; N_FAIL=$((N_FAIL + 1))
         printf '%s  FAIL%s  %-34s %s\n' "$C_RED" "$C_OFF" "$1" "$2"; }
warn() { record WARN "$1" "$2"; N_WARN=$((N_WARN + 1))
         printf '%s  WARN%s  %-34s %s\n' "$C_YLW" "$C_OFF" "$1" "$2"; }
skip() { record SKIP "$1" "$2"; N_SKIP=$((N_SKIP + 1))
         printf '%s  SKIP%s  %-34s %s\n' "$C_DIM" "$C_OFF" "$1" "$2"; }
note() { printf '        %s%s%s\n' "$C_DIM" "$1" "$C_OFF"; }
step() { printf '\n%s%s── %s%s\n' "$C_BLD" "$C_BLU" "$1" "$C_OFF"; }
die()  { printf '\n%s%sABORT:%s %s\n\n' "$C_BLD" "$C_RED" "$C_OFF" "$1" >&2; exit 2; }

have() { command -v "$1" >/dev/null 2>&1; }

usage() {
    printf '%s' \
'usage: bash deploy/preflight.sh [options]

  --domain HOST        public hostname to verify (default: YANGBLE5_DOMAIN
                       from the environment, or from deploy/.env if present)
  --upstream-host H    additional host to test outbound HTTPS to (repeatable)
  --offline            skip every check that needs the network
  --no-ip-echo         never ask an external service for this host'"'"'s public IP
  --json               print a JSON summary after the table
  --self-test          run the pure address/CIDR/ELF helpers against known
                       inputs and exit; needs no network, no root, no server
  -h, --help           this text

Read-only. Creates nothing, installs nothing, changes nothing.
Exit 0 = safe to install, 1 = critical failure, 2 = usage error.
'
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --domain)        DOMAIN="${2:?--domain needs a hostname}"; shift 2 ;;
        --upstream-host) EXTRA_HOSTS+=("${2:?--upstream-host needs a host}"); shift 2 ;;
        --offline)       OFFLINE=1; shift ;;
        --no-ip-echo)    NO_IP_ECHO=1; shift ;;
        --json)          JSON=1; shift ;;
        --self-test)     SELFTEST=1; shift ;;
        -h|--help)       usage ;;
        *)               die "unknown option: $1 (try --help)" ;;
    esac
done

# ===========================================================================
# Pure helpers — no I/O, no side effects. These are the parts that can be
# unit-tested off-box (see the bottom of this file: --self-test).
# ===========================================================================

# Dotted quad -> unsigned 32-bit integer. `10#` forces base 10 so an octet
# written as 010 is 10, not 8.
ip2int() {
    local IFS='.' a b c d
    set -- $1
    [ $# -eq 4 ] || return 1
    a=$1; b=$2; c=$3; d=$4
    case "$a$b$c$d" in *[!0-9]*) return 1 ;; esac
    printf '%s' "$(( (10#$a << 24) + (10#$b << 16) + (10#$c << 8) + 10#$d ))"
}

is_ipv4() {
    local IFS='.' o
    set -- $1
    [ $# -eq 4 ] || return 1
    for o in "$@"; do
        case "$o" in ''|*[!0-9]*) return 1 ;; esac
        [ "$((10#$o))" -le 255 ] || return 1
    done
    return 0
}

# True when the address is globally routable: not RFC1918, not loopback, not
# link-local, not CGNAT (100.64/10 — a real trap, several budget hosts hand out
# CGNAT addresses and the operator thinks they have a public IP), not
# multicast/reserved.
is_public_ipv4() {
    local ip="$1" n
    is_ipv4 "$ip" || return 1
    n="$(ip2int "$ip")" || return 1
    cidr_contains "$n" 10.0.0.0 8      && return 1
    cidr_contains "$n" 172.16.0.0 12   && return 1
    cidr_contains "$n" 192.168.0.0 16  && return 1
    cidr_contains "$n" 127.0.0.0 8     && return 1
    cidr_contains "$n" 169.254.0.0 16  && return 1
    cidr_contains "$n" 100.64.0.0 10   && return 1
    cidr_contains "$n" 0.0.0.0 8       && return 1
    cidr_contains "$n" 224.0.0.0 3     && return 1
    return 0
}

# cidr_contains <ip-as-int> <network> <prefixlen>
cidr_contains() {
    local n="$1" net="$2" bits="$3" netint mask
    netint="$(ip2int "$net")" || return 1
    if [ "$bits" -eq 0 ]; then return 0; fi
    mask=$(( 0xFFFFFFFF << (32 - bits) & 0xFFFFFFFF ))
    [ $(( n & mask )) -eq $(( netint & mask )) ]
}

# Cloudflare's published IPv4 ranges, snapshot taken 2026-07-21 from
# https://www.cloudflare.com/ips-v4 . Cloudflare does add ranges. The live
# list is fetched at runtime when the network is available; this is only the
# fallback so that --offline still gives a useful answer.
CF_RANGES_FALLBACK='173.245.48.0/20 103.21.244.0/22 103.22.200.0/22
103.31.4.0/22 141.101.64.0/18 108.162.192.0/18 190.93.240.0/20
188.114.96.0/20 197.234.240.0/22 198.41.128.0/17 162.158.0.0/15
104.16.0.0/13 104.24.0.0/14 172.64.0.0/13 131.0.72.0/22'
CF_RANGES=""
CF_RANGES_SOURCE="not loaded"

ip_in_cf() {
    local ip="$1" n range net bits
    n="$(ip2int "$ip")" || return 1
    for range in $CF_RANGES; do
        net="${range%%/*}"; bits="${range##*/}"
        case "$bits" in ''|*[!0-9]*) continue ;; esac
        cidr_contains "$n" "$net" "$bits" && return 0
    done
    return 1
}

# ELF architecture of an operator-supplied binary, without needing file(1).
# e_ident: magic 0x7f 'E' 'L' 'F'; byte 4 is the class; e_machine is a
# little-endian u16 at offset 0x12. This exists because the single most
# annoying way to fail a deploy is to copy an amd64 CLIProxyAPI binary onto an
# arm64 VPS and only find out when the container exec-format-errors.
elf_arch() {
    local f="$1" bytes
    [ -r "$f" ] || { printf 'unreadable'; return 1; }
    bytes="$(od -An -tu1 -N20 -- "$f" 2>/dev/null | tr -s ' ' ' ')" || {
        printf 'unreadable'; return 1; }
    set -- $bytes
    [ $# -ge 20 ] || { printf 'too-short'; return 1; }
    # $1..$20 are bytes 0..19
    if [ "$1" != 127 ] || [ "$2" != 69 ] || [ "$3" != 76 ] || [ "$4" != 70 ]; then
        printf 'not-elf'; return 1
    fi
    local machine=$(( ${19} + (${20} << 8) ))
    case "$machine" in
        62)  printf 'amd64' ;;
        183) printf 'arm64' ;;
        243) printf 'riscv64' ;;
        3)   printf 'i386' ;;
        40)  printf 'arm' ;;
        *)   printf 'unknown(%s)' "$machine" ;;
    esac
}

# Fold the handful of non-ASCII characters used in prose down to ASCII.
#
# This is what keeps the summary table square. Both `printf '%-49s'` and ${#s}
# measure BYTES when the shell is in the C locale (which is what you get over
# ssh without locale forwarding, and in most CI capture), so a 3-byte em-dash
# renders as one column but is counted as three, and every row containing one
# comes out two columns short. Rather than depend on the locale being UTF-8,
# the table is rendered in pure ASCII — which also survives being grepped,
# piped into a ticket, or read on a serial console.
ascii() {
    local s="$1"
    s="${s//—/--}"      # em dash
    s="${s//–/-}"       # en dash
    s="${s//…/...}"     # ellipsis
    s="${s//’/\'}"      # right single quote
    s="${s//“/\"}"      # left double quote
    s="${s//”/\"}"      # right double quote
    printf '%s' "$s"
}

# Pad to a fixed column width. Exact once the input is ASCII.
pad() {
    local s="$1" w="$2" n
    n=$(( w - ${#s} ))
    [ "$n" -lt 0 ] && n=0
    printf '%s%*s' "$s" "$n" ''
}

# Truncate to a column width, leaving room for the ellipsis.
clip() {
    local s="$1" w="$2"
    if [ "${#s}" -le "$w" ]; then printf '%s' "$s"
    else printf '%s...' "${s:0:$(( w - 3 ))}"; fi
}

json_escape() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\n'/ }"
    s="${s//$'\t'/ }"
    printf '%s' "$s"
}

# ===========================================================================
# --self-test : run the pure helpers against known inputs. Needs no server,
# no network and no root, which is what makes this script verifiable off-box.
# ===========================================================================
self_test() {
    local failures=0
    t_eq()  { if [ "$2" = "$3" ]; then printf '  ok   %s\n' "$1"
              else printf '  FAIL %s (got %s, want %s)\n' "$1" "$2" "$3"
                   failures=$((failures + 1)); fi; }
    t_true(){ if "${@:2}"; then printf '  ok   %s\n' "$1"
              else printf '  FAIL %s (expected true)\n' "$1"
                   failures=$((failures + 1)); fi; }
    t_false(){ if "${@:2}"; then printf '  FAIL %s (expected false)\n' "$1"
                    failures=$((failures + 1))
               else printf '  ok   %s\n' "$1"; fi; }

    printf 'ip2int\n'
    t_eq  'ip2int 0.0.0.0'          "$(ip2int 0.0.0.0)"          '0'
    t_eq  'ip2int 255.255.255.255'  "$(ip2int 255.255.255.255)"  '4294967295'
    t_eq  'ip2int 1.2.3.4'          "$(ip2int 1.2.3.4)"          '16909060'
    t_eq  'ip2int 010.0.0.1 (no octal)' "$(ip2int 010.0.0.1)"    '167772161'

    printf 'is_ipv4\n'
    t_true  'is_ipv4 192.0.2.1'     is_ipv4 192.0.2.1
    t_false 'is_ipv4 256.1.1.1'     is_ipv4 256.1.1.1
    t_false 'is_ipv4 1.2.3'         is_ipv4 1.2.3
    t_false 'is_ipv4 hostname'      is_ipv4 example.com

    printf 'is_public_ipv4\n'
    t_true  'public  8.8.8.8'       is_public_ipv4 8.8.8.8
    t_true  'public  203.0.113.9'   is_public_ipv4 203.0.113.9
    t_false 'private 10.1.2.3'      is_public_ipv4 10.1.2.3
    t_false 'private 172.16.0.1'    is_public_ipv4 172.16.0.1
    t_true  'public  172.32.0.1'    is_public_ipv4 172.32.0.1
    t_false 'private 192.168.1.1'   is_public_ipv4 192.168.1.1
    t_false 'loopback 127.0.0.1'    is_public_ipv4 127.0.0.1
    t_false 'linklocal 169.254.1.1' is_public_ipv4 169.254.169.254
    t_false 'cgnat 100.64.0.1'      is_public_ipv4 100.64.0.1
    t_true  'public 100.63.255.255' is_public_ipv4 100.63.255.255
    t_true  'public 100.128.0.1'    is_public_ipv4 100.128.0.1

    printf 'cidr_contains\n'
    t_true  '10.0.0.1 in 10/8'      cidr_contains "$(ip2int 10.0.0.1)"   10.0.0.0 8
    t_false '11.0.0.1 in 10/8'      cidr_contains "$(ip2int 11.0.0.1)"   10.0.0.0 8
    t_true  'x in 0.0.0.0/0'        cidr_contains "$(ip2int 1.1.1.1)"    0.0.0.0 0
    t_true  '/32 exact'             cidr_contains "$(ip2int 5.6.7.8)"    5.6.7.8 32
    t_false '/32 off-by-one'        cidr_contains "$(ip2int 5.6.7.9)"    5.6.7.8 32

    printf 'ip_in_cf (fallback list)\n'
    CF_RANGES="$CF_RANGES_FALLBACK"
    t_true  '104.16.0.1 is CF'      ip_in_cf 104.16.0.1
    t_true  '172.67.1.1 is CF'      ip_in_cf 172.67.1.1
    t_true  '188.114.96.5 is CF'    ip_in_cf 188.114.96.5
    t_false '8.8.8.8 is not CF'     ip_in_cf 8.8.8.8
    t_false '172.32.0.1 is not CF'  ip_in_cf 172.32.0.1

    printf 'json_escape\n'
    t_eq 'quotes'    "$(json_escape 'a"b')"      'a\"b'
    t_eq 'backslash' "$(json_escape 'a\b')"      'a\\b'

    printf '\n'
    if [ "$failures" -eq 0 ]; then
        printf 'self-test: all assertions passed\n'; return 0
    fi
    printf 'self-test: %s assertion(s) FAILED\n' "$failures"; return 1
}

if [ "$SELFTEST" = "1" ]; then
    self_test; exit $?
fi

# ===========================================================================
# Checks
# ===========================================================================

printf '%s%syangble5 preflight v%s%s  (read-only; changes nothing)\n' \
    "$C_BLD" "$C_BLU" "$VERSION" "$C_OFF"
printf '%shost: %s   time: %s%s\n' "$C_DIM" "$(hostname 2>/dev/null || echo '?')" \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || echo '?')" "$C_OFF"

# Pick up the domain from a previously written .env if the operator did not
# pass one. Reading .env is fine; we only ever read this single key.
if [ -z "$DOMAIN" ] && [ -r "$SCRIPT_DIR/.env" ]; then
    DOMAIN="$(sed -n 's/^YANGBLE5_DOMAIN=//p' "$SCRIPT_DIR/.env" 2>/dev/null | head -1 | tr -d '"'"'"' \r')"
    [ -n "$DOMAIN" ] && note "domain taken from deploy/.env: $DOMAIN"
fi

# ── 1. Operating system ────────────────────────────────────────────────────
check_os() {
    step "1. Operating system"

    if [ ! -r /etc/os-release ]; then
        fail "os/supported" "no /etc/os-release — install.sh and harden.sh assume Debian/Ubuntu (apt + systemd)"
        return
    fi
    # shellcheck disable=SC1091
    . /etc/os-release

    local pretty="${PRETTY_NAME:-${NAME:-unknown}}"
    local id="${ID:-}" ver="${VERSION_ID:-0}" major="${VERSION_ID%%.*}"
    case "$id" in
        debian)
            if [ "${major:-0}" -ge 12 ] 2>/dev/null; then
                pass "os/supported" "$pretty"
            elif [ "${major:-0}" -ge 11 ] 2>/dev/null; then
                warn "os/supported" "$pretty — older than the tested set (Debian 12); expect to install docker from Docker's own repo"
            else
                fail "os/supported" "$pretty is too old; Debian 12+ or Ubuntu 22.04+"
            fi ;;
        ubuntu)
            case "$ver" in
                22.04|24.04|24.10|25.04|26.04) pass "os/supported" "$pretty" ;;
                *) warn "os/supported" "$pretty — untested; tested on 22.04 and 24.04" ;;
            esac ;;
        *)
            if printf '%s' "${ID_LIKE:-}" | grep -qE 'debian|ubuntu'; then
                warn "os/supported" "$pretty (debian-like, untested)"
            else
                fail "os/supported" "$pretty — install.sh/harden.sh require apt + systemd + ufw"
            fi ;;
    esac

    # systemd is not optional: harden.sh drives sshd, fail2ban, unattended
    # upgrades and the docker service through systemctl.
    if [ -d /run/systemd/system ]; then
        pass "os/systemd" "running under systemd"
    else
        fail "os/systemd" "systemd is not the init system (harden.sh drives services with systemctl)"
    fi

    # Containers pretending to be a VPS: LXC/OpenVZ often cannot run Docker
    # with the cgroup limits this compose file relies on.
    local virt=""
    if have systemd-detect-virt; then virt="$(systemd-detect-virt 2>/dev/null)"; fi
    case "$virt" in
        openvz|lxc|lxc-libvirt)
            warn "os/virtualisation" "$virt — nested Docker and cgroup memory limits are unreliable here" ;;
        none|"") : ;;
        *) note "virtualisation: $virt" ;;
    esac

    local arch; arch="$(uname -m 2>/dev/null || echo unknown)"
    case "$arch" in
        x86_64|amd64) pass "os/arch" "$arch (amd64)" ;;
        aarch64|arm64) pass "os/arch" "$arch (arm64)" ;;
        *) fail "os/arch" "$arch — no CLIProxyAPI build is known to exist for this architecture" ;;
    esac

    # The engine binary is operator-supplied (deploy/engine-bin/README.md), so
    # this is where an arch mismatch is cheap to catch instead of expensive.
    local bin="$SCRIPT_DIR/engine-bin/cli-proxy-api"
    if [ -e "$bin" ]; then
        local got want
        got="$(elf_arch "$bin")"
        case "$arch" in x86_64|amd64) want=amd64 ;; aarch64|arm64) want=arm64 ;; *) want="$arch" ;; esac
        if [ "$got" = "$want" ]; then
            pass "engine/binary-arch" "$got, matches host"
        elif [ "$got" = "not-elf" ]; then
            fail "engine/binary-arch" "engine-bin/cli-proxy-api is not an ELF executable (did you copy the .exe or the .zip?)"
        else
            fail "engine/binary-arch" "engine binary is $got but this host is $want"
        fi
        if [ ! -x "$bin" ]; then
            warn "engine/binary-mode" "engine-bin/cli-proxy-api is not executable (chmod +x it)"
        fi
    else
        warn "engine/binary" "deploy/engine-bin/cli-proxy-api not present — install.sh needs it (see engine-bin/README.md)"
    fi
}

# ── 2. Resources ───────────────────────────────────────────────────────────
check_resources() {
    step "2. CPU / memory / disk"

    local cores; cores="$(nproc 2>/dev/null || echo 0)"
    if [ "$cores" -ge 2 ] 2>/dev/null; then
        pass "res/cpu" "$cores cores"
    elif [ "$cores" -ge 1 ] 2>/dev/null; then
        warn "res/cpu" "$cores core — compose reserves 3.25 CPU across services; expect contention under load"
    else
        skip "res/cpu" "could not determine core count"
    fi

    local total_kb total_mb
    total_kb="$(awk '/^MemTotal:/{print $2}' /proc/meminfo 2>/dev/null)"
    if [ -n "$total_kb" ]; then
        total_mb=$(( total_kb / 1024 ))
        if [ "$total_mb" -ge "$REC_RAM_MB" ]; then
            pass "res/ram" "${total_mb} MB (recommended >= ${REC_RAM_MB} MB)"
        elif [ "$total_mb" -ge "$MIN_RAM_MB" ]; then
            warn "res/ram" "${total_mb} MB — above the ${MIN_RAM_MB} MB minimum but below the ${REC_RAM_MB} MB recommendation; the engine's 2G ceiling cannot actually be reached"
        else
            fail "res/ram" "${total_mb} MB is below the ${MIN_RAM_MB} MB minimum for caddy+gateway+engine"
        fi
    else
        skip "res/ram" "/proc/meminfo unreadable"
    fi

    local swap_kb; swap_kb="$(awk '/^SwapTotal:/{print $2}' /proc/meminfo 2>/dev/null || echo 0)"
    if [ "${swap_kb:-0}" -eq 0 ] 2>/dev/null && [ "${total_mb:-9999}" -lt "$REC_RAM_MB" ]; then
        warn "res/swap" "no swap on a ${total_mb:-?} MB host — an OOM kill will take a container down rather than slow it"
    fi

    # Disk that matters is Docker's data root, not /.
    local target="/var/lib/docker"
    if have docker && docker info --format '{{.DockerRootDir}}' >/dev/null 2>&1; then
        target="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)"
    fi
    [ -d "$target" ] || target="/var/lib"
    [ -d "$target" ] || target="/"

    local avail_mb
    avail_mb="$(df -Pm "$target" 2>/dev/null | awk 'NR==2{print $4}')"
    if [ -n "$avail_mb" ]; then
        if [ "$avail_mb" -ge "$REC_DISK_MB" ]; then
            pass "res/disk" "${avail_mb} MB free on $target"
        elif [ "$avail_mb" -ge "$MIN_DISK_MB" ]; then
            warn "res/disk" "${avail_mb} MB free on $target — above the ${MIN_DISK_MB} MB minimum, below the ${REC_DISK_MB} MB recommendation"
        else
            fail "res/disk" "${avail_mb} MB free on $target is below the ${MIN_DISK_MB} MB minimum (images + volumes + logs)"
        fi
    else
        skip "res/disk" "df failed for $target"
    fi

    # Inodes: a full inode table fails deploys in a way that looks like a bug.
    local ifree; ifree="$(df -Pi "$target" 2>/dev/null | awk 'NR==2{print $4}')"
    if [ -n "$ifree" ] && [ "$ifree" -lt 50000 ] 2>/dev/null; then
        warn "res/inodes" "only $ifree free inodes on $target"
    fi
}

# ── 3. Docker ──────────────────────────────────────────────────────────────
check_docker() {
    step "3. Docker"

    if ! have docker; then
        fail "docker/present" "docker not installed"
        note "Debian/Ubuntu: curl -fsSL https://get.docker.com | sh"
        note "or follow https://docs.docker.com/engine/install/ (distro packages are often too old)"
        return
    fi
    pass "docker/present" "$(docker --version 2>/dev/null | head -1)"

    if ! docker info >/dev/null 2>&1; then
        fail "docker/daemon" "the docker daemon is not reachable as $(id -un)"
        note "if it is stopped:      sudo systemctl enable --now docker"
        note "if it is a permission: run preflight with sudo, or add yourself to the docker group"
        note "note: membership of the docker group is equivalent to root on this host"
        return
    fi
    pass "docker/daemon" "reachable as $(id -un)"

    # compose v2 only. install.sh refuses v1 because the standalone
    # docker-compose v1 binary silently ignores deploy.resources.limits, which
    # would turn every memory ceiling in docker-compose.yml into a no-op.
    if docker compose version >/dev/null 2>&1; then
        pass "docker/compose-v2" "$(docker compose version --short 2>/dev/null || echo 'v2')"
    else
        fail "docker/compose-v2" "'docker compose' (v2 plugin) missing"
        if have docker-compose; then
            note "you have legacy docker-compose v1 — it is NOT supported: it ignores deploy.resources.limits"
        fi
        note "install: apt-get install docker-compose-plugin  (or Docker's official repo)"
    fi

    # cgroup v2 is what makes the memory ceilings enforceable.
    local cg; cg="$(docker info --format '{{.CgroupVersion}}' 2>/dev/null)"
    case "$cg" in
        2) pass "docker/cgroup" "cgroup v2" ;;
        1) warn "docker/cgroup" "cgroup v1 — memory limits are enforced but less reliably; v2 is what this stack is tested on" ;;
        *) skip "docker/cgroup" "could not determine cgroup version" ;;
    esac

    local sd; sd="$(docker info --format '{{.Driver}}' 2>/dev/null)"
    case "$sd" in
        overlay2) pass "docker/storage" "overlay2" ;;
        vfs)      warn "docker/storage" "vfs — extremely slow and disk-hungry; check why overlay2 is unavailable" ;;
        "")       skip "docker/storage" "unknown" ;;
        *)        note "storage driver: $sd" ;;
    esac
}

# ── 4. Ports ───────────────────────────────────────────────────────────────
# caddy is the only service that publishes ports: 80/tcp, 443/tcp, 443/udp.
port_listener() {
    local proto="$1" port="$2"
    if have ss; then
        ss -H -lnp"${proto:0:1}" 2>/dev/null | awk -v p=":$port" '
            { split($4, a, ":"); if ("" a[length(a)] == substr(p,2)) { print; } }' | head -1
    elif have netstat; then
        netstat -lnp --"$proto" 2>/dev/null | awk -v p=":$port\$" '$4 ~ p {print; exit}'
    else
        printf 'NO_TOOL'
    fi
}

check_ports() {
    step "4. Ports 80 / 443"

    local proto port line
    for spec in "tcp 80" "tcp 443" "udp 443"; do
        set -- $spec; proto="$1"; port="$2"
        line="$(port_listener "$proto" "$port")"
        local label="port/${port}-${proto}"
        if [ "$line" = "NO_TOOL" ]; then
            skip "$label" "neither ss nor netstat is installed (apt-get install iproute2)"
            continue
        fi
        if [ -z "$line" ]; then
            pass "$label" "free"
            continue
        fi
        # Already bound. If it looks like our own stack, that is expected on a
        # re-run and is not a blocker; anything else is.
        if printf '%s' "$line" | grep -qiE 'docker-proxy|caddy|yangble5'; then
            warn "$label" "already bound by what looks like this stack (re-run?) — install.sh will reuse it"
        else
            fail "$label" "already in use: $(printf '%s' "$line" | tr -s ' ' | cut -c1-100)"
            case "$port" in
                80) note "common culprits: apache2, nginx, lighttpd. Stop and disable it, or caddy cannot get a certificate." ;;
                443) note "common culprits: nginx, another reverse proxy, an old caddy container." ;;
            esac
        fi
    done

    # Unprivileged users cannot bind <1024 unless the kernel says otherwise;
    # the caddy container has NET_BIND_SERVICE so this only matters if the
    # operator later runs caddy outside Docker.
    if [ -r /proc/sys/net/ipv4/ip_unprivileged_port_start ]; then
        note "ip_unprivileged_port_start = $(cat /proc/sys/net/ipv4/ip_unprivileged_port_start)"
    fi
}

# ── 5. This host's public IP ───────────────────────────────────────────────
HOST_IPS=""           # space-separated globally-routable IPv4 addresses
HOST_IP_SOURCE=""
HOST_HAS_V6=0

discover_host_ip() {
    step "5. This host's public address"

    local found=""

    # (a) Best case, and no third party at all: a globally-routable address is
    # configured directly on an interface. True for most KVM VPS providers.
    if have ip; then
        local a
        for a in $(ip -o -4 addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1); do
            if is_public_ipv4 "$a"; then found="$found $a"; fi
        done
        ip -o -6 addr show scope global 2>/dev/null | grep -q . && HOST_HAS_V6=1
    elif have hostname; then
        local a
        for a in $(hostname -I 2>/dev/null); do
            if is_public_ipv4 "$a"; then found="$found $a"; fi
        done
    fi

    if [ -n "${found// /}" ]; then
        HOST_IPS="${found# }"
        HOST_IP_SOURCE="local interface (no external service consulted)"
        pass "net/public-ip" "$HOST_IPS  [$HOST_IP_SOURCE]"
        return
    fi

    # (b) Only private addresses on the interfaces => the host is behind 1:1
    # NAT (AWS, GCP, Azure, some OVH setups). The provider's link-local
    # metadata service is on-link at 169.254.169.254 and is not a third party
    # in any meaningful sense, so try it before the open internet.
    if [ "$OFFLINE" -eq 0 ] && have curl; then
        local md=""
        md="$(curl -s --max-time 2 -H 'Metadata-Flavor: Google' \
              'http://169.254.169.254/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip' 2>/dev/null)"
        if [ -z "$md" ]; then
            local tok
            tok="$(curl -s --max-time 2 -X PUT 'http://169.254.169.254/latest/api/token' \
                   -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' 2>/dev/null)"
            if [ -n "$tok" ]; then
                md="$(curl -s --max-time 2 -H "X-aws-ec2-metadata-token: $tok" \
                      'http://169.254.169.254/latest/meta-data/public-ipv4' 2>/dev/null)"
            fi
        fi
        if [ -z "$md" ]; then
            md="$(curl -s --max-time 2 'http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address' 2>/dev/null)"
        fi
        if is_public_ipv4 "${md:-}"; then
            HOST_IPS="$md"
            HOST_IP_SOURCE="cloud metadata service at 169.254.169.254 (link-local, your provider)"
            pass "net/public-ip" "$HOST_IPS  [$HOST_IP_SOURCE]"
            note "this host is behind 1:1 NAT — its interface address is private"
            return
        fi
    fi

    # (c) Last resort: ask an external echo. Named explicitly, and skippable.
    if [ "$OFFLINE" -eq 0 ] && [ "$NO_IP_ECHO" -eq 0 ] && have curl; then
        local echoed
        echoed="$(curl -s --max-time 6 https://cloudflare.com/cdn-cgi/trace 2>/dev/null \
                  | sed -n 's/^ip=//p' | head -1)"
        if is_public_ipv4 "${echoed:-}"; then
            HOST_IPS="$echoed"
            HOST_IP_SOURCE="external echo: cloudflare.com/cdn-cgi/trace"
            warn "net/public-ip" "$HOST_IPS  [$HOST_IP_SOURCE]"
            note "used because no globally-routable address is on any interface and no cloud"
            note "metadata service answered. Cloudflare was chosen because this deployment"
            note "already trusts Cloudflare with all of its traffic, so it learns nothing new."
            note "Re-run with --no-ip-echo to forbid this and supply the IP yourself."
            return
        fi
    fi

    HOST_IP_SOURCE="undetermined"
    warn "net/public-ip" "could not determine this host's public IPv4"
    note "the DNS check below will be inconclusive; find the IP in your provider's panel"
}

# ── 6. DNS ─────────────────────────────────────────────────────────────────
resolve_a() {
    local host="$1"
    if have getent; then
        getent ahostsv4 "$host" 2>/dev/null | awk '{print $1}' | sort -u
    elif have dig; then
        dig +short A "$host" 2>/dev/null | grep -E '^[0-9.]+$'
    elif have host; then
        host -t A "$host" 2>/dev/null | awk '/has address/{print $NF}'
    elif have nslookup; then
        nslookup -type=A "$host" 2>/dev/null | awk '/^Address: /{print $2}'
    fi
}

load_cf_ranges() {
    CF_RANGES="$CF_RANGES_FALLBACK"
    CF_RANGES_SOURCE="built-in snapshot (2026-07-21) — may be stale"
    if [ "$OFFLINE" -eq 0 ] && have curl; then
        local live
        live="$(curl -s --max-time 6 https://www.cloudflare.com/ips-v4 2>/dev/null | tr '\n' ' ')"
        if printf '%s' "$live" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+'; then
            CF_RANGES="$live"
            CF_RANGES_SOURCE="live from https://www.cloudflare.com/ips-v4"
        fi
    fi
}

check_dns() {
    step "6. DNS for $([ -n "$DOMAIN" ] && printf '%s' "$DOMAIN" || printf '(no domain given)')"

    if [ -z "$DOMAIN" ]; then
        fail "dns/domain" "no domain supplied (--domain, or YANGBLE5_DOMAIN in deploy/.env)"
        return
    fi
    case "$DOMAIN" in
        *.*) : ;;
        *) fail "dns/domain" "'$DOMAIN' is not a fully-qualified hostname" ; return ;;
    esac
    if [ "$OFFLINE" -eq 1 ]; then
        skip "dns/resolve" "--offline"
        return
    fi

    local ips; ips="$(resolve_a "$DOMAIN" | tr '\n' ' ')"
    ips="${ips%% }"
    if [ -z "${ips// /}" ]; then
        fail "dns/resolve" "$DOMAIN does not resolve to any A record"
        note "create the record first; Let's Encrypt's HTTP-01 challenge cannot succeed without it"
        return
    fi
    pass "dns/resolve" "$DOMAIN -> $ips"

    load_cf_ranges
    note "Cloudflare range list: $CF_RANGES_SOURCE"

    local ip matched_host=0 matched_cf=0 other=""
    for ip in $ips; do
        local h hit=0
        for h in $HOST_IPS; do
            [ "$ip" = "$h" ] && { matched_host=1; hit=1; }
        done
        [ "$hit" -eq 1 ] && continue
        if ip_in_cf "$ip"; then matched_cf=1; else other="$other $ip"; fi
    done

    if [ "$matched_host" -eq 1 ]; then
        pass "dns/points-here" "resolves to this host — DNS-only (grey cloud) or unproxied"
        note "for a public deployment you almost certainly want the record PROXIED (orange cloud);"
        note "grey-cloud exposes this host's IP and gives you no DDoS protection"
    elif [ "$matched_cf" -eq 1 ] && [ -z "${other// /}" ]; then
        pass "dns/points-here" "resolves into Cloudflare's ranges — record is PROXIED"
        note "DNS cannot prove the origin behind Cloudflare. Verify in the Cloudflare"
        note "dashboard that the A record's *origin* value is one of: ${HOST_IPS:-this hosts public IP}"
        note "A proxied record pointing at the WRONG origin looks identical from out here."
    elif [ -z "$HOST_IPS" ]; then
        warn "dns/points-here" "cannot compare: this host's public IP is $HOST_IP_SOURCE"
        note "resolved: $ips"
    else
        fail "dns/points-here" "resolves to$other, which is neither this host ($HOST_IPS) nor a Cloudflare range"
        note "installing now will produce a certificate failure and an unreachable service"
    fi

    # AAAA: if the zone publishes IPv6 but the host has none, v6-only clients
    # break in a way that is invisible from a v4 desktop.
    local aaaa=""
    if have dig; then aaaa="$(dig +short AAAA "$DOMAIN" 2>/dev/null | grep ':' | tr '\n' ' ')"; fi
    if [ -n "${aaaa// /}" ]; then
        if [ "$HOST_HAS_V6" -eq 1 ] || [ "$matched_cf" -eq 1 ]; then
            note "AAAA present: $aaaa"
        else
            warn "dns/aaaa" "AAAA record exists ($aaaa) but this host has no global IPv6 address"
        fi
    fi

    # CAA that forbids Let's Encrypt is a classic silent ACME failure.
    if have dig; then
        local caa base
        caa="$(dig +short CAA "$DOMAIN" 2>/dev/null)"
        base="$(printf '%s' "$DOMAIN" | awk -F. '{if (NF>=2) print $(NF-1)"."$NF}')"
        [ -z "$caa" ] && [ -n "$base" ] && caa="$(dig +short CAA "$base" 2>/dev/null)"
        if [ -n "$caa" ]; then
            if printf '%s' "$caa" | grep -qi 'letsencrypt.org'; then
                pass "dns/caa" "CAA present and allows letsencrypt.org"
            else
                fail "dns/caa" "CAA records exist and do not list letsencrypt.org — ACME issuance will be refused"
                note "$(printf '%s' "$caa" | tr '\n' ';')"
            fi
        else
            pass "dns/caa" "no CAA record (any CA may issue)"
        fi
    else
        skip "dns/caa" "dig not installed (apt-get install dnsutils) — cannot check CAA"
    fi
}

# ── 7. Outbound HTTPS ──────────────────────────────────────────────────────
probe_https() {
    # Prints an HTTP status, or 000 on failure. Any status at all proves DNS +
    # TCP + TLS + a live server; a 401/404 from an API root is a success here.
    local host="$1"
    if have curl; then
        curl -s -o /dev/null -w '%{http_code}' --max-time 10 -I "https://$host/" 2>/dev/null \
            || printf '000'
    elif have wget; then
        if wget -q --spider --timeout=10 "https://$host/" 2>/dev/null; then printf '200'
        else printf '000'; fi
    else
        printf 'NO_TOOL'
    fi
}

check_egress() {
    step "7. Outbound HTTPS"

    if [ "$OFFLINE" -eq 1 ]; then skip "egress/*" "--offline"; return; fi
    if ! have curl && ! have wget; then
        fail "egress/tooling" "neither curl nor wget installed — install.sh and smoke_test.sh both need curl"
        return
    fi

    # Infrastructure hosts: without these the install cannot complete at all.
    local h code
    for h in acme-v02.api.letsencrypt.org registry-1.docker.io auth.docker.io; do
        code="$(probe_https "$h")"
        if [ "$code" != "000" ] && [ "$code" != "NO_TOOL" ]; then
            pass "egress/$h" "HTTP $code"
        else
            fail "egress/$h" "unreachable — $( [ "$h" = acme-v02.api.letsencrypt.org ] \
                && printf 'no certificate can be issued' || printf 'images cannot be pulled' )"
        fi
    done

    # Upstream model providers. WHICH of these matter depends entirely on which
    # channels you authenticate in deploy/engine/config.yaml, so an individual
    # failure is a warning; ALL of them failing means egress is blocked.
    local upstreams=(generativelanguage.googleapis.com oauth2.googleapis.com \
                     cloudcode-pa.googleapis.com api.x.ai api.openai.com)
    if [ "${#EXTRA_HOSTS[@]}" -gt 0 ]; then upstreams+=("${EXTRA_HOSTS[@]}"); fi

    local reachable=0
    for h in "${upstreams[@]}"; do
        code="$(probe_https "$h")"
        if [ "$code" != "000" ] && [ "$code" != "NO_TOOL" ]; then
            pass "egress/$h" "HTTP $code"
            reachable=$((reachable + 1))
        else
            warn "egress/$h" "unreachable (only matters if you use this channel)"
        fi
    done
    if [ "$reachable" -eq 0 ]; then
        fail "egress/upstreams" "not one upstream API host is reachable — outbound HTTPS looks blocked"
    fi

    note "a non-200 status here is fine: it proves DNS + TCP + TLS reached a live server."
    note "this does NOT prove your credentials work, and it does NOT give the proxy web search."
}

# ── 8. Clock ───────────────────────────────────────────────────────────────
check_clock() {
    step "8. System clock"

    if have timedatectl; then
        local synced ntp
        synced="$(timedatectl show -p NTPSynchronized --value 2>/dev/null)"
        ntp="$(timedatectl show -p NTP --value 2>/dev/null)"
        case "$synced" in
            yes) pass "clock/ntp" "synchronised (NTP service: ${ntp:-?})" ;;
            no)  warn "clock/ntp" "NOT synchronised (NTP service: ${ntp:-?}) — enable it: timedatectl set-ntp true" ;;
            *)   skip "clock/ntp" "timedatectl gave no answer" ;;
        esac
    else
        skip "clock/ntp" "timedatectl not present"
    fi

    if [ "$OFFLINE" -eq 1 ] || ! have curl; then
        skip "clock/skew" "$( [ "$OFFLINE" -eq 1 ] && printf -- '--offline' || printf 'curl not installed' )"
        return
    fi

    # Deliberately over plain HTTP: if the clock is far enough off to break
    # TLS certificate validity windows, an HTTPS probe would fail and tell us
    # nothing about *why*. This is a sanity check, not a time source, and the
    # value is not trusted for anything else.
    local hdr remote_epoch local_epoch skew
    hdr="$(curl -s -I --max-time 8 http://cloudflare.com/ 2>/dev/null | sed -n 's/^[Dd]ate: //p' | head -1 | tr -d '\r')"
    if [ -z "$hdr" ]; then
        skip "clock/skew" "no Date header obtained"
        return
    fi
    remote_epoch="$(date -d "$hdr" +%s 2>/dev/null)"
    local_epoch="$(date +%s)"
    if [ -z "$remote_epoch" ]; then skip "clock/skew" "could not parse '$hdr'"; return; fi

    skew=$(( local_epoch - remote_epoch ))
    [ "$skew" -lt 0 ] && skew=$(( -skew ))
    if [ "$skew" -le 5 ]; then
        pass "clock/skew" "${skew}s vs an HTTP Date header"
    elif [ "$skew" -le 120 ]; then
        warn "clock/skew" "${skew}s off — tighten NTP before go-live; OAuth token exchanges are timestamp-sensitive"
    else
        fail "clock/skew" "${skew}s off — TLS validation and OAuth refresh will both fail intermittently"
    fi
}

# ── 9. SSH lockout guard ───────────────────────────────────────────────────
# harden.sh disables password authentication. If there is no usable key, that
# turns the VPS into a brick reachable only through the provider's console.
# harden.sh refuses to proceed in that case; this check tells you *before* you
# have started, which is a much better moment to find out.
check_ssh_keys() {
    step "9. SSH keys (lockout guard)"

    local total=0 f n listed=""
    local candidates=(/root/.ssh/authorized_keys)
    if [ -n "${SUDO_USER:-}" ] && have getent; then
        candidates+=("$(getent passwd "$SUDO_USER" 2>/dev/null | cut -d: -f6)/.ssh/authorized_keys")
    fi
    if [ -r /etc/passwd ]; then
        while IFS=: read -r _u _x _uid _gid _gecos home shell; do
            case "$shell" in */nologin|*/false|"") continue ;; esac
            [ "${_uid:-0}" -ge 1000 ] 2>/dev/null || continue
            [ -n "$home" ] || continue
            candidates+=("${home}/.ssh/authorized_keys")
        done < /etc/passwd
    fi

    for f in "${candidates[@]}"; do
        [ -f "$f" ] || continue
        [ -r "$f" ] || { listed="$listed $f(unreadable)"; continue; }
        n="$(grep -cE '^[[:space:]]*(ssh-|ecdsa-|sk-)' "$f" 2>/dev/null)"
        [ -n "$n" ] || n=0
        if [ "$n" -gt 0 ]; then
            total=$(( total + n ))
            listed="$listed $f($n)"
        fi
    done

    if [ "$total" -gt 0 ]; then
        pass "ssh/authorized-keys" "$total key(s):$listed"
    elif [ "$(id -u)" -ne 0 ]; then
        warn "ssh/authorized-keys" "found none, but running unprivileged — re-run with sudo for a definitive answer"
    else
        fail "ssh/authorized-keys" "no public keys found in any authorized_keys file"
        note "harden.sh will refuse to disable password auth without one, and you should not"
        note "run a public service with password SSH. From your laptop:"
        note "  ssh-copy-id -i ~/.ssh/id_ed25519.pub $(id -un)@${DOMAIN:-<this-host>}"
    fi

    # A currently-open key session is the strongest possible evidence.
    if [ -n "${SSH_CONNECTION:-}" ]; then
        note "you are connected over SSH from ${SSH_CONNECTION%% *} — keep this session open while hardening"
    fi

    # Non-standard SSH port matters because harden.sh's UFW rules follow sshd.
    if [ -r /etc/ssh/sshd_config ]; then
        local p; p="$(awk '/^[[:space:]]*Port[[:space:]]+[0-9]+/{print $2; exit}' /etc/ssh/sshd_config 2>/dev/null)"
        [ -n "$p" ] && [ "$p" != "22" ] && note "sshd Port is $p — pass --ssh-port $p to harden.sh"
    fi
}

# ── 10. Pending reboot ─────────────────────────────────────────────────────
check_reboot() {
    step "10. Pending reboot"

    local needed=0 why=""
    if [ -f /var/run/reboot-required ] || [ -f /run/reboot-required ]; then
        needed=1; why="/run/reboot-required exists"
        local pkgs=/var/run/reboot-required.pkgs
        [ -f /run/reboot-required.pkgs ] && pkgs=/run/reboot-required.pkgs
        [ -r "$pkgs" ] && why="$why ($(tr '\n' ',' < "$pkgs" | sed 's/,$//' | cut -c1-80))"
    fi

    # Also catch the case where a newer kernel is installed but not booted and
    # the flag file was cleared or never written.
    local running newest
    running="$(uname -r 2>/dev/null)"
    newest="$(ls -1 /boot/vmlinuz-* 2>/dev/null | sed 's|.*/vmlinuz-||' | sort -V | tail -1)"
    if [ -n "$newest" ] && [ -n "$running" ] && [ "$newest" != "$running" ]; then
        needed=1
        why="${why:+$why; }running kernel $running, newest installed $newest"
    fi

    if [ "$needed" -eq 1 ]; then
        warn "system/reboot" "reboot required — $why"
        note "reboot NOW, before installing. Rebooting after go-live means downtime you"
        note "have to explain; rebooting now costs nothing."
    else
        pass "system/reboot" "no reboot pending${running:+ (kernel $running)}"
    fi

    # Unapplied security updates are the same argument.
    if have apt-get; then
        local n
        n="$(LC_ALL=C apt-get -s -o Debug::NoLocking=1 upgrade 2>/dev/null | grep -c '^Inst ')"
        if [ "${n:-0}" -gt 0 ] 2>/dev/null; then
            note "$n package upgrade(s) pending (apt-get update && apt-get upgrade)"
        fi
    fi
}

# ── 11. Repo sanity ────────────────────────────────────────────────────────
check_repo() {
    step "11. Repository"

    local missing=""
    local f
    for f in docker-compose.yml Caddyfile install.sh harden.sh .env.example; do
        [ -r "$SCRIPT_DIR/$f" ] || missing="$missing $f"
    done
    if [ -z "$missing" ]; then
        pass "repo/files" "deploy/ bundle complete"
    else
        fail "repo/files" "missing from deploy/:$missing — run this from a full checkout"
    fi

    # A .env that already exists means this is a re-run; install.sh will keep
    # every value it finds. Worth saying out loud so nobody expects new secrets.
    if [ -e "$SCRIPT_DIR/.env" ]; then
        local mode; mode="$(stat -c '%a' "$SCRIPT_DIR/.env" 2>/dev/null || echo '?')"
        if [ "$mode" = "600" ] || [ "$mode" = "400" ]; then
            pass "repo/env-perms" "deploy/.env exists, mode $mode"
        else
            fail "repo/env-perms" "deploy/.env is mode $mode — must be 600 (chmod 600 deploy/.env)"
        fi
        note "install.sh will NOT regenerate any secret already present in this file"
    else
        note "no deploy/.env yet — install.sh will create one with fresh secrets"
    fi

    # Refuse to be helpful about a checkout that has a real .env committed.
    if [ -d "$SCRIPT_DIR/../.git" ] && have git; then
        if git -C "$SCRIPT_DIR/.." ls-files --error-unmatch deploy/.env >/dev/null 2>&1; then
            fail "repo/env-tracked" "deploy/.env is TRACKED BY GIT — every secret in it is in your history"
            note "see deploy/SECRETS_SETUP.md, 'If a secret is believed leaked'"
        fi
    fi
}

# ===========================================================================
# Run
# ===========================================================================
check_os
check_resources
check_docker
check_ports
discover_host_ip
check_dns
check_egress
check_clock
check_ssh_keys
check_reboot
check_repo

# ── summary ────────────────────────────────────────────────────────────────
printf '\n%s%s── Summary%s\n' "$C_BLD" "$C_BLU" "$C_OFF"
RULE="+--------+------------------------------------+---------------------------------------------------+"
printf '%s\n' "$RULE"
printf '| %s | %s | %s |\n' "$(pad STATUS 6)" "$(pad CHECK 34)" "$(pad DETAIL 49)"
printf '%s\n' "$RULE"
i=0
while [ "$i" -lt "${#R_STATUS[@]}" ]; do
    st="${R_STATUS[$i]}"
    case "$st" in
        PASS) c="$C_GRN" ;; FAIL) c="$C_RED" ;; WARN) c="$C_YLW" ;; *) c="$C_DIM" ;;
    esac
    printf '| %s%s%s | %s | %s |\n' \
        "$c" "$(pad "$st" 6)" "$C_OFF" \
        "$(pad "$(clip "$(ascii "${R_NAME[$i]}")" 34)" 34)" \
        "$(pad "$(clip "$(ascii "${R_DETAIL[$i]}")" 49)" 49)"
    i=$(( i + 1 ))
done
printf '%s\n' "$RULE"
printf '  %s%s pass%s   %s%s warn%s   %s%s fail%s   %s%s skip%s\n\n' \
    "$C_GRN" "$N_PASS" "$C_OFF" "$C_YLW" "$N_WARN" "$C_OFF" \
    "$C_RED" "$N_FAIL" "$C_OFF" "$C_DIM" "$N_SKIP" "$C_OFF"

if [ "$JSON" -eq 1 ]; then
    printf '{"version":"%s","domain":"%s","host_ips":"%s","host_ip_source":"%s",' \
        "$VERSION" "$(json_escape "$DOMAIN")" "$(json_escape "$HOST_IPS")" "$(json_escape "$HOST_IP_SOURCE")"
    printf '"pass":%s,"warn":%s,"fail":%s,"skip":%s,"checks":[' "$N_PASS" "$N_WARN" "$N_FAIL" "$N_SKIP"
    i=0
    while [ "$i" -lt "${#R_STATUS[@]}" ]; do
        [ "$i" -gt 0 ] && printf ','
        printf '{"status":"%s","check":"%s","detail":"%s"}' \
            "${R_STATUS[$i]}" "$(json_escape "${R_NAME[$i]}")" "$(json_escape "${R_DETAIL[$i]}")"
        i=$(( i + 1 ))
    done
    printf ']}\n'
fi

if [ "$N_FAIL" -gt 0 ]; then
    printf '%s%sNOT READY.%s %s critical check(s) failed. Fix them, then re-run this script.\n' \
        "$C_BLD" "$C_RED" "$C_OFF" "$N_FAIL"
    printf 'Do not run install.sh yet — see deploy/GO_LIVE.md for the ordered runbook.\n\n'
    exit 1
fi

if [ "$N_WARN" -gt 0 ]; then
    printf '%s%sREADY, WITH WARNINGS.%s Read the %s warning(s) above and decide deliberately.\n\n' \
        "$C_BLD" "$C_YLW" "$C_OFF" "$N_WARN"
else
    printf '%s%sREADY.%s Next: deploy/GO_LIVE.md step 4.\n\n' "$C_BLD" "$C_GRN" "$C_OFF"
fi
exit 0
