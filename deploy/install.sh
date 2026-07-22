#!/usr/bin/env bash
#
# yangble5 — one-command installer for a public VPS deployment.
#
#   sudo bash deploy/install.sh --domain api.example.com --email you@example.com
#
# SAFE TO RE-RUN. Every step checks the current state first:
#   * an existing .env is never regenerated, only completed with missing keys
#   * existing secrets are never rewritten
#   * an existing engine config.yaml is never overwritten
#   * the service user, directories and volumes are created only if absent
#
# WHAT IT DOES NOT DO
#   * it does not harden the host — run deploy/harden.sh afterwards
#   * it does not configure Cloudflare — see deploy/cloudflare.md
#   * it does not supply CLIProxyAPI; you provide the binary or an image
#     (deploy/engine-bin/README.md)
#
# SECRETS: the ONLY secret this script ever prints is the one-time bootstrap
# invite code at the very end. Everything else is written to a 0600 .env owned
# by root and never echoed.
#
set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

# ── options ────────────────────────────────────────────────────────────────
PREFIX="${PREFIX:-/opt/yangble5}"
SERVICE_USER="${SERVICE_USER:-yangble5}"
DOMAIN=""
ACME_EMAIL=""
REGISTRATION_MODE="invite"
NO_START=0
ASSUME_YES=0

# ── output helpers ─────────────────────────────────────────────────────────
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YLW=$'\033[33m'
    C_BLU=$'\033[36m'; C_BLD=$'\033[1m'; C_OFF=$'\033[0m'
else
    C_RED=""; C_GRN=""; C_YLW=""; C_BLU=""; C_BLD=""; C_OFF=""
fi
ok()   { printf '%s  ok%s   %s\n' "$C_GRN" "$C_OFF" "$1"; }
info() { printf '       %s\n' "$1"; }
warn() { printf '%s  warn%s %s\n' "$C_YLW" "$C_OFF" "$1" >&2; }
die()  { printf '\n%s%sABORT:%s %s\n\n' "$C_BLD" "$C_RED" "$C_OFF" "$1" >&2; exit 1; }
step() { printf '\n%s%s── %s%s\n' "$C_BLD" "$C_BLU" "$1" "$C_OFF"; }

usage() {
    cat <<'USAGE'
usage: sudo bash deploy/install.sh [options]

  --domain HOST          public hostname (required)
  --email ADDR           Let's Encrypt account e-mail (required)
  --prefix PATH          install prefix (default /opt/yangble5)
  --user NAME            service user to create (default yangble5)
  --registration MODE    invite | open | closed (default invite)
  --no-start             set everything up but do not start containers
  --yes, -y              do not prompt
  -h, --help             this text
USAGE
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --domain)       DOMAIN="${2:?--domain needs a hostname}"; shift 2 ;;
        --email)        ACME_EMAIL="${2:?--email needs an address}"; shift 2 ;;
        --prefix)       PREFIX="${2:?--prefix needs a path}"; shift 2 ;;
        --user)         SERVICE_USER="${2:?--user needs a name}"; shift 2 ;;
        --registration) REGISTRATION_MODE="${2:?--registration needs a mode}"; shift 2 ;;
        --no-start)     NO_START=1; shift ;;
        --yes|-y)       ASSUME_YES=1; shift ;;
        -h|--help)      usage ;;
        *)              die "unknown option: $1 (try --help)" ;;
    esac
done

# ── secret generation ──────────────────────────────────────────────────────
# 32 bytes from the kernel CSPRNG, hex-encoded. openssl is preferred only
# because it is one call; /dev/urandom is the same entropy source.
gen_secret() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32
    else
        od -An -tx1 -N32 /dev/urandom | tr -d ' \n'
    fi
}

