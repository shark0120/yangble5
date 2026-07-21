#!/usr/bin/env bash
#
# yangble5 — VPS hardening. Idempotent: safe to run again after every change.
#
# WHAT IT DOES
#   1. UFW           default deny incoming, allow SSH + 80 + 443, rate-limit SSH
#   2. fail2ban      jails for sshd and for the gateway's auth surface
#   3. sysctl        network + kernel hardening (without breaking Docker)
#   4. apt           unattended security upgrades
#   5. sshd          key-only, no root password, no empty passwords
#   6. optional      restrict 80/443 to Cloudflare's ranges (--cloudflare-only)
#
# WHAT IT WILL NOT DO
#   It will not disable password authentication until it has found at least one
#   usable public key in an authorized_keys file. If it finds none it aborts
#   with instructions instead of locking you out of your own machine.
#
# USAGE
#   sudo bash deploy/harden.sh                       # typical
#   sudo bash deploy/harden.sh --behind-cloudflare   # add CF ranges to ignoreip
#   sudo bash deploy/harden.sh --cloudflare-only     # + drop non-CF traffic on 80/443
#   sudo bash deploy/harden.sh --dry-run             # print, change nothing
#   sudo bash deploy/harden.sh --skip-ssh            # everything except sshd
#
# TESTED ON: Debian 12 and Ubuntu 22.04/24.04 (apt + systemd + ufw).
#
set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly STAMP="$(date +%Y%m%d-%H%M%S)"

# ── options ────────────────────────────────────────────────────────────────
PREFIX="${PREFIX:-/opt/yangble5}"
SSH_PORT=""
DRY_RUN=0
SKIP_SSH=0
FORCE_FAIL2BAN=0
BEHIND_CLOUDFLARE=0
CLOUDFLARE_ONLY=0
UNDO_CLOUDFLARE_ONLY=0
AUTO_REBOOT=""
ASSUME_YES=0

# ── output helpers ─────────────────────────────────────────────────────────
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YLW=$'\033[33m'
    C_BLU=$'\033[36m'; C_BLD=$'\033[1m'; C_OFF=$'\033[0m'
else
    C_RED=""; C_GRN=""; C_YLW=""; C_BLU=""; C_BLD=""; C_OFF=""
fi

log()   { printf '%s[ %s ]%s %s\n' "$C_BLU" "$1" "$C_OFF" "$2"; }
ok()    { printf '%s  ok%s   %s\n' "$C_GRN" "$C_OFF" "$1"; }
warn()  { printf '%s  warn%s %s\n' "$C_YLW" "$C_OFF" "$1" >&2; }
die()   { printf '\n%s%sABORT:%s %s\n\n' "$C_BLD" "$C_RED" "$C_OFF" "$1" >&2; exit 1; }
step()  { printf '\n%s%s── %s %s%s\n' "$C_BLD" "$C_BLU" "$1" "$(printf '─%.0s' $(seq 1 $((60 - ${#1}))))" "$C_OFF"; }

# Every mutation goes through run(), so --dry-run is honest rather than
# approximate: there is no second code path that could forget to check it.
run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  %sDRY%s  %s\n' "$C_YLW" "$C_OFF" "$*"
        return 0
    fi
    "$@"
}

# Write $2 to file $1, keeping a timestamped backup only when content changes.
write_file() {
    local path="$1" content="$2"
    if [ -f "$path" ] && [ "$(cat -- "$path")" = "$content" ]; then
        ok "$path already correct"
        return 0
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  %sDRY%s  write %s (%s lines)\n' "$C_YLW" "$C_OFF" "$path" \
            "$(printf '%s\n' "$content" | wc -l)"
        return 0
    fi
    if [ -f "$path" ]; then
        cp -a -- "$path" "${path}.yangble5.bak-${STAMP}"
        warn "backed up existing $path -> ${path}.yangble5.bak-${STAMP}"
    fi
    mkdir -p -- "$(dirname -- "$path")"
    printf '%s\n' "$content" > "$path"
    ok "wrote $path"
}

usage() {
    sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^#\{1,2\} \{0,1\}//'
    exit 0
}

