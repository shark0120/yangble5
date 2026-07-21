#!/usr/bin/env bash
#
# make_history.sh - build the initial, reviewable commit history for yangble5.
#
# WHY THIS EXISTS
# ---------------
# The repository was assembled as one working tree with no commits. A single
# "initial commit" containing the entire tree is unreviewable, and so is a history
# invented after the fact by a tool nobody can read. This script is the readable
# alternative: it declares, in one table you can audit in thirty seconds, which
# file lands in which commit and why - and then it refuses to do anything until
# a human passes --apply.
#
# WHAT IT WILL NEVER DO
# ---------------------
#   * It never talks to a remote. No fetch, no upload, no publishing of any kind.
#     Getting the history onto GitHub is a separate, manual, operator decision
#     documented in RELEASING.md.
#   * It never rewrites history. If the repository already has commits, the
#     script either reports "already applied" (when the existing history matches
#     the plan exactly) or refuses outright. There is no --force.
#   * It never runs by accident. With no flags it prints the plan and exits 0.
#
# USAGE
#   bash scripts/make_history.sh                # print the plan, change nothing
#   bash scripts/make_history.sh --dry-run      # full per-file assignment + verification
#   bash scripts/make_history.sh --apply        # actually create the commits
#
# OPTIONS
#   --dry-run              Print every file, the commit it lands in, and the
#                          "every file assigned exactly once" verification.
#   --apply                Create the commits. Requires the preflight to pass.
#   --author "N <e@x>"     Commit author. Default: $YANGBLE5_COMMIT_AUTHOR, else
#                          the repository's own user.name/user.email.
#   --date-base <iso>      Timestamp of the FIRST commit. Default 2026-07-21T09:00:00+08:00.
#   --date-step <minutes>  Spacing between commits. Default 7.
#   --date-offset <+HHMM>  Timezone offset recorded in the commits. Default +0800.
#   --date-now             Use the real current time instead of --date-base.
#   --branch <name|keep>   Branch to build the history on. Default "main"
#                          (the repository is currently on "master", which GitHub
#                          would keep as the default branch). "keep" leaves it alone.
#   -h, --help             This text.
#
# DATES AND AUTHORSHIP ARE EXPLICIT, ON PURPOSE
#   Both GIT_AUTHOR_DATE and GIT_COMMITTER_DATE are set for every commit, to the
#   same value, in git's raw "@<epoch> <offset>" form. Two consequences:
#     1. The resulting commit hashes are deterministic - the same tree, author and
#        base date produce the same history on any machine, so a reviewer can
#        re-run this and compare hashes.
#     2. Nothing is backdated silently. The default base date is the date the
#        measurements in docs/ were taken (2026-07-21); pass --date-now if you
#        would rather the history carry the real wall clock.
#
# EXIT CODES
#   0  plan/dry-run printed, or apply succeeded, or history already applied
#   1  refused (preflight failure, orphan files, existing history that differs)
#   2  usage error
#
set -euo pipefail

# ---------------------------------------------------------------------------
# 0. Argument parsing
# ---------------------------------------------------------------------------

MODE="plan"                       # plan | dry-run | apply
AUTHOR="${YANGBLE5_COMMIT_AUTHOR:-}"
DATE_BASE="2026-07-21T09:00:00+08:00"
DATE_STEP_MIN=7
DATE_OFFSET="+0800"
USE_NOW=0
TARGET_BRANCH="main"

die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
usage_die() { printf 'usage error: %s\nTry --help.\n' "$*" >&2; exit 2; }
say()  { printf '%s\n' "$*"; }
rule() { printf '%s\n' "------------------------------------------------------------------------"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)     MODE="dry-run" ;;
    --apply)       MODE="apply" ;;
    --author)      [ $# -ge 2 ] || usage_die "--author needs a value"; AUTHOR="$2"; shift ;;
    --date-base)   [ $# -ge 2 ] || usage_die "--date-base needs a value"; DATE_BASE="$2"; shift ;;
    --date-step)   [ $# -ge 2 ] || usage_die "--date-step needs a value"; DATE_STEP_MIN="$2"; shift ;;
    --date-offset) [ $# -ge 2 ] || usage_die "--date-offset needs a value"; DATE_OFFSET="$2"; shift ;;
    --date-now)    USE_NOW=1 ;;
    --branch)      [ $# -ge 2 ] || usage_die "--branch needs a value"; TARGET_BRANCH="$2"; shift ;;
    -h|--help)     awk 'NR>1 && !/^#/{exit} NR>1{sub(/^# ?/,""); print}' "$0"; exit 0 ;;
    *)             usage_die "unknown argument: $1" ;;
  esac
  shift
done

case "$DATE_STEP_MIN" in ''|*[!0-9]*) usage_die "--date-step must be a whole number of minutes" ;; esac
case "$DATE_OFFSET" in [+-][0-9][0-9][0-9][0-9]) ;; *) usage_die "--date-offset must look like +0800" ;; esac

