# Operating this publicly

Read this before you expose any part of yangble5 beyond `127.0.0.1`. It is short, and it is
mostly about two ways operators get hurt: **bans** and **invoices**.

Nothing here is legal advice. Your provider's terms are the authority; go read them.

---

## 1. The hard rule: pooled personal OAuth accounts must never back a public service

The engine can hold many OAuth credentials and fail over between them. That capability exists so
**one person** can spread **their own** usage across **their own** accounts. It is not a
multi-tenant licence.

If you point a public endpoint at a pool of personal accounts - yours, your friends', or
accounts you collected - you are sharing accounts with strangers. Every major provider's terms
prohibit this in some form. The realistic outcomes, in the order they usually happen:

1. Traffic from one residential IP that looks like dozens of unrelated users triggers automated
   abuse detection.
2. The accounts get rate-limited, then suspended. Not the service - the **accounts**, including
   whatever else those Google/OpenAI/xAI accounts are used for.
3. Bans can follow the human, not just the credential. Appeals for "I was proxying my personal
   account to the internet" do not go well.

The people who get hurt worst are the ones who lent you an account. Do not put someone else's
personal account behind a public endpoint, even if they said yes, even if they are family.

**One-line version: personal OAuth credentials are for personal use. A public service needs
keys that are licensed for it.**

---

## 2. What a public deployment actually requires

* **Paid, properly licensed API keys**, on a plan whose terms permit serving third parties.
  Check specifically for language about resale, redistribution, proxying and multi-user access.
  If the terms are ambiguous, ask the provider in writing before you launch, not after.
* **Your own commercial relationship with the provider**, so quota and billing are yours to
  manage and yours to be liable for.
* **An accurate description to your users** of which upstream models you serve and what data
  they can see. You are the data controller in their eyes now.
* **Acceptance that you are a provider.** Abuse complaints, illegal-content reports, DMCA,
  minors, and someone's automated agent stuck in a retry loop all become your problem.

If none of that appeals, run this on localhost. That is the configuration the whole repository
is written for, and it is the one we measured.

---

## 3. Credits are operator-funded. Design the cap first

**This project promises no credits, no free tier, and no daily allowance.** Any allowance your
deployment offers is paid for by *you*, out of *your* upstream billing, and any figure you
publish is a promise only you can keep. We are not putting a number in this repository, and you
should be equally careful about putting one on your landing page.

Cap the spend **before** you open registration, not after the first surprise invoice.

> **Read this before you trust the caps.** The startup check that refuses an uncapped
> deployment fires **only when `REGISTRATION_MODE=open`**. In `invite` (the default) and in
> `closed`, the gateway starts happily with **every global ceiling at its default of
> `0`, which means unlimited**. An invite-only instance is *not* capped because you
> installed it; it is capped when you set a number. Verify with `GET /admin/stats`, which
> reports the ceilings actually in force.

Every name below is the canonical spelling accepted by `gateway/config.py`. Each one is read
both bare and with the `YANGBLE5_` prefix (`_raw()` tries `NAME`, then `YANGBLE5_NAME`), so
`GLOBAL_MONTHLY_USD_BUDGET` and `YANGBLE5_GLOBAL_MONTHLY_USD_BUDGET` are the same setting.
All of them are listed with their defaults in [`gateway/.env.example`](../gateway/.env.example);
the operator-facing subset is in [`deploy/.env.example`](../deploy/.env.example).

**Operator ceilings — these bound *your* total exposure. All four default to `0` = unlimited.**