# ── argument parsing ───────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
    case "$1" in
        --prefix)             PREFIX="${2:?--prefix needs a path}"; shift 2 ;;
        --ssh-port)           SSH_PORT="${2:?--ssh-port needs a number}"; shift 2 ;;
        --auto-reboot)        AUTO_REBOOT="${2:?--auto-reboot needs HH:MM}"; shift 2 ;;
        --dry-run)            DRY_RUN=1; shift ;;
        --skip-ssh)           SKIP_SSH=1; shift ;;
        --force-fail2ban)     FORCE_FAIL2BAN=1; shift ;;
        --behind-cloudflare)  BEHIND_CLOUDFLARE=1; shift ;;
        --cloudflare-only)    BEHIND_CLOUDFLARE=1; CLOUDFLARE_ONLY=1; shift ;;
        --no-cloudflare-only) UNDO_CLOUDFLARE_ONLY=1; shift ;;
        --yes|-y)             ASSUME_YES=1; shift ;;
        -h|--help)            usage ;;
        *)                    die "unknown option: $1 (try --help)" ;;
    esac
done

# ── preflight ──────────────────────────────────────────────────────────────
preflight() {
    step "Preflight"

    [ "$(id -u)" -eq 0 ] || die "run as root: sudo bash $0"

    [ -r /etc/os-release ] || die "cannot read /etc/os-release; this script targets Debian/Ubuntu"
    # shellcheck disable=SC1091
    . /etc/os-release
    case "${ID:-}${ID_LIKE:-}" in
        *debian*|*ubuntu*) ok "OS: ${PRETTY_NAME:-unknown}" ;;
        *) die "unsupported OS: ${PRETTY_NAME:-unknown}. This script uses apt, ufw and systemd." ;;
    esac

    command -v systemctl >/dev/null 2>&1 || die "systemd not found"

    if [ -z "$SSH_PORT" ]; then
        # sshd -T prints the EFFECTIVE config, which is the only source that
        # accounts for Include files, Match blocks and compiled-in defaults.
        SSH_PORT="$(sshd -T 2>/dev/null | awk '/^port /{print $2; exit}')" || true
        [ -n "$SSH_PORT" ] || SSH_PORT="$(awk '/^[[:space:]]*Port[[:space:]]+[0-9]+/{print $2; exit}' /etc/ssh/sshd_config 2>/dev/null)" || true
        [ -n "$SSH_PORT" ] || SSH_PORT=22
    fi
    case "$SSH_PORT" in
        ''|*[!0-9]*) die "could not determine a numeric SSH port (got '$SSH_PORT'); pass --ssh-port" ;;
    esac
    ok "SSH port: $SSH_PORT"

    if [ "$DRY_RUN" -eq 1 ]; then
        warn "DRY RUN — nothing will be changed"
    fi
}

# ── Cloudflare ranges ──────────────────────────────────────────────────────
# Fetched live so the list does not rot, with a baked-in fallback so the script
# still works on a host with no outbound access. Every fetched line is
# validated as a CIDR before use: this list feeds firewall rules, and a
# mangled response must fail closed rather than become a rule.
CF_V4=""
CF_V6=""

CF_V4_FALLBACK="173.245.48.0/20 103.21.244.0/22 103.22.200.0/22 103.31.4.0/22
141.101.64.0/18 108.162.192.0/18 190.93.240.0/20 188.114.96.0/20
197.234.240.0/22 198.41.128.0/17 162.158.0.0/15 104.16.0.0/13
104.24.0.0/14 172.64.0.0/13 131.0.72.0/22"

CF_V6_FALLBACK="2400:cb00::/32 2606:4700::/32 2803:f800::/32 2405:b500::/32
2405:8100::/32 2a06:98c0::/29 2c0f:f248::/32"

fetch_cloudflare_ranges() {
    local url="$1" fallback="$2" pattern="$3" body="" out=""
    if command -v curl >/dev/null 2>&1; then
        body="$(curl -fsS --max-time 15 "$url" 2>/dev/null)" || body=""
    fi
    if [ -n "$body" ]; then
        out="$(printf '%s\n' "$body" | grep -E "$pattern" || true)"
    fi
    # A truncated or hijacked response is worse than a slightly stale list.
    if [ "$(printf '%s\n' "$out" | grep -c . || true)" -lt 5 ]; then
        printf '%s\n' "$fallback"
        return 1
    fi
    printf '%s\n' "$out"
    return 0
}

load_cloudflare_ranges() {
    local rc=0
    CF_V4="$(fetch_cloudflare_ranges https://www.cloudflare.com/ips-v4 "$CF_V4_FALLBACK" \
        '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+$')" || rc=1
    CF_V6="$(fetch_cloudflare_ranges https://www.cloudflare.com/ips-v6 "$CF_V6_FALLBACK" \
        '^[0-9a-fA-F:]+/[0-9]+$')" || rc=1
    if [ "$rc" -ne 0 ]; then
        warn "could not fetch Cloudflare ranges; using the built-in list (may be stale)"
        warn "refresh manually from https://www.cloudflare.com/ips/"
    else
        ok "fetched Cloudflare ranges ($(printf '%s\n' "$CF_V4" | grep -c .) v4, $(printf '%s\n' "$CF_V6" | grep -c .) v6)"
    fi
}

