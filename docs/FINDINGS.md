# Findings

Everything below was produced on **2026-07-21**, on **one Windows 11 machine**, against
**CLIProxyAPI 7.1.23** (`cli-proxy-api.exe`, windows/amd64) with Gemini reached through the
engine's `antigravity` OAuth channel, plus a secondary xAI (`grok`) channel.

This document exists so a skeptical reader can check us. Each finding carries a status label:

| Label | Meaning |
|---|---|
| **Verified (source/binary)** | Read out of the engine's own source and confirmed present in the exact binary we ran. |
| **Measured** | A number our tooling recorded on a live request. The record is reproduced verbatim. |
| **Observed** | We saw it happen, without a repro script. Treat as an anecdote. |
| **Reasoned** | A conclusion argued from the two above. Not a measurement. Labelled as such every time. |

If you only read one section, read [Finding 1](#finding-1-a-two-member-model-pool-rotates-per-request-and-ignores-your-routing-policy).

**Contents**

1. [A two-member model pool rotates per request and ignores your routing policy](#finding-1-a-two-member-model-pool-rotates-per-request-and-ignores-your-routing-policy) - Verified (source/binary)
2. [99.53% token-weighted prompt-cache hit rate on warm rounds](#finding-2-9953-token-weighted-prompt-cache-hit-rate-on-warm-rounds) - Measured
3. [748,918 tokens ingested with no truncation](#finding-3-748918-tokens-ingested-with-no-truncation) - Measured
4. [Clients cap themselves at 200K unless you tell them not to](#finding-4-clients-cap-themselves-at-200k-unless-you-tell-them-not-to) - Verified (client docs) + Observed
5. [A mid-conversation system role 400s the streaming path only](#finding-5-a-mid-conversation-system-role-400s-the-streaming-path-only) - Verified (source) + Observed
6. [Measuring cache hit rate without lying to yourself](#finding-6-measuring-cache-hit-rate-without-lying-to-yourself) - Design

Appendix: [What we did not verify](#appendix-what-we-did-not-verify).

---

## Finding 1: a two-member model pool rotates per request and ignores your routing policy

**Status: Verified (source/binary). The ~50% ceiling that follows from it is Reasoned, not measured.**

### The symptom that started it

A long Claude Code session against a model alias backed by a two-member
`openai-compatibility` pool behaved as if the prompt cache did not exist. Cached-token counts
came back at or near zero on turns where nearly the whole prompt was byte-identical to the
previous turn. Roughly half the requests also failed outright with a 502. `routing.strategy`
and `session-affinity` were both configured, so on paper the session should have been pinned.

### The cause, in the engine's own code

In CLIProxyAPI 7.1.23, when **one alias maps to two upstream model names** inside an
`openai-compatibility` provider, the upstream model for each request is selected by a
**global rotating counter**, not by your routing configuration. The relevant symbols live in:

```
sdk/cliproxy/auth/conductor.go
    nextModelPoolOffset(...)                   # increment-and-return counter
    modelPoolOffsets                           # the offset state
    openAICompatModelPoolKey(...)              # the map key: the POOL, not the session
    resolveOpenAICompatUpstreamModelPool(...)  # picks member[offset % len(members)]
    executeStreamWithModelPool(...)            # same selection on the streaming path
```

The offset is keyed by the **pool**. No session identifier, conversation id, credential id or
`metadata.user_id` participates in the selection. Two consequences follow directly:

* **`routing.strategy` is not consulted.** Whether you set `fill-first` or `round-robin`, the
  member choice is made by this counter.
* **`session-affinity` is not consulted.** Session affinity binds a session to a *credential*.
  It does not bind a session to a *pool member*. A session that is correctly pinned to one
  account still alternates between the two upstream model names.

So one conversation's consecutive requests land on different upstream models, forever, by
design of that code path - with both settings "correct".

### Verify it against the binary you are actually running

Do not take our word for which functions exist in your build:

```bash
# Windows / Git Bash / Linux - works on the shipped binary, no Go toolchain needed
strings cli-proxy-api.exe | grep -E 'nextModelPoolOffset|modelPoolOffsets|conductor\.go'
```

On our `cli-proxy-api.exe` (7.1.23, windows/amd64, 43,395,584 bytes) every symbol came back
non-zero. The exact script we ran, and its exact output:

```python
# count_symbols.py
data = open("cli-proxy-api.exe", "rb").read()
for pat in [b"nextModelPoolOffset", b"modelPoolOffsets", b"conductor.go",
            b"openAICompatModelPoolKey", b"resolveOpenAICompatUpstreamModelPool",
            b"executeStreamWithModelPool"]:
    print(pat.decode(), "->", data.count(pat))
```

```
nextModelPoolOffset -> 3
modelPoolOffsets -> 1
conductor.go -> 1
openAICompatModelPoolKey -> 1
resolveOpenAICompatUpstreamModelPool -> 2
executeStreamWithModelPool -> 2
```

That establishes the code path is present in the artifact we measured. Then read the function
in the upstream source for **your** version - behaviour may differ outside 7.1.23.

### Why this destroys prompt caching

The load-bearing fact: **upstream prompt caches are scoped per model and per account.** They
do not live in this repo, and they do not live in the engine. The proxy cannot cache anything
on your behalf; all it can do is make sure consecutive requests in one conversation reach the
same cache.

Split one conversation across N upstream models and you split its cache N ways. With N = 2 and
strict alternation:

* Request *k* lands on member A. The prefix it can read from A's cache is at best the prefix
  written by request *k-2*.
* Everything the conversation added in the intervening turn - the last user message, the last
  assistant reply, the last tool result - is not in A's cache. It is in B's.
* So on every single request you pay a fresh write for two turns' worth of tokens instead of
  one, and you do it forever.

> **The ~50% figure is a ceiling argument, not a measurement.** With a two-member rotation, at
> most every other request can read the cache entry its predecessor wrote. We did **not** run a
> controlled A/B of pool-vs-direct hit rate: by the time we understood the mechanism the pool
> config was already known-broken for an unrelated reason (below), and rebuilding a known-broken
> configuration to benchmark it was not worth the upstream spend. What we measured is the
> *after* state: [99.53% warm](#finding-2-9953-token-weighted-prompt-cache-hit-rate-on-warm-rounds).
> If you want the *before* number, the tooling to produce it ships in this repo - we would
> genuinely like to see it.

### Two aggravating factors specific to our deployment

Both are worth knowing because they are easy to reproduce accidentally:

1. **The self-loop drops the session id.** Our pool's `base-url` pointed back at the same proxy
   (`http://127.0.0.1:8318/v1`), so a Claude-format request was translated to OpenAI format and
   re-entered the engine. The Claude-to-OpenAI translation maps only `user`; `metadata.user_id` -
   which is where Claude Code puts its session identifier - does not survive the hop. Session
   affinity then falls back to a hash of the first few messages, which can bind the same
   conversation to a **different account**, whose cache is cold. Two translation hops also cost
   CPU on every multi-hundred-thousand-token request.
2. **The second member did not exist.** Our pool's second entry, `gemini-3-pro-high`, was never
   registered by the `antigravity` provider on this build. Every request that rotated onto it
   returned **502 "unknown provider"**. Roughly half of all requests failed outright - which,
   incidentally, is what made the problem visible at all. A pool whose members are all valid
   fails *silently*: you just quietly pay double.

### The fix: a direct 1:1 alias on the provider channel

**Before** - one alias, two upstream names, self-referencing base URL:

```yaml
openai-compatibility:
  - name: "yang-pool"
    base-url: "http://127.0.0.1:8318/v1"    # self-loop back into this same proxy
    api-key-entries:
      - api-key: "${YANGBLE5_API_KEY}"
    models:
      - name: "gemini-pro-agent"
        alias: "yangble5"
      - name: "gemini-3-pro-high"           # rotated onto ~every other request; also 502s
        alias: "yangble5"
```

**After** - a direct alias on the provider channel:

```yaml
routing:
  strategy: "fill-first"        # NOTE: shipped default is "round-robin"
  session-affinity: true        # NOTE: shipped default is false
  session-affinity-ttl: "12h"   # NOTE: shipped default is "1h"

oauth-model-alias:
  antigravity:
    - name: "gemini-pro-agent"
      alias: "yangble5"
      fork: true                # keep the original name visible as well
```

The three `NOTE`s matter: CLIProxyAPI's shipped `config.example.yaml` defaults are
`round-robin` / `session-affinity: false` / `1h`, and those defaults are cache-hostile for long
agent sessions. Check for yourself:

```bash
grep -nE '^routing:|strategy:|session-affinity' config.example.yaml
#   strategy: "round-robin" # round-robin (default), fill-first
#   session-affinity: false # default: false
#   session-affinity-ttl: "1h"
```

**Why `fill-first` rather than `round-robin`:** the session-to-credential table is in memory.
After an engine restart it is empty, and every live conversation gets re-bound on its next
request. `fill-first` deterministically re-picks the first healthy credential - usually the same
account it had before, so the upstream cache is still warm. `round-robin` spreads restarts
across accounts and pays a cold write for each one. (Reasoned from the documented semantics of
the two strategies plus per-account cache scoping; we did not benchmark restart behaviour.)

**Why 12h:** the TTL is a sliding window on the session-to-account binding. One work day of a
single long session stays on one account, which is exactly the window over which a large cached
prefix is worth anything.

**What the direct alias buys, concretely:** one translation hop instead of two; the real Claude
Code session id survives to the credential-pinning logic; a single stable upstream model, so
there is exactly one cache to hit; and Gemini's `cachedContentTokenCount` surfaces to the client
as `cache_read_input_tokens`, which is the only reason any of this is measurable from outside.

---

## Finding 2: 99.53% token-weighted prompt-cache hit rate on warm rounds

**Status: Measured. Single machine, single run.**

Command:

```bash
python tools/cache_bench.py --model yangble5 --prefix-tokens 600000 --rounds 4
```

Per-request records as the engine reported them, captured by `tools/cache_stats_sidecar.py`
into `stats.json`. Nothing here is averaged, smoothed or reordered:

| Round | Prompt tokens | `cache_read` | Hit | Uncached tail | Round-trip (non-streaming) |
|---:|---:|---:|---:|---:|---:|
| 1 (cold) | 748,918 | 0 | 0.00% | 748,918 | 21,410 ms |
| 2 | 748,933 | 745,438 | 99.53% | 3,495 | 10,753 ms |
| 3 | 748,948 | 745,430 | 99.53% | 3,518 | 23,457 ms |
| 4 | 748,963 | 745,422 | 99.53% | 3,541 | 22,381 ms |

Warm token-weighted hit rate:

```
(745,438 + 745,430 + 745,422) / (748,933 + 748,948 + 748,963)
= 2,236,290 / 2,246,844
= 0.99530  ->  99.53%
```

Read this with the following in mind.

**It is warm-only, on purpose.** Round 1 is a cold cache write and is 0.00% by construction.
Every session you ever start pays exactly one of those. Folding it into this run gives
**74.6%**, and that number is a function of how many rounds you chose to run - run 40 rounds and
"the" hit rate climbs toward 97% having measured nothing new. We report the warm figure because
it is what a long session experiences after its first request, and we print the cold round
beside it so you can compute either. See
[BENCHMARK.md](BENCHMARK.md#5-why-the-cold-round-is-excluded).

**It is an upper bound for this harness, not a typical value.** The uncached tail is whatever
the conversation added since the previous request, and this harness adds **exactly 15 tokens per
round** (748,918 -> 748,933 -> 748,948 -> 748,963) - the most cache-favourable session shape
that can exist. A real agent turn appends a tool result, a file read, a diff or a test log:
hundreds to tens of thousands of uncached tokens, every turn, which pushes the ratio down. The
~3.5K uncached remainder we actually measured is already much larger than those 15 tokens,
because the upstream caches at a coarse granularity and adds fixed per-request overhead. Full
treatment of this confound in
[BENCHMARK.md §7](BENCHMARK.md#7-known-confounds).

**It is prefix-size dependent.** Because that uncached remainder is roughly *constant* rather
than proportional, it is a smaller fraction of a bigger prompt, so the hit rate **rises as the
prefix grows**. We observed that direction; the magnitude at any other prefix size is **not in
the released evidence set**, which contains exactly one run - the 748,918-token one above.
The tool's default (`--prefix-tokens 30000`) will not reach 99%. **Do not quote 99.53% as a
universal number** - it is what this upstream's cache granularity does at a ~749K prefix, with a
15-token-per-round tail, on one machine, once. If you want the number at your prefix size, run
the tool at your prefix size; that is why it ships.

**Latency did not improve predictably, and we are not going to pretend otherwise.** Round 2 was
roughly 2x faster than the cold round (10,753 ms vs 21,410 ms). Rounds 3 and 4 were *slower than
the cold round* (23,457 ms and 22,381 ms) while reading 99.53% of their prompt from cache -
**two of the three warm rounds were slower than cold.** No latency-improvement claim is
supportable from this run. Every figure in that column is also a **complete non-streaming round
trip**, not time-to-first-token: the harness sends `stream: false`, and TTFT was never measured
anywhere in this repository. Single run, no repetitions, shared upstream, zero control over
provider-side load. Prompt caching reduces **cost** predictably; on this evidence it does not
reduce **wall-clock time** predictably. Treat the latency column as an anecdote.

**`--prefix-tokens` is a target, not a count.** The generator sizes the corpus at ~30 tokens per
line; the live tokenizer counted ~37. `--prefix-tokens 600000` therefore produced a
748,918-token prompt. The tool always reports the number the upstream returned, never its own
estimate.

---

## Finding 3: 748,918 tokens ingested with no truncation

**Status: Measured (ingestion). Recall over that context: not measured.**

The cold round of the run above carried a **748,918-token** prompt and came back with a normal
completion and a normal `usage` block. The previous local record on this setup was 712K.

Be precise about what that does and does not show:

* **It does show** the whole prompt reached the upstream and was counted by it. The 748,918
  figure is the upstream's own `promptTokenCount`, relayed by the engine as `input_tokens` - not
  a client-side estimate. Nothing in the client, the shim or the engine silently dropped
  content, and no error was raised.
* **It does not show** that the model attended to, or can recall, any particular fact from that
  context. We did not run a needle-in-a-haystack retrieval test. If your workload depends on
  recall at 700K, measure that separately; token count is not comprehension.
* **It does not show** that 1,000,000 works. We measured 748,918. The next number we can
  honestly put in a table is the next one we measure.

Related, and going the other way: on this same setup the `grok-4.5` alias **appears to accept
prompts beyond roughly 256K and silently drop content rather than erroring** - *Observed, no
repro script, treat as unverified*. It is called out because silent truncation is the worst
possible failure mode for a coding agent, and because it is a reminder that a proxy cannot merge
context windows: pooling two 256K models does not give you 512K.

---

## Finding 4: clients cap themselves at 200K unless you tell them not to

**Status: Verified against client documentation; effect Observed in use.**

Your alias is, by construction, a model name no client has ever heard of. Clients guess a
conservative context window for unrecognized names, and then act on the guess.

**Claude Code** assumes a 200K window for unrecognized model names and begins auto-compacting
the conversation long before the real window is reached - so a 1M-context upstream behaves like
a 200K one, and every compaction is *also* a cache-destroying prompt rewrite. The official
environment variable (Claude Code v2.1.193 and later, listed in the environment-variables
reference at `code.claude.com/docs/en/env-vars`) moves the boundary:

```bash
export CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000
export ANTHROPIC_BASE_URL=http://127.0.0.1:8320   # shim; use :8318 with engine >= 7.2.93
export ANTHROPIC_AUTH_TOKEN="$YANGBLE5_API_KEY"
export ANTHROPIC_MODEL=yangble5
```

**Codex**, in `config.toml`:

```toml
model = "yangble5"
model_provider = "yangble5"
model_context_window = 1000000
model_max_output_tokens = 65536

[model_providers.yangble5]
base_url = "http://127.0.0.1:8318/v1"
env_key = "YANGBLE5_API_KEY"
wire_api = "chat"
```

**The honest caveat.** Declaring a larger window does not create context. It only moves the
point at which your client decides to compact. If the upstream cannot actually take what you
then send it, you have traded early compaction for a truncation or an error - which is why this
finding is inseparable from Finding 3: verify with `cache_bench.py --prefix-tokens N` that
prompts the size you are claiming come back with a matching `input_tokens`. We verified 748,918.
We did not verify 1,000,000.

---

## Finding 5: a mid-conversation system role 400s the streaming path only

**Status: Verified (source-level, both the bug and the upstream fix). Symptom Observed.**

### Symptom

Claude Code sessions against the `antigravity` channel failed with:

```
400  Request contains an invalid argument
```

"Intermittently" is the word everyone reaches for, and it is wrong. The *same* conversation
would work and then fail one turn later, with no change in prompt size, model or account.

### Cause

CLIProxyAPI 7.1.23's antigravity **streaming** translator passes `messages[].role` through
verbatim; it rewrites `assistant` to `model` and leaves everything else alone. Claude Code 2.1.x
and later, with the mid-conversation-system beta active, injects a message with
`role: "system"` in the *middle* of the `messages` array (the Agent-tool agent list). Gemini's
`streamGenerateContent` rejects that role outright.

### Why it hid

The **non-streaming** `generateContent` path tolerates the same role. So the failure tracked
*transport*, not content: whenever a turn happened to go non-streaming it succeeded, and the
moment the same conversation streamed, it 400'd. Anyone bisecting by *prompt content* - the
obvious first move when an error says "invalid argument" - finds nothing, because the content is
irrelevant. That is the entire reason this cost real time to find.

### How to reproduce and bisect it yourself

This is the procedure that isolates it, and it is the one to reuse for the next bug of this
shape:

1. Capture the exact failing request body (the engine logs the request; or point your client at
   a recording proxy).
2. Replay that body twice with `curl`, changing **only** `"stream": true` to `false`. If
   non-streaming succeeds and streaming 400s on byte-identical content, the bug is in the
   streaming translation path, not in your prompt. Stop bisecting content at this point - most
   people do not, and that is where the afternoon goes.
3. Now bisect the `messages` array: halve it, replay streaming, keep whichever half still 400s.
   Converge on a minimal reproducer. Ours reduces to three messages, the middle one carrying
   `"role": "system"`.
4. Confirm by flipping that one role to `"user"` and replaying. It passes.

### Fix

Upstream fixed this in **v7.2.93** by mapping `system` to `user` in
`internal/translator/antigravity/claude/antigravity_claude_request.go`.
`tools/claude_shim.py` applies that exact mapping in front of an older engine.

The shim's one non-obvious safety property, and the reason it is safe to leave in the path:

```python
# tools/claude_shim.py
if b'"system"' not in body:
    return body        # byte-identical passthrough
```

A body that does not need fixing is forwarded **byte for byte**. Re-serialising an untouched
body would change whitespace, and the upstream prompt cache keys on exact bytes - a "harmless"
JSON round-trip on every request would have quietly cost us the entire result in Finding 2.
This behaviour is covered by `tests/test_claude_shim.py`.

**Retire the shim** the moment you are on engine >= 7.2.93: point `ANTHROPIC_BASE_URL` back at
the engine port and delete the file. It holds no state and nothing depends on it.

---

## Finding 6: measuring cache hit rate without lying to yourself

**Status: Design. The failure modes below are real ones we hit.**

Cache hit rate is unusually easy to report dishonestly, mostly by accident. Four decisions in
the tooling, and why each one is the way it is:

**1. The benchmark never reads the management queue.** The engine's
`/v0/management/usage-queue` is **consume-on-read**: records are deleted as they are handed out.
Two readers therefore *split* the stream and both report confidently wrong numbers. So
`cache_bench.py` takes usage off each HTTP response it made itself, and
`cache_stats_sidecar.py` is the single queue consumer. The sidecar binds a loopback port
(`--lock-port`, default 8319) as a single-instance lock, so a second copy fails loudly instead
of silently halving everyone's stats.

**2. Token-weighted, not the mean of per-round ratios.** A 700K-token round and a 200-token
round are not equally important. Averaging ratios lets a handful of trivial requests drag the
headline figure around by tens of points - in either direction, which is worse than useless.

```python
def token_weighted_hit_rate(rounds):
    return sum(r["cache_read"] for r in rounds) / sum(r["prompt_total"] for r in rounds)
```

**3. The denominator is normalised across two incompatible upstream conventions.** Providers
disagree about whether `input_tokens` already includes the cached read, and the disagreement is
invisible in the payload:

* Verified live on 2026-07-21: CLIProxyAPI relays Gemini's `promptTokenCount` straight to
  `input_tokens`, and it **already includes** the cached portion (`input >= cache_read`).
* The pure Anthropic convention is the opposite: `input_tokens` counts only the *uncached*
  remainder, so `cache_read` routinely exceeds it.

Dividing by raw `input_tokens` gives either the right answer or a nonsensical rate above 100%,
depending on who answered. `prompt_denominator()` takes `input` when it is already the larger of
the two and `input + cache_read` when it is not - identical under both conventions, and
incapable of reporting more than 100%.

**4. The benchmark is built to fail loudly.** If every round returns zero cached tokens, it says
so in plain language and tells you to raise `--prefix-tokens`, rather than printing `0.00%` next
to a green checkmark. Some upstreams do not expose cache accounting on this path at all; on
those the 99% goal is simply unreachable, and the correct output is to report that. Exit codes:
`0` at or above `--target`, `1` below it or if no warm rounds ran, `2` if the measurement never
happened (transport/HTTP/decode failure) - so CI cannot mistake a broken run for a bad score.

The deterministic corpus matters too: the prefix is generated from an index, with no uuid,
timestamp or dict iteration order anywhere in it. Anything random in the prefix would invalidate
the cache on every round and make a *working* cache look broken - or, if you only ever tested
that way, make a broken cache indistinguishable from a working one.

---

## Appendix: what we did not verify

Listed so nobody has to guess where the edges are.

* **No pool-vs-direct A/B.** The ~50% ceiling in Finding 1 is reasoned from source, not measured.
* **No repetitions, no error bars.** One run per configuration, one machine, one afternoon.
* **No cross-provider comparison.** Every number here is Gemini via `antigravity`. We do not know
  what the same script reports against OpenAI, Anthropic direct, or a local model - running it
  there is one command, and we would like to see the output.
* **No restart-behaviour benchmark.** The `fill-first` argument is reasoned from documented
  strategy semantics plus per-account cache scoping.
* **No recall test at long context.** Ingestion of 748,918 tokens is measured; comprehension is
  not.
* **No web search, at all.** Nothing routed through this proxy performs a live web search.
  Measured 2026-07-21: asked what the current year was, the Gemini upstream answered **2024** and
  the Grok upstream answered **2025**. Everything you get back is parametric recall behind a
  training cutoff.
* **Upstream behaviour is not a constant.** Providers change caching granularity, quotas and
  model routing without notice. A number measured in July 2026 may not survive to August. That is
  the reason the measurement tool ships in this repo instead of just its output.