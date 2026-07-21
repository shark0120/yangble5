# Reddit

Two variants. They are **not** the same post with the subreddit name swapped — the audiences
fail differently, so the honest disclosures that matter are different in each.

Read each subreddit's rules the week you post; several have self-promotion windows, mandatory
flair, or a "no blogspam" rule that a repo link can trip.

**Do not cross-post the same text to both.** Reddit surfaces it, and both audiences overlap
enough to notice.

**Post one, wait a day, read what got contested, then post the other with that fixed.**

---

## Variant 1 — r/LocalLLaMA

**Read this first:** r/LocalLLaMA is a *local inference* subreddit and this project talks to
cloud upstreams. If you bury that, the top comment will be "this isn't local" and it will be
correct. So it goes in the second sentence, and the actual offer to this audience is the
**provider-agnostic benchmark**, which does run against llama.cpp / vLLM / TGI. Lead with the
tool, not the stack.

**Flair:** Resources or Tutorial | Discussion (check current options — flair is enforced)

### Title

```
I found my LLM proxy was splitting one conversation's prompt cache across two upstreams — here's the benchmark that caught it
```

Shorter alternate if the above gets truncated in the feed:

```
A prompt-cache hit-rate benchmark you can point at any /v1/messages endpoint (found a real bug in mine)
```

### Body

Upfront, because it matters here: **the numbers below are from cloud upstreams, not local
models.** The part I think is useful to this sub is the measurement tool, which is stdlib-only
Python and works against anything that speaks Anthropic-format `/v1/messages` and reports usage
— llama.cpp's server, vLLM, TGI, a LiteLLM instance, whatever you already run. I have not run it
against a local backend, and I'd like to see what it says when someone does.

**The bug.** I was running long agent sessions through CLIProxyAPI (MIT Go proxy, not mine) and
the prompt cache appeared to do nothing — cached-token counts near zero on turns where the prompt
was almost byte-identical to the previous one.

It's in the source. In 7.1.23, when one model alias maps to **two** upstream model names in an
`openai-compatibility` pool, the upstream is picked per request by a global rotating counter —
`nextModelPoolOffset` in `sdk/cliproxy/auth/conductor.go`, with the offset keyed by the *pool*.
No session id, conversation id or credential id is in that key. Which means:

- `routing.strategy` isn't consulted for member selection
- `session-affinity` isn't either — it pins a session to a *credential*, never to a pool member

I had both set. Neither did anything. And since upstream prompt caches are scoped **per model per
account**, one conversation alternating across two upstream models has its cache split in half,
forever, with no error and no log line. Mine was only visible because the pool's second member
didn't exist on that provider and 502'd — a pool whose members are all *valid* fails completely
silently and you just quietly pay double.

Check your own binary without a Go toolchain:

    strings cli-proxy-api.exe | grep -E 'nextModelPoolOffset|modelPoolOffsets|conductor\.go'

Also worth knowing regardless of what proxy you run: that engine's shipped defaults are
`round-robin` + `session-affinity: false` + `1h` TTL. All three are cache-hostile for long agent
sessions. Fix was a direct 1:1 alias on the provider channel, `fill-first`, affinity on, 12h.

**What I measured after, single run, one machine:** 99.53% token-weighted cache hit rate on warm
rounds at a 748,918-token prompt, ingested with no truncation.

    round 1 (cold): prompt 748,918  cached       0   0.00%  21,410 ms
    round 2:        prompt 748,933  cached 745,438  99.53%  10,753 ms
    round 3:        prompt 748,948  cached 745,430  99.53%  23,457 ms
    round 4:        prompt 748,963  cached 745,422  99.53%  22,381 ms

**Caveats, because this sub will find them anyway and I'd rather say them:**

- **Warm rounds only.** Round 1 is a cold write, 0% by construction, and every session pays one.
  Fold it in and this run is 74.6% — which is really just a function of how many rounds I ran.
- **Upper bound, not a typical value.** My simulated conversation grows by exactly 15 tokens per
  round — the most cache-favourable shape possible. A real agent turn appends a tool result or a
  diff, which is orders of magnitude bigger and uncached. Expect lower.
- **Prefix-size dependent.** The uncached remainder is roughly *constant* (~3.5K tokens), not
  proportional, so the ratio rises as the prefix grows. Direction observed; I'm not publishing a
  magnitude for other prefix sizes because the released evidence is one run at 748,918 tokens.
  The 30K default won't hit 99%. Don't quote 99.53% as a universal number.
- **The "~50% ceiling" from the pool bug is reasoned from source, not measured.** I never ran a
  clean pool-vs-direct A/B — by the time I understood it, the pool config was broken for a second
  unrelated reason, so any A/B would have measured that instead.
- **n=1**, one machine, one afternoon, no error bars, no cross-provider comparison.
- **Latency did not improve predictably — there is no speed claim here.** Round 2 was 2x faster than cold; rounds 3 and 4 were *slower*
  than cold while reading 99.5% from cache. Caching cuts cost predictably, not wall-clock.
- **No live web search through any of this.** Measured: asked the year, Gemini said 2024, Grok
  said 2025. Pure parametric recall behind a training cutoff.

**Two things the tooling does that I'd steal for your own harness even if you ignore the rest:**

1. **Token-weighted, not the mean of per-round ratios.** A 700K round and a 200-token round are
   not equally important; averaging ratios lets a few trivial requests swing the headline by tens
   of points in either direction.
2. **Normalise the denominator.** Providers disagree about whether `input_tokens` already
   includes the cached read, and the payload doesn't tell you which convention you got. Gemini via
   this engine: it's included (`input >= cache_read`). Anthropic native: the opposite, so
   `cache_read` routinely exceeds `input`. Divide blindly and you get either the right answer or
   >100% depending on who answered. Also: build the prefix deterministically — one uuid or
   timestamp near the front invalidates the whole cached prefix and makes a working cache measure
   0%.