# ── 0. packages ────────────────────────────────────────────────────────────
install_packages() {
    step "Packages"
    local want=(ufw fail2ban unattended-upgrades apt-listchanges ca-certificates curl)
    local missing=()
    local p
    for p in "${want[@]}"; do
        dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p")
    done
    if [ "${#missing[@]}" -eq 0 ]; then
        ok "all required packages present"
        return 0
    fi
    log apt "installing: ${missing[*]}"
    run env DEBIAN_FRONTEND=noninteractive apt-get update -qq
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "${missing[@]}"
    ok "installed ${missing[*]}"
}

# ── 1. UFW ─────────────────────────────────────────────────────────────────
harden_ufw() {
    step "Firewall (UFW)"

    command -v ufw >/dev/null 2>&1 || die "ufw not installed"

    # Defaults first, so that even if a later rule fails the box is not left
    # accepting everything.
    run ufw default deny incoming
    run ufw default allow outgoing
    # Docker needs to forward between its bridges and out to the internet.
    # Leaving this at deny is a classic way to break every container's
    # outbound connectivity while the firewall looks "more secure".
    run ufw default allow routed

    # `limit` adds a connection rate limit on top of allow: 6 connections per
    # 30s from one source gets dropped. Cheap insurance for SSH.
    run ufw limit "${SSH_PORT}/tcp" comment 'yangble5: ssh'
    run ufw allow 80/tcp   comment 'yangble5: http (ACME + redirect)'
    run ufw allow 443/tcp  comment 'yangble5: https'
    run ufw allow 443/udp  comment 'yangble5: http/3'

    if ufw status 2>/dev/null | head -1 | grep -qi 'inactive'; then
        # --force skips the "may disrupt existing ssh connections" prompt,
        # which would otherwise hang a non-interactive run forever.
        run ufw --force enable
    fi
    run ufw reload
    ok "UFW: deny incoming, allow ${SSH_PORT}/tcp (limited), 80/tcp, 443/tcp+udp"

    cat <<'NOTE'

  NOTE — UFW does not filter traffic to Docker-published ports.
  Docker inserts its own DNAT rules ahead of UFW's, so a published container
  port is reachable even when `ufw status` says otherwise. In this stack that
  is acceptable because the only published ports are Caddy's 80/443, which are
  meant to be public, and because gateway/engine publish nothing at all.
  It is also why --cloudflare-only writes rules into DOCKER-USER instead of
  asking UFW to do something it cannot.

NOTE
}

# ── 2. fail2ban ────────────────────────────────────────────────────────────
harden_fail2ban() {
    step "fail2ban"

    local filter_src="${SCRIPT_DIR}/fail2ban/filter.d/yangble5-auth.conf"
    local jail_tmpl="${SCRIPT_DIR}/fail2ban/jail.d/yangble5.local.tmpl"
    local jail_dst="/etc/fail2ban/jail.d/yangble5.local"
    local access_log="${PREFIX}/logs/caddy/access.log"

    [ -f "$filter_src" ] || die "missing $filter_src (run this script from a checkout of the repo)"
    [ -f "$jail_tmpl" ]  || die "missing $jail_tmpl"

    write_file /etc/fail2ban/filter.d/yangble5-auth.conf "$(cat -- "$filter_src")"

    # The gateway jail is only useful once Caddy has actually written a log.
    # Enabling a jail whose logpath does not exist makes fail2ban fail to
    # start, taking the sshd jail down with it — so decide honestly.
    local gateway_jail="true"
    if [ ! -f "$access_log" ]; then
        gateway_jail="false"
        warn "no access log at $access_log yet — the yangble5-auth jail is installed but DISABLED"
        warn "start the stack, then re-run: sudo bash $0 --force-fail2ban"
    fi

    local ignoreip=""
    if [ "$BEHIND_CLOUDFLARE" -eq 1 ]; then
        ignoreip="$(printf '%s %s' "$(printf '%s\n' "$CF_V4" | tr '\n' ' ')" \
                                    "$(printf '%s\n' "$CF_V6" | tr '\n' ' ')")"
        ignoreip="$(printf '%s' "$ignoreip" | tr -s ' ')"
    fi

    local rendered
    rendered="$(sed \
        -e "s|@@SSH_PORT@@|${SSH_PORT}|g" \
        -e "s|@@CADDY_ACCESS_LOG@@|${access_log}|g" \
        -e "s|@@GATEWAY_JAIL_ENABLED@@|${gateway_jail}|g" \
        -e "s|@@IGNOREIP@@|${ignoreip}|g" \
        -- "$jail_tmpl")"

    if [ -f "$jail_dst" ] && [ "$FORCE_FAIL2BAN" -eq 0 ]; then
        ok "$jail_dst exists — left alone (--force-fail2ban to overwrite)"
    else
        write_file "$jail_dst" "$rendered"
    fi

    run systemctl enable --now fail2ban

    if [ "$DRY_RUN" -eq 0 ]; then
        # Config errors here are silent-but-fatal: the unit stays up on the old
        # config and the new jail simply never runs.
        if fail2ban-client reload >/dev/null 2>&1; then
            ok "fail2ban reloaded"
        else
            warn "fail2ban reload failed — check: journalctl -u fail2ban -n 50"
        fi
        fail2ban-client status 2>/dev/null | sed 's/^/    /' || true
    fi

    cat <<NOTE

  VERIFY the gateway filter against your own log before trusting it:
      fail2ban-regex ${access_log} \\
                     /etc/fail2ban/filter.d/yangble5-auth.conf --print-all-matched
  "0 matched" means the filter is decorative. Field order in Caddy's JSON log
  is not a stable API.

NOTE
}

