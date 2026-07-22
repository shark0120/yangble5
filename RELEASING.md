# Releasing yangble5

This is the checklist a release runs through. It is written to be followed by a person, in order,
with no step marked "obviously fine".

A release of this project is unusual in one way that shapes everything below: **most of what we
ship is claims about measurements.** A bug in `gateway/app.py` inconveniences an operator; a
number quoted without its qualifier misleads a reader who will never see the retraction. So the
checklist spends more effort on the claims than on the code.

---

## 0. Who can cut a release

Anyone with write access, but the pre-release checklist is not delegable to CI: the parts that
matter (do the numbers still carry their disclosures? is anything in the README no longer true?)
cannot be automated. CI is a floor, not a gate.

That said, this document has been wrong about CI before — it claimed three Python versions when
the matrix ran five, and described a secret scan by a shape count that had stopped being true.
So: **`.github/workflows/ci.yml` is the source of truth for what CI does.** Where this file names
a job, a scope or a version, it is quoting the workflow, and if the two disagree the workflow is
right and this file is stale. Fix it in the same commit you notice it.

What CI covers, by job id, so the checklist below can refer to them by name:

| Job | What a green tick means |
|---|---|
| `test` | pytest + `ruff check .` + `ruff format --check tools byok`, on Ubuntu and Windows, on every Python in the matrix |
| `tools-are-stdlib-only` | nothing in `tools/` or `byok/` imports a third-party package, proved on a bare interpreter |
| `offline-self-checks` | every `.sh` and `.ps1` parses; `--help` and `--dry-run` paths still work and still write nothing; `pyproject.toml` agrees with the test matrix |
| `installer-digests` | every `site/*.sh`/`*.ps1` has a `.sha256` and each digest matches its file **in the tree** |
| `published-numbers` | `tools/sitecheck.py` accounts for every figure on every file under `site/`, and its two negative controls prove the checker can still go red |
| `no-secrets` | no key-shaped string, operator path, non-reserved email domain, or un-allowlisted IP address |
| `live-site-drift` | **scheduled only, skipped on PRs.** `tools/drift_check.py` says the *served* site is this commit |

What CI does **not** cover, and therefore what section 3 is for: whether the numbers still carry
their qualifiers, whether the README is still true, and — until the scheduled job's next run —
whether what is deployed is what is in this tree. That last one is section 3.6 and it is the only
step in this document with no manual alternative.

---

## 1. Versioning policy

