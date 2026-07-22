# Honest comparison: yangble5 vs LiteLLM vs OpenRouter vs direct API

## How to read this, and what it's worth

**A comparison table written by an author who wins every row is worth nothing.** We lose most of
the rows below, and the rows we lose are more decision-relevant for most readers than the ones we
win. If you read only one section, read
[Where yangble5 loses](#where-yangble5-loses-read-this-part-first).

**Epistemic status, stated up front because it constrains everything here:**

- Claims about **yangble5** are measured or reasoned, labelled as such, and reproducible with
  tooling in this repo.
- Claims about **LiteLLM, OpenRouter and direct API use** are read from their **public
  documentation and general availability as of 2026-07-21**. We have **not** benchmarked them,
  have **not** run the cache benchmark against them, and do **not** claim any of them exhibits the
  cache-splitting behaviour documented in [`FINDINGS.md`](../FINDINGS.md). That bug is specific to
  **CLIProxyAPI 7.1.23**, which is the engine *this* project sits on.
- Feature sets in this space change monthly. **If a cell about someone else's project is wrong,
  it's a bug — open an issue and we'll fix it.** We would rather be corrected than flattered.
- We have deliberately **not** put competitor performance numbers in this table, because we have
  not measured them and inventing them would be worse than omitting them.

---

## The short version

| If you… | Use |
|---|---|
| Are getting started, or want maximum model choice with zero setup | **OpenRouter** |
| Need a production multi-provider gateway with budgets, observability, SSO | **LiteLLM** |
| Only use one provider and don't need routing | **Direct API** — fewest moving parts, and everything else is overhead |
| Run long agent sessions, want traffic to stay local, and want to *prove* your cache works | **yangble5** — and read the losses below first |

Nothing here is exclusive. The most sensible combination for a lot of people is *direct API for
production, yangble5's benchmark pointed at whatever they already run*.

---

## Where yangble5 loses (read this part first)

| Dimension | yangble5 | LiteLLM | OpenRouter | Direct API |
|---|---|---|---|---|
| **Provider / model breadth** | 🔴 **Worst.** Whatever CLIProxyAPI supports. We measured exactly **one** upstream (Gemini via `antigravity`) on **one** day. | 🟢 100+ providers (their docs) | 🟢 Hundreds of models, one key | 🟡 One provider, all of its models |
| **Ecosystem maturity** | 🔴 **Worst.** Days old. No production users we know of. No track record. | 🟢 Large team, heavy adoption, long history | 🟢 Established hosted service | 🟢 The provider itself |
| **Bus factor** | 🔴 **1.** Single maintainer, first open-source release. If we lose interest, it stops. | 🟢 Company + large contributor base | 🟢 Funded company | 🟢 The provider |
| **Live web search** | 🔴 **None.** Measured 2026-07-21: asked the year, Gemini said **2024**, Grok said **2025**. Pure parametric recall. | 🟡 Passes through provider tools | 🟢 Search-capable models / plugins available | 🟢 Native grounding / search tools |
| **Setup time** | 🔴 Bring your own engine binary, authenticate OAuth, write config, verify. Slowest here by a distance. | 🟡 Deploy a service, configure it | 🟢 One API key, minutes | 🟢 One API key, minutes |
| **Observability / dashboards** | 🔴 A sidecar that writes `stats.json`. That's it. | 🟢 Dashboards, callbacks, logging integrations | 🟢 Hosted dashboard and usage UI | 🟡 Provider console |
| **Enterprise features** (SSO, RBAC, audit, teams) | 🔴 **None.** | 🟢 Yes | 🟡 Org/team features | 🟡 Varies |
| **Benchmark rigour of published claims** | 🔴 **n=1.** One machine, one run, one afternoon, no error bars, no cross-provider comparison. | ⚪ Not directly comparable | ⚪ Not directly comparable | ⚪ n/a |
| **Hosted option** | 🔴 **One, and it is a hobby instance.** The maintainer runs a public instance at [yangble5.com](https://yangble5.com); registration is **open** — `POST /auth/register` issues a key to anyone who asks. The tokens are billed to the **operator's own personal upstream accounts**, the 1M-context tier is served by **exactly one** personal OAuth credential, and there is **no SLA, no support, no company and no uptime commitment**. Treat it as a demo that can vanish. The repo also *ships* the gateway and landing page so **you** can become the operator instead — with the bill, the abuse reports and the provider terms. See [`OPERATING_A_PUBLIC_SERVICE.md`](../OPERATING_A_PUBLIC_SERVICE.md). | 🟡 Self-host or their cloud | 🟢 Fully hosted | 🟢 Fully hosted |
| **Breadth of tested configurations** | 🔴 One OS (Windows 11), one engine version (7.1.23), one upstream channel. | 🟢 Broad | 🟢 Broad | 🟢 Broad |
| **Failure modes you inherit** | 🔴 Ours **plus** CLIProxyAPI's — a third-party engine we don't control and didn't write. | 🟡 Theirs | 🟡 Theirs | 🟢 Fewest — one hop |
| **Documentation breadth** | 🟡 Deep on a narrow topic; nonexistent outside it. | 🟢 Broad | 🟢 Broad | 🟢 Broad |

That's **eleven rows where yangble5 is the worst option in the table**, several of them decisive.
For the majority of readers, one of the other three columns is the correct answer.

---

## Where yangble5 is different, or better

| Dimension | yangble5 | LiteLLM | OpenRouter | Direct API |
|---|---|---|---|---|
| **Ships a standalone prompt-cache hit-rate benchmark** | 🟢 Token-weighted, cold round separated, denominator normalised across provider conventions, fails loudly when it can't measure. Runs against **any** Anthropic-format `/v1/messages` endpoint — including the other three columns. | 🟡 Exposes cache metrics; we did **not** evaluate whether they answer this question. Tell us if they do. | 🔴 Not to our knowledge | 🔴 You build it |
| **Do your prompts leave your infrastructure?** *(self-hosted / BYOK / localhost)* | 🟢 No third party — you → your own upstream | 🟢 Self-hosted: no third party | 🔴 By design, they see your traffic | 🟢 No third party |
| **Do your prompts leave your infrastructure?** *(using the maintainer's instance at yangble5.com)* | 🔴 **Yes.** The operator is a third party in your prompt path and can read every request. Same trust question as any hosted proxy — it is not privileged by being open source. If that is unacceptable, run your own or use BYOK. | ⚪ n/a | 🔴 Same, by design | ⚪ n/a |
| **Documented source-level cache-splitting finding + fix** | 🟢 With line-level references and a `strings` check you can run on your own binary | ⚪ n/a | ⚪ n/a | ⚪ n/a |
| **Long-agent-session cache tuning documented** | 🟢 Session affinity, TTL, `fill-first` rationale, and why the engine's shipped defaults are cache-hostile | 🟡 Configurable; less written about this specific failure | ⚪ Abstracted away | 🔴 Your problem |
| **Client-side 1M context unlock documented** | 🟢 `CLAUDE_CODE_MAX_CONTEXT_TOKENS`, Codex `model_context_window`, with the caveat that it doesn't create context | ⚪ Out of scope | ⚪ Out of scope | ⚪ Out of scope |
| **Compatibility shim for the streaming `system`-role 400** | 🟡 Yes — but it's a **workaround for engine < 7.2.93** and should be deleted on upgrade | ⚪ n/a | ⚪ n/a | ⚪ n/a |
| **Cost per token** | ⚪ Whatever your upstream charges | ⚪ Same | 🟡 Upstream + their margin | 🟢 Upstream, no markup |
| **License** | 🟢 MIT | 🟢 Open source | 🔴 Hosted service, not self-hostable | ⚪ n/a |

Note how few rows this table has, and that the strongest one is a **measurement tool that works
against the competition**. That's the honest shape of this project: it is narrow, and its most
portable contribution is not the proxy config.

---

## The comparison we did not run, and should have

The obvious experiment — **run `cache_bench.py` against LiteLLM, OpenRouter and a direct
Anthropic endpoint at matched measured prompt sizes and publish all four numbers** — is one we
have **not** done. It's the single most useful thing anyone could contribute, including if the
result is unflattering.

If you run it, hold these equal or the comparison means nothing:

- **measured** prompt size (adjust `--prefix-tokens` per provider until reported `prompt_total`
  matches — tokenizers differ, so equal `--prefix-tokens` is *not* equal prompt size)
- `--rounds`, `--max-tokens`, `--interval`
- one session id for the whole run
- report the cold round separately for every provider
- state which `input_tokens` convention each provider used (see
  [`BENCHMARK.md` §4](../BENCHMARK.md))

A provider that reports no cache fields at all is **not scoring 0%** — it's unmeasurable on that
path, and the tool says so rather than printing a zero next to a green checkmark.

---

## Things that would make us the wrong choice

Stated plainly, because the fastest way to lose someone's trust is to let them find this out
themselves after a weekend:

- **You need current information.** There's no live web search. Measured: Gemini said 2024, Grok
  said 2025. Use something else.
- **You need many providers.** We measured one. LiteLLM and OpenRouter are not close competitors
  here; they're categorically better.
- **You need someone to call.** Single maintainer, no support, no SLA, no company.
- **You're deploying to production this quarter.** The published measurements are n=1 and days
  old. That's not a foundation to build a business on yet.
- **You want a hosted endpoint you can rely on.** One exists —
  [yangble5.com](https://yangble5.com), run by the maintainer, registration open — but it is a
  single-maintainer hobby instance with no SLA, no support and no company behind it, and its
  1M-context tier is served by **exactly one** personal OAuth credential. It is also precisely
  the shape of deployment [`OPERATING_A_PUBLIC_SERVICE.md`](../OPERATING_A_PUBLIC_SERVICE.md)
  §1 warns you not to build: a public endpoint funded by the operator's own personal accounts.
  That warning is not retracted by the fact that the author ignored it — read it before you copy
  the pattern, and do not build a business on the demo. If you want a hosted endpoint with
  uptime and a support contract, use OpenRouter or the provider directly.
- **Your sessions are short.** Prompt caching is a long-session optimisation. If you start a fresh
  conversation per task you pay a cold write every time and the warm hit rate is nearly irrelevant
  to you.
- **You're on CLIProxyAPI ≥ 7.2.93 and your cache is already fine.** Then take the three lines of
  YAML, delete the shim, run the benchmark once to confirm, and ignore the rest of this repo. That
  is a completely reasonable outcome and we'd consider it a success.

---

## One more disclosure

The engine underneath all of this — **[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)**,
MIT, by Luis Pater and Router-For.ME — does every genuinely hard thing in the yangble5 column:
speaking OpenAI / Gemini / Claude / Codex / Grok wire formats, OAuth credential management, and
failover. **We did not write it and this project is useless without it.** We do not redistribute
it; you bring your own binary.

The bug documented in [`FINDINGS.md`](../FINDINGS.md) is a bug in a specific version of a good
project, found by using it heavily. It is not a reason to avoid it, and the streaming-role bug
alongside it was **already fixed upstream in v7.2.93**. If this comparison is useful to you, go
star CLIProxyAPI before you star us.