# ---------------------------------------------------------------------------
# 1. The plan: commit groups, in build order
# ---------------------------------------------------------------------------
# Read this table first. Everything else in the file is enforcement.
#
# Order is the order a person would have built the project: scaffolding, then
# the CI that guards it, then the measurement tools that produced every number
# in the README, then the shim that made the client usable at all, then the
# service layers, then the documentation that explains the result.

GROUP_SUBJECT=(
  "chore: scaffold the repository with license, policies and packaging"
  "ci: matrix tests, a stdlib-only guard and a secret scan"
  "feat(tools): prompt-cache benchmark and durable stats sidecar"
  "fix(shim): backport upstream system-role mapping for antigravity streaming"
  "feat(gateway): public gateway with quota, rate limiting and abuse controls"
  "feat(byok): bring-your-own-key onboarding and engine config generator"
  "feat(deploy): hardened docker/caddy deployment bundle"
  "feat(site): static landing and verification pages for the public gateway"
  "docs: findings, benchmark methodology and public-service operating guide"
  "chore(release): changelog, release process and repository metadata"
)

# Commit bodies. Kept in one place so the history reads as prose, not as a
# sequence of one-line shrugs.
group_body() {
  case "$1" in
  0) cat <<'EOF'
MIT license, code of conduct, contribution guide, security policy, .gitignore
and packaging metadata.

Two packaging decisions are load-bearing and are commented in pyproject.toml
rather than left to be rediscovered:

- project.dependencies is deliberately empty. Everything under tools/ is
  standard-library only so a single file can be copied onto an operator's
  machine and run with the system Python: no virtualenv, no wheel build, no
  supply chain. A dependency here would quietly end that property.
- The gateway's fastapi/httpx live in an optional [gateway] extra, so installing
  this project purely for the measurement tools pulls in nothing at all.

.gitignore is written against a specific hazard: this code was ported out of a
live install that keeps OAuth tokens, API keys and a 40MB engine binary next to
the sources. Secrets, live configs and the binary are excluded by shape, not by
memory.
EOF
    ;;
  1) cat <<'EOF'
Three jobs, each answering a question that would otherwise be answered by
someone's recollection:

- pytest across {ubuntu, windows} x Python {3.11, 3.12, 3.13}, fail-fast off,
  because "fails on Windows only" is exactly the bug this matrix exists for.
- A separate job on a BARE interpreter that imports every module in tools/ and
  runs each one as a plain script. Running this inside the matrix job would
  prove nothing: fastapi and httpx are already installed there, so a stray
  third-party import in tools/ would sail straight through.
- A secret scan that fails the build on API-key-shaped strings, management-key
  prefixes or absolute operator paths. Cheap backstop for a repository ported
  out of an install that carried live credentials.

Plus issue and pull-request templates that ask for the engine version and
whether the failing request was streaming - the two facts that decide most of
the bug reports this project will receive.
EOF
    ;;
  2) cat <<'EOF'
The measurement half of the project. Every number quoted in README.md and
docs/ was produced by these two files, and they are the reason the claims can
be checked rather than believed.

- tools/cache_bench.py generates a calibrated long prefix, runs an N-round
  session against the operator's own upstream, and reports a TOKEN-WEIGHTED hit
  rate. Cold round 1 and warm rounds 2..N are reported separately and never
  averaged into one headline: round 1 is a cache write and is 0% by
  construction, so a blended number silently changes meaning with the round
  count.
- tools/cache_stats_sidecar.py records prompt tokens, cache_read tokens,
  uncached tail and latency per request into a JSON file that survives a
  restart, so a hit rate can be recomputed from raw per-request records instead
  of trusted.

Standard library only, and the CI job in the previous commit keeps it that way.
EOF
    ;;
  3) cat <<'EOF'
CLIProxyAPI 7.1.23's antigravity STREAMING translator passes messages[].role
through verbatim: it rewrites "assistant" to "model" and leaves everything else
alone. Claude Code >= 2.1.x injects a message with role "system" in the MIDDLE
of the messages array, and Gemini's streamGenerateContent rejects that role with
400 "Request contains an invalid argument".

The non-streaming generateContent path tolerates the same role. That is why the
failure looked intermittent and why it cost real time to find: it tracks
TRANSPORT, not content, so bisecting the prompt finds nothing.

Upstream fixed this in v7.2.93 by mapping system -> user in
internal/translator/antigravity/claude/antigravity_claude_request.go. This shim
applies that exact mapping in front of an older engine.

The one non-obvious safety property: a body that does not contain "system" is
forwarded BYTE FOR BYTE. Re-serialising an untouched body would change
whitespace, and the upstream prompt cache keys on exact bytes - a "harmless"
JSON round-trip on every request would have destroyed the cache result this
project is about. tests/test_claude_shim.py covers it.

This shim is a workaround, not a feature. On engine >= 7.2.93, point
ANTHROPIC_BASE_URL back at the engine port and delete the file; it holds no
state and nothing depends on it.
EOF
    ;;
  4) cat <<'EOF'
