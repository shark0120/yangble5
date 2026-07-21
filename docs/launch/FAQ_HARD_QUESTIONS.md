# FAQ: the hard questions

The easy questions are in the README. These are the ones that get asked with an edge, and the
answers below are the ones we actually believe rather than the ones that sell best.

Two ground rules we tried to hold to:

1. **Concede the true part first.** Most of these criticisms are substantially correct. A reply
   that argues before conceding reads as evasion, and deserves to.
2. **Never answer a factual question with a vibe.** If we measured it, the number and its
   qualifier are here. If we didn't, this document says "we didn't" instead of implying we did.

---

## 1. "Isn't this just a proxy? Isn't it just a config file?"

**Largely yes, and the config file is three lines.**

We'd rather you copied those three lines and never starred anything than have you believe there's
more machinery here than there is. It's MIT, no attribution required.

What we'd claim is genuinely non-obvious is not the fix but the *diagnosis*. A split prompt cache
produces **no error, no log line, and no alert** — it produces a larger bill. In our case the only
reason it surfaced at all was an unrelated second fault (one pool member didn't exist, so it
502'd). A pool whose members are all *valid* fails completely silently, indefinitely.

So the thing we think has standalone value is the **measurement harness**, not the config: a
token-weighted, cold-round-separated, denominator-normalised cache-hit benchmark that fails loudly
when it can't measure, and which you can point at any Anthropic-format `/v1/messages` endpoint.
That plus the source-level writeup is the deliverable. The YAML is a footnote to it.

And to be explicit about the layering: **the Go engine that does every hard thing here —
speaking five wire formats, OAuth credential management, failover — is CLIProxyAPI, MIT, and it
is not ours.** Take that away and there is no project.

---

## 2. "Is this against Google's / your provider's Terms of Service?"

**Answering the sharp version of this question rather than the comfortable one.**

There are two very different deployments and they have opposite answers.

**A public, shared endpoint backed by a pool of personal free-tier accounts: yes, that is a real
problem, and we do not defend it.**

That is account sharing. Essentially every major provider prohibits it in some form. The
realistic sequence, in the order it actually happens:

1. Traffic from one residential IP that looks like dozens of unrelated users trips automated
   abuse detection.
2. The accounts get rate-limited, then suspended — **not the service, the accounts**, including
   everything else those Google / OpenAI / xAI logins are attached to.
3. Bans can follow the human, not just the credential. "I was proxying my personal account to the
   internet" is not an appeal that goes well.

The people hurt worst are whoever lent you an account. Don't put someone else's personal account
behind a public endpoint, even if they agreed, even if they're family.

**The project's documented recommendation is BYOK, or paid keys on a plan whose terms permit
serving third parties.** That is written down in
[`docs/OPERATING_A_PUBLIC_SERVICE.md`](../OPERATING_A_PUBLIC_SERVICE.md), whose pre-launch
checklist opens with "no personal OAuth account is in the pool", and the gateway **refuses to
start** in a configuration with public registration enabled and no spend ceiling.

**Localhost, against your own account: this is ordinary client configuration.** You're making the
same API calls to the same provider on the same credential you were already using; a local process
is reformatting the request. That's what every SDK does. This is the configuration everything in
the repo was measured against and the one it's written for.

**Where we genuinely don't know:** whether any *specific* provider's terms permit any *specific*
BYOK reselling arrangement. We're not lawyers, this isn't legal advice, and terms differ by
provider, plan and region. If you're building something commercial, read your provider's terms on
proxying, resale and multi-user access — and if they're ambiguous, ask in writing **before** you
launch, not after.

---

## 3. "Why not just use OpenRouter or LiteLLM?"

**For most people: do.** Genuinely. There's a full table in
[`comparison.md`](comparison.md) where we lose rows on ecosystem maturity, provider breadth, live
web search and bus factor.

Short version of when each is right:

- **OpenRouter** — you want one key, zero setup, enormous model breadth, and you're fine with a
  third party seeing your prompts. Almost always the right answer for getting started.
- **LiteLLM** — you want a mature, well-staffed self-hosted gateway with real observability,
  budgets, SSO and 100+ providers. If you need a *product*, this is the one.
- **Direct API** — fewest moving parts, one less thing to fail, the provider's own SLA. If you
  don't need multi-provider routing, this is the correct default and everything else is overhead.
- **This** — narrow. You want traffic to stay on your machine, you're running long agent sessions
  where cache behaviour dominates cost, and you want to *prove* the cache is working rather than
  assume it.

One thing worth separating: **the benchmark is not tied to our stack.** It needs an
Anthropic-format `/v1/messages` endpoint that reports usage, so you can point it at LiteLLM, at
OpenRouter, or at a local llama.cpp server. If you use one of those, we'd still like to see the
number.

**What we will not claim:** we have *not* tested whether LiteLLM or OpenRouter have equivalent
cache-splitting behaviour. The bug documented here is specific to CLIProxyAPI 7.1.23. Do not read
it as a claim about anyone else's routing code.

---

## 4. "Your 99.53% is cherry-picked."

**It is selected, yes — and every axis of selection is disclosed. Here's the full picture so you
can pick your own number.**

Three things make that figure look better than a naive reading suggests:

**(a) It excludes the cold round.** Round 1 of any session is a cache *write*: 0% by construction,
not by failure. Every session you ever start pays exactly one.

| What you include | This run's number |
|---|---|
| Warm rounds 2–4, token-weighted | **99.53%** |
| All 4 rounds including the cold write | **74.6%** |

Check us: `2,236,290 / 2,995,762 = 0.7465`.

**(b) It's prefix-size dependent, and that's the interesting part.** The uncached tail is roughly
*constant* (~3.5K tokens at a 749K prefix) because it's the conversation growth since the last
request, not a fraction of the prompt. So the ratio necessarily improves as the prefix grows:

| Measured prefix | Warm token-weighted hit rate |
|---|---|
| ~749K tokens | 99.53% |
| ~91K tokens | 94.00% |
| ~30K (tool default) | does not reach 99% |

**Do not quote 99.53% as a universal number.** It's what this upstream's cache granularity does at
that prompt size.

**(c) The comparison figure is not a measurement.** The "~50%" pool ceiling is reasoned from the
source, never measured. See question 5's sibling below.

**The criticism we'd make that's stronger than the usual one:** including the cold round makes the
headline a function of `--rounds`. Run 40 rounds and "the" hit rate climbs toward 97% having
measured nothing new about the cache — it measures your choice of `--rounds`. Neither framing is
honest alone, which is why the tool prints per-round prompt size, cached count, and the cold round
on separate lines rather than emitting one headline percentage.

**Which number should you use?** If your workload starts a fresh conversation per task, the warm
number is nearly irrelevant to you — you pay a cold write every time. Prompt caching is a
long-session optimisation and this benchmark measures long sessions deliberately.

---

## 5. "Single machine, single run. That's not a benchmark."

**Correct.** One sample, one Windows 11 laptop, one afternoon (2026-07-21), no repetitions, no
confidence intervals, no isolation from provider-side load, consumer broadband, no cross-provider
comparison. [`BENCHMARK.md`](../BENCHMARK.md) lists the confounds we know about and didn't control
for, and the latency column is explicitly labelled an anecdote rather than a result — because in
that single run, two warm rounds were *slower* than the cold round while reading 99.5% of their
prompt from cache.

**One narrow push-back.** For the cache-hit figure specifically, the quantity is a ratio of two
token counts that the upstream itself reports, both taken from the same HTTP response. It is far
less sensitive to machine, CPU and network variance than a latency benchmark would be. That
doesn't make n=1 sufficient — it makes it a *different kind* of insufficient than it would be for
a throughput claim.

**And the related one: the "~50% pool ceiling" is not measured at all.** It is a ceiling argument
from the source: with a two-member rotation, at best every other request can read the cache entry
its predecessor wrote. We never ran a controlled pool-vs-direct A/B, because by the time we
understood the mechanism the pool config had a second unrelated fault (a member that didn't exist
on that provider build, 502ing ~half of all requests), so any A/B would have measured that fault
instead. Rebuilding a known-broken config to benchmark it meant paying for several million
upstream tokens to confirm something already legible in the source.

**This is why the tool ships instead of just its output.** Upstream providers change caching
granularity, quotas and routing without notice; a number measured in July 2026 may not survive to
August. A run that contradicts us is more interesting to us than one that confirms us, and we'll
put it in the README with your name on it.

---

## 6. "What happens when your free accounts get banned?"

**The premise deserves correcting first, and then the real question underneath it deserves a real
answer.**

The correction: **this project ships no accounts and provides no credits.** There is no pool of
"our" free accounts anywhere in the repository — no credential, no account name, no hardcoded key
(CI greps every commit for key-shaped strings). Every token you spend bills to an upstream account
*you* configured. In the localhost configuration everything was measured against, there's nothing
of ours in the path at all.

The real question underneath: **if you build a public service on pooled personal accounts, what
happens?** They get banned. See question 2 — that's not a risk to be managed, it's the documented
outcome, and it's why the recommendation is BYOK or licensed paid keys.

**And the honest structural version, which is the one worth worrying about:** any project in this
category is downstream of provider decisions nobody here controls. A provider can change caching
behaviour, tighten quotas, alter OAuth channels, or make a model alias vanish — without notice and
without recourse. That's true of this project, of every other proxy, and of anything built on a
free tier anywhere.

Our answer to that isn't a promise, it's a design stance: **the durable artifact here is the
measurement tool and the writeup, not a service.** If every upstream in it stopped working
tomorrow, `cache_bench.py` would still tell you whether *your* stack is wasting *your* cache, and
the conductor.go finding would still be true of that version of that engine. We deliberately built
the thing that survives its dependencies rather than the thing that depends on them.

---

## 7. "Why should I trust a `curl | sh` one-liner?"

**You shouldn't, and you don't have to.**

The general objection is correct and we're not going to talk you out of it: piping a URL into a
shell executes whatever that URL serves at the moment you run it, and it's a real supply-chain
concern.

What the installer does about it:

- **Refuses to run as root.** Uses no `sudo`.
- **Writes only** under `$HOME/.yangble5` and `$HOME/.local/bin`.
- **Does not edit** `.bashrc`, `.zshrc`, `.profile`, or your `PATH`.
- **Does not touch your existing Claude Code login.** The launcher uses a separate
  `CLAUDE_CONFIG_DIR`, so your Anthropic account, `~/.claude`, and your existing subscription are
  untouched. Run plain `claude` and you get your normal setup, unchanged.
- **Downloads and executes no second artifact.** The only network traffic is JSON to and from the
  API. Because nothing executable is ever fetched, there's no second binary to SHA256-pin — and
  the script says in its own header that if a future version *does* need to fetch a component, it
  must hardcode and verify that component's SHA256 before executing it.
- **Ships `--dry-run`**, which prints every action it would take and changes nothing.
- **Writes an uninstaller** that removes everything it created.
- **Collects no** name, email, MAC address, serial number, or file contents.

**What we'd actually recommend:** download it, read it, then run the local copy. It's POSIX `sh`
and it's written to be read — the header explains itself in plain language, including a note
addressed to AI agents with shell access telling them to read it to the human first.

**Or skip it entirely.** The tools in `tools/` are standard-library-only Python. You can copy a
single file onto a box and run it. The installer is a convenience, not a dependency.

---

## 8. "Is my prompt data being logged?"

**In the localhost configuration: nothing in this repository logs prompts or completions, and
nothing of ours is in your network path.** Your requests go from your machine to whatever upstream
you configured, on your own credential. There is no telemetry, no phone-home, no analytics.

The provider still sees your prompts, obviously — they're the ones running the model. That's true
of any client and isn't something a proxy can change.

**If you run the optional public gateway**, the posture is documented and deliberate:

- **Never log prompts or completions.** Not for debugging, not temporarily. The operating guide
  states this as a rule rather than a default, because people paste private repositories into
  coding agents and that content must not survive in disk logs.
- **Issued API keys are stored hashed** (scrypt, per-key salt, server-side pepper). There is no
  code path that can print a user's key after issuance — plan for "regenerate", not "recover".
- **IP addresses are stored as salted hashes.** Abuse detection needs distinct-IP counts, not a
  location log of everyone who used your service.
- **Client credentials are stripped at the edge.** `Authorization` and `x-api-key` from the caller
  are dropped before the request goes upstream.

**The honest limits of that assurance**, from [`SECURITY.md`](../../SECURITY.md): there is **no
end-to-end encryption and no per-user isolation at rest** — all users share one SQLite database.
And if you run a public instance, *you* become the data controller and it's your obligation to
disclose what you retain. We can tell you what our code does; we can't tell you what an operator
you've never met has deployed.

**Trust posture we'd suggest generally:** don't take our word for any of the above. It's MIT and
it's a small amount of readable Python. `grep` it for logging calls yourself — that's a faster and
more reliable answer than anything we can say here.
