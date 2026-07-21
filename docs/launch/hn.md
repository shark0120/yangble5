# Hacker News — Show HN

---

## PRE-FLIGHT (internal — do not paste any of this)

- [ ] **File the pool-rotation behaviour with CLIProxyAPI upstream first.** We are publishing a
      source-level finding about somebody else's MIT-licensed project. If the maintainer reads
      about it on HN before hearing from us, that is the fastest possible way to lose the thread,
      and we would deserve it. Link the issue from the post once it exists.
- [ ] Repo public, README renders, `cache_bench.py` runs clean from a fresh clone.
- [ ] The CLIProxyAPI credit is visible **above the fold** on the repo README. It is.
- [ ] Post Tue–Thu, roughly 08:00–11:00 US Eastern. Be at the keyboard for the next three hours.
      An unanswered Show HN dies; a well-answered one with a hostile top comment does fine.
- [ ] No vote solicitation anywhere, including Threads/LINE/Discord. HN detects it and it is fatal.
- [ ] Decide before posting: are we comfortable that every number below is one we can defend at
      2am against someone who has read the source? If not, cut the number, not the qualifier.
- [ ] The "Claude Code 3/3 successful end-to-end" observation is backed by a raw record at
      [`docs/evidence/claude-code-e2e.md`](../evidence/claude-code-e2e.md). If you mention it,
      link that file in the same breath and call it a **smoke test, not a benchmark** - three
      manual runs of one prompt show the path works and measure nothing else. Never let it sit
      next to 99.53% as though both were measurements of the same kind.

---

## Title

All candidates are ≤ 80 characters, no emoji, no superlative, no "revolutionary".

**Primary (73 chars):**

```
Show HN: Your LLM proxy may be quietly splitting your prompt cache in two
```

**Alternate A (73 chars)** — leads with the artifact rather than the finding, safer if the
moderators reclassify the post as not-a-Show-HN:

```
Show HN: Yangble5 – a prompt-cache benchmark that found a bug in my proxy
```

**Alternate B (70 chars)** — most specific, best if we want the CLIProxyAPI maintainers to find it:

```
Show HN: A model-pool counter in my LLM proxy ignored session affinity
```

Prefer the **Primary**. It states a falsifiable claim, contains no adjective, and the reader
knows within seven words whether it applies to them. Avoid anything containing "10x", "destroying",
"you're doing it wrong", or a percentage — HN reads a percentage in a title as a sales pitch.

---

## Body

> Paste everything between the rules. Roughly 600 words. HN's text field accepts Markdown-ish
> formatting only for paragraphs and links — the asterisks below are literal and read fine as
> plain text, but strip them if you prefer.

---

I run long Claude Code sessions against a self-hosted proxy, and one of them was behaving as if
the prompt cache did not exist — near-zero cached-token counts on turns where almost the whole
prompt was byte-identical to the previous turn. About half the requests also 502'd, which is the
only reason I noticed at all.

The cause turned out to be in the proxy's source, not my config. In CLIProxyAPI 7.1.23, when one
model alias maps to two upstream model names inside an `openai-compatibility` pool, the upstream
for each request is chosen by a global rotating counter in
`sdk/cliproxy/auth/conductor.go` (`nextModelPoolOffset`, state in `modelPoolOffsets`, keyed by
`openAICompatModelPoolKey`). The key is the **pool**. No session id, conversation id, credential
id or `metadata.user_id` participates in the choice. So:

- `routing.strategy` is not consulted for member selection.
- `session-affinity` is not consulted either — it binds a session to a *credential*, never to a
  pool member.

Both of mine were set "correctly" and neither did anything. Upstream prompt caches are scoped per
model and per account, so one conversation alternating between two upstream models has its cache
split in two, permanently. You can check whether the code path is in the binary you are running
without a Go toolchain:

    strings cli-proxy-api.exe | grep -E 'nextModelPoolOffset|modelPoolOffsets|conductor\.go'

The fix was a direct 1:1 alias on the provider channel (`oauth-model-alias`) instead of a pool,
plus `fill-first` and `session-affinity: true` — note that the engine's shipped defaults are
`round-robin` / `session-affinity: false` / `1h` TTL, which are cache-hostile for long agent
sessions.

*What I measured after the fix*, with a script that ships in the repo: **99.53% token-weighted
prompt-cache hit rate on warm rounds** (rounds 2–4 of a 4-round session) at a **748,918-token**
prompt, which was ingested with no truncation. Raw per-round numbers, not averaged:

    round 1 (cold): prompt 748,918  cached       0   0.00%  21,410 ms
    round 2:        prompt 748,933  cached 745,438  99.53%  10,753 ms
    round 3:        prompt 748,948  cached 745,430  99.53%  23,457 ms
    round 4:        prompt 748,963  cached 745,422  99.53%  22,381 ms