# ── 3. sysctl ──────────────────────────────────────────────────────────────
harden_sysctl() {
    step "Kernel / network (sysctl)"

    # Two things deliberately NOT set here, because both break this stack:
    #   net.ipv4.ip_forward=0        kills every container's networking
    #   net.ipv6.conf.all.accept_ra=0  kills IPv6 on VPS hosts using SLAAC
    # A hardening guide that sets them is not wrong in general; it is wrong on
    # a Docker host with a cloud-assigned IPv6 address.
    write_file /etc/sysctl.d/99-yangble5.conf "$(cat <<'SYSCTL'
# Installed by yangble5 harden.sh. Managed file — re-run the script to update.

# ── Spoofing / routing ────────────────────────────────────────────────────
# Reverse-path filtering. 1 = strict. If this host has multiple interfaces
# with asymmetric routing, change to 2 (loose) rather than 0.
net.ipv4.conf.all.rp_filter = 1
net.ipv4.conf.default.rp_filter = 1

net.ipv4.conf.all.accept_source_route = 0
net.ipv4.conf.default.accept_source_route = 0
net.ipv6.conf.all.accept_source_route = 0
net.ipv6.conf.default.accept_source_route = 0

# ICMP redirects rewrite the routing table on the word of a remote host.
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv4.conf.all.secure_redirects = 0
net.ipv6.conf.all.accept_redirects = 0
net.ipv6.conf.default.accept_redirects = 0

# This host is not a router for anyone else, even though it forwards for
# Docker, so it should never advertise better routes.
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.default.send_redirects = 0

# ── Noise / amplification ─────────────────────────────────────────────────
net.ipv4.icmp_echo_ignore_broadcasts = 1
net.ipv4.icmp_ignore_bogus_error_responses = 1
net.ipv4.conf.all.log_martians = 1
net.ipv4.conf.default.log_martians = 1

# ── SYN flood ─────────────────────────────────────────────────────────────
net.ipv4.tcp_syncookies = 1
net.ipv4.tcp_max_syn_backlog = 4096
net.ipv4.tcp_synack_retries = 3

# ── Connection capacity ───────────────────────────────────────────────────
# Long-lived streaming responses mean many concurrent sockets held open for
# minutes each, so the accept queue and the ephemeral port range matter more
# here than on a typical request/response web host.
net.core.somaxconn = 4096
net.core.netdev_max_backlog = 5000
net.ipv4.ip_local_port_range = 10240 65535
net.ipv4.tcp_fin_timeout = 20

# TCP keepalive well under the ~350s idle window most NAT devices and cloud
# load balancers enforce, so an idle-but-alive stream is not silently reaped.
net.ipv4.tcp_keepalive_time = 120
net.ipv4.tcp_keepalive_intvl = 20
net.ipv4.tcp_keepalive_probes = 5

# ── Kernel information leaks ──────────────────────────────────────────────
kernel.dmesg_restrict = 1
kernel.kptr_restrict = 2
# 1 = a process may only ptrace its own descendants. Stops one compromised
# service from reading another's memory (and its secrets).
kernel.yama.ptrace_scope = 1

# ── Filesystem ────────────────────────────────────────────────────────────
fs.protected_hardlinks = 1
fs.protected_symlinks = 1
fs.protected_fifos = 1
fs.protected_regular = 2

# ── Memory ────────────────────────────────────────────────────────────────
# An engine parsing a multi-megabyte prompt should fail its own allocation
# rather than have the OOM killer pick a victim at random.
vm.overcommit_memory = 0
vm.swappiness = 10
SYSCTL
)"

    if [ "$DRY_RUN" -eq 0 ]; then
        if sysctl --system >/dev/null 2>&1; then
            ok "sysctl applied"
        else
            warn "sysctl --system reported errors (a key may not exist on this kernel):"
            sysctl -p /etc/sysctl.d/99-yangble5.conf 2>&1 | sed 's/^/    /' || true
        fi
    fi
}

