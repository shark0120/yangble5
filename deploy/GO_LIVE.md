# Go-live runbook

The ordered sequence for putting yangble5 on a public domain, with an explicit
abort condition at every step. Work top to bottom. **Do not skip ahead** — each
step assumes the previous one verified clean.

Companion documents:

| Document | What it covers |
|---|---|
| [`SECRETS_SETUP.md`](SECRETS_SETUP.md) | supplying upstream credentials without leaking them |
| [`cloudflare.md`](cloudflare.md) | the Cloudflare settings referenced in step 2 |
| [`runbook.md`](runbook.md) | day-to-day operations after go-live |
| [`../SECURITY.md`](../SECURITY.md) | operator responsibilities |

---

## Before you start: what you are actually launching

Be clear about this, because step 8 asks you to describe it in public.

You are launching **a proxy** in front of **someone else's model APIs**, built on
[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) — a third-party MIT-licensed
Go project that is not ours and must be credited wherever you describe this
service. Everything in `gateway/`, `tools/`, `deploy/` and `docs/` is ours.

You are **not** launching a model. Nothing here is trained, fine-tuned or
merged. An alias is a name, not a new model.

Every token your users spend is **billed to the upstream account you
configure**. There is no free pool. Read `SECRETS_SETUP.md` before you decide
whose credentials go in.

Known limits that will generate support questions on day one:

- **No live web search.** Measured 2026-07-21: asked what year it is, Gemini
  answered "2024" and Grok answered "2025". The proxy adds no retrieval.
- **The 99.53% cache hit rate is warm-only** — rounds 2-4 of a 4-round session.
  Round 1 of *every* session is a cold write and is 0% by construction. It was
  measured on one Windows 11 machine, one run per configuration, at a ~749K
  prefix. It is not a promise about your hardware, your prefix size, or next
  month's upstream behaviour.
- **`tools/claude_shim.py` is a workaround**, not a feature. It backports the
  `role:"system"` streaming fix for engines older than v7.2.93. If you are on
  7.2.93 or newer, retire it (`runbook.md` §10).

---

## Step 1 — DNS

- [ ] Create the `A` record for your hostname pointing at the VPS's public IPv4.
- [ ] If the host has a global IPv6 address, create the `AAAA` record too. If it
      does not, **create no AAAA record** — a dangling AAAA breaks v6-only
      clients invisibly.
- [ ] Leave it **DNS-only (grey cloud) for now**. You will switch it to proxied
      in step 2, after you have confirmed it resolves.

**Verify**

```sh
dig +short A api.example.com
# must print exactly the VPS IP from your provider's control panel
```

**ABORT IF** the record resolves to an address you do not recognise, or to a
previous tenant's server. Fix DNS and wait for the old TTL to expire before
continuing — a stale record means Let's Encrypt validates against the wrong
host and you will spend an hour debugging a certificate that was never going to
be issued.

**Rollback** Delete the record. Nothing else has been created yet.

---

## Step 2 — Cloudflare

Full detail is in [`cloudflare.md`](cloudflare.md); this is the ordered subset
required to go live.

- [ ] Switch the record to **Proxied (orange cloud)**.
- [ ] **SSL/TLS → Overview → Full (strict)**.
- [ ] **Rocket Loader: OFF**, **Auto Minify: OFF** (both rewrite response bodies).
- [ ] **Bot Fight Mode: OFF** — it challenges API clients, which cannot solve
      challenges.
- [ ] Cache Rule: **bypass cache** for `/v1/*` and `/api/*`.
- [ ] WAF custom rule: **block `/v0/*`** (defence in depth; Caddy also 404s it).

> **Never select SSL mode "Flexible."** It terminates TLS at Cloudflare and
> speaks plain HTTP to your origin, putting every prompt and every API key on
> the public internet in clear text.

### The streaming caveat — read this before step 6

On Free, Pro and Business plans Cloudflare kills a connection that is **silent
for ~100 seconds** with a **524**. It is an *idle* timeout, not a total-duration
cap: a ten-minute stream that keeps emitting tokens is fine; a request that
thinks for 101 seconds before its first byte is not.

Two defences are already in this repo and you must not remove either:

- `nonstream-keepalive-interval: 15` in the engine config, which emits a blank
  line every 15s so the connection is never silent.
- `flush_interval -1` plus `Cache-Control: no-transform` in the Caddyfile, so
  nothing buffers or re-encodes `text/event-stream`.

Step 6 tests this specifically. It is the single most likely thing to be broken
in a deployment that otherwise looks perfect.

**Verify**

```sh
dig +short A api.example.com     # now returns Cloudflare anycast IPs, not yours
```

