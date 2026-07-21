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

To confirm no stale version strings remain:

```bash
git grep -nE '0\.1\.0' -- . ':!CHANGELOG.md'
```

---

## 3. Pre-release checklist

Every box must be ticked on the exact tree that will be tagged. Not "on a similar tree yesterday".

### 3.1 Tests and lint

```bash
python -m pip install -e ".[dev]"
python -m ruff check .
python -m ruff format --check tools
python -m pytest -q
```

- [ ] Full suite green locally.
- [ ] Green in CI on **both** operating systems and **all three** Python versions. A skipped
      Windows job is a red job.
- [ ] The `tools-are-stdlib-only` job passed. If it was skipped, the release does not go out:
      that job is the only thing keeping `tools/` copy-and-run-able.
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
- [ ] Skim `git log -p` for the release range. Do it even though the scan passed - the scan knows
      four shapes of secret and reality knows more.

### 3.3 Claims audit

This is the step that distinguishes this project from a normal one. **Every** quantitative claim
in `README.md`, `docs/`, `assets/`, `site/` and the release notes must satisfy all of:

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
- yangble5 is **not a model**, not a training run, not a fine-tune, not a hosted service and not a
  source of free credits. Every token is billed to the upstream account *you* configure.

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
- [ ] Open a fresh `## [Unreleased]` section in `CHANGELOG.md` if step 2 did not.
- [ ] File issues for anything the checklist forced you to notice and skip.

## 7. Things this project does not do at release time

Stated explicitly so nobody has to guess:

- **No `npm publish`, no PyPI upload.** Not currently a published package. Adding one is a
  separate decision requiring the owner's explicit approval.
- **No binary artifacts.** The CLIProxyAPI engine is not redistributed, in any form, ever.
- **No automated announcement.** Anything that posts publicly is done by a human who has read
  section 3.3 that day.