# ── 4. unattended upgrades ─────────────────────────────────────────────────
harden_unattended() {
    step "Unattended security upgrades"

    write_file /etc/apt/apt.conf.d/20auto-upgrades "$(cat <<'AUTO'
// Installed by yangble5 harden.sh.
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
AUTO
)"

    local reboot_line='Unattended-Upgrade::Automatic-Reboot "false";'
    local reboot_time_line='//Unattended-Upgrade::Automatic-Reboot-Time "03:00";'
    if [ -n "$AUTO_REBOOT" ]; then
        reboot_line='Unattended-Upgrade::Automatic-Reboot "true";'
        reboot_time_line="Unattended-Upgrade::Automatic-Reboot-Time \"${AUTO_REBOOT}\";"
    fi

    write_file /etc/apt/apt.conf.d/51yangble5-unattended-upgrades "$(cat <<AUTOUP
// Installed by yangble5 harden.sh.
//
// SECURITY ORIGINS ONLY. Auto-installing every update on a host that is
// serving live traffic trades one risk for another; security patches are the
// trade worth making.
Unattended-Upgrade::Origins-Pattern {
    "origin=Debian,codename=\${distro_codename},label=Debian-Security";
    "origin=Debian,codename=\${distro_codename}-security,label=Debian-Security";
    "origin=Ubuntu,archive=\${distro_codename}-security";
    "o=Ubuntu,a=\${distro_codename}-security";
};

// Docker's own packages are deliberately absent: an unattended dockerd
// upgrade restarts the daemon, which drops every in-flight stream. Upgrade
// Docker by hand at a time you choose (runbook.md).
Unattended-Upgrade::Package-Blacklist {
    "docker-ce";
    "docker-ce-cli";
    "containerd.io";
};

Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
Unattended-Upgrade::Remove-Unused-Dependencies "true";

// A kernel or libc update does not take effect until a reboot. Left off by
// default so a reboot never surprises you mid-stream; pass --auto-reboot HH:MM
// if you would rather be patched than present.
${reboot_line}
${reboot_time_line}
AUTOUP
)"

    run systemctl enable --now unattended-upgrades
    if [ -n "$AUTO_REBOOT" ]; then
        ok "unattended security upgrades on; automatic reboot at ${AUTO_REBOOT}"
    else
        ok "unattended security upgrades on; automatic reboot OFF"
        warn "kernel/libc patches will not take effect until you reboot: check /var/run/reboot-required"
    fi
}

# ── 5. SSH ─────────────────────────────────────────────────────────────────
# Counts real public keys (non-empty, non-comment lines) across every
# plausible authorized_keys file. This is the guard that stands between the
# operator and a locked door.
count_authorized_keys() {
    local total=0 f n
    local candidates=("/root/.ssh/authorized_keys")
    [ -n "${SUDO_USER:-}" ] && candidates+=("$(getent passwd "$SUDO_USER" | cut -d: -f6)/.ssh/authorized_keys")
    # Anything else with a real shell and a home directory.
    while IFS=: read -r _u _x _uid _gid _gecos home shell; do
        case "$shell" in
            */nologin|*/false|"") continue ;;
        esac
        [ "${_uid}" -ge 1000 ] 2>/dev/null || continue
        candidates+=("${home}/.ssh/authorized_keys")
    done < /etc/passwd

    for f in "${candidates[@]}"; do
        [ -f "$f" ] || continue
        n="$(grep -cE '^[[:space:]]*(ssh-|ecdsa-|sk-)' "$f" 2>/dev/null || true)"
        [ -n "$n" ] || n=0
        if [ "$n" -gt 0 ]; then
            printf '    %s: %s key(s)\n' "$f" "$n" >&2
            total=$((total + n))
        fi
    done
    printf '%s' "$total"
}

