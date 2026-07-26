# yangble5 release pack — drafts only, every publish action belongs to the user

This file is the single handoff for the release, upstream report and announcement drafts.
It is deliberately **not executable**. No agent should create a tag, a GitHub Release, an
upstream issue or a social post from this file. The user reviews and performs every public action.

The repository backlog intended for v0.2.0 is complete. The release date and changelog anchor are
now fixed; keep `[FINAL_COMMIT]`, `[FINAL_CI_RUN_URL]` and `[UPSTREAM_ISSUE_URL]` until the final
CI run is green and the user has opened the upstream issue. Replacing those earlier would turn a
draft into a false receipt.

## Decision: recommend `v0.2.0`

The project records `0.1.0` as its first public release, even though the only local tag today is
`v0.1.0-rc1`. The unreleased HTTP contract renames `/pool/status.capped` to
`pool_ceiling_configured`, and the deployment contract now refuses to start an existing
BYOK-enabled stack without an encryption key. Both are explicitly BREAKING.

The repository's policy says a pre-1.0 breaking change bumps MINOR. Therefore the next release is
`0.2.0`, not a second `0.1.0` and not `0.1.1`.

The final release-preparation commit moves these together:

- `pyproject.toml` `[project].version`: `0.1.0` → `0.2.0`
- `gateway/__init__.py` `__version__`: `0.1.0` → `0.2.0`
- `site/README.md` public health example: `0.1.0` → `0.2.0`
- `CHANGELOG.md`: rename the accumulated section to `[0.2.0] - 2026-07-27`, open a new empty
  Unreleased section, and update both comparison links
- annotated tag name and message: `v0.2.0`, with the final changelog section as its message

`tests/test_release_version.py` locks the first three values together. Do not tag a tree whose
version lock, full suite, six release gates, final GitHub CI or live-site drift check is red.

## Public-action boundary and order

All five rows below are **USER ONLY**. The order is intentional: announcement drafts promise an
upstream issue link, so the issue must exist before any announcement.

| Order | User-only action | Paste/use |
|---|---|---|
| 1 | Open the CLIProxyAPI issue | the upstream issue draft below |
| 2 | Review the final release commit, create the annotated `v0.2.0` tag and push that tag | the version plan above plus `RELEASING.md` section 4 |
| 3 | Open GitHub's “Draft a new release”, select `v0.2.0`, paste the release body, attach only reviewed artifacts, then publish | the GitHub Release draft below |
| 4 | Replace every `[LINK]`, `[UPSTREAM_ISSUE_URL]` and `[FINAL_*]` in the launch copies | the substitution checklist below |
| 5 | Post to one venue at a time, starting with Show HN only after the upstream issue exists | `hn.md`, then the other venue-specific drafts |

Do not batch-post. `reddit.md` already requires waiting between its two variants so contested
claims can be corrected before the second post.

## GitHub Release draft

Fill `[FINAL_COMMIT]`, `[FINAL_CI_RUN_URL]` and `[UPSTREAM_ISSUE_URL]` from the final green tree
and the user-created upstream issue.