**ABORT IF** SSL mode is anything other than Full (strict) once the origin
certificate exists. Do not "temporarily" use Flexible to get past a handshake
error — that decision leaks credentials.

**Rollback** Set the record back to DNS-only (grey cloud). Note that this
exposes your origin IP permanently: it enters DNS history and cannot be
un-published. Prefer fixing forward.

---

## Step 3 — Preflight

Run on the VPS, from a checkout of this repo. It is read-only: it creates
nothing, installs nothing, and changes no configuration.

```sh
sudo bash deploy/preflight.sh --domain api.example.com
```

- [ ] Exit code is 0.
- [ ] Read every `WARN` and make a deliberate decision about each one.

Because the record is proxied now, the DNS check reports *"resolves into
Cloudflare's ranges — record is PROXIED"* and tells you it cannot see the
origin from outside. That is expected. Confirm the **origin** value in the
Cloudflare dashboard matches the IP preflight printed for this host.

**ABORT IF** the exit code is non-zero. Every critical check maps to something
that breaks during or after install:

| Failing check | What it costs you if ignored |
|---|---|
| `ssh/authorized-keys` | `harden.sh` disables password login; with no key you are locked out |
| `port/80-tcp` in use | Caddy cannot bind, no certificate is ever issued |
| `dns/caa` | ACME issuance is refused by the CA, silently |
| `clock/skew` | TLS validity windows and OAuth token exchange both fail intermittently |
| `engine/binary-arch` | the engine container exec-format-errors on first start |
| `egress/*` | images cannot be pulled, or the upstream cannot be reached |

**Rollback** None needed — nothing has changed.

---

## Step 4 — Harden the host

Run this **before** install, so the box is never sitting exposed and unhardened
while you debug a deployment.

```sh
# Keep your current SSH session OPEN in another terminal until step 4 verifies.
sudo bash deploy/harden.sh --behind-cloudflare
```

- [ ] UFW active, default deny incoming, SSH + 80 + 443 allowed.
- [ ] `sshd` key-only.
- [ ] Unattended security upgrades enabled.

Do **not** pass `--cloudflare-only` yet. That restricts 80/443 to Cloudflare's
ranges, and if anything sends you back to a grey-cloud record for certificate
troubleshooting you would be locking out the ACME validation you are trying to
fix. It is step 7.

`harden.sh` installs the `yangble5-auth` fail2ban jail but leaves it **disabled**
when `/opt/yangble5/logs/caddy/access.log` does not exist yet — which is always
true on a first run, because install.sh has not run. That is intended. You
re-run `harden.sh` in step 7 to activate it.

**Verify — from a SECOND terminal, without closing the first:**

```sh
ssh you@api.example.com 'echo still-in'
sudo ufw status verbose
```

**ABORT IF** the second SSH session does not succeed. You still have the first
session open; use it to undo:

```sh
sudo ufw disable
# harden.sh backed up the original as /etc/ssh/sshd_config.yangble5.bak-<stamp>
sudo cp /etc/ssh/sshd_config.yangble5.bak-* /etc/ssh/sshd_config
sudo systemctl restart ssh
```

**Rollback** As above. Every file `harden.sh` rewrites is backed up next to the
original with a `.yangble5.bak-<timestamp>` suffix.

---

## Step 5 — Install

Supply the engine binary and the upstream credentials **first** — see
[`SECRETS_SETUP.md`](SECRETS_SETUP.md). Do not paste credentials into a chat
window, a ticket, or this terminal's history.

```sh
sudo bash deploy/install.sh --domain api.example.com --email you@example.com
```

The installer is idempotent: re-running it never regenerates an existing secret
and never overwrites an existing engine config.

- [ ] Containers report healthy: `cd /opt/yangble5/app/deploy && docker compose ps`
- [ ] A certificate was issued: `docker compose logs caddy | grep -i "certificate obtained"`
- [ ] Record the one-time bootstrap invite code it prints. It is shown **once**.

**Verify**

```sh
cd /opt/yangble5/app/deploy
docker compose ps                 # every service Up, none restarting
curl -sS https://api.example.com/health
```

**ABORT IF** any container is in a restart loop, or the certificate was not
issued within a few minutes. Diagnose before touching anything else:

```sh
docker compose logs --tail=100 caddy
docker compose logs --tail=100 gateway
docker compose logs --tail=100 engine
```

A restart-looping `engine` is almost always one of: wrong binary architecture
(preflight would have caught it), a malformed `engine/config.yaml`, or upstream
credentials that were never authenticated.

**Rollback**

```sh
cd /opt/yangble5/app/deploy
docker compose down               # stops everything; volumes and .env survive
```

Nothing is publicly reachable once Caddy is down. Your data and secrets remain
in the named volumes and in `deploy/.env`.