harden_ssh() {
    step "SSH daemon"

    if [ "$SKIP_SSH" -eq 1 ]; then
        warn "--skip-ssh: sshd left untouched"
        return 0
    fi

    local sshd_config=/etc/ssh/sshd_config
    [ -f "$sshd_config" ] || die "no $sshd_config — is OpenSSH installed?"

    log check "looking for authorized_keys entries"
    local nkeys
    nkeys="$(count_authorized_keys)"
    if [ "${nkeys:-0}" -lt 1 ]; then
        die "$(cat <<'NOKEYS'
No SSH public keys found in any authorized_keys file.

Disabling password authentication now would lock you out of this machine, so
this script is stopping instead. Fix it from your LOCAL machine:

    ssh-copy-id -i ~/.ssh/id_ed25519.pub user@this-host

or, by hand, on this host:

    mkdir -p ~/.ssh && chmod 700 ~/.ssh
    printf '%s\n' 'ssh-ed25519 AAAA... you@laptop' >> ~/.ssh/authorized_keys
    chmod 600 ~/.ssh/authorized_keys

Then OPEN A SECOND SSH SESSION and confirm the key works before re-running
this script. Keep the first session open until you have.

If you do not want SSH touched at all, re-run with --skip-ssh.
NOKEYS
)"
    fi
    ok "found ${nkeys} public key(s) — safe to disable password authentication"

    # Prefer a drop-in: it survives an OpenSSH package upgrade rewriting the
    # main file, and it is trivially reversible (delete one file).
    local dropin_dir=/etc/ssh/sshd_config.d
    local use_dropin=0
    if grep -qE '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/\*\.conf' "$sshd_config"; then
        use_dropin=1
    fi

    local hardening
    hardening="$(cat <<'SSHD'
# Installed by yangble5 harden.sh. Managed file — re-run the script to update.
#
# NOTE ON PRECEDENCE: in OpenSSH the FIRST occurrence of a keyword wins, and
# the Include of this directory sits near the top of sshd_config, so these
# values override whatever appears later in the main file.

# Keys only. Every one of these must be off: leaving KbdInteractive on is the
# usual way a "password authentication disabled" host still accepts passwords
# through PAM.
PasswordAuthentication no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
PermitEmptyPasswords no
PubkeyAuthentication yes

# Root may still log in with a key (you may need it for recovery), never with
# a password.
PermitRootLogin prohibit-password

# Slow down brute force and free up sockets held by half-open sessions.
MaxAuthTries 3
MaxSessions 10
LoginGraceTime 30

# Drop idle sessions after ~10 minutes.
ClientAliveInterval 300
ClientAliveCountMax 2

# Attack surface that a headless VPS has no use for.
X11Forwarding no
AllowAgentForwarding no
AllowTcpForwarding no
PermitUserEnvironment no
PermitTunnel no

UsePAM yes
SSHD
)"

    if [ "$use_dropin" -eq 1 ]; then
        run mkdir -p "$dropin_dir"
        write_file "${dropin_dir}/99-yangble5.conf" "$hardening"
    else
        warn "$sshd_config has no Include for sshd_config.d/*.conf"
        warn "appending to the main file instead (backed up first)"
        if [ "$DRY_RUN" -eq 0 ]; then
            if ! grep -q 'yangble5 harden.sh' "$sshd_config"; then
                cp -a -- "$sshd_config" "${sshd_config}.yangble5.bak-${STAMP}"
                printf '\n%s\n' "$hardening" >> "$sshd_config"
                ok "appended hardening block to $sshd_config"
            else
                warn "hardening block already present in $sshd_config — edit it by hand"
            fi
        fi
    fi

    # Validate BEFORE reloading. A bad sshd_config plus a reload is how a
    # remote host becomes a support ticket.
    if [ "$DRY_RUN" -eq 0 ]; then
        if ! sshd -t 2>/tmp/yb5-sshd-test.$$; then
            warn "sshd rejected the new configuration:"
            sed 's/^/    /' /tmp/yb5-sshd-test.$$ >&2 || true
            rm -f /tmp/yb5-sshd-test.$$
            die "sshd config invalid — NOT reloading. Your current session is unaffected. Restore from the .bak file above."
        fi
        rm -f /tmp/yb5-sshd-test.$$
        ok "sshd -t: configuration valid"

        # reload, not restart: existing sessions survive.
        local unit=ssh
        systemctl list-unit-files 2>/dev/null | grep -q '^sshd\.service' && unit=sshd
        run systemctl reload "$unit" || run systemctl restart "$unit"
        ok "reloaded ${unit}.service"
    fi

    printf '\n  %sBEFORE YOU CLOSE THIS SESSION:%s open a second terminal and run\n' "$C_BLD" "$C_OFF"
    printf '      ssh -p %s %s@%s\n' "$SSH_PORT" "${SUDO_USER:-root}" "$(hostname -f 2>/dev/null || hostname)"
    printf '  If it fails, you still have this session to undo it.\n\n'
}