A FastAPI service for the case where the engine is exposed beyond localhost:
per-key quota accounting, rate limiting, durable usage records, upstream
selection and abuse controls.

Kept behind the optional [gateway] extra so the measurement tools still install
with zero dependencies. Nothing in tools/ imports anything from here.
EOF
    ;;
  5) cat <<'EOF'
Bring-your-own-key onboarding: renders an engine configuration from a template
so the operator supplies their own upstream credentials and this project never
handles, stores or ships anybody's keys.

The generated configuration uses a DIRECT 1:1 OAuth model alias and
routing.strategy fill-first. That is not a style preference - it is the fix for
the pool-rotation cache defect documented in docs/FINDINGS.md, so the tests
parse the rendered output with a real YAML implementation and assert the
property holds. "It looks like YAML" is not the property that matters.
EOF
    ;;
  6) cat <<'EOF'
Docker Compose stack (engine, gateway, Caddy), Caddy TLS drop-ins, a fail2ban
filter and jail template for authentication abuse, and harden.sh.

The CLIProxyAPI binary is deliberately NOT redistributed: it is somebody else's
MIT-licensed work, and a 40MB binary committed to git history cannot be taken
back out. deploy/engine-bin/ carries a README explaining where the operator
gets their own copy.

Every live artifact - .env, config.yaml, TLS material, Caddy drop-ins - is an
.example or a template. The real ones are ignored by .gitignore.
EOF
    ;;
  7) cat <<'EOF'
Static landing page and a verification page for anyone pointed at a hosted
instance, plus the install/uninstall scripts they are told to read before
running.

The pages state what this is - a proxy configuration and a measurement harness
in front of a third-party engine - and what it is not: not a model, not a
training run, not a hosted source of free credits. Every token is billed to the
upstream account the operator configured.
EOF
    ;;
  8) cat <<'EOF'
docs/FINDINGS.md    - every finding with an explicit status label (Verified
                      (source/binary) / Measured / Observed / Reasoned) and an
                      appendix listing what was NOT verified.
docs/BENCHMARK.md   - methodology: how to reproduce the numbers, and the ways a
                      cache benchmark lies to the person running it.
docs/OPERATING_A_PUBLIC_SERVICE.md
                    - what changes when other people can reach your instance.
docs/diagrams/      - request path, where the prompt cache actually lives, and
                      the failure mode that was fixed. Colour-coded by who wrote
                      each box, because that decides who deserves credit and who
                      to file bugs against.
README.md           - the results table, with its footnotes attached rather than
                      relegated to a link.
assets/             - self-authored artwork only: a plain-SVG social card with
                      no external references and no third-party logos. Every
                      number on the card carries its qualifier, because a social
                      card is exactly where numbers get quoted without their
                      footnotes.

The disclosures travel with the claims, in all of these files: the 99.53% figure
is WARM-ONLY (rounds 2-4; cold round 1 is 0% by construction and every session
pays one); it was measured on ONE Windows 11 machine, ONE run per configuration,
on 2026-07-21; it is prefix-size dependent and is not a universal number; the
latency improvement did not hold on rounds 3 and 4; nothing routed through this
proxy performs a live web search (asked for the current year, Gemini answered
2024 and Grok answered 2025); and CLIProxyAPI is a third-party MIT project
without which none of this works.
EOF
    ;;
  9) cat <<'EOF'
CHANGELOG.md in Keep a Changelog format, RELEASING.md with the pre-release
checklist and release-notes template, docs/REPO_METADATA.md with the exact
strings for the GitHub About panel, and this script.

scripts/make_history.sh is what produced the history you are reading. It is
committed so the history is auditable rather than merely plausible: the file/
commit assignment is a table at the top of the script, it verifies that every
tracked file lands in exactly one commit before it will run, it never contacts a
remote, and it refuses to touch a repository that already has commits.
EOF
    ;;
  *) die "no body defined for group $1" ;;
  esac
}

# ---------------------------------------------------------------------------
# 2. File -> commit assignment rules
# ---------------------------------------------------------------------------
# "<glob>|<group index>", evaluated in order, FIRST MATCH WINS. Order matters:
# the specific entries (tools/claude_shim.py, gateway/byok.py) must precede the
# directory catch-alls that would otherwise swallow them.
#
# A file that matches no rule is an ORPHAN and is a hard failure - the script
# will not commit it and will not silently drop it. Adding a new top-level area
# to the repository means adding a line here, which is the point.