# A human-transcribable invite code, e.g. yb5-7mk9g59h-h86mmmg6.
#
# The hex digits a-f are remapped to g,h,j,k,m,n — an alphabet with no 0/O and
# no 1/l/I to mistype. The mapping is 1:1 over a uniform hex string, so the
# result is still uniform: 16 symbols x 16 characters = 64 bits of entropy,
# which is far beyond guessable for a single-use code that is also behind the
# edge's 10-requests-per-minute auth rate limit.
gen_invite() {
    local raw
    raw="$(gen_secret | tr -d '\n' | tr 'abcdef' 'ghjkmn' | head -c 16)"
    printf 'yb5-%s-%s' "${raw:0:8}" "${raw:8:8}"
}

# ── 1. preflight ───────────────────────────────────────────────────────────
preflight() {
    step "Preflight"

    [ "$(id -u)" -eq 0 ] || die "run as root: sudo bash $0 ..."

    [ -r /etc/os-release ] || die "cannot read /etc/os-release"
    # shellcheck disable=SC1091
    . /etc/os-release
    case "${ID:-}${ID_LIKE:-}" in
        *debian*|*ubuntu*) ok "OS: ${PRETTY_NAME:-unknown}" ;;
        *) warn "untested OS: ${PRETTY_NAME:-unknown} (this script assumes apt + systemd)" ;;
    esac

    command -v docker >/dev/null 2>&1 \
        || die "docker not found. Install it first: https://docs.docker.com/engine/install/"
    docker compose version >/dev/null 2>&1 \
        || die "'docker compose' (v2) not found. The legacy 'docker-compose' v1 binary is NOT supported: it ignores deploy.resources.limits, so every resource ceiling in docker-compose.yml would silently do nothing."
    ok "docker: $(docker --version | head -1)"
    ok "compose: $(docker compose version --short 2>/dev/null || echo v2)"

    docker info >/dev/null 2>&1 || die "the docker daemon is not running (systemctl start docker)"

    # Interactive prompts only when we have a terminal to prompt on.
    if [ -z "$DOMAIN" ]; then
        if [ -t 0 ]; then
            printf '  Public hostname (e.g. api.example.com): '
            read -r DOMAIN
        fi
    fi
    [ -n "$DOMAIN" ] || die "--domain is required"
    case "$DOMAIN" in
        *.*) : ;;
        *) die "'$DOMAIN' does not look like a fully-qualified hostname" ;;
    esac

    if [ -z "$ACME_EMAIL" ]; then
        if [ -t 0 ]; then
            printf "  Let's Encrypt account e-mail: "
            read -r ACME_EMAIL
        fi
    fi
    [ -n "$ACME_EMAIL" ] || die "--email is required (Let's Encrypt needs an account address)"

    case "$REGISTRATION_MODE" in
        invite|open|closed) ok "registration mode: $REGISTRATION_MODE" ;;
        *) die "--registration must be invite, open or closed" ;;
    esac

    # A DNS record that does not point here means ACME will fail. Warn rather
    # than abort: the operator may be about to create the record, or may be
    # using the DNS-01 challenge where this does not matter.
    local resolved
    resolved="$(getent hosts "$DOMAIN" 2>/dev/null | awk '{print $1}' | head -1 || true)"
    if [ -z "$resolved" ]; then
        warn "$DOMAIN does not resolve yet — create the DNS record before the stack starts, or certificate issuance will fail"
    else
        ok "$DOMAIN resolves to $resolved"
        info "(behind Cloudflare this will be a Cloudflare address — that is expected)"
    fi

    local free_mb
    free_mb="$(df -Pm "$(dirname -- "$PREFIX")" 2>/dev/null | awk 'NR==2{print $4}')" || free_mb=0
    if [ "${free_mb:-0}" -lt 4096 ]; then
        warn "only ${free_mb}MB free on $(dirname -- "$PREFIX") — building the Caddy image from source needs a few GB"
    fi
}