---

## Step 6 — Smoke test

Run this **from your laptop**, not from the VPS. Running it on the server would
still pass the health checks while telling you nothing about whether the
internet can reach you — and it would make the "management surface is not
exposed" check meaningless.

```sh
export YANGBLE5_API_KEY=yb5_...          # a real key, issued via the invite code
bash deploy/smoke_test.sh --base-url https://api.example.com
```

- [ ] TLS verifies, certificate has sensible remaining life.
- [ ] `/health` returns 200 with `status=ok`.
- [ ] `/pool/status` returns 200 and contains **no dollar figures**.
- [ ] An anonymous request is rejected **401**.
- [ ] A garbage key is rejected **401**.
- [ ] The non-streaming round trip returns 200 with content.
- [ ] **The streaming round trip delivers events progressively, not in one burst.**
- [ ] `/v0/management/*` returns **404** from the internet.
- [ ] The engine's port 8318 does not answer from the internet.

Two of these checks spend tokens (`max_tokens=16` each). `--no-spend` skips
them, but never announce on a run that skipped them.

**ABORT IF** any check fails. Three failures are emergencies rather than bugs:

| Failure | Meaning | Do this now |
|---|---|---|
| `auth/anonymous-rejected` = 200 | you are running an **open proxy billed to you** | kill switch, below, immediately |
| `block/v0/...` = 200 | the **credential-minting API is public** | kill switch, then rotate every upstream credential |
| `engine/port-8318` answers | the gateway is being **bypassed** — no auth, no budget, no rate limit | kill switch, check UFW and provider firewall |

A `api/streaming` failure of `BUFFERED` is not an emergency but is a
go/no-go blocker: agents will appear frozen. Work through
`cloudflare.md` → "Response buffering" before continuing.

**Rollback** `docker compose down`, as in step 5.

---

## Step 7 — Lock the origin down

Only now, with a working certificate and a passing smoke test:

- [ ] Re-run hardening with the Cloudflare restriction and the now-existing access log:

```sh
sudo bash /opt/yangble5/app/deploy/harden.sh --cloudflare-only
```

This drops non-Cloudflare traffic on 80/443 and activates the `yangble5-auth`
fail2ban jail, which could not be enabled in step 4 because the Caddy access
log did not exist yet.

- [ ] Re-run the smoke test. It must still pass **from your laptop**, because
      your laptop's traffic now arrives via Cloudflare.

**Verify**

```sh
sudo ufw status numbered | head -30
sudo fail2ban-client status yangble5-auth
```

**ABORT IF** the smoke test now fails. You have almost certainly locked out
something legitimate.

**Rollback**

```sh
sudo bash /opt/yangble5/app/deploy/harden.sh --no-cloudflare-only
```

---

## Step 8 — Open registration