RULES=(
  # --- 0: scaffolding -----------------------------------------------------
  ".gitignore|0"
  "LICENSE|0"
  "CODE_OF_CONDUCT.md|0"
  "CONTRIBUTING.md|0"
  "SECURITY.md|0"
  "pyproject.toml|0"

  # --- 1: CI and repository automation ------------------------------------
  ".github/*|1"

  # --- 3: the shim (before the tools/ catch-all) --------------------------
  "tools/claude_shim.py|3"
  "tests/test_claude_shim.py|3"

  # --- 2: measurement tooling ---------------------------------------------
  "tools/*|2"
  "tests/conftest.py|2"
  "tests/test_cache_*.py|2"

  # --- 5: byok (before the gateway/ catch-all) ----------------------------
  "byok/*|5"
  "gateway/byok.py|5"
  "tests/test_byok*.py|5"

  # --- 4: gateway ---------------------------------------------------------
  "gateway/*|4"
  "tests/test_gateway*.py|4"

  # --- 6: deployment bundle -----------------------------------------------
  "deploy/*|6"

  # --- 7: public site -----------------------------------------------------
  "site/*|7"

  # --- 9: release engineering (before the docs/ catch-all) ----------------
  "CHANGELOG.md|9"
  "RELEASING.md|9"
  "docs/REPO_METADATA.md|9"
  "scripts/*|9"

  # --- 8: documentation ---------------------------------------------------
  "README.md|8"
  "docs/*|8"
  "assets/*|8"
)

# ---------------------------------------------------------------------------
# 3. Preflight
# ---------------------------------------------------------------------------

SELF="$0"

# 3a. Self-check: this script must never grow a remote-writing or
#     history-rewriting operation. The patterns are assembled from fragments so
#     that this check cannot match its own source.
FORBIDDEN_OPS="$(printf '%s' \
  'git[[:space:]]+p' 'ush' \
  '|fil' 'ter-branch' \
  '|fil' 'ter-repo' \
  '|reset[[:space:]]+--h' 'ard' \
  '|git[[:space:]]+reb' 'ase' \
  '|commit[[:space:]]+--am' 'end')"
if grep -nE "$FORBIDDEN_OPS" "$SELF" >/dev/null 2>&1; then
  die "self-check failed: this script contains a remote-writing or history-rewriting operation.
     That is not allowed here. Remove it, or move the operation into RELEASING.md
     where a human performs it deliberately."
fi

command -v git >/dev/null 2>&1 || die "git not found on PATH"

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || die "not inside a git repository"
cd "$REPO_ROOT"

# 3b. Identity check: refuse to run anywhere except this repository.
[ -f pyproject.toml ] || die "no pyproject.toml at $REPO_ROOT - this is not the yangble5 repository"
grep -q '^name *= *"yangble5"' pyproject.toml \
  || die "pyproject.toml at $REPO_ROOT is not yangble5's - refusing to touch this repository"

# 3c. Live-install check. The operator's running install lives in a sibling
#     directory and holds real OAuth tokens. Committing from it would publish
#     credentials, so any marker of a live install is a hard stop.
for marker in auth config.yaml config.local.yaml gateway/data; do
  [ -e "$marker" ] && die "found '$marker' in $REPO_ROOT.
     That is a LIVE INSTALL marker, not an open-source tree. Refusing to create
     history from a directory that may contain credentials."
done

# 3d. No merge/rebase/bisect in progress.
GIT_DIR_PATH="$(git rev-parse --git-dir)"
for state in MERGE_HEAD CHERRY_PICK_HEAD BISECT_LOG rebase-merge rebase-apply; do
  [ -e "$GIT_DIR_PATH/$state" ] && die "repository is mid-operation ($state). Finish or abort it first."
done

# ---------------------------------------------------------------------------
# 4. Collect candidate files
# ---------------------------------------------------------------------------
# The universe is everything git would put in a commit: files already in the
# index, plus untracked files that .gitignore does not exclude. Using only
# `git ls-files` would silently drop the untracked half of the tree, which is
# the failure mode this script exists to prevent.

FILES=()
while IFS= read -r -d '' f; do
  case "$f" in
    *$'\n'*) die "candidate path contains a newline: $f - refusing (the assignment report cannot represent it safely)" ;;
  esac
  FILES+=("$f")
done < <(git ls-files -z --cached --others --exclude-standard)

[ "${#FILES[@]}" -gt 0 ] || die "no candidate files found - nothing to commit"

N_TRACKED="$(git ls-files | wc -l | tr -d ' ')"
N_UNTRACKED="$(git ls-files --others --exclude-standard | wc -l | tr -d ' ')"

