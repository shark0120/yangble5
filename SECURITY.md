# Security policy

## Reporting a vulnerability

**Do not open a public issue.** Use GitHub's private reporting:

<https://github.com/shark0120/yangble5/security/advisories/new>

Include what you did, what happened, and what you think an attacker gets out of it. A working
proof of concept is welcome but not required - a clear description of the mechanism is enough
to act on.

**Expectations, honestly stated:** this is a small project maintained by one person. Reports
are handled on a best-effort basis; there is no SLA and no bug bounty. You will get an
acknowledgement and, if the report is valid, a fix and credit in the release notes unless you
prefer otherwise. Please give a reasonable window before disclosing publicly.

## Supported versions

The `main` branch is the only supported version. There are no long-term support branches.

Note that the **engine** version matters independently of ours. Every measurement in this
repository was taken against CLIProxyAPI **7.1.23**. If you run **< 7.2.93** you also need
`tools/claude_shim.py` in the request path; see [`deploy/runbook.md`](deploy/runbook.md) §10.

## Security model

Be clear about the thing you are securing. **yangble5 is not a model.** It is a proxy stack over
third-party Gemini / Grok / GPT upstreams, built on CLIProxyAPI — which we did not write — plus a
configuration, an optional gateway, and measurement tooling.

Two consequences dominate everything else:

1. **Every prompt passes through it in plaintext.** The engine sees the whole conversation.
   Anything that compromises the engine compromises the content of every request it handles.
2. **The upstream credentials are the operator's, and so is the bill.** There is no yangble5
   account, no yangble5 credits, no yangble5 quota.

### Default posture: local only

Out of the box the engine binds `127.0.0.1`. Nothing on the LAN, let alone the internet, can
reach it. A public deployment is an explicit, opt-in decision made by running the bundle in
[`deploy/`](deploy/).

### Trust boundaries in the public deployment

```
internet ──443──▶ caddy ──edge net──▶ gateway ──backend net──▶ engine ──▶ upstream APIs
```

| Boundary | Enforced by |
|---|---|
| Internet → edge | Caddy is the only container with published ports; TLS, security headers, body-size limit, per-IP rate limits |
| Edge → gateway | private Docker network; the gateway publishes no port |
| Gateway → engine | separate Docker network; **Caddy is not on it**, so a compromised edge cannot reach the engine directly |
| Caller identity | per-user `yb5_` keys stored as salted *and peppered* scrypt hashes; the pepper lives in `.env`, not in the database |
| Upstream credential | added by the gateway; every client-supplied credential header is stripped first, so a caller can neither smuggle in their own nor read the operator's |
| Spend | per-key daily token and cost budgets (on by default), plus optional whole-pool daily and monthly ceilings in tokens or dollars — **every whole-pool ceiling defaults to `0` = unlimited and is only mandatory when `REGISTRATION_MODE=open`** |
| Management API | `/v0/*` returns 404 at the edge; the engine publishes no port; the management key is still required |

### Deliberate design decisions

* **Issued keys are unrecoverable by design.** Only a scrypt digest, its salt and a pepper
  fingerprint are stored. Plan for "regenerate", not "recover" — and note that an attacker with
  the database file still lacks the pepper.
* **Client IPs are stored hashed.** Abuse detection needs to count distinct sources, not to know
  who they were.
* **Containers run unprivileged**: all capabilities dropped, `no-new-privileges`, read-only root
  filesystem for the gateway, non-root uid. Caddy keeps exactly one capability
  (`NET_BIND_SERVICE`) because it binds 80/443.
* **Config is mounted read-only.** A config write is the shortest path from "compromised engine"
  to "attacker-controlled upstream". The one exception is the OAuth token directory, which must
  be writable because the engine refreshes tokens in place.
* **Streaming timeouts are unbounded on purpose.** A response can legitimately stay open for
  many minutes, so request body size, header size and per-IP rates are capped instead.

### What is explicitly NOT protected

A security model that lists only wins is marketing. These are real:

* **Prompts are not confidential from the operator.** Whoever runs the instance can read every
  request. If you use someone else's yangble5, you are trusting that person.
* **Prompts are not confidential from the upstream provider.** They go to Google / xAI / OpenAI
  under *their* terms.
* **No end-to-end encryption and no per-user isolation at rest.** All users share one SQLite
  database and one set of upstream credentials.
* **No defence against a malicious operator or a host compromise.** Root on the box means the
  `.env`, the pepper, the database and every upstream OAuth token.
* **`cost_usd` is not a bill.** The shipped price table is a placeholder, so a budget cap
  enforces a *relative* ceiling until you supply real rates. The authoritative number is your
  provider's console.
* **`docker` group membership is root-equivalent.** `install.sh` creates an unprivileged service
  user for the container processes, but driving the Docker daemon still needs root. The service
  user is not a sandbox around the whole stack, and the installer says so.

## In scope

* **`gateway/`** - authentication, key issuance and storage, quota and spend-cap enforcement,
  rate limiting, anything that lets a caller bypass a limit, read another user's data, or reach
  the engine directly.
* **`tools/claude_shim.py`** - request smuggling, header injection, response splitting, or any
  way a request through the shim reaches the engine differently than intended.
* **`tools/cache_bench.py` / `tools/cache_stats_sidecar.py`** - credential leakage into argv,
  logs, or written files.
* **`deploy/`** - a configuration in this repository that exposes the engine, the management
  API, or a secret when followed as written.
* **Any secret committed to this repository.** See below.

## Out of scope

