# A/B: pool rotation vs session affinity — warm-prefix cache, direct comparison

**Pre-registered 2026-08-01 before execution; the registered sections were not
edited after the run.** This is the first controlled comparison behind the
claim this repository was founded on ("pool rotation destroys warm prompt
caching") — which until this run rested on a source-level mechanism reading
plus single-arm measurements. **The pre-registered threshold was not met.**
That result is recorded verbatim below, because a verification method you can
overrule when it disappoints you is not a verification method.

## Registered protocol

- **H1**: on the same workload, per-request rotation (round-robin, no session
  affinity) yields a warm-round token-weighted cache-read ratio at least
  **30 percentage points below** session affinity (fill-first + affinity).
  **H0**: the gap is smaller. Whichever holds is published verbatim.
- **Primary metric**: Σ`cache_read_input_tokens` / Σ(`input_tokens` +
  `cache_read_input_tokens`) over rounds 2–8 (warm), token-weighted, per arm.
- **Protocol**: 2 arms × 8 rounds (1 cold + 7 warm), fixed ~100K-char latin
  prefix per arm as `system`, arm-specific first line so the arms can never
  share a cache entry; small varying question per round; `max_tokens` 64;
  2 s between rounds; `/v1/messages` against the local engine
  (CLIProxyAPI 7.2.93), model alias `grok-4.5` (upstream self-identifies as
  grok-4.3), xai pool of 5 credentials.
  - Arm A (affinity): `strategy: fill-first` + `session-affinity: true`,
    one fixed session id for the whole arm.
  - Arm B (rotation): `strategy: round-robin` + `session-affinity: false`
    (engine restarted onto that config, restored and restarted after),
    a fresh session id every round.
- **Stop rules, fixed in advance**: cold-round read ratio > 10% → abort
  (contamination); 2 consecutive request failures → abort (sick pool);
  cache fields missing twice → abort (unmeasurable); 700K input-token
  breaker; fixed sample — **no early stopping on interim results**.
- **Sample boundary** (the population this result speaks for, and nothing
  else): one machine, one day, one prefix size (~17K tokens measured), one
  model family, one provider's cache implementation, 5-account pool,
  7 warm rounds per arm, 2 s gaps. No extrapolation in either direction.

## Results (verbatim)

Raw per-round records: [`armA-20260801-065314.jsonl`](armA-20260801-065314.jsonl),
[`armB-20260801-065657.jsonl`](armB-20260801-065657.jsonl). Total spend
≈ 276K input tokens. No stop rule fired.

| | Arm A (affinity) | Arm B (rotation) |
|---|---|---|
| Warm token-weighted read ratio | **17.17%** | **6.45%** |
| Per-round read ratio, r2..r8 | 0.7 / 0.7 / 8.5 / 1.1 / 8.5 / **99.8** / 0.7 % | 8.5 / 0.7 / 8.5 / 1.1 / 8.9 / 8.9 / 8.5 % |
| Full-prefix cache hits | **1 of 7** (r7: read 17,280 of 17,310) | **0 of 8** (ceiling ≈ the shared ~1.5K engine-injected prefix) |
| `cache_creation_input_tokens` | not reported by upstream (recorded as unknown, not zero) | same |

**Δ = 17.2 − 6.5 = 10.7 pp < 30 pp → per the registered decision rule: H0,
the claim is NOT supported at this boundary.**

## What the run actually showed

1. **The dominant phenomenon at this scale is not rotation — it is that the
   provider's cache is unstable at a ~17K-token prefix even WITH affinity**
   (1 full hit in 7 warm rounds; the rest oscillate between 0.7% and 8.5%).
   The 99.53% headline elsewhere in this repository was measured at a ~749K
   prefix; that is a different behavioural regime, and neither result
   licenses conclusions about the other.
2. The mechanism signature is visible but small here: the only full-prefix
   hit occurred under affinity, and rotation's best round is exactly the
   engine-injected common prefix every account shares. Mechanism direction
   and effect size are different claims; this run measured the second.
3. Known limit: the engine does not report which pool account served each
   request, so rotation is verified by configuration plus behavioural
   signature, not by direct observation.

## What this changes about this repository's claims

The mechanism finding in [docs/FINDINGS.md](../../docs/FINDINGS.md)
(caches are scoped per account; splitting one conversation across accounts
splits its cache) remains source-verified and untouched. What this run
removes is the licence to state the END-TO-END effect without a scale
qualifier: at small prefixes and short gaps, the measured gap was 10.7 pp,
and the large-prefix regime (≥100K tokens, where a full-hit-versus-no-hit
difference would dominate) is **unmeasured** — a controlled A/B there costs
≥1.6M tokens and has not been run.