# 4b. Secret scan over the candidate set.
#
#     THE PATTERN IS NOT DEFINED HERE. There is exactly one authoritative copy,
#     in the `no-secrets` job of .github/workflows/ci.yml, and this script lifts
#     it out - the same lift CONTRIBUTING.md documents for running the scan by
#     hand. This file used to carry its own four-shape pattern and describe it as
#     "the same shapes CI rejects, slightly broadened". It was neither: CI knows
#     about two dozen shapes, and the local copy missed every OpenAI-style key
#     with a hyphen in it (sk-ant-api03-, sk-or-v1-, sk-proj-) as well as
#     forward-slash operator paths. That is what a second copy does - it does not
#     announce that it has drifted, it just quietly stops covering things.
#
#     What this script adds over CI's `git grep` is REACH, not a different
#     pattern: the candidate set below includes UNTRACKED files, which is exactly
#     where a credential copied in from somewhere else sits before anybody has
#     decided to commit it. RELEASING.md 3.2 leans on that reach, so the pattern
#     it reaches with had better be the real one.
#
#     `sed` rather than `grep -oP`: PCRE is not available on every grep this
#     script has to run under (BSD/macOS has none), and CONTRIBUTING.md already
#     records that under Git Bash a locale-less `grep -oP` yields an EMPTY
#     pattern silently - which would turn this scan into a no-op. Both lifts are
#     therefore checked for emptiness, and then the self-test below proves the
#     lifted pattern actually matches things before anything relies on it.
CI_WORKFLOW=".github/workflows/ci.yml"
[ -f "$CI_WORKFLOW" ] \
  || die "$CI_WORKFLOW is missing - it holds the only copy of the secret-scan pattern, so the scan cannot run"

SECRET_RE="$(sed -n "s/.*git grep -nIE '\([^']*\)'.*/\1/p" "$CI_WORKFLOW" | head -1)"
[ -n "$SECRET_RE" ] \
  || die "could not lift the secret-scan pattern out of $CI_WORKFLOW (the 'git grep -nIE' line changed shape?)"

# The same trailing filter CI applies: drop lines that announce themselves as
# fixtures. A leaked credential does not contain the word "fake"; a test fixture
# that says FAKE four times is not a leak, and reporting it every run is how a
# check stops being read.
FIXTURE_RE="$(sed -n "s/.*grep -viE '\([^']*\)'.*/\1/p" "$CI_WORKFLOW" | head -1)"
[ -n "$FIXTURE_RE" ] \
  || die "could not lift the fixture filter out of $CI_WORKFLOW (the 'grep -viE' line changed shape?)"

# 4b-i. SELF-TEST - run BEFORE the pattern is trusted with anything.
#
#     A scanner that has never been shown to catch anything is a decoration, and
#     a silently-broken one is worse than none: it converts "we scan for this"
#     into a false assurance that a release checklist then rests on. So: assert
#     the lifted pattern matches a known sample of every shape this repository
#     has actually been at risk of, and assert it does NOT match an ordinary
#     line. The negative case is not decoration either - an empty or over-broad
#     lift would sail through every positive assertion.
#
#     The samples are assembled from ADJACENT QUOTED FRAGMENTS ('PRIV''ATE') so
#     the bytes of this file never spell out the strings it hunts. That is the
#     technique ci.yml already uses on itself, and it is safe here only because
#     the assertions below fail loudly if the assembly ever breaks - which is
#     precisely the failure this whole block exists to make impossible.
self_test_secret_re() {
  local label sample entry rc=0
  local -a POSITIVE=(
    "openai key|sk-""proj-AbCdEfGhIjKlMnOpQrSt_1234-5678"
    "anthropic key|sk-""ant-api03-AAbbCCddEEffGGhh_iiJJkkLL-mmNN"
    "openrouter key|sk-""or-v1-0123456789abcdef0123456789abcdef"
    "google api key|AIza""SyA0000000000000000000000000000000a"
    "oauth token|ya29"".A0AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "github token|ghp""_000000000000000000000000000000000000"
    "aws key id|AKIA""0000000000000000"
    "slack token|xoxb""-0000000000-abcdefghij"
    "bcrypt hash|\$2b\$12\$0000000000000000000000000000000000000000000000000000a"
    "private key|-----BEGIN OPENSSH PRIV""ATE KEY-----"
    "private key (bare)|-----BEGIN PRIV""ATE KEY-----"
    "legacy mgmt key|yang-""admin-0123456789"
    "operator path (backslash)|C:\\Us""ers\\Someone\\Desktop\\notes.txt"
    "operator path (forward slash)|C:/Us""ers/Someone/Desktop/notes.txt"
  )
  for entry in "${POSITIVE[@]}"; do
    label="${entry%%|*}"
    sample="${entry#*|}"
    if ! printf '%s\n' "$sample" | grep -qIE "$SECRET_RE"; then
      say "  SELF-TEST FAIL: the secret pattern does not match a $label sample."
      rc=1
    fi
  done
  # Negative: an ordinary line must NOT match, or the pattern is matching
  # everything and every "PASS" it prints is meaningless.
  if printf '%s\n' "a perfectly ordinary line of prose with no credential in it" \
      | grep -qIE "$SECRET_RE"; then
    say "  SELF-TEST FAIL: the secret pattern matches ordinary text - it is too broad to mean anything."
    rc=1
  fi
  return "$rc"
}

self_test_secret_re \
  || die "the secret-scan self-test failed (see above). The scan is broken, so its result cannot be trusted. Refusing to continue."

