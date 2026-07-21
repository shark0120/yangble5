# Cloudflare in front of yangble5

> **What is measured and what is not.** The yangble5 numbers quoted in this
> repository (99.53% warm token-weighted prompt-cache hit rate, 748,918-token
> prompt, 21.4s → 10.8s warm latency) were measured on one Windows machine
> talking to a *local* engine — **not** through Cloudflare. Nothing in this
> document has been benchmarked end-to-end by us. The Cloudflare behaviour
> described below is vendor-documented behaviour plus widely-reproduced
> community findings; treat the numbers as "what to design for", and verify on
> your own zone before you rely on them.

This is an API endpoint that holds single HTTP responses open for minutes.
That is close to the worst-case traffic shape for a CDN, and most of the
defaults you would want on a website are wrong here.

---

## 1. The short version

| Do | Don't |
|---|---|
| Proxied (orange cloud) A/AAAA record | Grey-cloud it and expose your origin IP |
| SSL/TLS mode **Full (strict)** | **Flexible** — it sends plaintext to your origin |
| Cache rule: bypass cache for `/v1/*` | Rely on defaults to not cache API traffic |
| WAF custom rule blocking `/v0/*` | Assume Caddy's 404 is your only line |
| Rate limiting rule on the auth path | Bot Fight Mode (it breaks API clients) |
| Keep-alives on so the origin never goes 100s silent | Assume the free plan can raise that limit |

---

## 2. DNS

Create the record **proxied**:

```
Type  Name   Content          Proxy status
A     api    203.0.113.10     Proxied (orange cloud)
AAAA  api    2001:db8::10     Proxied (orange cloud)
```

The orange cloud is the entire point: it hides your origin IP, absorbs
volumetric DDoS and gives you WAF and rate limiting. Note that it also means:

- your origin never sees a real client IP on the TCP connection — it sees a
  Cloudflare edge. Caddy resolves the real one from `Cf-Connecting-Ip`
  (`trusted_proxies` in the Caddyfile), which is why that list must stay
  reasonably current;
- host-level `fail2ban` bans become nearly useless for proxied traffic. See
  §7.

**Hiding the origin only works if it stays hidden.** The IP leaks through old
DNS history, certificate transparency logs for a directly-issued cert, and any
service on the same box that connects out. Assume it will be found and lock
the origin down (§7).

---

## 3. TLS

Set **SSL/TLS → Overview → Full (strict)**.

- **Flexible** terminates TLS at Cloudflare and speaks **plain HTTP** to your
  origin. Every prompt and every API key would cross the public internet in
  clear text. Never use it.
- **Full** encrypts to the origin but does not validate the certificate.
- **Full (strict)** encrypts and validates. Caddy has a real Let's Encrypt
  certificate, so this just works.

### Getting the certificate issued while proxied

This trips up almost everyone. With the orange cloud on, Cloudflare terminates
TLS at its edge, so:

- **TLS-ALPN-01 cannot work.** The challenge never reaches your origin.
- **HTTP-01 usually works** — Cloudflare passes `/.well-known/acme-challenge/`
  through — but it is fragile. "Always Use HTTPS", some Bot Fight behaviour,
  and aggressive cache rules can all intercept it.
- **DNS-01 always works** and depends on nothing reaching your origin at all.

If issuance fails, use DNS-01: put `CLOUDFLARE_API_TOKEN` in `.env` and rename
`caddy/conf.d/tls-cloudflare.conf.example` to `tls-cloudflare.conf`. The token
must be a **scoped** token (`Zone / DNS / Edit` on that one zone), never the
Global API Key.

The alternative — grey-cloud the record, let Caddy issue over HTTP-01, then
re-enable the orange cloud — works, but it publishes your origin IP into
public DNS and into the certificate transparency log for as long as it takes.

---

## 4. Long streams: the 100-second problem

**This is the section that matters most.**

Cloudflare gives up on an origin that has not sent anything and returns
**error 524** after roughly **100 seconds**. Two things about it are commonly
misunderstood:

1. **It is a timeout on the origin going silent, not a cap on total response
   length.** Once your origin starts sending bytes and keeps sending them, a
   response can run far longer than 100 seconds. A ten-minute SSE stream that
   emits tokens throughout is *fine* on the free plan.