# ── 2. service user ────────────────────────────────────────────────────────
create_service_user() {
    step "Service user"

    if id -u "$SERVICE_USER" >/dev/null 2>&1; then
        ok "user '$SERVICE_USER' already exists"
    else
        # --system: no password ageing, low UID, and no home clutter.
        # nologin shell: this identity exists to own files and to be the UID
        # inside the containers, never to log in.
        useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
        ok "created system user '$SERVICE_USER'"
    fi

    SERVICE_UID="$(id -u "$SERVICE_USER")"
    SERVICE_GID="$(id -g "$SERVICE_USER")"
    ok "uid:gid = ${SERVICE_UID}:${SERVICE_GID}"

    cat <<'NOTE'

  HONEST NOTE ON WHAT THIS BUYS YOU
  The service user is the identity the gateway and engine PROCESSES run as
  inside their containers, and it owns the data on disk. It is not the
  identity that runs `docker compose` — driving the Docker daemon requires
  root (membership of the `docker` group is equivalent to root, which is why
  this script does not create one). So: a compromise of the gateway lands on
  an unprivileged uid with a read-only root filesystem and no capabilities;
  a compromise of whatever can run `docker` is a full host compromise.

NOTE
}

# ── 3. layout ──────────────────────────────────────────────────────────────
create_layout() {
    step "Directory layout under $PREFIX"

    local d
    for d in "$PREFIX" "$PREFIX/app" "$PREFIX/logs" "$PREFIX/logs/caddy" "$PREFIX/backups"; do
        if [ -d "$d" ]; then
            ok "$d exists"
        else
            mkdir -p "$d"
            ok "created $d"
        fi
    done

    # Caddy runs as root inside its container (it binds 80/443), so it writes
    # the access log as root. fail2ban reads it as root too. The directory is
    # therefore root-owned on purpose.
    chmod 0755 "$PREFIX" "$PREFIX/logs" "$PREFIX/logs/caddy"
    # Backups contain the user database. Nobody but root needs to read them.
    chmod 0700 "$PREFIX/backups"

    # Copy the repo in, so that later `git pull`s in the operator's checkout
    # cannot silently change what a running stack is built from.
    if [ "$(cd -- "$REPO_DIR" && pwd)" = "$(cd -- "$PREFIX/app" 2>/dev/null && pwd || echo _none_)" ]; then
        ok "already running from $PREFIX/app"
    else
        # -a preserves modes; --delete is deliberately NOT used, so operator
        # files under $PREFIX/app (engine-bin, engine/config.yaml, .env) survive.
        if command -v rsync >/dev/null 2>&1; then
            rsync -a --exclude '.git' "$REPO_DIR"/ "$PREFIX/app"/
        else
            cp -a "$REPO_DIR"/. "$PREFIX/app"/
            rm -rf "$PREFIX/app/.git"
        fi
        ok "copied repository to $PREFIX/app"
    fi

    DEPLOY_DIR="$PREFIX/app/deploy"
    [ -f "$DEPLOY_DIR/docker-compose.yml" ] || die "expected $DEPLOY_DIR/docker-compose.yml after copy"
}

# ── 4. .env ────────────────────────────────────────────────────────────────
# Idempotency rule: an existing key in .env is NEVER touched. Missing keys are
# appended from the template, with __GENERATE__ replaced by a fresh secret.
# That way re-running after an upgrade picks up new settings without rotating
# secrets (which would invalidate every issued API key) behind your back.
env_get() {
    local key="$1" file="$2"
    [ -f "$file" ] || return 1
    sed -n "s/^${key}=//p" "$file" | head -1
}

env_set() {
    local key="$1" value="$2" file="$3"
    if grep -qE "^${key}=" "$file" 2>/dev/null; then
        # `|` as the sed delimiter: values contain / (paths, URLs) but not |.
        sed -i "s|^${key}=.*|${key}=${value}|" "$file"
    else
        printf '%s=%s\n' "$key" "$value" >> "$file"
    fi
}