SCAN_HITS=""
for f in "${FILES[@]}"; do
  # These four quote the pattern (or document it) and so always match themselves.
  # This is the same exclusion list ci.yml uses, minus this script: the fragment
  # trick above means this file has nothing to hide from its own scan, which is
  # the property the exclusion list is there to work around.
  case "$f" in
    .github/workflows/ci.yml|CONTRIBUTING.md|RELEASING.md|SECURITY.md) continue ;;
  esac
  [ -f "$f" ] || continue
  # NOTE: the exit status of a pipeline is the LAST command's, which is 0 even
  # when grep matched nothing - so test the captured text, not the status.
  hit="$(grep -nIE "$SECRET_RE" -- "$f" 2>/dev/null | grep -viE "$FIXTURE_RE" | head -3 || true)"
  if [ -n "$hit" ]; then
    SCAN_HITS="${SCAN_HITS}${f}:
${hit}
"
  fi
done

# ---------------------------------------------------------------------------
# 5. Classify
# ---------------------------------------------------------------------------

N_GROUPS="${#GROUP_SUBJECT[@]}"
declare -a GROUP_FILES
declare -a GROUP_COUNT
i=0
while [ "$i" -lt "$N_GROUPS" ]; do GROUP_FILES[$i]=""; GROUP_COUNT[$i]=0; i=$((i + 1)); done

ORPHANS=""
N_ORPHANS=0
AMBIGUOUS=""
N_AMBIGUOUS=0
N_ASSIGNED=0
ALL_ASSIGNED=""

for f in "${FILES[@]}"; do
  winner=""
  matched_rules=""
  n_match=0
  for entry in "${RULES[@]}"; do
    pat="${entry%%|*}"
    grp="${entry##*|}"
    # shellcheck disable=SC2254  # the glob must stay unquoted: it is a pattern
    case "$f" in
      $pat)
        n_match=$((n_match + 1))
        matched_rules="${matched_rules}${matched_rules:+, }${pat}->${grp}"
        [ -z "$winner" ] && winner="$grp"
        ;;
    esac
  done
  if [ -z "$winner" ]; then
    ORPHANS="${ORPHANS}  ${f}
"
    N_ORPHANS=$((N_ORPHANS + 1))
    continue
  fi
  if [ "$n_match" -gt 1 ]; then
    AMBIGUOUS="${AMBIGUOUS}  ${f}
      matched: ${matched_rules}   (first rule wins -> commit $((winner + 1)))
"
    N_AMBIGUOUS=$((N_AMBIGUOUS + 1))
  fi
  GROUP_FILES[$winner]="${GROUP_FILES[$winner]}${f}
"
  ALL_ASSIGNED="${ALL_ASSIGNED}${f}
"
  GROUP_COUNT[$winner]=$(( ${GROUP_COUNT[$winner]} + 1 ))
  N_ASSIGNED=$((N_ASSIGNED + 1))
done

# Independent recount: sum the per-group lists rather than trusting the counter
# above. If these two ever disagree, a file was assigned twice.
SUM_GROUPS=0
i=0
while [ "$i" -lt "$N_GROUPS" ]; do
  SUM_GROUPS=$(( SUM_GROUPS + ${GROUP_COUNT[$i]} ))
  i=$((i + 1))
done

N_DUPLICATE=0
DUPES="$(printf '%s' "$ALL_ASSIGNED" | grep -v '^$' | sort | uniq -d || true)"
[ -n "$DUPES" ] && N_DUPLICATE="$(printf '%s\n' "$DUPES" | wc -l | tr -d ' ')"

N_EMPTY_GROUPS=0
i=0
while [ "$i" -lt "$N_GROUPS" ]; do
  [ "${GROUP_COUNT[$i]}" -eq 0 ] && N_EMPTY_GROUPS=$((N_EMPTY_GROUPS + 1))
  i=$((i + 1))
done

VERIFY_OK=1
[ "$N_ORPHANS"   -eq 0 ] || VERIFY_OK=0
[ "$N_DUPLICATE" -eq 0 ] || VERIFY_OK=0
[ "$N_ASSIGNED"  -eq "${#FILES[@]}" ] || VERIFY_OK=0
[ "$SUM_GROUPS"  -eq "${#FILES[@]}" ] || VERIFY_OK=0

# ---------------------------------------------------------------------------
# 6. Resolve author and dates
# ---------------------------------------------------------------------------

if [ -z "$AUTHOR" ]; then
  cfg_name="$(git config user.name || true)"
  cfg_mail="$(git config user.email || true)"
  if [ -n "$cfg_name" ] && [ -n "$cfg_mail" ]; then
    AUTHOR="$cfg_name <$cfg_mail>"
  fi
fi
case "$AUTHOR" in
  *"<"*"@"*">"*) ;;
  "") die "no commit author. Set git config user.name/user.email, export
     YANGBLE5_COMMIT_AUTHOR, or pass --author \"Name <mail@example.com>\"." ;;
  *) die "--author must look like: Name <mail@example.com> (got: $AUTHOR)" ;;
esac