| Control | Setting | Default | Why it exists |
|---|---|---|---|
| Whole-pool monthly cap, in dollars | `GLOBAL_MONTHLY_USD_BUDGET` | `0` (unlimited) | The ceiling to set if your price table is real. |
| Whole-pool monthly cap, in tokens | `GLOBAL_MONTHLY_TOKEN_BUDGET` | `0` (unlimited) | The honest ceiling when you have *not* calibrated prices. Legacy alias: `GLOBAL_BUDGET_TOKENS`. |
| Whole-pool daily cap, in dollars | `GLOBAL_DAILY_USD_BUDGET` | `0` (unlimited) | Fails as "come back after 00:00 UTC" instead of "this instance is done until the 1st". |
| Whole-pool daily cap, in tokens | `GLOBAL_DAILY_TOKEN_BUDGET` | `0` (unlimited) | Same, without needing prices. |
| Warn threshold | `GLOBAL_BUDGET_WARN_RATIO` | `0.9` | Fraction of a ceiling at which the gateway starts warning. Lower it if you want earlier notice. |
| Operator's reserved slice | `OPERATOR_RESERVE_FRACTION` | `0.25` | Bottom fraction of the pool only `is_operator` keys may spend, so public traffic cannot starve your own daily driver. |

**Per-key ceilings — these bound one bad actor, not your bill.**

| Control | Setting | Default | Why it exists |
|---|---|---|---|
| Per-key daily tokens | `DAILY_TOKEN_BUDGET` | `2000000` | Bounds one runaway agent loop. Legacy alias: `USER_DAILY_TOKENS`. |
| Per-key daily dollars | `DAILY_COST_USD_BUDGET` | `2.0` | The same bound expressed in money. |
| Keys per IP / IPs per key | `MAX_KEYS_PER_IP`, `MAX_IPS_PER_KEY` | see `.env.example` | Bounds farming and key sharing. |
| Registrations per IP per day | `REGISTER_MAX_PER_IP_PER_DAY` | see `.env.example` | Bounds signup floods. |

**Registration mode.**

| Control | Setting | Default | Notes |
|---|---|---|---|
| Registration mode | `REGISTRATION_MODE` | `invite` | Exactly one of `invite`, `open`, `closed`. Anything else is fatal at startup. |
| Legacy boolean | `REGISTRATION_OPEN` | unset | A **boolean** alias honoured only when `REGISTRATION_MODE` is unset: true -> `open`, false -> `closed`. It cannot express `invite`. Prefer `REGISTRATION_MODE`. |

**Rate and concurrency:** `RATE_LIMIT_RPM`, `RATE_LIMIT_CONCURRENCY`, `AUTH_RPM_PER_IP`
(limiter primitives in `gateway/ratelimit.py`, wired up in `gateway/app.py`). These bound burst damage and upstream 429s. They are
**in-process**, so they are per uvicorn worker: run one worker, or divide the limits by the
worker count.

> **The dollar ceilings are only as honest as your price table.** With neither
> `PRICE_TABLE_JSON` nor `PRICE_TABLE_FILE` set, the gateway falls back to a single placeholder
> entry (`default`: $5.00 input / $0.50 cached input / $15.00 output per 1M tokens) and flags
> itself as `prices_are_placeholder: true` in `/admin/stats` (and logs a warning at
> startup). Those numbers are a
> deliberately conservative stand-in, **not** your provider's prices. Until you supply a real
> table, every `*_USD_BUDGET` is a cap on an *estimate*, and the token ceilings are the ones
> that actually correspond to something you can verify against an invoice.

What `REGISTRATION_MODE=open` does enforce at startup: at least one of the four global ceilings
above must be > 0, **and** at least one of `DAILY_TOKEN_BUDGET` / `DAILY_COST_USD_BUDGET` must
be > 0. The gateway raises `ConfigError` and refuses to boot otherwise. That is the whole of the
automatic protection; every other mode trusts you.

A single agent session in this repository's own benchmark moved **2,995,762 prompt tokens in
four requests**. One user with a scripted loop and a 749K prefix can spend more in an hour than
you planned for the month. Set the global cap to a number you would be willing to lose, and
treat "0 = unlimited" as a bug in your deployment rather than a default.

Two more habits worth having:

* **Alert on the trend, not the trip.** By the time the hard cap fires, the money is spent. Watch
  daily burn against the cap. `GLOBAL_BUDGET_WARN_RATIO` defaults to `0.9`, which is late
  notice - set it to `0.5` if you want to hear about it while you can still act.