2. **You cannot raise it on the free plan.** The origin timeout is adjustable
   only on Enterprise. On Free/Pro/Business, 100 seconds of silence is 524,
   and there is no setting, no page rule and no support ticket that changes
   that.

### What that means in practice

| Traffic shape | Free plan |
|---|---|
| Streaming (SSE), first token within 100s, tokens keep flowing | Works |
| Streaming, but time-to-first-token > 100s | **524** |
| Non-streaming request that thinks for > 100s before replying | **524** |

> **We cannot tell you where you sit in that table, because we never measured
> time-to-first-token.** `tools/cache_bench.py` sends `stream: false`, so every
> latency figure in this repository is a **complete non-streaming round trip**,
> request sent to last byte received. On the 748,918-token prompt those round
> trips were **21,410 ms cold** and **10,753 / 23,457 / 22,381 ms** on the three
> warm rounds — one machine, one run, a local engine with no Cloudflare hop.
> Read against row 3 of the table, the slowest of those (23.5s) still sits well
> inside 100s, so a same-shaped request would probably not 524. But rows 1 and 2
> are about TTFT, and **nothing here measures TTFT** — a round trip is an upper
> bound on it, never a substitute. Any budget you derive for streaming
> first-token time from these numbers is unsupported by this repository's
> evidence. A bigger prompt, a slow upstream, or a Cloudflare hop moves all of
> it. Measure your own before you rely on the margin.

### Mitigations, in order of effectiveness

1. **Keep-alives on non-streaming responses.** `deploy/engine/config.example.yaml`
   sets `nonstream-keepalive-interval: 15`, which makes CLIProxyAPI emit a
   blank line every 15 seconds while a non-streaming response is still being
   generated. The connection is never silent for 100 seconds, so 524 never
   fires. This is the single most important setting on this page — do not
   remove it because it looks like noise.
2. **Prefer streaming clients.** Claude Code and Codex stream by default.
3. **Do not put slow non-streaming endpoints behind the proxied record.** If
   you have one, give it a separate grey-clouded hostname.

### Response buffering

Cloudflare may buffer or transform a response body, which shows up as tokens
arriving in bursts, or an agent that looks frozen and then dumps everything at
once. Defences already in this repo:

- Caddy sets `Cache-Control: no-store, no-transform` on `/v1/*`
  (`no-transform` is the standards-based "do not re-encode this" signal).
- Caddy's `encode` block deliberately **excludes** `text/event-stream`, so the
  stream is never gzipped.
- `flush_interval -1` in the reverse proxy flushes every write immediately.

On the Cloudflare side, turn **Rocket Loader off** and **Auto Minify off** (it
is deprecated, but check it is not still enabled on an older zone). Both
rewrite response bodies and neither has any business touching an API.

---

## 5. Caching

Cloudflare will not cache a `POST` by default, but "by default" is not a
security control. Create an explicit **Cache Rule**:

```
If   URI Path starts with "/v1/"  or  URI Path starts with "/api/"
Then Cache eligibility: Bypass cache
```

Inference responses contain user data. A cache hit that served one user's
completion to another would be the worst bug this deployment could have.

---

## 6. WAF, rate limiting and bots

### Bot Fight Mode — leave it OFF

Bot Fight Mode (free) challenges clients that do not look like browsers. Every
client here — `curl`, Claude Code, Codex, any SDK — looks exactly like what it
is designed to block, and an API client cannot solve a JavaScript challenge.
Turning it on breaks your service in a way that is hard to diagnose, because
the request never reaches your logs.

Super Bot Fight Mode (Pro and above) allows per-path exceptions, so on a paid
plan you can enable it and exclude `/v1/*`. On free, the switch is zone-wide:
leave it off, or host the API on a zone of its own.

### WAF custom rule: block the management surface

Defence in depth. Caddy already returns 404 for `/v0/*`, but a rule at the
edge means the request never reaches your VPS at all:

```
If   URI Path starts with "/v0/"
Then Block
```

### Rate limiting