if [ "$USE_NOW" -eq 1 ]; then
  BASE_EPOCH="$(date +%s)"
  DATE_SOURCE="--date-now (real wall clock at run time)"
else
  BASE_EPOCH="$(date -d "$DATE_BASE" +%s 2>/dev/null)" \
    || die "could not parse --date-base '$DATE_BASE' (needs GNU date; try 2026-07-21T09:00:00+08:00)"
  DATE_SOURCE="--date-base $DATE_BASE, +${DATE_STEP_MIN}min per commit, offset $DATE_OFFSET"
fi

commit_date_for() { printf '@%s %s' "$(( BASE_EPOCH + $1 * DATE_STEP_MIN * 60 ))" "$DATE_OFFSET"; }

CURRENT_BRANCH="$(git symbolic-ref --short -q HEAD || echo '(detached)')"
HAS_COMMITS=0
git rev-parse --verify -q HEAD >/dev/null 2>&1 && HAS_COMMITS=1

# ---------------------------------------------------------------------------
# 7. Report
# ---------------------------------------------------------------------------

rule
say "yangble5 - initial history plan"
rule
say "repository      : $REPO_ROOT"
say "branch          : $CURRENT_BRANCH$( [ "$TARGET_BRANCH" != "keep" ] && [ "$TARGET_BRANCH" != "$CURRENT_BRANCH" ] && printf '  ->  %s (on --apply)' "$TARGET_BRANCH" )"
say "existing commits: $( [ "$HAS_COMMITS" -eq 1 ] && git rev-list --count HEAD || echo 0 )"
say "author          : $AUTHOR"
say "dates           : $DATE_SOURCE"
say "candidate files : ${#FILES[@]}  ($N_TRACKED already staged, $N_UNTRACKED untracked and not ignored)"
say "mode            : $MODE"
say ""

i=0
while [ "$i" -lt "$N_GROUPS" ]; do
  n="${GROUP_COUNT[$i]}"
  printf '  %2d. [%3d files] %s%s\n' "$((i + 1))" "$n" "${GROUP_SUBJECT[$i]}" \
    "$( [ "$n" -eq 0 ] && printf '   (EMPTY - will be skipped)' )"
  if [ "$MODE" = "dry-run" ] && [ "$n" -gt 0 ]; then
    printf '      commit date: %s\n' "$(commit_date_for "$i")"
    printf '%s' "${GROUP_FILES[$i]}" | while IFS= read -r line; do
      [ -n "$line" ] && printf '        %s\n' "$line"
    done
    printf '\n'
  fi
  i=$((i + 1))
done

say ""
rule
say "VERIFICATION - every candidate file lands in exactly one commit"
rule
printf '  candidate files (index + untracked, ignores applied) : %s\n' "${#FILES[@]}"
printf '  assigned to a commit                                 : %s\n' "$N_ASSIGNED"
printf '  sum of per-commit file lists (independent recount)   : %s\n' "$SUM_GROUPS"
printf '  unassigned (ORPHANS)                                 : %s\n' "$N_ORPHANS"
printf '  appearing in more than one commit (DUPLICATES)       : %s\n' "$N_DUPLICATE"
printf '  matched by >1 rule, resolved by rule order           : %s\n' "$N_AMBIGUOUS"
printf '  commits with no files (skipped)                      : %s\n' "$N_EMPTY_GROUPS"

if [ "$N_ORPHANS" -gt 0 ]; then
  say ""
  say "  ORPHANED FILES - no rule matches these, so they would be silently left"
  say "  out of the history. Add a rule in section 2 of this script:"
  printf '%s' "$ORPHANS"
fi
if [ "$N_DUPLICATE" -gt 0 ]; then
  say ""
  say "  DUPLICATED FILES:"
  printf '%s\n' "$DUPES" | sed 's/^/    /'
fi
if [ "$MODE" = "dry-run" ] && [ "$N_AMBIGUOUS" -gt 0 ]; then
  say ""
  say "  MULTI-RULE MATCHES (not an error - reported so rule order stays honest):"
  printf '%s' "$AMBIGUOUS"
fi

say ""
if [ "$VERIFY_OK" -eq 1 ]; then
  say "  RESULT: PASS - all ${#FILES[@]} candidate files are assigned to exactly one commit."
else
  say "  RESULT: FAIL - the assignment is not a partition of the candidate files."
fi

if [ -n "$SCAN_HITS" ]; then
  say ""
  rule
  say "SECRET SCAN: FAIL - candidate files contain secret-shaped strings or"
  say "absolute operator paths. These must not enter git history."
  rule
  printf '%s' "$SCAN_HITS"
else
  say ""
  say "  SECRET SCAN: PASS - no key-shaped strings or absolute operator paths in the candidate set."
fi

say ""
rule

if [ "$MODE" != "apply" ]; then
  say "Nothing was changed. This script does not contact any remote and does not"
  say "rewrite existing history."
  if [ "$MODE" = "plan" ]; then
    say "Run with --dry-run for the full per-file assignment, then --apply to commit."
  else
    say "Re-run with --apply to create these commits. Publishing the result is a"
    say "separate manual step - see RELEASING.md."
  fi
  exit 0