# ── 6. optional: Cloudflare-only origin ────────────────────────────────────
# Drops traffic to 80/443 that did not come from a Cloudflare edge. This is the
# fix for the "attacker found my origin IP and skipped Cloudflare entirely"
# problem, which no amount of Cloudflare configuration can solve on its own.
#
# The rules go in DOCKER-USER, not INPUT: Docker-published ports are DNAT'd and
# traverse FORWARD, so INPUT rules never see them.
#
# They are also scoped to the PUBLIC INTERFACE (-i). Without that scoping the
# same rule would match containers' own outbound traffic to ports 80/443 --
# i.e. the engine calling the upstream provider APIs -- and drop it.
ORIGIN_LOCK_SCRIPT=/usr/local/sbin/yangble5-origin-lock
ORIGIN_LOCK_UNIT=/etc/systemd/system/yangble5-origin-lock.service

detect_public_iface() {
    ip route show default 2>/dev/null | awk '/^default/{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}'
}

install_origin_lock() {
    step "Cloudflare-only origin (80/443)"

    local iface
    iface="$(detect_public_iface)"
    [ -n "$iface" ] || die "could not detect the default-route interface; not writing firewall rules"
    ok "public interface: $iface"

    if [ "$ASSUME_YES" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
        printf '\n  %sThis drops all non-Cloudflare traffic to 80/443 on %s.%s\n' "$C_YLW" "$iface" "$C_OFF"
        printf '  If you ever turn the orange cloud OFF, your site goes dark until you run\n'
        printf '      sudo bash %s --no-cloudflare-only\n' "$0"
        printf '  SSH on port %s is NOT affected.\n\n' "$SSH_PORT"
        printf '  Continue? [y/N] '
        local reply=""
        read -r reply || true
        case "$reply" in
            y|Y|yes|YES) ;;
            *) warn "skipped Cloudflare-only origin lock"; return 0 ;;
        esac
    fi

    local body
    body="$(cat <<LOCK
#!/usr/bin/env bash
# Installed by yangble5 harden.sh. Re-applied at boot by
# yangble5-origin-lock.service, because iptables rules do not survive a reboot.
set -euo pipefail

IFACE="${iface}"
V4="$(printf '%s' "$CF_V4" | tr '\n' ' ')"
V6="$(printf '%s' "$CF_V6" | tr '\n' ' ')"

apply() {
    local ipt="\$1" chain="YB5-CF-ONLY" ranges="\$2"

    # Docker creates DOCKER-USER itself; if the daemon has not started yet
    # there is nothing to hook into and we should not invent it.
    "\$ipt" -n -L DOCKER-USER >/dev/null 2>&1 || return 0

    "\$ipt" -N "\$chain" 2>/dev/null || "\$ipt" -F "\$chain"
    local cidr
    for cidr in \$ranges; do
        "\$ipt" -A "\$chain" -s "\$cidr" -j RETURN
    done
    "\$ipt" -A "\$chain" -j DROP

    # -C tests for the rule; only insert when it is absent, so this is safe to
    # run repeatedly.
    "\$ipt" -C DOCKER-USER -i "\$IFACE" -p tcp -m multiport --dports 80,443 -j "\$chain" 2>/dev/null \\
        || "\$ipt" -I DOCKER-USER 1 -i "\$IFACE" -p tcp -m multiport --dports 80,443 -j "\$chain"
    "\$ipt" -C DOCKER-USER -i "\$IFACE" -p udp --dport 443 -j "\$chain" 2>/dev/null \\
        || "\$ipt" -I DOCKER-USER 1 -i "\$IFACE" -p udp --dport 443 -j "\$chain"
}

