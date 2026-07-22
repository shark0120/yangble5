# Go-live runbook

The ordered sequence for putting yangble5 on a public domain, with an explicit
abort condition at every step. Work top to bottom. **Do not skip ahead** — each
step assumes the previous one verified clean.

Companion documents:

| Document | What it covers |
|---|---|
| [`README.md`](README.md) | **which of the two deployment paths you are on** — decide there first |
| [`AAPANEL.md`](AAPANEL.md) | panel-managed nginx: file layout, `nginx -t`, reloading without dropping other sites |
| [`nginx/yangble5.com.conf.example`](nginx/yangble5.com.conf.example) | the server block for the behind-proxy path, including the streaming settings |
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

# Which path? Answer this before step 1

```sh
ss -ltnp | grep -E ':(80|443)\b'      # any output = something owns those ports
```

| | Empty host | Host already serves other sites |
|---|---|---|
| **Path** | **A — standalone** | **B — behind your web server** |
| Compose file | `docker-compose.yml` | `docker-compose.behind-proxy.yml` |
| Follow | **Steps 1–9 below** | **Path B**, immediately below |
| TLS | Caddy issues it | you already have it |

> On a host that is already serving sites, Path A takes them all down — it
> publishes Caddy on `0.0.0.0:80` and `0.0.0.0:443`. There is no flag that
> makes it safe there. [`README.md`](README.md) → "Which file do I use?"

---

# Path B — behind an existing web server

For a host where nginx (often panel-managed) already owns 80/443, the domain
already resolves, and its certificate already works. If any of those is not
true yet, do Steps 1–2 below (DNS, Cloudflare) first, then come back here.

Panel-managed nginx: read [`AAPANEL.md`](AAPANEL.md) alongside this. It has the
file paths, the reload that does not disturb the other sites, and the rollback.

The ordering is deliberate and it is **least-risk-first**. Every step before B4
is invisible to the sites already on the box; B4 is the only one that can
affect them, and it is a single graceful reload with a two-command rollback.

## B0 — Baseline, so you can prove you broke nothing

```sh
mkdir -p /root/yangble5-backup
nginx -T > /root/yangble5-backup/nginx-T.before.txt 2>&1
nginx -T 2>/dev/null | grep -c 'server_name'      # note this number
ss -ltnp | grep -E ':(80|443|8081|8318|8320)\b'   # note what is bound

# status of two other sites on this box, from your laptop:
curl -sS -o /dev/null -w '%{http_code}\n' https://<other-site-1>/
curl -sS -o /dev/null -w '%{http_code}\n' https://<other-site-2>/
```

- [ ] The `server_name` count and the two status codes are written down.
- [ ] `/root/yangble5-backup/nginx-T.before.txt` exists.

**ABORT IF** you cannot get a clean `nginx -T`. A config that does not
currently parse means the running nginx is serving an *older* config than what
is on disk, and your reload would apply someone else's unfinished edit along
with yours. Find out why before adding anything.

**Rollback** Nothing has changed.

## B1 — Static site first (zero risk, and it proves the vhost)

The landing page needs no gateway, no container and no config change. Shipping
it first means the risky steps happen on a vhost you have just seen working.

```sh
cp -a /www/wwwroot/yangble5.com /root/yangble5-backup/webroot.$(date +%s)
cp -a site/. /www/wwwroot/yangble5.com/
chown -R www:www /www/wwwroot/yangble5.com    # aaPanel's nginx user
```

**Verify — from your laptop, not the VPS:**

```sh
curl -sS -o /dev/null -w '%{http_code}\n' https://yangble5.com/          # 200
curl -fsS https://yangble5.com/install.sh | head -3                      # script
curl -fsS https://yangble5.com/install.sh.sha256                         # digest
curl -fsS https://yangble5.com/install.sh | sha256sum                    # matches
```

- [ ] The page renders and the status widget says 「狀態未知」. That is
      **correct** at this stage: `/api/health` is not routed yet, and the page
      is built to say "unknown" rather than invent a status.
- [ ] The digest matches the file. If it does not, you copied a stale pair.