<!-- RELEASE_DRAFT:BEGIN -->
```markdown
## yangble5 v0.2.0

**A reusable prompt-cache CI gate, a checkable measurement record, and safer contracts for the
self-hosted gateway around CLIProxyAPI.**

Built on [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) (MIT, third party, not ours).
yangble5 is configuration, compatibility tooling, a gateway and measurement/verification code
around that engine; it is not a model and does not provide free credits.

### Highlights

- **Use the cache guard from any GitHub repository.** `uses: shark0120/yangble5@v0.2.0` scans
  representative JSON/JSONL requests for volatile cacheable-prefix bytes and compares a pull
  request with its base commit. Its own CI proves a stable fixture passes and a deliberately
  cache-hostile fixture fails.
- **Recompute the released cache result offline.** `python tools/cache_bench.py --replay
  evidence/run-749k-20260721.jsonl` uses the same calculation as the live path, needs no API key
  and fails if a headline input was altered.
- **Public gateway contracts are discoverable and machine-checkable.** Registration, capacity,
  support, validation errors and the gateway version have one documented response shape.
- **The bundled agent skill is now namespaced as yangble5/yb5.** The retired colliding name remains
  only in the migration note; a repository-and-archive guard prevents it from returning.

### Breaking changes — read before upgrading a self-hosted stack

1. `/pool/status` renamed `capped` to `pool_ceiling_configured`. Gate clients on
   `accepting_requests`; the old field never meant “the pool is full”.
2. A BYOK-enabled Compose deployment now requires `YANGBLE5_BYOK_ENCRYPTION_KEY`. Previous
   documentation enabled BYOK without wiring that key, so user-supplied provider credentials
   could be stored in plaintext SQLite. Existing stacks without the variable now refuse to start
   instead of silently continuing. Set a generated encryption key, or explicitly set
   `YANGBLE5_BYOK_ENABLED=false` and do not accept user credentials.

### Added / Changed / Fixed

See [CHANGELOG.md](https://github.com/shark0120/yangble5/blob/v0.2.0/CHANGELOG.md#020---2026-07-27) for the complete entry, including the response
contract changes, Python 3.10 support, streaming-test repair and documentation/CI locks.

### Numbers in this release, and their conditions

| Measurement | Value | Conditions |
|---|---|---|
| Prompt-cache read ratio, token-weighted, **warm rounds only** | 99.53% | rounds 2–4 of 4; cold round 1 is 0% by construction; ~749K-token prefix; one Windows 11 machine; one run; 2026-07-21; CLIProxyAPI 7.1.23 |
| Largest prompt ingested without truncation | 748,918 tokens | same run; ingestion only — recall at that size was not tested |
| Complete non-streaming round trips, rounds 1–4 | 21,293 / 10,693 / 23,405 / 22,337 ms | same run; rounds 3 and 4 were slower than cold; anecdotal latency, not a latency claim |

Offline reproduction:

`python tools/cache_bench.py --replay evidence/run-749k-20260721.jsonl`

### Please read before quoting those numbers

- The 99.53% figure is **warm-only**. Every session pays one cold round at 0%. The same recorded
  run is 74.6% when the cold round is folded in.
- It is **prefix-size dependent and an upper bound for this harness shape**: the tail grows only
  15 tokens per round. The released evidence contains no result for another prefix size.
- This is one machine and one run, with no repetitions or error bars. Upstream caching can change.
- Cache reads did **not** reliably make requests faster: two warm rounds were slower than cold.
- No live web search happened through this proxy. The measured upstream answers were stale.
- `tools/claude_shim.py` is a workaround for engines older than v7.2.93, not architecture.
- The rotating-pool mechanism is verified in CLIProxyAPI source. No broken-pool versus direct
  A/B was run; do not quote a measured “before” result. Upstream report: [UPSTREAM_ISSUE_URL].
- This repository is software, not a service, but the maintainer runs one public demo instance at
  `https://yangble5.com`. It has no SLA or support commitment, uses the operator's upstream
  accounts, and the operator can read every request. Use BYOK or self-host for confidential data.

### Release receipt

- Release commit: `[FINAL_COMMIT]`
- Full GitHub CI: [FINAL_CI_RUN_URL]
- Release date: `2026-07-27`

### Optional reviewed artifact

Attach this only if its bytes still match the repository sidecar:

| File | SHA-256 |
|---|---|
| `yangble5-skill-v1.0.0.tar.gz` | `bff3216e71a08f26a5b25d32f10966c5aff566a84507db0ebc8774b2e5ddf6dd` |
```
<!-- RELEASE_DRAFT:END -->

## CLIProxyAPI upstream issue draft

### Pre-post audit, not part of the issue body

Checked read-only on 2026-07-27:

- Current upstream release:
  [`v7.2.101`](https://github.com/router-for-me/CLIProxyAPI/releases/tag/v7.2.101), published
  2026-07-26 UTC.
- The current tagged source still keys a model-pool counter by auth/provider/requested alias and
  increments it without a session input:
  [`conductor_models.go` lines 80–108](https://github.com/router-for-me/CLIProxyAPI/blob/v7.2.101/sdk/cliproxy/auth/conductor_models.go#L80-L108).
- The current tagged test explicitly expects three same-alias calls to choose model A, model B,
  model A:
  [`openai_compat_pool_test.go` lines 259–287](https://github.com/router-for-me/CLIProxyAPI/blob/v7.2.101/sdk/cliproxy/auth/openai_compat_pool_test.go#L259-L287).
- GitHub issue searches for the exact function name, model-pool rotation and session affinity
  found no exact report of this same-alias model-pool/cache-locality case. Issue
  [#1382](https://github.com/router-for-me/CLIProxyAPI/issues/1382) is adjacent but concerns
  credential/account selection and Responses continuity, not this model-pool member selection.

Repeat that search immediately before posting. If a duplicate now exists, comment there instead
of opening a new issue. Do not claim a live v7.2.101 reproduction: the latest-version evidence
here is source plus upstream's own unit test. The original live observation was on v7.1.23.

Suggested title:

> Make OpenAI-compatible same-alias model-pool selection optionally session-sticky

<!-- UPSTREAM_ISSUE_DRAFT:BEGIN -->
```markdown
### Version

