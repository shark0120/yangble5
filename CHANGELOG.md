# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Two conventions specific to this repository, because they change how the entries should be read:

- **Every measured number carries its conditions.** No figure appears in this file without the
  machine, date and run count it came from. If that makes an entry long, the entry is long.
- **"Fixed" includes defects that live outside this repository.** yangble5 is a configuration,
  a shim and a measurement harness wrapped around a third-party engine
  ([CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI), MIT, not ours). Several things
  fixed in `0.1.0` were fixed *by changing what we ship around that engine*, not by patching it.
  Each such entry says exactly which of the two it is.

## [Unreleased]

Nothing yet.

## [0.1.0] - 2026-07-21

First public release. Everything below was developed against **CLIProxyAPI 7.1.23**
(`cli-proxy-api.exe`, windows/amd64) on **one Windows 11 machine**.

### Added

**Measurement tooling** (`tools/`, standard library only - no third-party imports, by design and
enforced in CI by a job that runs on a bare interpreter):

- `tools/cache_bench.py` - end-to-end prompt-cache benchmark. Generates a calibrated long prefix,
  runs an N-round session against your own upstream, and reports a **token-weighted** hit rate
  with cold round 1 and warm rounds 2..N reported separately rather than averaged together.
- `tools/cache_stats_sidecar.py` - durable per-request stats sidecar. Records prompt tokens,
  `cache_read` tokens, uncached tail and latency per request to a JSON file that survives a
  restart, so a hit-rate claim can be recomputed from raw records instead of trusted.
- `tools/claude_shim.py` - Claude-wire compatibility shim on `:8320` (see *Fixed*).

**Public gateway** (`gateway/`) - a FastAPI service that fronts the engine when it is exposed
beyond localhost: per-key quota accounting, rate limiting, usage records, upstream fan-out and
abuse controls. Kept behind an optional `[gateway]` extra so the measurement tools install with
zero dependencies.

**Bring-your-own-key onboarding** (`byok/`, `gateway/byok.py`) - generates an engine
configuration from a template so an operator supplies their own upstream credentials. The
generated config uses a direct 1:1 OAuth model alias and `routing.strategy: fill-first`; the test
suite parses the rendered output with a real YAML implementation and asserts that property,
because "it looks like YAML" is not the property that matters.

**Deployment bundle** (`deploy/`) - Docker Compose stack (engine, gateway, Caddy), Caddy TLS
drop-ins, a `fail2ban` filter and jail template for authentication abuse, and `harden.sh`. The
CLIProxyAPI binary is deliberately **not** redistributed; the operator brings their own.

**Documentation** (`docs/`, `README.md`):

- `docs/FINDINGS.md` - every finding with an explicit status label (Verified (source/binary) /
  Measured / Observed / Reasoned) and an appendix of what was *not* verified.
- `docs/BENCHMARK.md` - benchmark methodology, including how to reproduce the numbers below and
  how to lie to yourself while measuring a cache.
- `docs/OPERATING_A_PUBLIC_SERVICE.md` - operating guide for anyone exposing this to other people.

**Project infrastructure** - MIT license, code of conduct, contribution and security policy,
issue/PR templates, and a CI workflow that runs the suite on Linux and Windows across Python
3.11 / 3.12 / 3.13, plus two guard jobs: one that imports every tool on a *bare* interpreter with
no third-party packages installed, and one that fails the build on secret-shaped strings or
absolute operator paths.

**Measured results** (one Windows 11 machine, **single run per configuration**, 2026-07-21,
Gemini via the engine's `antigravity` OAuth channel - read *Known Issues* before quoting any of
these):

- 99.53% token-weighted prompt-cache hit rate on **warm** rounds (rounds 2-4 of a 4-round session).
- 0% on cold round 1 - every session's first request is a cache *write*, by construction.
- 748,918-token prompt processed with no truncation.
- Latency 21.4 s (cold round 1) -> 10.8 s (warm round 2) at that prompt size. Rounds 3 and 4 were
  *slower* than the cold round; see *Known Issues*.
- Claude Code end-to-end: 3/3 successful sessions through the stack.
- The automated suite (`pytest`) is green on the tagged tree. The exact test count is recorded
  when the tag is cut rather than quoted here, because a number written before the tag is a
  number that will be wrong by the time anyone reads it - see `RELEASING.md` section 3.1.

### Fixed