**ABORT IF** `install.sh` downloads as a file instead of displaying, and you
care about that — it means `default_type` is `application/octet-stream`. Not
dangerous (`curl | sh` is unaffected) but it defeats "read it before you run
it". The fix is in the nginx snippet's `location /`, applied in B3.

**Rollback**

```sh
rm -rf /www/wwwroot/yangble5.com
cp -a /root/yangble5-backup/webroot.<stamp> /www/wwwroot/yangble5.com
```

## B2 — Gateway on loopback (still invisible from the internet)

```sh
cd deploy
cp .env.example .env          # SECRETS_SETUP.md before you fill this in
```

Set at least these, and read the comment above each one in `.env.example`:

```sh
COMPOSE_FILE=docker-compose.behind-proxy.yml   # so plain `docker compose` is right
GATEWAY_PORT=8081                              # must equal every proxy_pass in B3
YANGBLE5_TRUST_PROXY_HEADERS=true
YANGBLE5_TRUSTED_PROXY_HOPS=1                  # matches the snippet's XFF handling
YANGBLE5_MAX_REQUEST_BYTES=33554432            # 32m, same as client_max_body_size
```

```sh
cp /path/to/cli-proxy-api engine-bin/cli-proxy-api
cp engine/config.example.yaml engine/config.yaml    # then edit it
docker compose up -d
docker compose ps
```

**Verify — on the VPS:**

```sh
curl -sS http://127.0.0.1:8081/health          # {"status":"ok",...}
ss -ltnp | grep 8081                           # 127.0.0.1:8081 — NOT 0.0.0.0
curl -sS -o /dev/null -w '%{http_code}\n' https://yangble5.com/health   # 404 — nginx
```

- [ ] `/health` answers on loopback.
- [ ] Port 8081 is bound to `127.0.0.1` **only**.
- [ ] The public 404 confirms nginx is still serving the static site and has
      not been touched.
- [ ] Ports 80/443 are still nginx's, and the other sites still answer.

**ABORT IF** `ss` shows `0.0.0.0:8081` or `[::]:8081`. The `ports:` line was
changed. `docker compose down`, restore it, and start again — a gateway on all
interfaces is reachable without your TLS, without the `/v0/*` block, and with
`X-Forwarded-For` under the caller's control. `ufw deny 8081` does **not** fix
it: Docker's `DOCKER-USER` chain is evaluated before UFW.

**ABORT IF** the engine container restart-loops. Almost always: wrong binary
architecture, malformed `engine/config.yaml`, or upstream credentials that were
never authenticated. `docker compose logs --tail=100 engine`.

**Rollback** `docker compose down`. Volumes, `.env` and the database survive.

## B3 — The nginx block (edit only; nothing is live yet)

Paste PART 1–3 of [`nginx/yangble5.com.conf.example`](nginx/yangble5.com.conf.example)
into your vhost. On aaPanel, use the file [`AAPANEL.md`](AAPANEL.md) §2 tells
you to — **not** the vhost file itself, which the panel regenerates.

**Install the security headers as a file, not as a paste.** This is the part of
the snippet that got skipped on yangble5.com's own deployment: the live site
served no CSP, no `nosniff`, no `X-Frame-Options` and no `Referrer-Policy`, and
nothing anywhere errored. An `include` is atomic — either it is there and all
eight headers apply, or the path is wrong and `nginx -t` fails loudly.

```sh
sudo install -D -m 0644 -o root -g root \
  /opt/yangble5/app/deploy/nginx/security-headers.conf \
  /etc/nginx/yangble5/security-headers.conf
# then, at SERVER level in the vhost (not inside a location):
#   include /etc/nginx/yangble5/security-headers.conf;
```

Cloudflare does **not** add these for you. A proxied zone passes origin
response headers through; it invents nothing. The one header the broken
deployment did serve came from Cloudflare's own HSTS toggle, which is why
"there is an HSTS header, so the config must have loaded" was wrong.

Back up first:

```sh
cp -a /www/server/panel/vhost/rewrite/yangble5.com.conf \
      /root/yangble5-backup/rewrite.yangble5.com.conf.$(date +%s)
```