- Live observation: v7.1.23, Windows amd64
- Source re-check: v7.2.101

### Configuration shape

One `openai-compatibility` provider has two upstream model names with the same public alias.
This is about selection *inside that model alias pool*, after an auth entry has been selected.

### Current behavior

The pool key is auth ID + provider key + requested alias, and `nextModelPoolOffset` increments a
counter for that key. It has no session/conversation input:

- [pool key and offset implementation in v7.2.101](https://github.com/router-for-me/CLIProxyAPI/blob/v7.2.101/sdk/cliproxy/auth/conductor_models.go#L80-L108)
- [current test expecting A → B → A for three calls](https://github.com/router-for-me/CLIProxyAPI/blob/v7.2.101/sdk/cliproxy/auth/openai_compat_pool_test.go#L259-L287)

That is useful load distribution, but it is hostile to prompt-cache locality when consecutive
turns in one conversation need to return to the same upstream account/model cache. Credential
session affinity does not select a member of this model pool.

### Requested behavior

Would you consider a configurable session-sticky policy for same-alias model pools when a stable
session identity is available, while retaining the current rotating behavior as the default for
backward compatibility?

I do not want to prescribe the implementation. Useful outcomes could be either:

- a documented/configurable “sticky within this alias pool” policy, or
- a supported way for the caller/router to select one pool member consistently for a session.

### Evidence boundary

The rotation mechanism is verified in current tagged source and its test. I observed the
cache-locality symptom on v7.1.23, then worked around it by using a direct 1:1 alias.

I did **not** run a controlled pool-versus-direct A/B, so I am not claiming a measured hit-rate,
latency or cost improvement for this change. Any percentage ceiling for a two-member rotation is
a structural argument, not benchmark data.

Minimal public reproduction and the raw direct-alias measurement record are documented here:
https://github.com/shark0120/yangble5

Thanks for maintaining CLIProxyAPI. If the intended contract is that a same-alias model pool
always rotates and cache-sensitive callers should use a 1:1 alias, documenting that limitation
would also resolve the ambiguity.
```
<!-- UPSTREAM_ISSUE_DRAFT:END -->

## Announcement substitution checklist

The repository URL is fixed:

`https://github.com/shark0120/yangble5`

After the user opens the upstream issue, replace `[UPSTREAM_ISSUE_URL]` with its canonical URL.
Then replace `[LINK]` in each venue draft with the repository URL unless the surrounding sentence
explicitly asks for the upstream issue.

Run these read-only searches before any post:

```bash
git grep -nF '[LINK]' -- docs/launch
git grep -nF '[UPSTREAM_ISSUE_URL]' -- docs/launch
git grep -nE '\[FINAL_[A-Z_]+\]' -- docs/launch
```

Expected immediately before publication: no placeholder in the text being pasted. Placeholders
may remain in unused venue drafts only if the user is not posting them.

Venue order and special dependency:

| Draft | What must be filled before paste |
|---|---|
| `hn.md` | repository link at the body; upstream issue link in prepared reply 7; do not post at all until the issue exists |
| `x-thread.md` | repository link in the last post; upstream issue link in the “did you tell the maintainer” reply |
| `reddit.md` | repository link in each chosen variant; post only one variant first |
| `ptt-threads.md` | repository link in the chosen PTT or Threads copy |
| GitHub Release body above | all `[FINAL_*]` fields and upstream issue URL |

The venue drafts remain separate because their tone and disclosure placement differ. Do not
replace them with one generic cross-post.

## Final review immediately before the user presses anything

- [ ] All remaining project backlog work intended for v0.2.0 is committed.
- [ ] The exact release tree passes the six commands in `RELEASING.md`.
- [ ] The final GitHub run is completed/success; skipped is acceptable only for the scheduled
      live-site job on a push.
- [ ] `python tools/drift_check.py` names every published file and exits 0 from outside the origin.
- [ ] Version source and both mirrors say `0.2.0`.
- [ ] The changelog date, compare links and release anchor are final.
- [ ] The upstream duplicate search was repeated against current issues.
- [ ] The user opened the upstream issue and its URL replaced every required placeholder.
- [ ] Any attached archive matches its sidecar and the checksum printed in the release body.
- [ ] The user—not an agent—creates/pushes the tag, publishes the GitHub Release and posts each
      announcement.
