# `deploy/` — hardened public deployment bundle

Everything needed to run yangble5 on a VPS with a domain, behind Cloudflare.

## Which file do I use? Answer this before anything else

**Is something already listening on port 80 or 443 on this host?**

```sh
ss -ltnp | grep -E ':(80|443)\b'      # any output = YES
```

| | **No — the box is empty** | **Yes — a web server, panel or another app owns 80/443** |
|---|---|---|
| Compose file | `docker-compose.yml` | **`docker-compose.behind-proxy.yml`** |
| TLS terminated by | Caddy, in this stack | your existing web server |
| Ports published | `0.0.0.0:80`, `0.0.0.0:443` | `127.0.0.1:8081` **only** |
| Installer | `install.sh` | manual — [`GO_LIVE.md`](GO_LIVE.md) → Path B |
| Web-server config | `Caddyfile` (shipped, works as-is) | [`nginx/yangble5.com.conf.example`](nginx/yangble5.com.conf.example), pasted into your vhost |
| Panel-managed nginx (aaPanel/BT/cPanel/Plesk) | — | [`AAPANEL.md`](AAPANEL.md) |

> **Say it plainly: on a host that is already serving other sites,
> `docker-compose.yml` will take them down.** It publishes Caddy on
> `0.0.0.0:80` and `0.0.0.0:443`. Docker cannot bind a port another process
> holds, so `docker compose up -d` either fails outright, or — if you "fixed"
> it by stopping nginx first — every other site on that box returns
> connection-refused from that moment until you stop the stack and start nginx
> again. This is not a theoretical risk. It is what happens, immediately, on a
> host with 28 vhosts.
>
> There is no flag or environment variable that makes `docker-compose.yml`
> safe on a busy host. Use the other file.

Everything else in this bundle — the gateway, the engine, `.env`,
`smoke_test.sh`, `runbook.md`, `SECRETS_SETUP.md`, `cloudflare.md` — is shared
by both paths and reads the same either way.

> **Read this too.** Running this bundle means putting a proxy for
> **your** upstream LLM accounts on the public internet. Every token your users
> spend is billed to you. `SECURITY.md` → "Operator responsibilities" is not
> boilerplate; it is the list of things that go expensively wrong.

---

## Quick start (standalone path — empty host)

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

`install.sh` targets the **standalone** path only. It writes a systemd unit and
brings up `docker-compose.yml`, i.e. Caddy on 80/443. Do not run it on a host
that is already serving sites.

---

## Quick start (behind-proxy path — host already runs a web server)

No installer. Five steps, each with an abort condition, in
[`GO_LIVE.md`](GO_LIVE.md) → "Path B". The short version:

```sh
git clone https://github.com/shark0120/yangble5
cd yangble5/deploy

cp .env.example .env            # then fill it in; SECRETS_SETUP.md first
echo 'COMPOSE_FILE=docker-compose.behind-proxy.yml' >> .env
echo 'GATEWAY_PORT=8081'        >> .env    # .env.example ships 8000 — see below
cp /path/to/cli-proxy-api engine-bin/cli-proxy-api
cp engine/config.example.yaml engine/config.yaml

docker compose up -d            # publishes 127.0.0.1:8081 and nothing else
curl -sS http://127.0.0.1:8081/health

# static site: zero risk, do it first and confirm it before touching nginx
cp -a site/. /www/wwwroot/yangble5.com/

# then the nginx block — AAPANEL.md if a panel owns the vhost
nginx -t && nginx -s reload
```

`GATEWAY_PORT` is the one value that must agree in three places: `.env`, the
`ports:` line in `docker-compose.behind-proxy.yml` (which defaults to `8081`),
and every `proxy_pass` in the nginx snippet. `.env.example` ships `8000`
because that is the standalone stack's internal port. Pick one number and make
all three match; a mismatch shows up as `502` from nginx and nothing in the
gateway log.

---

## What is here

