# `deploy/` — hardened public deployment bundle

Everything needed to run yangble5 on a VPS with a domain, behind Cloudflare.

> **Read this first.** Running this bundle means putting a proxy for
> **your** upstream LLM accounts on the public internet. Every token your users
> spend is billed to you. `SECURITY.md` → "Operator responsibilities" is not
> boilerplate; it is the list of things that go expensively wrong.

---

## Quick start

```sh
git clone https://github.com/shark0120/yangble5
cd yangble5

# 1. Supply CLIProxyAPI (we do not redistribute it — engine-bin/README.md)
cp /path/to/cli-proxy-api deploy/engine-bin/cli-proxy-api

# 2. Install
sudo bash deploy/install.sh --domain api.example.com --email you@example.com

# 3. Harden the host
sudo bash /opt/yangble5/app/deploy/harden.sh --behind-cloudflare

# 4. Configure Cloudflare
#    deploy/cloudflare.md  — do not skip §4, the 100-second origin timeout
```

`install.sh` is safe to re-run. It never regenerates an existing secret.

---

## What is here

| File | Purpose |
|---|---|
| `docker-compose.yml` | the stack: caddy (public) → gateway → engine, plus an optional `shim` profile |
| `Caddyfile` | TLS, security headers, rate limits, streaming-safe timeouts, `/v0/*` blocked |
| `Dockerfile.caddy` | custom Caddy build (`rate_limit` + `cloudflare` DNS modules) |
| `Dockerfile.gateway` | packages `gateway/` and `tools/` |
| `Dockerfile.engine` | wraps an **operator-supplied** CLIProxyAPI binary |
| `.env.example` | every setting, with `__GENERATE__` placeholders |
| `engine/config.example.yaml` | engine config: the 1:1 alias fix, routing, keep-alives |
| `install.sh` | one-command installer, idempotent |
| `harden.sh` | UFW, fail2ban, sysctl, unattended upgrades, SSH |
| `fail2ban/` | filter + jail template used by `harden.sh` |
| `caddy/conf.d/` | optional site drop-ins (DNS-01 TLS, mTLS) |
| `cloudflare.md` | DDoS/WAF setup and the streaming caveats |
| `runbook.md` | day-two operations |

---

## Architecture, and why

```
internet ──80/443──▶ caddy ──edge──▶ gateway ──backend──▶ engine ──▶ upstream APIs
                       │                │                    │
                  only service      no published port   no published port
                  with ports:                           reachable only from
                                                        the backend network
```

**Only Caddy publishes ports.** The gateway and engine have no `ports:` key at
all, so Docker never installs a host DNAT rule for them. They cannot be
reached from the internet even if UFW is later misconfigured. That is the
container-native equivalent of the single-machine rule that the engine binds
`127.0.0.1` only.

**Caddy is not on the backend network.** A compromised edge cannot talk to the
engine directly; it has to go through the gateway, which is where
authentication, quotas and spend caps live.

**Neither network is `internal: true`.** That flag also blocks *outbound*
traffic, and the engine must reach the upstream provider APIs while Caddy must
reach Let's Encrypt. Isolation here comes from not publishing ports, not from
severing egress.

---

## The gateway's contract

The gateway image runs `gateway.app:app` under uvicorn on `GATEWAY_PORT`
(default 8000) and is expected to serve:

| Path | Purpose |
|---|---|
| `/healthz` | unauthenticated liveness; used by the container healthcheck |
| `/v1/*` | inference, authenticated with a `yb5_` key |
| `/register`, `/login`, `/auth/*` | account and key management |

Every setting is read from the environment as either `NAME` or
`YANGBLE5_NAME`. **`gateway/config.py::Settings.from_env` is the source of
truth** — an unrecognised variable is silently ignored, which is the worst
possible failure mode for a budget cap you believed you had set. Check that
file before adding anything to `.env`.

---

## The engine is yours to supply

We do not vendor, redistribute or download CLIProxyAPI. See
`engine-bin/README.md`. Verify the checksum: that binary sees every prompt and
holds every upstream credential.

`engine/config.example.yaml` documents the settings that matter here,
including the **1:1 model alias** that is the point of this project — mapping
one alias to two upstream models makes CLIProxyAPI rotate upstreams on a
global counter that ignores both `routing.strategy` and session affinity,
which caps the upstream prompt-cache hit rate at roughly 50%.

### Running an engine older than 7.2.93

CLIProxyAPI 7.1.23's antigravity streaming translator forwards
`messages[].role` verbatim, so a Claude Code >= 2.1.x client that injects a
mid-conversation `role: "system"` message gets a 400 from Gemini. Enable the
shim:

```sh
# .env
YANGBLE5_ENGINE_URL=http://shim:8320
```
```sh
docker compose --profile shim up -d
```

Retire it after upgrading — `runbook.md` §10.

---

## Stock Caddy instead of the custom build

Caddy has no built-in rate limiting, so the default is a custom build. If you
would rather not compile anything:

1. `CADDY_IMAGE=caddy:2` in `.env`
2. delete every `rate_limit { ... }` block from the `Caddyfile`
3. do your rate limiting at Cloudflare and in the gateway instead

That is a real supported trade-off, not a downgrade to hide. Without step 2
Caddy refuses to start with `unrecognized directive: rate_limit`.

---

## Verification status

Honest accounting of what has and has not been exercised:

| Artifact | What was actually checked | Not checked |
|---|---|---|
| `harden.sh` | `bash -n`; the iptables script it *generates* was rendered and `bash -n`'d too | never run on a host; UFW/fail2ban/sysctl/sshd effects unverified |
| `install.sh` | `bash -n`; `.env` merge unit-tested — idempotent, preserves existing secrets, picks up new template keys, prints the admin key once and never again | never run as root; no user/dir/container creation exercised |
| `docker-compose.yml` | parses as YAML; verified only `caddy` publishes ports, network membership, `cap_drop`/`read_only`/limits; every `${VAR}` cross-checked against `.env.example`; every gateway env var cross-checked against `gateway/config.py` by AST | **never run through `docker compose`** |
| `Caddyfile` | brace balance, snippet/matcher resolution, and 20 streaming + security invariants (including the absence-invariants: no site-level `write` timeout, `text/event-stream` excluded from `encode`) | **never run through `caddy validate`** |
| `fail2ban` filter | regex unit-tested against synthetic Caddy JSON in compact and spaced form: 8 positive/negative cases each, IPv4 and IPv6, confirms it extracts `remote_ip` not `client_ip` | **never loaded by fail2ban**; `datepattern` unverified |
| `fail2ban` jail template | renders and parses as INI with `[DEFAULT]` inheritance | ban actions never exercised |
| `engine/config.example.yaml` | parses as YAML; asserted every alias maps 1:1 to a single upstream model | never loaded by CLIProxyAPI |
| Dockerfiles | reviewed | **never built** |
| End-to-end on a VPS | — | **never done** |

Docker was not available on the machine where this bundle was written, and it
has never been deployed. The syntax is checked and the logic is tested where it
could be tested offline; **the runtime behaviour is not**. Treat the first run
as a test, keep a second SSH session open while running `harden.sh`, and
validate before you reload:

```sh
docker compose config                                             # compose
docker compose exec caddy caddy validate --config /etc/caddy/Caddyfile
sudo fail2ban-regex /opt/yangble5/logs/caddy/access.log \
     /etc/fail2ban/filter.d/yangble5-auth.conf
sudo sshd -t                                                      # before any reload
```