remove() {
    local ipt="\$1" chain="YB5-CF-ONLY"
    "\$ipt" -n -L DOCKER-USER >/dev/null 2>&1 || return 0
    while "\$ipt" -C DOCKER-USER -i "\$IFACE" -p tcp -m multiport --dports 80,443 -j "\$chain" 2>/dev/null; do
        "\$ipt" -D DOCKER-USER -i "\$IFACE" -p tcp -m multiport --dports 80,443 -j "\$chain"
    done
    while "\$ipt" -C DOCKER-USER -i "\$IFACE" -p udp --dport 443 -j "\$chain" 2>/dev/null; do
        "\$ipt" -D DOCKER-USER -i "\$IFACE" -p udp --dport 443 -j "\$chain"
    done
    "\$ipt" -F "\$chain" 2>/dev/null || true
    "\$ipt" -X "\$chain" 2>/dev/null || true
}

# The trailing '|| true' matters: on a host with no ip6tables the
# short-circuit would otherwise make this script exit non-zero, and systemd
# would report the unit as failed even though the v4 rules applied fine.
case "\${1:-apply}" in
    apply)
        apply iptables "\$V4"
        if command -v ip6tables >/dev/null 2>&1; then
            apply ip6tables "\$V6" || true
        fi
        ;;
    remove)
        remove iptables
        if command -v ip6tables >/dev/null 2>&1; then
            remove ip6tables || true
        fi
        ;;
    *) echo "usage: \$0 {apply|remove}" >&2; exit 2 ;;
esac
exit 0
LOCK
)"
    write_file "$ORIGIN_LOCK_SCRIPT" "$body"
    run chmod 0755 "$ORIGIN_LOCK_SCRIPT"

    write_file "$ORIGIN_LOCK_UNIT" "$(cat <<'UNIT'
[Unit]
Description=yangble5: restrict 80/443 to Cloudflare ranges (DOCKER-USER)
# After docker: the DOCKER-USER chain does not exist until the daemon has
# started, and re-applying afterwards is what makes this survive a reboot.
After=docker.service network-online.target
Wants=network-online.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/yangble5-origin-lock apply
ExecStop=/usr/local/sbin/yangble5-origin-lock remove

[Install]
WantedBy=multi-user.target
UNIT
)"

    run systemctl daemon-reload
    run systemctl enable --now yangble5-origin-lock.service
    ok "origin lock active and enabled at boot"
    warn "undo with: sudo bash $0 --no-cloudflare-only"
}

remove_origin_lock() {
    step "Removing Cloudflare-only origin lock"
    if [ -f "$ORIGIN_LOCK_UNIT" ]; then
        run systemctl disable --now yangble5-origin-lock.service || true
        run rm -f "$ORIGIN_LOCK_UNIT"
        run systemctl daemon-reload
    fi
    if [ -x "$ORIGIN_LOCK_SCRIPT" ]; then
        run "$ORIGIN_LOCK_SCRIPT" remove || true
        run rm -f "$ORIGIN_LOCK_SCRIPT"
    fi
    ok "origin lock removed — 80/443 accept traffic from anywhere again"
}

# ── summary ────────────────────────────────────────────────────────────────
summary() {
    step "Summary"
    cat <<SUM
  UFW              deny incoming; ${SSH_PORT}/tcp (rate-limited), 80/tcp, 443/tcp+udp
  fail2ban         sshd jail + yangble5-auth jail (verify with fail2ban-regex)
  sysctl           /etc/sysctl.d/99-yangble5.conf
  unattended       security origins only; reboot: ${AUTO_REBOOT:-off}
  sshd             $( [ "$SKIP_SSH" -eq 1 ] && echo "SKIPPED (--skip-ssh)" || echo "key-only, no root password, validated with sshd -t" )
  cloudflare-only  $( [ "$CLOUDFLARE_ONLY" -eq 1 ] && echo "ACTIVE on $(detect_public_iface)" || echo "not enabled" )

  What this does NOT do:
    * it does not protect you from a leaked upstream credential — see runbook.md
    * it does not rate-limit traffic that arrives through Cloudflare; only
      Cloudflare can do that (cloudflare.md)
    * it does not stop a runaway agent from spending your money; that is
      YANGBLE5_GLOBAL_MONTHLY_USD_BUDGET in .env

  Next:  deploy/cloudflare.md   then   deploy/runbook.md
SUM
}

# ── main ───────────────────────────────────────────────────────────────────
main() {
    preflight

    if [ "$UNDO_CLOUDFLARE_ONLY" -eq 1 ]; then
        remove_origin_lock
        exit 0
    fi

    install_packages
    [ "$BEHIND_CLOUDFLARE" -eq 1 ] && load_cloudflare_ranges
    harden_ufw
    harden_fail2ban
    harden_sysctl
    harden_unattended
    harden_ssh
    [ "$CLOUDFLARE_ONLY" -eq 1 ] && install_origin_lock
    summary
}

main "$@"