| File | Purpose |
|---|---|
| `docker-compose.yml` | **standalone**: caddy (public) → gateway → engine, plus an optional `shim` profile |
| `docker-compose.behind-proxy.yml` | **behind-proxy**: gateway on `127.0.0.1` only, no caddy, for a host that already serves 80/443 |
| `nginx/yangble5.com.conf.example` | the Caddyfile's rules as an nginx server block, with the streaming settings that silently break |
| `AAPANEL.md` | panel-managed nginx: which file survives a panel update, `nginx -t`, reloading without dropping the other sites, rollback |
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

**Standalone** (`docker-compose.yml`):

```
internet ──80/443──▶ caddy ──edge──▶ gateway ──backend──▶ engine ──▶ upstream APIs
                       │                │                    │
                  only service      no published port   no published port
                  with ports:                           reachable only from
                                                        the backend network
```

**Behind-proxy** (`docker-compose.behind-proxy.yml`): the same stack with the
edge removed. Your web server takes Caddy's place, and the one published port
moves to loopback.

```
internet ──80/443──▶ YOUR nginx ──127.0.0.1:8081──▶ gateway ──backend──▶ engine ──▶ upstream
                    (not managed here)                 │                    │
                                                 the only published    no published port
                                                 port, loopback only
```

Container names and the network name are identical in both files, so Docker
refuses to run the two stacks simultaneously instead of silently double-binding
the engine's credential volume. Named volumes are shared, so switching between
them keeps the database and the engine's OAuth tokens.

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
| `/health` | unauthenticated liveness; used by the container healthcheck |
| `/pool/status` | unauthenticated capacity summary; no dollar figures |
| `/v1/messages`, `/v1/chat/completions`, `/v1/responses`, `/v1/models` | inference, authenticated with a `yb5_` key |
| `/auth/register` | account and key management |
| `/usage`, `/byok` | per-key usage; bring-your-own-key |
| `/admin/*` | operator only; 404s an unauthenticated caller |

`/v1/*` is an **allowlist, not a prefix proxy** — `PROXY_ROUTES` in
`gateway/app.py`. A path not in that tuple 404s at the gateway even though the
engine may serve it. That is deliberate: the engine also exposes a management
API, and a prefix proxy would publish it the day someone adds a route.

`/health` is canonical. `/healthz` is registered as an alias on the same
handler, and `/api/health` is mapped to it at the edge — by the `Caddyfile` on
the standalone path and by `nginx/yangble5.com.conf.example` on the
behind-proxy path — because that is what the landing page's status widget
fetches first. All three spellings answer, and every probe in this repo uses
`/health` so there is one string to keep right.

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
global counter that ignores both `routing.strategy` and session affinity.
That **rotation mechanism is verified** in 7.1.23's source
(`nextModelPoolOffset` in `sdk/cliproxy/auth/conductor.go`). The **~50%
ceiling that follows from it is a reasoned structural upper bound, not a
measurement** — no pool-vs-direct A/B run exists in this repository. See
[`../docs/FINDINGS.md`](../docs/FINDINGS.md#finding-1-a-two-member-model-pool-rotates-per-request-and-ignores-your-routing-policy).

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
| `docker-compose.behind-proxy.yml` | parses as YAML; verified `gateway` is the only service with `ports:` and that it binds `127.0.0.1`; engine and shim have none | **never run through `docker compose`** |
| `nginx/yangble5.com.conf.example` | brace balance and directive termination checked by script; every path cross-checked against `PROXY_ROUTES` in `gateway/app.py`, `smoke_test.sh` and `site/index.html`; `TRUSTED_PROXY_HOPS` arithmetic traced against `client_ip()` | **never run through `nginx -t`**; no nginx was available, and no server was contacted |
| `AAPANEL.md` | paths are the documented aaPanel layout, and §1 gives the command to verify them on your own host instead of trusting the page | **never run against a live panel**; no reload, no rollback exercised |
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