* **CLIProxyAPI itself.** Bugs in the Go engine - OAuth handling, wire-format translation,
  routing, credential storage - belong at
  <https://github.com/router-for-me/CLIProxyAPI>. We integrate it; we did not write it.
* **Upstream model providers.** Their APIs, their quotas, their content filters.
* **Deployments that violate a provider's terms**, including any service backed by pooled
  personal OAuth accounts. That is a policy problem, not a vulnerability - see
  [`docs/OPERATING_A_PUBLIC_SERVICE.md`](docs/OPERATING_A_PUBLIC_SERVICE.md).
* **Missing hardening you have chosen not to configure**, e.g. running with
  `YANGBLE5_GLOBAL_MONTHLY_USD_BUDGET=0` (unlimited) and being surprised by the bill.

## Operator responsibilities

If you run an instance, these are yours and not ours. Stated explicitly because the most
expensive failures in this project are all in this list:

1. **Your upstream credentials.** You obtain them, authenticate them into the engine, rotate
   them, and revoke them when they leak. yangble5 stores them; it does not manage their
   lifecycle. If they are compromised, revoke at the provider first — nothing in this repository
   can do that for you.
2. **Your spend.** Every token your users consume is billed to your upstream accounts. Set an
   operator ceiling — any one of `YANGBLE5_GLOBAL_MONTHLY_USD_BUDGET`,
   `YANGBLE5_GLOBAL_MONTHLY_TOKEN_BUDGET`, `YANGBLE5_GLOBAL_DAILY_USD_BUDGET` or
   `YANGBLE5_GLOBAL_DAILY_TOKEN_BUDGET` — before you take signups. **All four default to `0`,
   which means unlimited.** The gateway refuses to start only when `REGISTRATION_MODE=open` and
   none of the four is set; in `invite` (the default) and `closed` it starts uncapped without
   complaint, so an invite-only instance is capped when *you* set a number and not before.
   Per-key ceilings (`YANGBLE5_DAILY_TOKEN_BUDGET`, default 2,000,000 tokens/day;
   `YANGBLE5_DAILY_COST_USD_BUDGET`, default $2.00/day) stop one key draining the global cap on
   day one. Any ceiling written in dollars is only as good as your price table: with neither
   `PRICE_TABLE_JSON` nor `PRICE_TABLE_FILE` configured the gateway uses conservative
   placeholder prices and reports `prices_are_placeholder: true` on `/admin/stats`. Full table
   of every budget setting and its default:
   [`docs/OPERATING_A_PUBLIC_SERVICE.md` §3](docs/OPERATING_A_PUBLIC_SERVICE.md#3-credits-are-operator-funded-design-the-cap-first).
3. **Provider terms of service.** Reselling or sharing access to an upstream account may violate
   them. Check. This project takes no position on your agreement with a third party and gives
   you no cover under it.
4. **Your users' data.** Prompts in flight, hashed IPs and usage history live in your database
   and your backups. Local law may treat that as personal data.
5. **Host security.** [`deploy/harden.sh`](deploy/harden.sh) is a starting point, not an audit.
6. **Staying current** — both yangble5 and CLIProxyAPI. See [`deploy/runbook.md`](deploy/runbook.md).

## Secrets in this repository

This repository must contain no API key, management key, OAuth token, account e-mail address,
or operator filesystem path. The tools here were ported from an install whose sources carried
all four, so this is enforced rather than assumed:

* `.gitignore` blocks `auth/`, `*.oauth`, `config.yaml`, `config.local.yaml`, `.env`, `*.db`,
  `*.pem`, `*.key` and the usual accidents.
* A CI job (`no-secrets` in `.github/workflows/ci.yml`) greps every push for key-shaped
  strings, the old management-key prefix, and Windows operator paths, and fails the build.
* Every tool reads its credentials from the environment. `cache_bench.py` deliberately refuses
  to accept the API key as a CLI flag: `argv` is readable by other users on most systems and
  lands in shell history.

**If you find a secret in this repository or its history**, report it privately as above. If it
is yours, rotate it first and report second - assume anything pushed to a public repository is
compromised the moment it lands, and that rewriting history does not un-publish it.

## Hardening notes for operators

The short version, expanded in [`docs/OPERATING_A_PUBLIC_SERVICE.md`](docs/OPERATING_A_PUBLIC_SERVICE.md):

* Never expose the engine port or `/v0/management/*` to the internet.
* Never log prompts or completions.
* Set a global spend cap before you take any signups. Nothing enforces one unless
  `REGISTRATION_MODE=open`; every ceiling ships as `0` = unlimited.
* Issued keys are stored hashed; plan for "regenerate", not "recover".
* Use paid keys licensed for serving third parties. Personal OAuth accounts are for personal
  use, and sharing them gets them banned.

The `deploy/` bundle implements most of this for a public host:

| Document | Covers |
|---|---|
| [`deploy/install.sh`](deploy/install.sh) | one-command install; generates secrets, never prints them (one bootstrap invite code excepted) |
| [`deploy/harden.sh`](deploy/harden.sh) | UFW, fail2ban, sysctl, unattended security upgrades, key-only SSH |
| [`deploy/cloudflare.md`](deploy/cloudflare.md) | DDoS/WAF setup, and what the free plan genuinely cannot do for long streams |
| [`deploy/runbook.md`](deploy/runbook.md) | spend, key rotation, suspension, backup/restore, engine upgrades, budget-cap incidents |

### If you leak your own secrets

`deploy/runbook.md` has the procedures: §5 engine key, §6 user keys, §7 the pepper (which
invalidates every issued key), §13 incident response. Rotating at the upstream provider is the
part only you can do.