- **Mid-conversation `system` role 400s the streaming path only** (fixed **in this repo**, as a
  backport). CLIProxyAPI 7.1.23's antigravity **streaming** translator passes `messages[].role`
  through verbatim - it rewrites `assistant` to `model` and leaves everything else alone. Claude
  Code >= 2.1.x injects a message with `role: "system"` in the *middle* of the `messages` array,
  and Gemini's `streamGenerateContent` rejects that role with
  `400 Request contains an invalid argument`. The **non-streaming** `generateContent` path
  tolerates the same role, which is precisely why the failure looked intermittent and why
  bisecting by prompt *content* finds nothing: the failure tracks transport, not content.
  Upstream fixed it in **v7.2.93** by mapping `system` -> `user` in
  `internal/translator/antigravity/claude/antigravity_claude_request.go`;
  `tools/claude_shim.py` applies that exact mapping in front of an older engine. The shim
  forwards any body that does not contain `"system"` **byte for byte** - re-serialising an
  untouched body would change whitespace, and the upstream prompt cache keys on exact bytes, so a
  "harmless" JSON round-trip would have silently destroyed the cache result above. Covered by
  `tests/test_claude_shim.py`.

- **A same-alias multi-model pool silently destroys prompt caching** (fixed **in the shipped
  configuration**; the engine itself is unchanged and still behaves this way). In CLIProxyAPI
  7.1.23, when one alias maps to two upstream model names inside an `openai-compatibility`
  provider, the upstream for each request is chosen by a **global rotating counter**
  (`nextModelPoolOffset` / `modelPoolOffsets` in `sdk/cliproxy/auth/conductor.go`), keyed by the
  *pool* - no session id, conversation id, credential id or `metadata.user_id` participates.
  `routing.strategy` and session affinity are both ignored on that path, so consecutive turns of
  one conversation land on alternating upstreams and each one sees a cold cache. With two
  members that caps the achievable hit rate at roughly 50% (that ceiling is *reasoned* from the
  code, not measured). Symptoms: near-zero cached tokens on turns whose prompts are almost
  byte-identical, plus frequent 502s. The fix we ship is a **direct 1:1 OAuth model alias** -
  no pool - which is what every configuration in `deploy/` and `byok/` now generates. Presence of
  the symbol in the exact binary measured was confirmed with
  `strings cli-proxy-api.exe | grep -E 'nextModelPoolOffset|conductor\.go'`.

- **Clients cap themselves at 200K unless told otherwise** (fixed **in the documented client
  settings**). Claude Code assumes a 200K context for model names it does not recognise and
  begins auto-compacting early - and every compaction invalidates the prompt cache the rest of
  this project exists to preserve. Setting the official
  `CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000` (supported from Claude Code v2.1.193) stops that;
  Codex uses `model_context_window`. Raising the number does not create context out of nothing -
  it stops the client from throwing context away that the upstream would have accepted.

### Known Issues

These are release-blocking to *quote*, not to *ship*. Read them before repeating any number above.

- **No live web search.** Nothing routed through this proxy performs a real web search. Measured
  2026-07-21: asked for the current year, the Gemini upstream answered **2024** and the Grok
  upstream answered **2025**. Every answer is parametric recall behind a training cutoff. Use an
  environment with real search if you need current facts.
- **The 99.53% figure is warm-only.** It covers rounds 2-4. Round 1 is a cold cache write and is
  0% by construction, and *every* session you start pays one. Folding the cold round in gives
  74.6% for the same run. Both numbers come from the same four records, which are printed
  verbatim in `README.md` and `docs/FINDINGS.md` so either can be recomputed.
- **The hit rate is prefix-size dependent.** The uncached tail is roughly constant (~3.5K tokens
  at a 749K prefix, ~5.4K at a 91K prefix), so the *ratio* improves as the prefix grows: 99.53%
  at `--prefix-tokens 600000`, 94.00% at `--prefix-tokens 75000`, and the tool's default
  (`--prefix-tokens 30000`) will not reach 99%. This is a property of the upstream's cache
  granularity, not of yangble5. 99.53% is not a universal number.
- **One machine, one run, no repetitions.** No confidence intervals, no cross-provider
  comparison, no second operator. Upstream providers change caching behaviour without notice; a
  number measured in July 2026 may not survive to August. `tools/cache_bench.py` ships so you can
  re-measure rather than trust us.
- **Latency is not a clean win.** Round 2 was about 2x faster than the cold round; rounds 3 and 4
  were *slower* than it. Single run, shared upstream, no control over provider-side load. Treat
  the latency figures as an anecdote.
- **Not measured:** context beyond 748,918 tokens; recall quality at long context (no
  needle-in-a-haystack test was run); any comparison against another provider or another proxy.
- **`tools/claude_shim.py` is a workaround, not a feature.** It exists only for engines older
  than v7.2.93. On 7.2.93 or newer, point `ANTHROPIC_BASE_URL` back at the engine port and delete
  the file - it holds no state and nothing depends on it.
- **CLIProxyAPI is third-party and this project is useless without it.** The Go engine is
  somebody else's work (MIT, Router-For.ME). We do not redistribute the binary and we are not
  affiliated with the project; bugs in the engine belong in the engine's tracker, not ours.
- **yangble5 is not a model.** Not a training run, not a fine-tune, not a hosted service, and not
  a source of free credits. Every token is billed to whatever upstream account *you* configure.

[Unreleased]: https://github.com/shark0120/yangble5/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/shark0120/yangble5/releases/tag/v0.1.0