Cloudflare's free plan includes a **small** number of custom WAF rules and a
**very** limited rate-limiting allowance — historically a single rule. Quotas
change; check your dashboard rather than trusting this paragraph. If you get
one rule, spend it on the registration/auth path, because that is the surface
where an attacker gets something (a free key) rather than merely costing you
CPU:

```
If    URI Path contains "/register"  or  URI Path contains "/login"
Then  Rate limit: 10 requests per 1 minute per IP
      Action: Block for 1 hour
```

This mirrors the `yb5_auth` zone in the Caddyfile. Having it in both places is
deliberate: the Cloudflare rule stops the traffic before it costs you
bandwidth, and the Caddy rule still works if you ever remove the orange cloud.

**Do not** put an aggressive rate limit on `/v1/*`. A coding agent doing
tool-calling legitimately fires bursts of requests, and a single agent run can
look like an attack. Per-request limits are a bad fit for this traffic; the
real control is the gateway's per-key token and cost budget, which counts what
you are actually paying for.

---

## 7. Locking the origin down

An attacker who learns your origin IP can skip Cloudflare entirely — and with
it every rule above. Two defences, best used together:

### a. Firewall: accept 80/443 only from Cloudflare

```sh
sudo bash deploy/harden.sh --cloudflare-only
```

This fetches Cloudflare's published ranges and installs iptables rules that
drop non-Cloudflare traffic to 80/443. Two implementation details matter:

- The rules go in the **DOCKER-USER** chain, not `INPUT`. Traffic to a
  Docker-published port is DNAT'd and traverses `FORWARD`; `INPUT` rules never
  see it. This is also why plain `ufw deny` cannot do this job.
- They are scoped to the **public interface**. Without that, the same rule
  would match containers' own outbound traffic to ports 80/443 — the engine
  calling the upstream provider APIs — and drop it.

SSH is untouched. If you later turn the orange cloud off, the site goes dark
until you run `sudo bash deploy/harden.sh --no-cloudflare-only`.

### b. Authenticated Origin Pulls (mTLS)

Cloudflare presents a client certificate and Caddy refuses connections without
it. Free on all plans. Setup notes and the Caddy snippet are in
`caddy/conf.d/tls-cloudflare.conf.example` — including a warning that the
`client_auth` syntax changed between Caddy 2.7 and 2.8, so validate before you
reload.

### c. fail2ban, honestly

The `yangble5-auth` jail installed by `harden.sh` matches the log field
`remote_ip` — the peer that actually opened the connection — and puts
Cloudflare's ranges in `ignoreip`. The consequence, stated plainly:

- **Abuse arriving through Cloudflare cannot be banned by fail2ban.** The peer
  is a Cloudflare edge shared with many unrelated users; banning it would take
  them out and would not touch the attacker.
- The jail therefore protects you against attackers hitting the origin
  **directly**. That is real, and it is exactly what a packet filter can do.
- Proxied abuse is handled at Cloudflare (§6) and by the gateway's own
  `AUTH_FAIL_LOCKOUT_THRESHOLD`, which counts per key rather than per packet.

---

## 8. Upload size

Cloudflare's free plan caps request bodies at **100 MB**. The Caddyfile sets
`request_body max_size 32MB`, so Caddy is the binding limit, not Cloudflare.
32 MB is comfortably more than our largest measured prompt (748,918 tokens is
a few MB of JSON). If you raise Caddy's limit past 100 MB while proxied,
Cloudflare will reject the request before Caddy ever sees it.

---

## 9. Checklist

```
[ ] DNS record proxied (orange cloud)
[ ] SSL/TLS mode: Full (strict)
[ ] Certificate issued (DNS-01 if HTTP-01 fought you)
[ ] Bot Fight Mode: OFF
[ ] Rocket Loader: OFF        Auto Minify: OFF
[ ] Cache rule: bypass cache for /v1/* and /api/*
[ ] WAF rule: block /v0/*
[ ] Rate limiting rule on /register and /login
[ ] Origin locked to Cloudflare ranges (harden.sh --cloudflare-only)
[ ] Authenticated Origin Pulls enabled (optional, recommended)
[ ] nonstream-keepalive-interval set in the engine config
[ ] Verified a long stream end-to-end through the proxied hostname
```

That last line is the one to actually do. Everything above is theory until you
have watched a ten-minute generation complete through your own zone.