create_env() {
    step "Environment file"

    local example="$DEPLOY_DIR/.env.example"
    local envfile="$DEPLOY_DIR/.env"
    [ -f "$example" ] || die "missing $example"

    local fresh=0
    if [ ! -f "$envfile" ]; then
        # Create empty with tight permissions BEFORE writing anything into it,
        # so the secrets are never briefly world-readable.
        (umask 077; : > "$envfile")
        fresh=1
        ok "created $envfile (0600)"
    else
        ok "$envfile exists — keeping existing values"
    fi
    chmod 0600 "$envfile"
    chown root:root "$envfile"

    local generated=0 added=0 line key value
    while IFS= read -r line; do
        case "$line" in
            ''|'#'*) continue ;;
            *=*) ;;
            *) continue ;;
        esac
        key="${line%%=*}"
        value="${line#*=}"
        if grep -qE "^${key}=" "$envfile" 2>/dev/null; then
            continue
        fi
        if [ "$value" = "__GENERATE__" ]; then
            value="$(gen_secret)"
            generated=$((generated + 1))
            # Remember the admin key ONLY if we minted it in this run, so a
            # re-run never re-prints a secret the operator already has.
            [ "$key" = "YANGBLE5_ADMIN_API_KEY" ] && NEW_ADMIN_KEY="$value"
        fi
        printf '%s=%s\n' "$key" "$value" >> "$envfile"
        added=$((added + 1))
    done < "$example"

    [ "$added" -gt 0 ] && ok "added $added setting(s) from the template"
    [ "$generated" -gt 0 ] && ok "generated $generated random secret(s) — only the admin key is ever printed"

    # Values this script owns rather than the template.
    env_set YANGBLE5_DOMAIN "$DOMAIN" "$envfile"
    env_set ACME_EMAIL "$ACME_EMAIL" "$envfile"
    # THE ONE IDENTITY. docker-compose.yml reads YANGBLE5_UID/YANGBLE5_GID twice:
    # once as build args (so the image creates that account and chowns /data and
    # /auth to it) and once as the runtime `user:`. Writing them here used to be
    # only half of that — the images hard-coded 10001 and nothing passed a build
    # arg — so the container ran as an id that owned nothing, SQLite could not
    # create the -wal file, and the stack never came up. sync_container_identity()
    # below is what keeps already-built images and already-created volumes in
    # step when this value changes.
    env_set YANGBLE5_UID "$SERVICE_UID" "$envfile"
    env_set YANGBLE5_GID "$SERVICE_GID" "$envfile"
    # Absolute, and matching the path harden.sh points the fail2ban jail at.
    env_set YANGBLE5_LOG_DIR "$PREFIX/logs" "$envfile"
    if [ "$fresh" -eq 1 ]; then
        env_set YANGBLE5_REGISTRATION_MODE "$REGISTRATION_MODE" "$envfile"
    fi
    ok "domain, e-mail, uid/gid and log dir set"

    # Two independent gates on `open`. Both are here rather than in the
    # gateway because here the message can name the file and the fix.
    local mode budget licensed
    mode="$(env_get YANGBLE5_REGISTRATION_MODE "$envfile" || echo invite)"
    budget="$(env_get YANGBLE5_GLOBAL_MONTHLY_USD_BUDGET "$envfile" || echo 0)"
    licensed="$(env_get YANGBLE5_POOL_LICENSED_FOR_THIRD_PARTIES "$envfile" || echo no)"
    if [ "$mode" = "open" ]; then
        # GATE 1 — money. A stranger minting keys against an uncapped balance.
        case "$budget" in
            ''|0|0.0|0.00) die "REGISTRATION_MODE=open needs YANGBLE5_GLOBAL_MONTHLY_USD_BUDGET > 0 in $envfile. Open registration against an uncapped balance means a stranger can spend your money without limit." ;;
            *) warn "open registration with a \$${budget}/month cap — that cap is the most you can lose to abuse before signups stop" ;;
        esac

        # GATE 2 — the accounts. This one is newer and it is the one that was
        # missing, because the gate above only ever asked about money.
        #
        # docs/OPERATING_A_PUBLIC_SERVICE.md §1 is unambiguous: pooled PERSONAL
        # OAuth credentials must never back a public endpoint. The engine's
        # multi-credential failover exists so ONE person can spread THEIR OWN
        # traffic across THEIR OWN accounts; it is not a multi-tenant licence.
        # Put strangers behind it and one origin IP starts producing the
        # request-shape diversity of dozens of unrelated humans, which is
        # exactly what provider abuse detection is built to find. The
        # escalation is rate-limit, then suspension, and the suspension lands
        # on the Google/xAI/OpenAI ACCOUNT — taking out everything else that
        # account is used for, not just yangble5. Where a tier is served by a
        # single credential, that is a total outage for the tier with no
        # failover.
        #
        # This script cannot inspect the engine's credential store, so it
        # cannot decide for you. What it CAN do is refuse to be the reason
        # nobody thought about it: `open` now requires a deliberate, recorded,
        # greppable statement in .env, not the absence of an objection.
        case "$(printf '%s' "$licensed" | tr 'A-Z' 'a-z' | tr -d '"'"'"' ')" in
            yes|true|1) ok "operator asserts the upstream pool is licensed for third-party serving" ;;
            *) die "REGISTRATION_MODE=open is refused while YANGBLE5_POOL_LICENSED_FOR_THIRD_PARTIES is '$licensed'.

  Open registration points every stranger on the internet at whatever
  credentials the engine holds. If any of them is a personal OAuth account
  (Google/antigravity, xAI, Codex, a friend's account), this is the exact
  configuration docs/OPERATING_A_PUBLIC_SERVICE.md §1 says must never exist,
  and the ban lands on the ACCOUNT, not on this service.

  Pick one, deliberately:

    a) Keep personal credentials  -> use invite or closed:
         --registration invite        (mint codes: deploy/runbook.md §3)
    b) BYOK-first                 -> stay on invite, and point users at
         /byok so they bring their own key; the shared pool is the fallback.
    c) You have keys that are actually licensed for serving third parties
       (a paid plan whose terms permit proxying/multi-user access, in
       writing) -> set YANGBLE5_POOL_LICENSED_FOR_THIRD_PARTIES=yes in
         $envfile and re-run.

  If you choose (c) and the pool is still best-effort or single-credential,
  say so publicly on the landing page and in the installer output: users
  planning around a tier deserve to know it can vanish without notice." ;;
        esac
    fi
}