Repo (MIT), full methodology written so it can be refuted, raw per-round records: [LINK]

The Go engine doing the actual protocol work is CLIProxyAPI and it's someone else's project —
star that one first, this is useless without it.

If anyone runs the benchmark against a local backend, please post the JSON, especially if it
contradicts me.

---

## Variant 2 — r/ClaudeAI

**Read this first:** this audience is practical Claude Code users, not proxy authors. They care
about "does this fix a thing that's biting me". Two things here genuinely do: the
`400 Request contains an invalid argument` error (which people search for) and the 200K
auto-compaction cap. Lead with those, keep the source-level finding as the explanation rather
than the headline.

**Careful:** do not frame this as "get Claude free" or "replace your subscription". It isn't
that, this sub reacts badly to it, and the mods reasonably remove it. It is: point Claude Code at
an upstream *you already pay for* and stop wasting the cache.

**Flair:** check current options — this sub enforces flair and has removed posts for it.

### Title

```
If your Claude Code keeps dying with "400 Request contains an invalid argument", it may be your proxy's streaming path — and two other things I got wrong
```

Shorter alternate:

```
Two Claude Code settings I had wrong for months: the 200K compaction cap, and a proxy bug that silently killed my prompt cache
```

### Body

This is about running Claude Code against a **self-hosted proxy and your own upstream account**.
If you use Claude Code normally against Anthropic, none of this applies to you and you can skip
it. Nothing here is about getting anything for free — every token still bills to whatever account
you configure.

Three things bit me, in increasing order of how long they took to find.

**1. Claude Code assumes 200K for any model name it doesn't recognise.**

If you point Claude Code at a custom model name, it guesses a conservative context window and
starts auto-compacting *long* before the real window is reached. So a 1M-context upstream behaves
like a 200K one — and every compaction is also a full prompt rewrite, which destroys your cache
on top of losing your context. The official env var (v2.1.193+) moves it:

    export CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000

Honest caveat: **this does not create context.** It only moves where the client decides to
compact. If the upstream can't actually take what you then send, you've traded early compaction
for a truncation or an error. Verify at the size you're claiming before you trust it — I verified
748,918 tokens ingested without truncation. I did **not** verify 1,000,000.

**2. `400 Request contains an invalid argument`, seemingly at random.**

This one cost me an afternoon and the fix is boring, so let me save you the afternoon.

Everyone calls it "intermittent". It isn't. The *same* conversation would work and then fail one
turn later with no change in prompt size, model or account. The cause: Claude Code 2.1.x+ injects
a message with `role: "system"` into the **middle** of the messages array. The proxy's
**streaming** translator passed roles through verbatim, and Gemini's `streamGenerateContent`
rejects a mid-array `system` role outright. The **non-streaming** path tolerates it.

So the failure tracked **transport, not content**. Whenever a turn happened to go non-streaming it
worked; the moment the same conversation streamed, it 400'd.

The generalisable debugging move, which is the actual reason I'm writing this one up: capture the
failing request body and **replay it twice with curl, changing only `"stream": true` to `false`.**
If non-streaming succeeds and streaming 400s on byte-identical content, stop bisecting your
prompt — the bug is in the streaming translation path and your content is irrelevant. Most people
(me) spend the afternoon bisecting content first, because the error says "invalid argument" and
that sounds like a content problem.

Fixed upstream in CLIProxyAPI v7.2.93 by mapping `system` → `user`. If you're on an older engine,
the repo has a small shim that backports exactly that mapping. **If you're on 7.2.93+, you don't
need it — delete it.**

**3. The expensive one: a model-pool config that silently split my prompt cache.**

I had one alias mapped to two upstream model names in an `openai-compatibility` pool. Turns out
the upstream is chosen per request by a **global rotating counter** (`nextModelPoolOffset` in
`sdk/cliproxy/auth/conductor.go`), keyed by the pool — no session id anywhere in it. So
`routing.strategy` and `session-affinity` are both ignored for that choice, even when set
correctly. Since prompt caches live **at the upstream, per model per account**, one conversation
bouncing between two models has its cache split in two.

Fix: a direct 1:1 alias (`oauth-model-alias`) instead of a pool, plus `fill-first`,
`session-affinity: true`, 12h TTL. Note the engine's shipped defaults are the cache-hostile ones
(`round-robin`, affinity off, 1h).

After the fix, measured with a script in the repo — **99.53% token-weighted cache hit rate on
warm rounds** at a 748,918-token prompt.

**The caveats, because they matter more than the number:**

- Warm rounds (2–4) only. The cold round is 0% by construction and every session pays one. With
  the cold round folded in this run is 74.6%.
- Prefix-size dependent, and an upper bound: the harness's tail grows 15 tokens/round, the most
  cache-favourable shape there is, and the hit rate rises with prefix size. The tool's 30K default
  won't reach 99%. 99.53% is not a universal number.
- One machine, one run, one afternoon. No repetitions, no error bars.
- Latency did **not** reliably improve: one warm round was 2x faster than cold, two were *slower*.
  Caching saves money much more reliably than time.
- The "pools cap you at ~50%" claim is reasoned from reading the source, **not** measured. I never
  ran a clean A/B.
- **No live web search.** Asked what year it is, the Gemini upstream said 2024 and Grok said 2025.
  If you need current information, this is the wrong setup.
- The underlying Go engine (CLIProxyAPI, MIT) is not mine. It does all the hard protocol work.

Repo with the config, the shim, the benchmark and the full writeup: [LINK]

Happy to answer anything, including "why not just pay for the normal thing", which for most
people remains the right answer.