- [ ] Every `proxy_pass` port equals `GATEWAY_PORT` from B2.
- [ ] `proxy_pass` targets the literal `127.0.0.1`, never `localhost`.
- [ ] The Cloudflare `set_real_ip_from` list is the one **you** generated with
      the curl one-liner in the snippet, not the copy in the file.
- [ ] If you are not behind Cloudflare, PART 1a is deleted.
- [ ] You added no `add_header` and no `proxy_set_header` inside any location.
      Either one silently drops the whole inherited set (snippet PART 2j).
- [ ] The `include` for `security-headers.conf` is present at **server** level,
      or PART 2j's eight `add_header` lines are pasted there verbatim.
      Grep for it rather than trusting your memory of pasting it:
      `nginx -T | grep -c 'Content-Security-Policy'` must be ≥ 1.

**ABORT IF** you cannot find where the vhost is loaded from. `nginx -T | grep -n
'configuration file'` — editing a file nginx does not load is the failure that
passes every test and changes nothing.

**Rollback** Restore the backup. Nothing was reloaded, so nothing is live.

## B4 — `nginx -t`, then reload

This is the only step that can affect the other sites, and it is the one with
the shortest rollback.

```sh
nginx -t
```

**ABORT IF** this prints anything but `syntax is ok` / `test is successful`.
Do not reload. Restore the backup from B3 and read the error — it names the
file and line. `nginx -t` validates the *entire* configuration, so an error may
be pre-existing and not yours; `diff` against
`/root/yangble5-backup/nginx-T.before.txt` to find out.

```sh
nginx -t && nginx -s reload         # or: /etc/init.d/nginx reload
```

`reload` re-reads the config, starts new workers and lets the old ones finish
their in-flight requests. Listening sockets are never closed and no connection
is dropped. **Never `restart` on a shared host** — that closes the sockets and
every site on the box refuses connections until nginx comes back.

**Verify — immediately, in this order:**

```sh
nginx -T 2>/dev/null | grep -c 'server_name'    # same number as B0
tail -50 /www/wwwlogs/nginx_error.log

# the other sites, from your laptop — same codes as B0:
curl -sS -o /dev/null -w '%{http_code}\n' https://<other-site-1>/
curl -sS -o /dev/null -w '%{http_code}\n' https://<other-site-2>/
```

- [ ] `server_name` count unchanged.
- [ ] Both other sites return exactly what they returned in B0.
- [ ] No new errors in the nginx error log.

**ABORT IF** either other site changed. Roll back **before** you debug:

```sh
cp -a /root/yangble5-backup/rewrite.yangble5.com.conf.<stamp> \
      /www/server/panel/vhost/rewrite/yangble5.com.conf
nginx -t && nginx -s reload
```

## B5 — Smoke test

Run it **from your laptop**. On the VPS it would pass the health checks while
telling you nothing about what the internet can reach, and it would make the
"management surface is not exposed" check meaningless.

```sh
export YANGBLE5_API_KEY=yb5_...      # a real key, issued via the invite code
bash deploy/smoke_test.sh --base-url https://yangble5.com
```

Same checklist as Step 6 below — it is path-independent, because it only ever
talks to the public URL:

- [ ] TLS verifies; certificate has sensible remaining life.
- [ ] `/health` returns 200 with `status=ok`.
- [ ] `/pool/status` returns 200 and contains **no dollar figures**.
- [ ] Anonymous and garbage-key requests are both rejected **401**.
- [ ] The non-streaming round trip returns 200 with content.
- [ ] **The streaming round trip delivers events progressively, not in one burst.**
- [ ] `/v0/management/*` returns **404** from the internet.
- [ ] The engine's port 8318 does not answer from the internet.
- [ ] **All eight security headers are served** (check 9). Not "an HSTS header
      exists" — the values, from off-host. This is the check that was missing
      when yangble5.com went live without a CSP.

If check 9 is red, the header block is not in the running config. Confirm from
outside; a `curl` on the VPS skips Cloudflare and answers a different question:

```sh
curl -sSI https://yangble5.com/ | grep -icE \
  'content-security-policy|x-content-type-options|x-frame-options|referrer-policy|strict-transport-security|cross-origin-|permissions-policy'
# 8, not 1 and not 0
```

Two behind-proxy-specific checks the standalone path does not need:

```sh
curl -sS https://yangble5.com/api/health        # 200 — the widget's endpoint
curl -si https://yangble5.com/admin/keys | head -1   # 404
```

- [ ] The landing page's status widget now shows a real status, not 「狀態未知」.
- [ ] `/admin/*` is 404 from outside. Use `curl http://127.0.0.1:8081/admin/...`
      over SSH instead.

**ABORT IF** any check fails. The three emergencies in Step 6's table apply
identically here. One extra failure mode is specific to this path:

| Failure | Meaning | Do this now |
|---|---|---|
| `api/streaming` = `BUFFERED` | nginx or Cloudflare is collecting the whole stream; agents will look frozen | snippet PART 4's four-item list, in order |
| every request 502 | `GATEWAY_PORT` and `proxy_pass` disagree, or `proxy_pass` says `localhost` on a dual-stack host | fix, `nginx -t`, reload |
| per-IP limits fire on innocent users | `TRUSTED_PROXY_HOPS` does not match how nginx writes `X-Forwarded-For` | snippet PART 1b's table |

**Rollback** B4's two commands. The static site returns; the gateway keeps
running on loopback with its database intact.

## B6 — Then carry on with the shared steps

Path B rejoins the main runbook here:

| Step | Applies? |
|---|---|
| Step 7 — lock the origin down | **Yes**, but read `harden.sh` before running it on a panel host: it assumes it owns UFW, and the panel manages firewall rules too. `AAPANEL.md` §8. |
| Step 8 — open registration | Yes, unchanged. |
| Step 9 — announce | Yes, unchanged. The claim rules do not depend on how you deployed. |
| Day 2, kill switch, what to watch | Yes — except the Caddy commands. Your kill switch is `docker stop yangble5-gateway`, which 502s the API and leaves the static site and the other 27 sites untouched. |

One thing Path B does **not** get: the fail2ban jail `harden.sh` installs reads
Caddy's JSON access log, and there is no Caddy here. Until an nginx filter
exists, your brute-force defence is the gateway's own
`YANGBLE5_AUTH_FAIL_LOCKOUT_*` settings plus Cloudflare. Do not tell yourself
otherwise.

---

# Path A — standalone, on an empty host

Steps 1–9 below are Path A. **Do not run them on a host that already serves
other sites.**

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

### 8.0 — The question that is not about money

Ask this one first, because the budget caps below do not answer it and cannot.

> **Is every upstream credential the engine holds licensed for serving third
> parties?**

If any of them is a personal OAuth account — your Google/antigravity account,
your xAI accounts, a Codex account, a friend's account — the answer is **no**,
and opening registration creates exactly the configuration
[`docs/OPERATING_A_PUBLIC_SERVICE.md` §1](../docs/OPERATING_A_PUBLIC_SERVICE.md)
declares must never exist.

The budget caps bound what abuse can **cost** you. They say nothing about what
abuse can get **suspended**:

- Your gateway multiplexes every public user onto those credentials from one
  origin IP. The provider sees one egress producing the request-shape diversity
  of dozens of unrelated humans — different working hours, different languages,
  different repositories.
- The documented escalation is rate-limit, then suspension of the **account**,
  not of yangble5. It takes out everything else that Google/xAI/OpenAI account
  is used for.
- If a tier is served by **one** credential, a single suspension is a total
  outage for that tier with **no failover**.

- [ ] Every credential is on a plan whose terms permit resale / redistribution
      / proxying / multi-user access, and you have that in writing.

If you cannot tick that box, do **not** go open. Two supported alternatives:

- **Stay on `invite`.** Safer default, costs you nothing but minting codes
  (`runbook.md` §3).