# ── 5. engine config ───────────────────────────────────────────────────────
create_engine_config() {
    step "Engine configuration"

    local example="$DEPLOY_DIR/engine/config.example.yaml"
    local target="$DEPLOY_DIR/engine/config.yaml"
    local envfile="$DEPLOY_DIR/.env"

    [ -f "$example" ] || die "missing $example"

    if [ -f "$target" ]; then
        ok "$target exists — left untouched"
    else
        local api_key mgmt_key
        api_key="$(env_get YANGBLE5_ENGINE_API_KEY "$envfile")"
        mgmt_key="$(env_get YANGBLE5_ENGINE_MANAGEMENT_KEY "$envfile")"
        [ -n "$api_key" ] || die "YANGBLE5_ENGINE_API_KEY missing from $envfile"

        # Substituting both secrets here is the single highest-value thing this
        # script does: a mismatch between .env and the engine's api-keys list
        # presents as every request returning 401, with nothing in the logs
        # that points at the cause.
        (umask 077
         sed -e "s|__ENGINE_API_KEY__|${api_key}|" \
             -e "s|__ENGINE_MANAGEMENT_KEY__|${mgmt_key}|" \
             -- "$example" > "$target")
        chmod 0640 "$target"
        chown "root:${SERVICE_GID}" "$target"
        ok "wrote $target with matching secrets (0640)"
    fi

    if [ -d "$DEPLOY_DIR/engine/auth" ]; then
        warn "$DEPLOY_DIR/engine/auth exists but is NOT used: OAuth tokens live in the engine_auth Docker volume"
    fi
}