fi

# ---------------------------------------------------------------------------
# 8. Apply
# ---------------------------------------------------------------------------

# 8a. Idempotency / no-rewrite. Running --apply twice must be safe.
if [ "$HAS_COMMITS" -eq 1 ]; then
  planned=""
  i=0
  while [ "$i" -lt "$N_GROUPS" ]; do
    [ "${GROUP_COUNT[$i]}" -gt 0 ] && planned="${planned}${GROUP_SUBJECT[$i]}
"
    i=$((i + 1))
  done
  existing="$(git log --reverse --format='%s')
"
  if [ "$existing" = "$planned" ]; then
    say "Already applied: the existing history matches this plan exactly ($(git rev-list --count HEAD) commits)."
    say "Nothing to do."
    exit 0
  fi
  die "this repository already has $(git rev-list --count HEAD) commit(s) and they do not match the plan.
     Refusing to touch existing history - that is what this script exists to avoid.
     Inspect with: git log --oneline
     If the history is wrong and unpublished, remove it deliberately by hand."
fi

if git for-each-ref --format='%(refname)' refs/remotes | grep -q .; then
  die "remote-tracking refs exist, so this history may already be published.
     Refusing to build an initial history on top of it."
fi

[ "$VERIFY_OK" -eq 1 ] || die "verification failed (see above). Refusing to build a history that
     drops or duplicates files."
[ -z "$SCAN_HITS" ] || die "the secret scan failed (see above). Refusing to commit."

# 8b. Working-tree expectation. With no commits, the only states this script
#     understands are: staged-new ("A "), staged-new-then-modified ("AM"), and
#     untracked ("??"). Anything else - a deletion, a rename, a conflict - means
#     the tree is not what this plan was written against.
UNEXPECTED=""
while IFS= read -r -d '' entry; do
  code="${entry:0:2}"
  path="${entry:3}"
  case "$code" in
    "A "|"AM"|"??") ;;
    *) UNEXPECTED="${UNEXPECTED}  [$code] $path
" ;;
  esac
done < <(git status --porcelain -z)

if [ -n "$UNEXPECTED" ]; then
  die "the working tree has changes this plan does not expect:
$UNEXPECTED     Expected only added ('A '/'AM') and untracked ('??') entries in a
     repository with no commits. Resolve these first."
fi

# 8c. Branch. Only ever done on an unborn HEAD, so no history moves.
if [ "$TARGET_BRANCH" != "keep" ] && [ "$TARGET_BRANCH" != "$CURRENT_BRANCH" ]; then
  say "==> moving unborn HEAD to refs/heads/$TARGET_BRANCH (was $CURRENT_BRANCH)"
  git symbolic-ref HEAD "refs/heads/$TARGET_BRANCH"
fi

# 8d. Commit, group by group.
#
# `git commit --only -- <paths>` builds the commit from exactly those paths and
# leaves the rest of the index alone. That matters here: the whole tree is
# already staged, so a plain `git commit` would put every file in commit 1.
made=0
i=0
while [ "$i" -lt "$N_GROUPS" ]; do
  n="${GROUP_COUNT[$i]}"
  if [ "$n" -eq 0 ]; then i=$((i + 1)); continue; fi

  paths=()
  while IFS= read -r line; do
    [ -n "$line" ] && paths+=("$line")
  done <<EOF
${GROUP_FILES[$i]}
EOF

  say "==> [$((i + 1))/$N_GROUPS] ${GROUP_SUBJECT[$i]}  ($n files)"
  git add -- "${paths[@]}"

  cdate="$(commit_date_for "$i")"
  GIT_AUTHOR_DATE="$cdate" GIT_COMMITTER_DATE="$cdate" \
    git commit -q --only --author="$AUTHOR" \
      -m "${GROUP_SUBJECT[$i]}" -m "$(group_body "$i")" -- "${paths[@]}"
  made=$((made + 1))
  i=$((i + 1))
done

# 8e. Post-conditions. Cheap, and they catch exactly the failure this script is
#     supposed to make impossible: a file that quietly did not make it in.
LEFTOVER="$(git status --porcelain | grep -v '^!!' || true)"
if [ -n "$LEFTOVER" ]; then
  say ""
  say "WARNING: the working tree is not clean after applying the plan. Files listed"
  say "below were not committed. This is a bug in the rules above, or the tree"
  say "changed while the script was running:"
  printf '%s\n' "$LEFTOVER" | sed 's/^/  /'
fi

FINAL_TRACKED="$(git ls-files | wc -l | tr -d ' ')"
say ""
rule
say "Done: $made commits, $FINAL_TRACKED files tracked at HEAD."
git log --oneline --reverse | sed 's/^/  /'
rule
say "Nothing has been published. Review with:"
say "  git log --stat"
say "and then follow RELEASING.md to publish and tag."