Until now registration has been `invite` (the installer's default) and the only
key is yours.

- [ ] Confirm the budget ceilings in `deploy/.env` are numbers you are willing
      to actually pay: `YANGBLE5_GLOBAL_MONTHLY_USD_BUDGET`,
      `YANGBLE5_DAILY_COST_USD_BUDGET`, `YANGBLE5_DAILY_TOKEN_BUDGET`.
- [ ] Confirm `YANGBLE5_REGISTER_MAX_PER_IP_PER_DAY` is set.
- [ ] Decide deliberately between staying on `invite` and moving to `open`.

Staying on **invite** is the safer default and costs you nothing but the effort
of minting codes (`runbook.md` §3). Going **open** means anyone on the internet
can create a key that spends your money, protected only by the budget caps you
just checked.

```sh
cd /opt/yangble5/app/deploy
sudo sed -i 's/^YANGBLE5_REGISTRATION_MODE=.*/YANGBLE5_REGISTRATION_MODE=open/' .env
docker compose up -d --force-recreate gateway
curl -sS https://api.example.com/health     # "registration":"open"
```

**ABORT IF** the global monthly budget is unset, zero, or larger than you can
absorb. An open registration mode with no cap is the most expensive mistake
available in this repo.

**Rollback** — closes the door in about five seconds:

```sh
sudo sed -i 's/^YANGBLE5_REGISTRATION_MODE=.*/YANGBLE5_REGISTRATION_MODE=closed/' .env
docker compose up -d --force-recreate gateway
```

Existing keys keep working; no new ones can be created.

---

## Step 9 — Announce

- [ ] The smoke test passed **without** `--no-spend`.
- [ ] You have watched one long streaming generation complete end-to-end
      through the proxied hostname, with your own eyes.
- [ ] Your announcement credits **CLIProxyAPI** prominently.

### What you may say

- "台灣人做的 AI 閘道" / "台灣人的 AI token 自由" — this is about who built and
  operates the gateway, and it is accurate.
- The measured numbers, **with their footnotes attached**: 99.53% is
  token-weighted, warm rounds only, one machine, one run, ~749K prefix; round 1
  is 0%.
- That a 748,918-token prompt was processed without truncation.
- That the cache finding (`nextModelPoolOffset` rotating the upstream per
  request and capping cache hits at ~50%) is reproducible from source.

### What you must not say

- That yangble5 is **a model**, or "台灣自己訓練的模型", or any suggestion of a
  homegrown Taiwanese LLM. It is a proxy in front of other companies' models.
- Any specific free-credit dollar figure.
- That it "beats" GPT, Claude or Gemini. Nothing here was benchmarked against
  another provider — the tool ships so people can measure for themselves.
- 99.53% without the warm-only qualifier.
- Anything implying live web search. There is none.

Do not invent testimonials and do not display the logos of companies that have
not endorsed this.

**ABORT IF** you cannot state the cache figure with its qualifier in the space
you have. Use "measured >99% prompt-cache reuse on warm rounds — see the
README for the full methodology and its limits" and link it.

**Rollback** You cannot un-announce. This is the one irreversible step, which
is why it is last.

---

# Day 2: the first 24 hours

## The kill switch

Know this before you need it. Fastest first:

```sh
cd /opt/yangble5/app/deploy

# 1. Cut public access in about a second. Keeps all state; gateway and engine
#    keep running, nothing can reach them.
docker compose stop caddy

# 2. Full stop. Volumes, database and .env all survive.
docker compose down

# 3. If Docker itself is wedged, cut it at the host.
sudo ufw deny 443 && sudo ufw deny 80
```

To stop *spending* without going offline — users get a clear error instead of a
dead host:

```sh
sudo sed -i 's/^YANGBLE5_GLOBAL_MONTHLY_USD_BUDGET=.*/YANGBLE5_GLOBAL_MONTHLY_USD_BUDGET=0.01/' .env
docker compose up -d --force-recreate gateway
```

Bring it back with `docker compose up -d`.

## What to watch, and what "healthy" looks like

Check at +1h, +6h and +24h.

| Signal | How to read it | Healthy | Investigate |
|---|---|---|---|
| Container state | `docker compose ps` | all `Up`, restart count stable | any service restarting more than once |
| Gateway errors | `docker compose logs --since=1h gateway \| grep -ci error` | near zero | a rising count, or the same error repeating |
| 5xx at the edge | `docker compose logs --since=1h caddy \| grep -c '"status":5'` | ~0 | any sustained rate |
| **524s** | same, `grep -c 524` | 0 | **any** — the 100s idle kill is biting; see `cloudflare.md` §4 |
| Auth failures | `grep -c 'authentication_error'` in gateway logs | a trickle | a burst from few IPs = credential stuffing; fail2ban should already be banning |
| Registrations | `runbook.md` §3 | matches what you expect from your announcement | a spike far beyond it, especially from one IP range |
| Spend | `runbook.md` §2 | tracking below your daily cap | on pace to exhaust the monthly cap in days |
| Distinct IPs per key | `runbook.md` §4 | 1-3 per key | above `YANGBLE5_ABUSE_DISTINCT_IP_THRESHOLD` = a shared key |
| Cache hit rate | `tools/cache_stats_sidecar.py` | warm rounds well above 90% | a drop toward ~50%, which means the model pool is rotating upstreams again |
| Disk | `df -h /var/lib/docker` | flat-ish | steady growth — check log rotation |

A hit rate sagging toward 50% is the specific regression this project exists to
prevent. It means an alias is mapping to more than one upstream model again;
re-read `docs/FINDINGS.md` and check `oauth-model-alias` is still 1:1.

## The three things most likely to go wrong

1. **Streaming looks frozen.** Buffering somewhere on the path. Re-run
   `smoke_test.sh` — the streaming check reports `BUFFERED` explicitly.
   `cloudflare.md` → "Response buffering".
2. **524 on long prompts.** The keep-alive is missing or the first token takes
   over 100 seconds. Check `nonstream-keepalive-interval` survived your edits.
3. **Spend far above expectation.** One key shared widely, or an agent in a
   retry loop. `runbook.md` §4 to find and suspend it; the budget cap is the
   backstop, not the plan.

## First-week follow-ups

- [ ] Verify a backup actually restores (`runbook.md` §8, §9). An untested
      backup is not a backup.
- [ ] Confirm unattended security upgrades are applying.
- [ ] Watch the certificate auto-renew, or diarise the expiry.
- [ ] If you are on engine < 7.2.93, plan the upgrade and retire
      `tools/claude_shim.py` (`runbook.md` §10).