# ── 6. engine binary ───────────────────────────────────────────────────────
check_engine_binary() {
    step "Engine binary"

    local bin="$DEPLOY_DIR/engine-bin/cli-proxy-api"
    local envfile="$DEPLOY_DIR/.env"
    local image
    image="$(env_get ENGINE_IMAGE "$envfile" || echo '')"

    if [ -f "$bin" ]; then
        chmod 0755 "$bin"
        ok "found $bin ($(du -h "$bin" | awk '{print $1}'))"
        ENGINE_READY=1
        return 0
    fi

    if [ -n "$image" ] && [ "$image" != "yangble5/engine:local" ] \
       && docker image inspect "$image" >/dev/null 2>&1; then
        ok "using pre-existing image $image"
        ENGINE_READY=1
        return 0
    fi

    ENGINE_READY=0
    warn "no engine binary and no pre-built engine image"
}

# ── 6b. container identity ─────────────────────────────────────────────────
# Everything runs as SERVICE_UID:SERVICE_GID inside the containers. Two things
# can drift out of step with that, and both present identically — "unable to
# open database file" or a silent failure to refresh an OAuth token — with
# nothing naming the cause:
#
#   1. IMAGES built for a different uid. Harmless to fix: the uid is a build arg
#      now, and changing a build arg invalidates the cache by itself, so the
#      rebuild in start_stack picks it up. We only need to SAY so.
#   2. VOLUMES that already exist. Docker seeds a fresh named volume from the
#      image's directory ownership, so the Dockerfile's chown covers the first
#      boot and nothing else. An existing volume keeps the ownership it had, and
#      no rebuild will ever touch it.
#
# The stamp file records what the last successful run built for, so the common
# case (nothing changed) costs one `cat`.
IDENTITY_STAMP=""
IDENTITY_CHANGED=0

sync_container_identity() {
    step "Container identity"

    IDENTITY_STAMP="$PREFIX/.image-uid"
    local want="${SERVICE_UID}:${SERVICE_GID}"
    local had=""
    [ -f "$IDENTITY_STAMP" ] && had="$(cat -- "$IDENTITY_STAMP" 2>/dev/null || true)"

    if [ "$had" = "$want" ]; then
        ok "images and volumes already built for uid:gid ${want}"
        return 0
    fi

    IDENTITY_CHANGED=1
    if [ -n "$had" ]; then
        warn "container uid:gid changed: ${had} -> ${want}"
        warn "images will rebuild and any existing data volume will be re-owned"
    else
        ok "container uid:gid = ${want}"
    fi
}

# Re-own the named volumes. Called AFTER the build, because it borrows the
# gateway image we just built rather than pulling a utility image the operator
# did not ask for.
#
# Plain `docker run`, not `compose run`: every service in docker-compose.yml
# drops ALL capabilities, and chown needs CAP_CHOWN. That restriction is
# deliberate and this one-shot deliberately steps outside it.
repair_volume_ownership() {
    [ "$IDENTITY_CHANGED" -eq 1 ] || return 0

    local envfile="$DEPLOY_DIR/.env"
    local image vol repaired=0
    image="$(env_get GATEWAY_IMAGE "$envfile" || echo '')"
    [ -n "$image" ] || image="yangble5/gateway:local"

    if ! docker image inspect "$image" >/dev/null 2>&1; then
        warn "cannot re-own volumes: image $image not found (build must have failed)"
        return 0
    fi

    # Volume names are stable because docker-compose.yml sets `name: yangble5`.
    for vol in yangble5_gateway_data yangble5_engine_auth; do
        docker volume inspect "$vol" >/dev/null 2>&1 || continue
        if docker run --rm --user 0:0 -v "${vol}:/mnt" "$image" \
             chown -R "${SERVICE_UID}:${SERVICE_GID}" /mnt >/dev/null 2>&1; then
            ok "re-owned volume $vol to ${SERVICE_UID}:${SERVICE_GID}"
            repaired=$((repaired + 1))
        else
            warn "could not re-own volume $vol — the stack may fail to write to it"
            warn "  docker run --rm -u 0:0 -v ${vol}:/mnt ${image} chown -R ${SERVICE_UID}:${SERVICE_GID} /mnt"
        fi
    done
    [ "$repaired" -gt 0 ] || ok "no pre-existing volumes needed re-owning"
}