[Semantic Versioning](https://semver.org/spec/v2.0.0.html), with the public surface defined as:

| Surface | Covered by semver? |
|---|---|
| CLI flags and output format of `tools/*.py` | **Yes.** Renaming or removing a flag is a MAJOR change. |
| The JSON schema written by `tools/cache_stats_sidecar.py` | **Yes.** Consumers parse it. |
| HTTP routes and response shapes of `gateway/` | **Yes.** |
| The shim's wire behaviour (`tools/claude_shim.py`) | **Yes.** |
| Engine config templates in `deploy/` and `byok/` | **No**, but a breaking change gets a `BREAKING CHANGE:` footer and a `Changed` entry anyway. |
| Measured numbers in `docs/` | Not versioned. They are dated observations; a re-measurement is a `docs:` change with a new date, never an edit in place that quietly moves a number. |

Pre-1.0 (`0.y.z`), a breaking change bumps MINOR.

---

## 2. Version bump locations

There is one source of truth and two places that mirror it. Grep before you tag.

| Location | What to change | How to check |
|---|---|---|
| `pyproject.toml` -> `[project] version` | **The** version. Everything else follows. | `grep -n '^version' pyproject.toml` |
| `CHANGELOG.md` | Rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`, add a fresh empty `## [Unreleased]` above it, and update the two link definitions at the bottom. | `grep -n '^## \[' CHANGELOG.md` |
| `CHANGELOG.md` link refs | `[Unreleased]: .../compare/vX.Y.Z...HEAD` and `[X.Y.Z]: .../releases/tag/vX.Y.Z` | `tail -3 CHANGELOG.md` |
| Git tag | `vX.Y.Z` - annotated, never lightweight | `git tag -n9 vX.Y.Z` |

Nothing else hard-codes a version. If a future change adds a second copy (a `__version__`, a
Docker image tag, a badge), add it to this table in the same commit, or the next release will
ship an inconsistency.

To confirm no stale version strings remain, grep for the version you are **replacing**, not the
one you are cutting — the new version is supposed to appear:

```bash
# e.g. cutting 0.2.0 over 0.1.0
previous=0.1.0
git grep -nF "$previous" -- . ':!CHANGELOG.md'
```

`CHANGELOG.md` is excluded because it is the one file that must keep every old version forever.

---

## 3. Pre-release checklist

Every box must be ticked on the exact tree that will be tagged. Not "on a similar tree yesterday".

### 3.1 Tests and lint

```bash
python -m pip install -e ".[dev]"
python -m ruff check .
python -m ruff format --check tools byok
python -m pytest -q
```

The `ruff format` scope is `tools byok` — not `.` — and it must stay identical to the
`Format check (ruff)` step in `.github/workflows/ci.yml` and to `CONTRIBUTING.md`. Three
documents quoting three different scopes is how a release checklist stops being run. `gateway/`
and `tests/` are not `ruff format`-clean yet; widening the scope is a standalone commit, never
part of a release.

- [ ] Full suite green locally.
- [ ] Green in CI on **both** operating systems and **every** Python in the `test` matrix — at the
      time of writing 3.10, 3.11, 3.12, 3.13 and 3.14, ten matrix cells. Do not trust that list:
      read `strategy.matrix.python-version` in `.github/workflows/ci.yml`. It is the source of
      truth, and the `offline-self-checks` job fails the build if `requires-python` or the
      `Programming Language :: Python` classifiers disagree with it. Nothing checks the sentence
      you are reading, which is exactly why it was wrong before.
- [ ] A **skipped** job is a red job, with one exception: `live-site-drift` is `if`-gated to
      `schedule` and `workflow_dispatch`, so it is *supposed* to show as skipped on a push or a
      pull request. Section 3.6 is how you cover it at release time.
- [ ] The `tools-are-stdlib-only` job passed. If it was skipped, the release does not go out:
      that job is the only thing keeping `tools/` copy-and-run-able.
- [ ] `offline-self-checks`, `installer-digests`, `published-numbers` and `no-secrets` all passed.
      They are cheap and they are the ones nobody watches.
- [ ] Record the test count in the changelog entry, since it is quoted in the README.

### 3.2 Secrets and operator paths

```bash
# There is ONE copy of this pattern, in the `no-secrets` job of
# .github/workflows/ci.yml, and everything else lifts it out. This file used to
# carry its own four-shape version described as "a superset of CI's pattern"; it
# was a small subset of it, and it missed every key shape with a hyphen in it.
# `sed` rather than `grep -oP`: no PCRE required, and no locale in which the
# extraction silently yields an empty pattern.
pattern=$(sed -n "s/.*git grep -nIE '\([^']*\)'.*/\1/p" .github/workflows/ci.yml | head -1)
[ -n "$pattern" ] || { echo "could not lift the pattern out of the workflow"; exit 1; }

git grep -nIE "$pattern" -- . ':!.github/workflows/ci.yml' ':!CONTRIBUTING.md' \
        ':!RELEASING.md' ':!SECURITY.md' ':!scripts/make_history.sh' \
  | grep -viE 'fake|dummy|example|placeholder|changeme|redacted|not[-_]a[-_]real|__GENERATE__|<[A-Z_]+>'
```

- [ ] Zero hits. The exclusions are CI's own: those paths quote or document the pattern and
      therefore always match themselves. The trailing filter is CI's too - it drops lines that
      announce themselves as fixtures.
- [ ] `bash scripts/make_history.sh --dry-run` reports **SECRET SCAN: PASS**. It lifts the same
      one pattern and runs it over the candidate file set, including files that are still
      untracked - which `git grep` alone would miss, and which is the whole reason this step
      exists in addition to the command above. It also **self-tests the pattern first**, against
      a sample of every shape, and refuses to report PASS if the pattern has stopped matching
      them; a scan that cannot be shown to catch anything must not be allowed to reassure you.
- [ ] No `.env`, `config.yaml`, `auth/`, `*.pem`, `*.key` or `gateway/data/` in the tree:
      `git ls-files | grep -nE '(^|/)(auth/|\.env$|config\.yaml$)|\.(pem|key)$'`
- [ ] `deploy/engine-bin/` contains only its `README.md`. The CLIProxyAPI binary is never
      redistributed and a 40MB blob cannot be removed from published history.
- [ ] Skim `git log -p` for the release range. Do it even though the scan passed. The scan knows
      the shapes enumerated in that one pattern in `ci.yml` — provider keys, GitHub tokens, AWS
      key ids, Slack tokens, Stripe keys, bcrypt hashes, PEM private keys, JWTs, operator paths
      and GCP project identifiers — and reality knows more. Do not write a count here; the last
      one said "four" long after the pattern had grown past twenty, and a stale number in a
      security step is worse than no number, because it tells you the coverage is small enough
      to hold in your head.

### 3.3 Claims audit

This is the step that distinguishes this project from a normal one.

Part of it *is* automated now, and knowing exactly which part is what keeps the rest from being
skipped. `tools/sitecheck.py`, run by the `published-numbers` job, accounts for every figure on
every file under `site/` — including files added after it was last edited, which it refuses to
classify silently. Two negative controls in that job plant a bogus hit rate and a new file and
require the checker to go red, because a guard that cannot be shown to fail is decorative; that
is not hypothetical here, the guard's regex once could not see a decimal and printed OK over the
headline number for as long as nobody looked.

What it cannot judge: whether a number that is *correct* is also *qualified*. A page can pass
`sitecheck.py` and still be a lie by omission. `sitecheck.py` covers `site/`, not `README.md`,
`docs/` or the release notes you are about to write. So:

**Every** quantitative claim in `README.md`, `docs/`, `assets/`, `site/` and the release notes
must satisfy all of:

- [ ] The number is reproducible by a command that is printed next to it.
- [ ] Its conditions travel with it: date, one machine, single run per configuration, engine
      version.
- [ ] The cache figure is labelled **warm-only** wherever it appears, and the 0% cold round is
      stated in the same place - not one link away.
- [ ] Nothing implies a live web search happens. It does not: measured 2026-07-21, the Gemini
      upstream said the year was 2024 and the Grok upstream said 2025.
- [ ] Nothing implies this is a model, a training run, a fine-tune, or a source of free credits,
      and no specific dollar figure of free credit appears anywhere.
- [ ] No comparative claim against another provider or proxy appears. None was measured.
- [ ] CLIProxyAPI is credited prominently, by name, with a link, and described as third party.
      Its version (7.1.23) appears next to every measured number.
- [ ] The shim is described as a workaround for engines older than v7.2.93, not as a feature.
- [ ] No third-party logos, no testimonials, no invented endorsements.

If a number changed since the last release, it gets a **new dated entry**. Numbers are not edited
in place.

### 3.4 Documentation consistency

- [ ] `README.md`, `docs/FINDINGS.md`, `docs/BENCHMARK.md` and `assets/README.md` agree on every
      shared figure. They repeat the same numbers on purpose; check them against each other.
- [ ] Every internal link resolves (moved a file? check the anchors too).
- [ ] `CHANGELOG.md` describes what a *user* observes, not which files moved.
- [ ] `docs/REPO_METADATA.md` still matches what is actually in the GitHub About panel.
- [ ] The install instructions were run once, from scratch, on a machine that has never had this
      project on it. "It works on the dev box" is not evidence.

### 3.5 Dependencies

- [ ] `project.dependencies` is still empty. If a release added one, that is a MAJOR-shaped
      decision and needs an explicit `Changed` entry explaining why the copy-one-file-and-run
      property was given up.
- [ ] Open Dependabot PRs triaged - merged or explicitly deferred with a reason.

### 3.6 Is what we deployed what we wrote?

**This step has no manual alternative and no "looks fine" version. Run the command.**

Everything above this line asks whether the tree is self-consistent. None of it can tell you what
a visitor to `https://yangble5.com` is actually served, and that is not a theoretical gap:

* The deployed pages were once a **full day older than the repo**, and nothing compared them. Six
  audit findings cited line numbers that were wrong by 100–470 lines, and several defects recorded
  as fixed were still live. Every check in section 3 was green the whole time, because every check
  in section 3 was looking at the repo.
* Cloudflare's Email Address Obfuscation rewrote `--email you@example.com` inside a `<pre>` on the
  live page, so the **published install command was broken for every visitor** while the origin
  served the correct bytes. Nothing that checks the origin can see this. Neither can `sha256sum`
  against a file on disk.

```bash
# From your laptop. NOT from the VPS, and not on any machine whose resolver
# points yangble5.com at the origin.
python tools/drift_check.py
```

Better, if you have `bash` and thirty spare seconds — it runs the same comparison plus the
vantage-point guard and the published-digest check, spends no tokens, and needs no API key:

```bash
bash deploy/smoke_test.sh --no-spend --base-url https://yangble5.com
```

- [ ] Exit status 0, and it named every published file. Read the list. "0 problems" is also what a
      check that examined nothing prints.
- [ ] **It ran from outside the origin.** A hosts entry, a split-horizon resolver, a tunnel, or
      simply being logged into the server sends the request straight to the origin and skips the
      CDN — and the CDN is the half of the path that has actually corrupted a published file here.
      Such a run does not prove nothing; it proves the origin is correct, which was *already true*
      on the day of the second incident while every visitor was being served a broken install
      command. It answers a question nobody was asking. `deploy/smoke_test.sh` check 11 refuses to
      report a result when the peer that answered was loopback, an RFC1918 address, or an address
      on the machine running it; `python tools/drift_check.py` on its own does not, so this box is
      yours to tick honestly.
- [ ] Any difference was resolved by **deploying**, never by editing the repo to match what is
      live, and never by adding to `EDGE_STRIPS` in `tools/drift_check.py`. That list is the set of
      edge transformations this project has agreed to; every entry in it is a difference the check
      will never report again. Adding one to make a red run green is how the next rewrite ships.

The same comparison runs two other ways, and neither replaces this one:

| Where | When | Why it is not enough on its own |
|---|---|---|
| `deploy/smoke_test.sh` check 11 | whenever an operator runs the post-deploy smoke test | Only if someone runs it, and only for the deployment they just made |
| `live-site-drift` job in `ci.yml` | daily, on a schedule | Up to 24 hours late, and GitHub silently disables scheduled workflows in repositories inactive for 60 days |

If `python` is not available where you are, `deploy/smoke_test.sh` check 12 still verifies each
published `.sha256` against its published payload over the network. It catches a rewritten or
half-deployed installer. It **cannot** see a page that is merely stale, which was the first
incident. It is not a substitute for this step.

---

## 4. Cutting the release

```bash
# 1. Version bump + changelog, in one commit.
$EDITOR pyproject.toml CHANGELOG.md
git add pyproject.toml CHANGELOG.md
git commit -m "chore(release): 0.2.0"

# 2. Annotated tag. The message is the changelog section for this version,
#    so `git show vX.Y.Z` is self-contained for anyone without network access.
git tag -a v0.2.0 -F - <<'TAG'
v0.2.0

<paste the CHANGELOG section for this version>
TAG

# 3. Publish the branch and the tag (deliberately manual, and deliberately not
#    scripted anywhere in this repository).
#      git push origin main
#      git push origin v0.2.0
```

> `scripts/make_history.sh` builds the *initial* history only. It never contacts a remote and
> refuses to run against a repository that already has commits. Publishing is always a human
> action.

Then draft the GitHub release from the tag, using the template below.

---

## 5. GitHub release notes template

Copy this verbatim and fill it in. The disclosure block is not optional and is not shortened -
release notes get screenshotted and quoted far more often than `docs/` does.

```markdown
## yangble5 vX.Y.Z

**One-line summary of what changed for a user.**

Built on [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) (MIT, third party, not ours -
this project is a configuration, a compatibility shim and a measurement harness around it).

### Highlights
- ...
- ...

### Added / Changed / Fixed
See [CHANGELOG.md](CHANGELOG.md#xyz---yyyy-mm-dd) for the full entry.

### Numbers in this release, and their conditions
| Measurement | Value | Conditions |
|---|---|---|
| Prompt-cache hit rate, token-weighted, **warm rounds only** | 99.53% | rounds 2-4 of 4; cold round 1 is 0% by construction; ~749K-token prefix; one Windows 11 machine; single run; 2026-07-21; CLIProxyAPI 7.1.23 |
| Largest prompt ingested without truncation | 748,918 tokens | same run; ingestion only - recall at that size was not tested |
| Latency, cold -> best warm round | 21.4 s -> 10.8 s | same run; rounds 3 and 4 were *slower* than the cold round; treat as an anecdote |

Reproduce with:
`python tools/cache_bench.py --model <your-alias> --prefix-tokens 600000 --rounds 4`

### Please read before quoting anything above
- The cache figure is **warm-only**. Every session pays one cold round at 0%. The same run is
  74.6% if the cold round is folded in.
- It is **prefix-size dependent**: 99.53% is what a ~749K prefix scored, and the tool's default
  prefix will not reach 99%. Hit rate rises with prefix size (direction observed); the magnitude
  at any other prefix size is **not in the released evidence set** and no second figure is
  published. Do not add one without a run to back it.
- **One machine, one run per configuration, no repetitions, no error bars**, on the date stated.
  Upstream caching behaviour changes without notice.
- **No live web search** happens through this proxy. Measured 2026-07-21: asked for the current
  year, the Gemini upstream answered 2024 and the Grok upstream answered 2025.
- `tools/claude_shim.py` is a **workaround** for engines older than v7.2.93, which fixed the bug
  upstream. On a newer engine, delete it.
- yangble5 is **not a model**, not a training run, not a fine-tune and not a source of free
  credits. Every token is billed to an upstream account someone configures and pays for.
- **This repository is software, not a service — but one public instance does exist.** The
  maintainer runs `https://yangble5.com` with self-serve registration **open**; it is funded by
  the operator's own personal upstream accounts, carries no SLA and no support, and the operator
  is a third party who can read every request sent to it. Do not describe the project as "not a
  hosted service" in release notes while that instance is up.

### Checksums
| File | SHA-256 |
|---|---|
| ... | ... |
```

---

## 6. Post-release

- [ ] Re-read the published release page as a stranger would. If any number on it can be
      screenshotted without its qualifier, move the qualifier up.
- [ ] Confirm the CI badge on the released tag is green.
- [ ] **If this release was also deployed to `https://yangble5.com`, run section 3.6 again, after
      the deploy.** Section 3.6 ran against the previous deployment; a release that changed
      anything under `site/` has invalidated that answer. A deploy that half-applied looks
      identical to a deploy that did not happen, and both look identical to success until
      something compares the bytes.
- [ ] Trigger `live-site-drift` by hand once (`Actions` -> `CI` -> `Run workflow`) rather than
      waiting up to 24 hours for the schedule. It is the same comparison run from a machine that
      has never had this repository on it, which is a slightly stronger statement than your
      laptop can make.
- [ ] Open a fresh `## [Unreleased]` section in `CHANGELOG.md` if step 2 did not.
- [ ] File issues for anything the checklist forced you to notice and skip.

## 7. Things this project does not do at release time

Stated explicitly so nobody has to guess:

- **No `npm publish`, no PyPI upload.** Not currently a published package. Adding one is a
  separate decision requiring the owner's explicit approval.
- **No binary artifacts.** The CLIProxyAPI engine is not redistributed, in any form, ever.
- **No automated announcement.** Anything that posts publicly is done by a human who has read
  section 3.3 that day.
