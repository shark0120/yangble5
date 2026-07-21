# X / Twitter thread

**8 posts.** Post 1 must stand alone — assume 90% of readers see only that one, and that it will
be screenshotted and quoted out of context. So post 1 must be true *in isolation*, with no
qualifier living in a later post that a screenshot would cut off.

Character counts are given per post and were measured, not estimated. All are under 280 so the
thread works on a free account.

**Hard rule for this platform:** no percentage appears in this thread without the word "warm" or
an explicit caveat in the *same* post. A bare "99.53%" is the single most screenshot-able and most
misleading thing this project could emit.

---

## 1/ (267 chars) — the hook, stands alone

```
I spent an afternoon debugging why my prompt cache "wasn't working."

It was working. My proxy was splitting one conversation across two upstream models, so each
one only ever saw half the conversation.

No error. No log line. Just a bill twice the size it should be.
```

Why this works alone: it's a complete story with a concrete, checkable claim and no number to
misquote. Anyone running an LLM proxy immediately wants to know if it's them.

---

## 2/ (271 chars) — the mechanism

```
The cause was in the engine's source, not my config.

CLIProxyAPI 7.1.23: map one alias to two upstream model names in an openai-compatibility pool,
and the upstream is picked per request by a global rotating counter.

sdk/cliproxy/auth/conductor.go → nextModelPoolOffset
```

---

## 3/ (269 chars) — why your config didn't save you

```
The counter is keyed by the POOL. No session id, conversation id or credential id is in that key.

So routing.strategy isn't consulted for member selection.
And session-affinity isn't either — it binds a session to a CREDENTIAL, never to a pool member.

I had set both.
```

---

## 4/ (268 chars) — the load-bearing fact

```
The reason this matters:

Prompt caches live at the UPSTREAM, scoped per model per account. Not in your proxy.

All a proxy can do is make sure consecutive requests in one conversation reach the same cache.
Split a conversation across 2 models and you split its cache.
```

---

## 5/ (237 chars) — the check, which is the shareable part

```
Check your own binary. No Go toolchain needed:

  strings cli-proxy-api.exe | grep nextModelPoolOffset

Then read that function in YOUR version — I only reviewed 7.1.23.

Fix was a direct 1:1 alias instead of a pool. Three lines of YAML.
```

---

## 6/ (267 chars) — the measurement, correctly qualified

```
After the fix, measured with a script that ships in the repo:

99.53% token-weighted prompt-cache hit rate on WARM rounds (2-4 of 4), at a 748,918-token
prompt ingested with no truncation.

Cold round 1 is 0% by construction. Every session you start pays exactly one.
```

---

## 7/ (268 chars) — the caveats, in the thread, not in a reply

```
Where that number is weak, before you ask:

- warm-only; all 4 rounds incl. cold = 74.6%
- prefix-dependent, and an upper bound: the tail grows 15 tok/round, the most
  cache-friendly shape there is. Not a universal number.
- one machine, one run, no error bars
- latency did NOT reliably improve — 2 warm rounds were slower than cold
```

---

## 8/ (261 chars) — credit, "what this is not", link

```
Not a model. Not a hosted service. Not free credits — every token bills to your own upstream.
No live web search: asked the year, Gemini said 2024, Grok said 2025.

The Go engine doing the real work is CLIProxyAPI (MIT), not mine. Star that first.

Repo: [LINK]
```

---

## Optional 9/ — only if the thread gets traction

```
The "pools cap you at ~50%" line going around this thread is MINE and it is not a measurement.

It's a ceiling argument reasoned from the source. I never ran a clean pool-vs-direct A/B.

If you run one, I'll put your number in the README and delete my reasoning.
```

Post this **only** if you see people quoting a "50%" figure as measured. It is the single most
likely thing to be misrepresented, and getting ahead of it costs nothing and buys a lot.

---

## Reply-guy handling

| Reply | Response |
|---|---|
| "so it's free Claude?" | No. Every token bills to an upstream account you configure and pay for. This makes a session you already pay for cheaper; it doesn't make it free. |
| "just use OpenRouter" | For most people, yes. Honest comparison table in the repo, and we lose rows on provider breadth, ecosystem maturity and bus factor. |
| "99.53% is cherry-picked" | Yes, and post 7 says so. Warm-only, prefix-dependent, n=1, and an upper bound — the harness tail grows 15 tokens/round. Cold-included is 74.6%. |
| "did you tell the maintainer" | [Link the upstream issue. If it isn't filed, don't post the thread — see hn.md pre-flight.] |
| "is this against ToS" | A public pool of personal free-tier accounts is, and I don't defend it. Documented recommendation is BYOK or licensed paid keys. |
| Someone screenshots "99.53%" alone | Quote-tweet your own post 7. Do not argue in replies; just make the caveat as visible as the number. |

---

## What not to do here

- No "🧵" emoji, no "a thread:", no "let me explain". Post 1 already earns the scroll or doesn't.
- Do not put the Taiwan framing in this thread. It's for PTT and Threads, where the audience is
  the one it's addressed to. Here it reads as unrelated positioning and dilutes a technical hook.
- Do not tag Anthropic, Google or xAI. They are unaffiliated upstreams, not participants.
- Do not post a "99.53%" image card. If you make one image, make it the four-row per-round table
  with the cold round visible.
- Do not reply to bait about other proxies being broken too. You haven't tested them.
