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

Cap the spend **before** you open registration, not after the first surprise invoice. The
gateway in `gateway/` is built for exactly this, and it fails fast rather than warning:
a configuration with public registration enabled and no spend ceiling is refused at startup.

The controls, and the order to think about them:

| Control | Setting | Why it exists |
|---|---|---|
| Global spend ceiling | `YANGBLE5_GLOBAL_BUDGET_TOKENS` | The only control that bounds your total exposure. Everything else is per-user. |
| Per-user daily allowance | `YANGBLE5_USER_DAILY_TOKENS` | Bounds one bad actor, or one runaway agent loop. |
| Registration mode | `YANGBLE5_REGISTRATION_OPEN` / invite / closed | Freeze signups when you are over budget **without** taking the service down for existing users. |
| Rate + concurrency limits | `gateway/ratelimit.py` | Bounds burst damage and upstream 429s. Note: in-process, so per worker. Run one uvicorn worker, or divide the limits by the worker count. |

A single agent session in this repository's own benchmark moved **2,995,762 prompt tokens in
four requests**. One user with a scripted loop and a 749K prefix can spend more in an hour than
you planned for the month. Set the global cap to a number you would be willing to lose, and
treat "0 = unlimited" as a bug in your deployment rather than a default.

Two more habits worth having:

* **Alert on the trend, not the trip.** By the time the hard cap fires, the money is spent. Watch
  daily burn against the cap and get told at 50%.
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
- [ ] `YANGBLE5_GLOBAL_BUDGET_TOKENS` is set to a number you can afford to lose entirely.
- [ ] `YANGBLE5_USER_DAILY_TOKENS` is set. Per-user rate and concurrency limits are set, and you
      have divided them by your worker count.
- [ ] Registration is `invite` or `closed` for launch. Opening it is a decision, not a default.
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