# ── 7. bring the stack up ──────────────────────────────────────────────────
# No -p flag: docker-compose.yml declares `name: yangble5`, so the project (and
# therefore every volume name above) is the same however this is invoked.
compose() { docker compose --project-directory "$DEPLOY_DIR" -f "$DEPLOY_DIR/docker-compose.yml" "$@"; }

start_stack() {
    step "Building and starting"

    if [ "$NO_START" -eq 1 ]; then
        warn "--no-start: skipping build and up"
        warn "volume ownership is NOT repaired until you run without --no-start"
        return 0
    fi
    if [ "${ENGINE_READY:-0}" -eq 0 ]; then
        warn "skipping start: the engine is not available yet"
        return 0
    fi

    info "building images (the Caddy build compiles from source — first run takes a few minutes)"
    compose build
    ok "images built"

    repair_volume_ownership

    compose up -d
    ok "stack started"

    # Recorded only once the stack is actually up with this identity, so an
    # aborted run does not leave a stamp claiming work that did not happen.
    if [ -n "$IDENTITY_STAMP" ]; then
        printf '%s:%s\n' "$SERVICE_UID" "$SERVICE_GID" > "$IDENTITY_STAMP"
        chmod 0600 "$IDENTITY_STAMP"
    fi

    # Certificate issuance and the engine's first credential load both take a
    # moment; report state rather than pretending it is instant.
    info "waiting for containers to report healthy (up to 120s)"
    local waited=0 unhealthy
    while [ "$waited" -lt 120 ]; do
        unhealthy="$(compose ps --format '{{.Service}} {{.Health}}' 2>/dev/null \
                     | awk '$2 != "healthy" && $2 != "" {print $1}' | tr '\n' ' ')"
        [ -z "$unhealthy" ] && break
        sleep 5
        waited=$((waited + 5))
    done
    if [ -n "${unhealthy:-}" ]; then
        warn "still not healthy after ${waited}s: ${unhealthy}"
        warn "check: docker compose -f $DEPLOY_DIR/docker-compose.yml logs --tail=50"
    else
        ok "all containers healthy"
    fi
}

# ── 8. one-time bootstrap invite ───────────────────────────────────────────
# THE ONLY SECRET THIS SCRIPT PRINTS. It is generated here, inserted into the
# database, shown once, and never written to disk by us.
bootstrap_invite() {
    local envfile="$DEPLOY_DIR/.env"
    local marker="$PREFIX/.bootstrap-done"

    if [ -f "$marker" ]; then
        ok "bootstrap invite already issued (remove $marker to issue another)"
        return 0
    fi
    if [ "$NO_START" -eq 1 ] || [ "${ENGINE_READY:-0}" -eq 0 ]; then
        return 0
    fi

    step "Bootstrap invite code"

    local code db_path
    code="$(gen_invite)"
    db_path="$(env_get YANGBLE5_DB_PATH "$envfile" || echo /data/yangble5.db)"

    if compose exec -T -e YB5_CODE="$code" -e YB5_DB="$db_path" gateway python - <<'PY' >/dev/null 2>&1
import os
from gateway.storage import Storage

# max_uses=1: a bootstrap code that can be redeemed twice is a bootstrap code
# that leaked. Mint more from the runbook once you are set up.
store = Storage(os.environ["YB5_DB"])
store.create_invite(os.environ["YB5_CODE"], label="bootstrap", max_uses=1)
store.close()
PY
    then
        touch "$marker"
        chmod 0600 "$marker"
        printf '\n  %s%sONE-TIME BOOTSTRAP INVITE CODE%s\n' "$C_BLD" "$C_GRN" "$C_OFF"
        printf '  %s%s%s\n' "$C_BLD" "$code" "$C_OFF"
        printf '  Single use. It is not stored anywhere you can read it back —\n'
        printf '  the database holds only a hash. Copy it now.\n\n'
    else
        warn "could not create the bootstrap invite automatically"
        warn "the gateway may not expose an app entrypoint yet; mint one by hand:"
        info "  see deploy/runbook.md → 'Mint an invite code'"
    fi
}