- **Make BYOK the front door.** `/byok` lets a user bring their own key; point
  the installer and the landing page at that as the default path and keep the
  shared pool as the opt-in fallback, not the reverse. A pool nobody is
  depending on cannot disappoint anybody when it is suspended.

If you go open anyway with a pool you know is best-effort, then the disclosure
is not optional — it is the only honest version of the offer:

- [ ] The landing page and the AI/install flow both say, in plain language,
      that the shared pool is **best-effort and experimental** and **may vanish
      without notice**.
- [ ] If a tier is served by a single credential, that fact is stated too.

### 8.1 — The money gate

- [ ] Confirm the budget ceilings in `deploy/.env` are numbers you are willing
      to actually pay: `YANGBLE5_GLOBAL_MONTHLY_USD_BUDGET`,
      `YANGBLE5_DAILY_COST_USD_BUDGET`, `YANGBLE5_DAILY_TOKEN_BUDGET`.
- [ ] Confirm `YANGBLE5_REGISTER_MAX_PER_IP_PER_DAY` is set.
- [ ] Decide deliberately between staying on `invite` and moving to `open`.

Going **open** means anyone on the internet can create a key that spends your
money, protected only by the budget caps you just checked.

### 8.2 — Flipping it

Both lines below are required. Editing only the mode is how the credential
question gets skipped — `install.sh` refuses `open` while the assertion is
`no`, and a bare `sed` on the mode bypasses that refusal entirely, so the
assertion is set here explicitly rather than assumed.

```sh
cd /opt/yangble5/app/deploy
sudo sed -i 's/^YANGBLE5_POOL_LICENSED_FOR_THIRD_PARTIES=.*/YANGBLE5_POOL_LICENSED_FOR_THIRD_PARTIES=yes/' .env
sudo sed -i 's/^YANGBLE5_REGISTRATION_MODE=.*/YANGBLE5_REGISTRATION_MODE=open/' .env
docker compose up -d --force-recreate gateway
curl -sS https://api.example.com/health     # "registration":"open"
```

**ABORT IF** you could not tick the box in 8.0. A suspension is not
rate-limited by your budget cap and is not undone by lowering it.

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
- That the **rotation mechanism** — `nextModelPoolOffset` in
  `sdk/cliproxy/auth/conductor.go` selecting the upstream per request from a
  global counter that consults neither `routing.strategy` nor session
  affinity — is **verified in CLIProxyAPI 7.1.23's source**, with the symbols
  present in the binary we ran. Say that much and no more.

### What you must not say

- That yangble5 is **a model**, or "台灣自己訓練的模型", or any suggestion of a
  homegrown Taiwanese LLM. It is a proxy in front of other companies' models.
- Any specific free-credit dollar figure.
- That it "beats" GPT, Claude or Gemini. Nothing here was benchmarked against
  another provider — the tool ships so people can measure for themselves.
- 99.53% without the warm-only qualifier.
- That the **~50% pool ceiling** is measured, benchmarked, or "reproducible
  from source". It is a **reasoned structural upper bound** argued from the
  rotation mechanism. **No pool-vs-direct A/B run exists in this repository**,
  so there is no "before" number and 50% must never be quoted as one. The
  mechanism is verified; the ceiling is reasoned. Do not merge the two.
- Any hit-rate figure at a prefix size other than the ~749K one. Only one run
  is in the released evidence set.
- Any claim that the cache made things **faster**. Two of the three warm rounds
  were *slower* than the cold round. There is no latency win to announce.
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
| Cache hit rate | `tools/cache_stats_sidecar.py` | warm rounds well above 90% | a drop toward ~50% — the signature the two-member rotation would be expected to produce; check the alias before concluding anything |
| Disk | `df -h /var/lib/docker` | flat-ish | steady growth — check log rotation |

A hit rate sagging toward 50% is the specific regression this project exists to
prevent. The likeliest cause is an alias mapping to more than one upstream model
again; re-read `docs/FINDINGS.md` and check `oauth-model-alias` is still 1:1.
(The ~50% figure is a reasoned ceiling, not a measured threshold — treat it as
the shape to look for, not a number to alert on.)

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