*Now the parts that will get me correctly beaten up, stated before you have to ask:*

**The "~50%" is not a measurement.** It is a ceiling argument: with a two-member rotation, at best
every other request can read what its predecessor wrote. I did not run a controlled pool-vs-direct
A/B. By the time I understood the mechanism, the pool config was already known-broken for a second
reason (one of its two members was never registered by the provider, so it 502'd), and rebuilding
a known-broken config to benchmark it wasn't worth the upstream spend. What is measured is the
*after* state only.

**99.53% is warm-only, prefix-size dependent, and an upper bound.** Round 1 is a cold write and
is 0% by construction; every session pays exactly one. Fold it in and this run is 74.6% — a
number that is really a function of how many rounds I chose to run. The harness's conversation
tail grows by exactly 15 tokens per round, which is the most cache-favourable shape possible; a
real agent turn appends far more, so expect lower. Hit rate also rises with prefix size
(direction observed; I'm not publishing a magnitude, because the released evidence set is one
run at 748,918 tokens), and the tool's 30K default will not reach 99%. Do not quote 99.53% as a
universal number.

**One machine, one run, one afternoon.** Windows 11, no repetitions, no error bars, no
cross-provider comparison. **Latency did not improve predictably — no speed claim is made anywhere** — round 2 was 2x faster than cold, and
rounds 3 and 4 were *slower* than cold while reading 99.5% of their prompt from cache. Caching
reduces cost predictably; on this evidence it does not reduce wall-clock predictably.

*What this is not:* not a model, not a fine-tune, not a training run, not a hosted service, and
not a source of free credits — every token bills to whatever upstream account you configure. The
Go engine that does all the real protocol work is **CLIProxyAPI**, MIT, someone else's excellent
work; I did not write it and this is useless without it. What is mine is the finding, the config,
a compatibility shim, and two measurement tools. **There is no live web search through it** —
asked what year it was, the Gemini upstream said 2024 and the Grok upstream said 2025. Treat
everything it returns as parametric recall behind a training cutoff.

Repo, methodology written to be refuted, and the raw records: [LINK]

The most useful thing anyone could do is run `cache_bench.py` against a provider I haven't tested
and post the JSON, especially if it contradicts me.

---

## Prepared replies

Post these as replies, not pre-emptively. Answer within ~20 minutes. Never argue with the premise
of a hostile comment before conceding the true part of it — on HN, conceding first is what buys
the rest of the reply a reading.

### 1. "This is just a config change. A YAML diff is not a project."

Largely fair, and worth being precise about. The YAML diff is three lines and I'd rather everyone
just copied it than starred anything. What took the time was *knowing which three lines*, and the
part I think has standalone value is the measurement harness — the reason this went unnoticed for
so long is that a silently-halved cache produces no error, no log line and no alert. It just costs
double. A pool whose members all happen to be valid fails completely silently; mine was only
visible because one member 502'd.

If the config is all you want: it's in the README, MIT, no attribution needed, and you don't need
the rest of the repo.

### 2. "99.53% is cherry-picked. You dropped the cold round and picked the prefix size that flatters you."

Both true, both stated in the post, and here is the arithmetic to check me with. Including the
cold round this run is 2,236,290 / 2,995,762 = 74.6%. At the tool's 30K default it does not reach
99%. I'm deliberately not quoting a figure for any intermediate prefix: the released evidence set
is a single run at 748,918 tokens, and a number I won't show you the raw rounds for isn't worth
anything.

The reason the warm number is the headline is that the uncached remainder is roughly *constant*
(~3.5K tokens at a 749K prefix) rather than proportional — it's the conversation growth since the
last request. So the ratio necessarily improves with prefix size, and any single number is
meaningless without the prefix size attached. That's why the tool prints prompt size, cached
count and cold round on every line rather than a headline percentage.

The stronger version of your criticism, which I'd rather make myself: including the cold round
makes the number a function of `--rounds`. Run 40 rounds and "the" hit rate climbs to ~97% having
measured nothing new. Neither framing is honest without the other, so the tool emits both.

### 3. "n=1 on one machine is not a benchmark."

Correct. It's one sample and the document calls it one sample — no repetitions, no confidence
intervals, no isolation from provider-side load, consumer broadband, single afternoon. That's
also why the latency column is explicitly labelled an anecdote rather than a result.

I'd push back on exactly one thing: for the *cache-hit* number specifically, the quantity is a
ratio of two token counts that the upstream itself reports, both taken from the same response.
It's far less sensitive to machine and network variance than a latency benchmark would be. That
doesn't make n=1 sufficient — it makes it a different kind of insufficient. The tool ships
precisely so the sample size stops being 1, and a contradicting run is more interesting to me
than a confirming one.

### 4. "You're comparing a measured 99.53% against a guessed 50%. That's not a comparison."

That is the correct criticism and I don't have a rebuttal, only a labelling defence: the ~50% is
described everywhere in the repo as a structural ceiling reasoned from source, never as a
measurement, including in the paragraph immediately under the table. I should not have needed you
to ask, and if the framing still reads as a before/after, that's a writing failure on my part.

Why I didn't measure it: the pool config had a second, unrelated fault — one of the two members
didn't exist on that provider build, so ~half of all requests 502'd. Any A/B I ran would have been
measuring that fault, not the rotation. Rebuilding a correct-but-pooled config purely to benchmark
it means paying for several million upstream tokens to confirm an argument I can already read in
the source. If someone does run it, I'll put their number in the README with their name on it and
delete mine.

### 5. "Isn't this ToS-violating free-tier laundering with extra steps?"

If you point a public endpoint at a pool of personal free-tier accounts: yes, that's account
sharing, essentially every provider prohibits it in some form, and the accounts get suspended —
not the service, the accounts, including whatever else those Google/OpenAI/xAI logins are used
for. The people hurt worst are whoever lent you an account. I'm not going to defend that pattern,
because I think it's indefensible.

The documented recommendation in the repo is BYOK or properly licensed paid keys, and
`docs/OPERATING_A_PUBLIC_SERVICE.md` exists specifically to say so at length, including a
pre-launch checklist whose first line is "no personal OAuth account is in the pool". The
configuration everything here was measured against is localhost against an account you already
pay for. The saving in that case is real and boring: a 99%-cached 749K prefix is billed very
differently from an uncached one, on a bill you were already paying.

### 6. "Why not just use LiteLLM / OpenRouter?"

For most people, do. LiteLLM has vastly more providers, real observability, budget management and
a large team; OpenRouter is one key and zero setup. There's an honest comparison table in the repo
that has us losing rows on ecosystem maturity, provider breadth, live web search and bus factor,
because a table where the author wins every row is worthless.

The narrow reason this exists: I wanted the traffic to stay on my machine, and I wanted to be able
to *prove* the cache was working rather than assume it. The benchmark is provider-agnostic — it
only needs an Anthropic-format `/v1/messages` endpoint that reports usage, so you can point it at
LiteLLM, at OpenRouter, or at llama.cpp, and I'd genuinely like to see those numbers.

### 7. "Why post this instead of filing it upstream?"

[Link the upstream issue here — see PRE-FLIGHT. If it is not filed, do not post the thread.]
Also worth saying: the second bug in the writeup, a mid-conversation `role: "system"` message
that 400s the streaming path but not the non-streaming one, *was* fixed upstream in v7.2.93. The
shim in this repo only exists to bridge older engines and is meant to be deleted on upgrade.

### 8. "Are you logging my prompts?"

Nothing in this repo logs prompts or completions, and the operating guide says never to add it,
not even temporarily for debugging. But you should not take my word for that — the localhost
configuration is the one to run, and in it nothing leaves your machine except the upstream call
you were making anyway. If you run the optional public gateway, you become the data controller
and that's your problem to disclose, which is also written down.

### 9. "The install one-liner is `curl | sh`."

Reasonable objection. The script refuses to run as root, uses no sudo, writes only under
`$HOME/.yangble5` and `$HOME/.local/bin`, does not edit your shell rc files, does not touch your
existing Claude Code login (separate `CLAUDE_CONFIG_DIR`), downloads and executes no second
artifact, and ships `--dry-run` which prints every action and changes nothing. Read it first;
it's written to be read, and the header explains itself in plain language. Or skip it entirely —
the tools are stdlib-only Python you can copy one file at a time.

---

## Tone rules for the thread

- Concede the true part of every criticism in the first sentence of the reply. Every time.
- Never say "great question", "actually", or "as I said in the post".
- If someone finds a real error, thank them, fix the repo within the hour, and reply with the
  commit link. That single move converts more hostile threads than any argument.
- If a comment is about the ~50% not being measured, do not defend it. Agree, explain why it
  wasn't measured, and offer to publish their number instead.
- Do not mention stars, upvotes, or the Taiwan framing anywhere in this thread. Neither belongs
  on HN and both read as marketing here.
- If the thread turns into "your provider will ban you for this": agree, point at the operating
  doc, and do not litigate individual providers' terms in public.