# ── 8b. one-time admin key ─────────────────────────────────────────────────
# Printed only on the run that generated it. On every later run it stays in
# .env and is never echoed — re-running the installer must not spray secrets
# into a terminal, a screen-share or a CI log.
print_admin_key() {
    [ -n "${NEW_ADMIN_KEY:-}" ] || return 0

    step "Admin API key"
    printf '  %s%s%s\n' "$C_BLD" "$NEW_ADMIN_KEY" "$C_OFF"
    cat <<'ADMIN'

  This authenticates you to the gateway's /admin/* endpoints — minting invite
  codes, listing keys, suspending users. It is NOT a user key and must never
  be handed to one.

  Shown once. It is stored in deploy/.env (0600, root-owned); read it back
  from there if you lose it:

      sudo grep '^YANGBLE5_ADMIN_API_KEY=' /opt/yangble5/app/deploy/.env

ADMIN
}

# ── 9. next steps ──────────────────────────────────────────────────────────
next_steps() {
    step "Next steps"
    cat <<STEPS
  1. DNS
       Point ${DOMAIN} at this host. Behind Cloudflare, keep the record
       PROXIED and read deploy/cloudflare.md FIRST — the 100s origin timeout
       and response buffering both affect long streams.

  2. Harden the host
       sudo bash ${PREFIX}/app/deploy/harden.sh --behind-cloudflare
       (drop the flag if you are not using Cloudflare)

  3. Engine credentials
       The engine needs upstream accounts authenticated into its auth volume.
       That is CLIProxyAPI's own login flow — yangble5 does not manage it.

  4. Point a client at it
       export ANTHROPIC_BASE_URL="https://${DOMAIN}"
       export ANTHROPIC_AUTH_TOKEN="<a yb5_ key>"
       export CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000

       That last variable is not cosmetic: Claude Code assumes a 200K window
       for model names it does not recognise and starts auto-compacting long
       before a 1M-context model would need it.

  5. Operations
       deploy/runbook.md — spend, key rotation, suspension, backup, upgrades.

  Useful:
       cd ${DEPLOY_DIR}
       docker compose ps
       docker compose logs -f gateway
       docker compose exec caddy caddy validate --config /etc/caddy/Caddyfile
STEPS

    if [ "${ENGINE_READY:-0}" -eq 0 ]; then
        cat <<ENGINE

  ${C_YLW}THE STACK IS NOT RUNNING YET${C_OFF} — no engine was found.
  Supply CLIProxyAPI, then re-run this script (it will pick up where it
  stopped and will not touch your .env):

       # option A: a binary you built or downloaded and checksummed
       cp cli-proxy-api ${DEPLOY_DIR}/engine-bin/cli-proxy-api

       # option B: an image you already trust
       #   set ENGINE_IMAGE=... in ${DEPLOY_DIR}/.env

       sudo bash ${PREFIX}/app/deploy/install.sh --domain ${DOMAIN} --email ${ACME_EMAIL}

  Details: deploy/engine-bin/README.md
ENGINE
    fi
    printf '\n'
}

# ── main ───────────────────────────────────────────────────────────────────
main() {
    preflight
    create_service_user
    create_layout
    create_env
    create_engine_config
    check_engine_binary
    sync_container_identity
    start_stack
    print_admin_key
    bootstrap_invite
    next_steps
}

main "$@"