* **Keep the kill switch trivial.** Freezing registration and dropping the global cap to a small
  number should be one command, and you should have done it once on purpose before you need it.

---

## 4. Security posture, briefly

The deployment scaffolding in `deploy/` encodes these; if you build your own, keep them:

* **The engine is never exposed.** Only the gateway is reachable from the internet, and it
  reverse-proxies to the engine over a private network. End users never hold a credential the
  engine would accept.
* **Client credentials are stripped at the edge.** `Authorization` and `x-api-key` from the
  caller are dropped before the request goes upstream, so nobody can smuggle their own provider
  key in - or read yours out.
* **Management endpoints are not public.** `/v0/management/*` is 404'd at the reverse proxy, and
  the management key is a second lock, not the first.
* **Issued keys are stored hashed** (scrypt with a per-key salt and a server-side pepper); there
  is no code path that can print a user's key after issuance. Plan for "regenerate", not
  "recover".
* **IP addresses are stored as salted hashes.** Abuse detection needs distinct-IP counts, not a
  location log of everyone who used your service.
* **Never log prompts or completions.** Not for debugging, not temporarily. Users will paste
  their private repository into a coding agent; that content must not survive in your disk logs.
* **Secrets live in the environment or a `chmod 600` `.env`**, never in a committed file. This
  repository ships `.env.example` with generated-at-install placeholders for that reason.

See [`SECURITY.md`](../SECURITY.md) for how to report a vulnerability, and `deploy/` for the
reference compose stack.

---

## 5. Pre-launch checklist

- [ ] Every upstream credential is a paid key licensed for serving third parties. No personal
      OAuth account is in the pool.
- [ ] At least one operator ceiling is a number you can afford to lose entirely:
      `GLOBAL_MONTHLY_USD_BUDGET`, `GLOBAL_MONTHLY_TOKEN_BUDGET`, `GLOBAL_DAILY_USD_BUDGET` or
      `GLOBAL_DAILY_TOKEN_BUDGET`. **They all default to `0` = unlimited, and nothing forces you
      to set one unless `REGISTRATION_MODE=open`.** Confirm the live value in `/admin/stats`
      rather than assuming your `.env` was read.
- [ ] If any ceiling is expressed in dollars, `PRICE_TABLE_JSON` or `PRICE_TABLE_FILE` holds
      *your provider's* prices. If `/admin/stats` still says `prices_are_placeholder: true`,
      your USD cap is capping a guess.
- [ ] `DAILY_TOKEN_BUDGET` and/or `DAILY_COST_USD_BUDGET` are set (they default to 2,000,000
      tokens / $2.00 per key per day). `RATE_LIMIT_RPM` and `RATE_LIMIT_CONCURRENCY` are set,
      and you have divided them by your uvicorn worker count.
- [ ] `REGISTRATION_MODE` is `invite` (the default) or `closed` for launch. Opening it is a
      decision, not a default. If you set the legacy `REGISTRATION_OPEN` boolean instead, know
      that it cannot express `invite` and is ignored entirely when `REGISTRATION_MODE` is set.
- [ ] The engine port is not reachable from the internet. Verified from off-host, not assumed.
- [ ] `/v0/management/*` returns 404 from the internet. Verified.
- [ ] Nothing logs prompts or completions. Grep your own config to confirm.
- [ ] You can freeze signups and drop the cap in one command, and you have tested it.
- [ ] Your landing page states which upstream models you serve, and promises no allowance you
      have not funded.
- [ ] You have read your provider's terms on proxying and multi-user access, this week.

---

## 6. If you are here because you want free Claude Code

Say the quiet part out loud: this repository will not get you that, and a public deployment
backed by borrowed accounts is the fastest way to lose the accounts you already have.

What it *will* do is make a long agent session against an upstream **you already pay for** cost
substantially less, because a 99%-cached 749K prefix is billed very differently from an uncached
one. That is a real saving on a real bill. It is not free inference, and there is no
configuration in this repository that turns it into free inference.