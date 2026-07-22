# Contributing

Thanks for looking. This project is small and opinionated, so here is what is actually useful
and what will be turned down.

## The most valuable contribution

**Run the benchmark on your setup and post the output.** Every number in this repository is one
run, on one Windows machine, against one upstream. That is the weakest part of the project and
the easiest for you to improve:

```bash
python tools/cache_bench.py --model <your-alias> --prefix-tokens 600000 --rounds 4 --json
```

Open an issue with the JSON, your provider, your engine version, and your OS. A result that
**contradicts** ours is more welcome than one that confirms it - please say so plainly in the
title. See [`docs/BENCHMARK.md`](docs/BENCHMARK.md#running-it-against-a-different-provider) for
what has to be held equal for a comparison to mean anything.

## The house rule on claims

Every quantitative claim in this repository must be one of:

* **Measured** - accompanied by the exact command that reproduces it, and the raw record.
* **Verified** - traceable to source or to official documentation, with the reference inline.
* **Observed** - seen once, no repro script, and labelled as an anecdote in the same sentence.
* **Reasoned** - argued from the above, and labelled "not a measurement" in the same paragraph.

A PR that adds a number without one of those labels will be asked to remove the number. If you
need a figure we do not have, write "not measured". That is a perfectly good thing for a
document to say, and it costs the project nothing compared to being caught rounding up.

## Setup

Python 3.10 or newer (`requires-python = ">=3.10"`; CI runs 3.10, 3.11, 3.12, 3.13 and 3.14 on
Ubuntu and Windows). The floor is 3.10 rather than something newer because that is the system
Python on Ubuntu 22.04 LTS, and a self-hoster should not have to add a PPA to run this. Those
three places — the matrix, `requires-python`, and the `Programming Language :: Python`
classifiers — are checked against each other by the `offline-self-checks` job, so adding a
version means adding it in all of them or the build fails.

Nothing may use a stdlib name newer than the floor. The two that bite are `datetime.UTC` (3.11+;
use `timezone.utc`) and `tomllib` (3.11+; import it under `try/except ModuleNotFoundError` with
`tomli`). The bare-interpreter CI job runs on the floor precisely to catch these.

```bash
git clone https://github.com/shark0120/yangble5
cd yangble5
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"                          # pytest, ruff, pyyaml, and fastapi/httpx
pytest -q
```

`[dev]` deliberately carries fastapi and httpx by name rather than as a self-referencing
`yangble5[gateway]`: the gateway test module imports fastapi at collection time, so without them a
plain `pip install -e ".[dev]"` cannot even collect the suite. pyyaml and tomli are test-only -
nothing under `tools/` or `byok/` may import either.

`tools/` is standard library only, and stays that way: it has to run on a machine where somebody
is debugging a proxy, not on a curated CI image. A dedicated CI job installs **nothing** and
imports every tool with a bare interpreter, so a stray `import httpx` in `tools/` fails the
build. `gateway/` may use its declared extras (`fastapi`, `httpx`).

## Code style

* **Standard library first.** A new third-party dependency in `tools/` needs a strong argument.
* **Type hints on public functions.** `from __future__ import annotations` at the top.
* **Docstrings explain WHY, not what.** The what is in the code. If a line looks wrong until you
  know a fact about an upstream's behaviour, that fact belongs in a comment - the codebase is
  full of examples.
* **Pure functions where the logic lives**, I/O at the edges. That is why the interesting parts
  of `cache_bench.py` and `claude_shim.py` are unit-testable without a socket.
* **Lint everything, format only what CI formats.** Run exactly what CI runs:

  ```sh
  python -m ruff check .
  python -m ruff format --check tools byok
  ```

  `ruff check` is repo-wide and must be clean. `ruff format` is deliberately **scoped to `tools`
  and `byok`** — the only directories `ruff format` has ever been applied to. `gateway/` and
  `tests/` have not been, so `ruff format --check .` reports a double-digit pile of files it
  would rewrite. (No count is quoted here on purpose: it changes every time a file is added, and
  a stale count in a contributing guide is a small lie that teaches people to skim it.) **Do not
  run `ruff format .` to "fix" that in your PR.** A repo-wide reformat buried inside an
  unrelated change makes that change impossible to review, which is the reason CI is scoped this
  way (`.github/workflows/ci.yml`, the `Format check (ruff)` step). If you want the rest
  formatted, do it as its own commit that touches nothing else and widens the CI scope to `.` in
  the same commit — that PR is welcome on its own.

## Tests

`pytest -q` must pass before you open a PR. New behaviour needs a test; bug fixes need a test
that fails before the fix. The tests are offline by design - none of them touch a network or an
upstream account, so they run in CI without credentials. They are, however, slow, and no wall-clock
figure is quoted here because the honest one depends entirely on your machine: a large share of
them shell out to `sh` or to PowerShell to exercise the installers and the generated launchers,
and on Windows every one of those spawns a fresh interpreter. Run the files you touched while you
work - `pytest -q tests/test_claude_shim.py` finishes in seconds - and the whole suite once before
you push.

### The gates that are not pytest

Two more checks run outside pytest, and both guard published claims rather than code. The first
one is offline:

```sh
python tools/sitecheck.py --self-test   # the checker proves it can still go red
python tools/sitecheck.py               # every figure under site/ traces to the record
```

`sitecheck.py` exits `2` if its own self-test failed, which is a different and worse result than
a page finding (`1`): a checker that cannot be shown to fail certifies nothing. It also classifies
every file under `site/`, so a **new** file that restates 99.53% or 748,918 fails the build rather
than quietly living outside the guard - `--coverage` prints that classification and `--inventory`
prints what was actually examined, because "0 problems" is also what a checker that read nothing
says.

The second needs a network, and its own `--help` says it has to be run from outside the origin
host or it proves nothing:

```sh
python tools/drift_check.py             # is the site that is SERVED the site in this repo?
```

It is not `sha256sum`. An edge legitimately rewrites a page - Cloudflare's e-mail obfuscation once
rewrote `--email you@example.com` inside a `<pre>` and broke the published install command for
every visitor while the origin served the correct bytes - so it compares against the repo copy
with the **known, enumerated** transformations applied and fails on anything else. Do not wave a
failure away as "just the CDN" without reading which transformation it could not account for; that
is the whole signal.

If you touched a shell or PowerShell script, the `offline-self-checks` job is worth reproducing
too. It parses every `*.sh` with `bash -n` and every `*.ps1` with the PowerShell parser, then runs
the paths a user reaches *before* anything is installed - the ones nothing else exercises:

```sh
bash deploy/preflight.sh --self-test     # pure address/CIDR/ELF helpers, no network, no root
bash deploy/smoke_test.sh --self-test    # the substring helper behind every header verdict
for s in deploy/harden.sh deploy/install.sh deploy/preflight.sh deploy/smoke_test.sh \
         scripts/make_history.sh site/install.sh site/uninstall.sh; do
  bash "$s" --help > /dev/null && echo "ok $s"
done
```

`smoke_test.sh --self-test` is there because of a bug worth knowing about if you write shell
here: **GNU grep 3.0 — the build Git Bash ships — aborts when `-i` and `-F` are combined.**

```console
$ printf nosniff | grep -qiF nosniff; echo $?
134                     # 128 + SIGABRT
```

Either flag alone is fine. The exit status is indistinguishable from "no match", so on
2026-07-23 the smoke test failed all eight security headers against an origin that was
serving all eight correctly — `expected to contain 'nosniff', got 'nosniff'`. A gate that
cannot pass gets waved away exactly like a gate that cannot fail gets trusted. For a
case-insensitive substring test, use the shell: lowercase both sides with `tr`, then
`case "$hay" in *"$needle"*)`. The quoted expansion matches literally, so glob characters in
the needle stay literal. `tests/test_smoke_test_helpers.py` fails any `*.sh` that combines the
two flags again.

### A check that cannot pass is as bad as one that cannot fail

That grep was not a one-off. In a single day, four separate checks in this repository were
found unable to give the right answer — every one of them green on Linux, red or degraded on
Windows, which is the direction that wastes the most time because it is green where the change
is written and red where it is reviewed. If you are adding a gate, these are the four traps:

| Trap | Symptom | What to write instead |
|---|---|---|
| `grep -iF` | SIGABRT (exit 134); the caller reads it as "no match" | `tr` to lowercase, then `case "$hay" in *"$needle"*)` |
| `datetime.date.today()` | Local time; a UTC+8 workstation and a UTC runner disagree about what day it is | `datetime.datetime.now(datetime.timezone.utc).date()` — `tools/sitecheck.py` has `_utc_today()` |
| `sha256sum` output | Git Bash writes `*` before the filename, GNU coreutils writes two spaces; a naive diff reports every file as changed | Normalise with `awk '{sub(/^\*/,"",$2); print $1, $2}'` |
| `subprocess.run(text=True)` | Decodes with the *parent's locale* codec (cp950, cp1252). When the child writes UTF-8 — one em dash is enough — the `UnicodeDecodeError` is raised on subprocess's internal reader thread, so the call does **not** raise: it returns with `stdout=None`, and pytest reports `PytestUnhandledThreadExceptionWarning`, naming neither the encoding nor the file | `encoding="utf-8"` when the child writes UTF-8; `errors="replace"` when it writes the console codepage |

The rule that catches all four: **after writing a check, break the thing it guards and confirm
it goes red — then confirm it goes green again.** Both halves. Three of the four above passed
the first half and failed the second, and one of them (`robots_problems`) was written, run
against the exact defect it existed for, and found to be *green* — because it asked "is the
file in `site/`?" when the file was on disk and simply never deployed.

`--help`, `--dry-run` and `--self-test` must touch nothing. CI asserts that literally: it points
`byok/setup.py --dry-run` at an empty directory and fails if the directory exists afterwards, and
it runs `site/uninstall.sh --dry-run` against a fake `$HOME` and fails if the file is gone. A
regression there writes an engine config, a `settings.json` and a secret env file onto a machine
whose owner asked for a preview.

Anything under `site/` that is digest-pinned - `install.sh`, `install.ps1`, `uninstall.*` and
every `*.sha256` - is published, and the digests are recomputed centrally. A PR that edits one of
those files and not its `.sha256` (or vice versa) fails the `installer-digests` job. If you
believe one of them has to change, say so in the PR description rather than hand-editing a digest.

## Secrets: the one unforgivable mistake

This repository must never contain an API key, a management key, an OAuth token, an account
e-mail address, or anybody's absolute filesystem path. These tools were ported from an install
whose sources carried all four, so this is a live risk, not a hypothetical one.

CI enforces it: the `no-secrets` job in `.github/workflows/ci.yml` greps the whole tree for
key-shaped strings (API keys, OAuth tokens, GitHub and AWS and Slack credentials, bcrypt hashes,
private-key headers), the legacy management-key prefix, and operator paths in both slash
directions; two further steps assert that every committed e-mail address is at a reserved domain
and that every IP address is either IANA-reserved or named in `.github/ip-allowlist.txt`. The
expressions live in the workflow and nowhere else, deliberately - one authoritative copy is the
only kind that cannot drift. Each of them is written so its own bytes never spell out the string
it hunts (`yang[-]admin[-]`, `Us[e]rs`, `PRIVATE[ ]KEY`), which is what lets the workflow pass
its own scan. Run the same check locally by lifting the pattern out of the workflow:

```bash
pattern=$(sed -n "s/.*git grep -nIE '\([^']*\)'.*/\1/p" .github/workflows/ci.yml | head -1)
[ -n "$pattern" ] || { echo "could not lift the pattern out of the workflow"; exit 1; }

git grep -nIE "$pattern" -- . ':!.github/workflows/ci.yml' ':!CONTRIBUTING.md' \
        ':!RELEASING.md' ':!SECURITY.md' ':!scripts/make_history.sh' \
  | grep -viE 'fake|dummy|example|placeholder|changeme|redacted|not[-_]a[-_]real|__GENERATE__|<[A-Z_]+>'
```

The trailing filter is the one CI applies too: it drops lines that announce themselves as
fixtures. Without it the command reports the handful of deliberately-fake keys in `tests/` on
every run, and a check that always prints three hits is a check nobody reads.

The extraction uses `sed`, not `grep -oP`, for two reasons that both end in a scan that looks
like it ran: PCRE is not compiled into every `grep` (BSD and macOS have none, so the lift fails
outright), and under Git Bash without a UTF-8 locale `grep -oP` silently yields an EMPTY
pattern - which then matches every line in the repository. `sed -n 's/...//p'` needs neither.

`scripts/make_history.sh` performs the same lift rather than carrying its own copy, and adds
the one thing a lifted pattern still needs: it **self-tests** the pattern against a sample of
every shape before trusting it, and refuses to run if any of them stops matching. That is what
turns a silently-rotted regex into a loud failure. It previously carried a private four-shape
copy advertised as "the same shapes CI rejects"; it was a subset, and it matched no key shape
containing a hyphen (`sk-ant-api03-`, `sk-or-v1-`, `sk-proj-`) and no forward-slash operator
path. If you ever find yourself pasting this pattern into a third place, lift it instead.

If you would rather not run any of that locally, push to a branch and let CI answer.

Everything configurable is read from an environment variable with a safe default. If your change
needs a new secret, add it to `deploy/.env.example` as a placeholder and document it - never as a
real value, not even a "test" one. If a key ever does land in a commit, treat it as compromised
and rotate it; rewriting the history is not a substitute.

## Pull requests

* One topic per PR. A refactor bundled with a behaviour change gets split.
* Say what you ran to verify it. "Tests pass" is fine for code; a claim about the upstream needs
  the command and its output.
* Update the docs in the same PR. A finding that is not written down did not happen.
* Be nice in review. See [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).

## What will be turned down

* Marketing language, superlatives, or a results table without reproduction commands.
* Numbers that cannot be traced to a command or a citation.
* Anything that makes the shim, the benchmark or the gateway depend on a service we cannot run
  offline.
* Features that only make sense for exposing pooled personal accounts to the public - see
  [`docs/OPERATING_A_PUBLIC_SERVICE.md`](docs/OPERATING_A_PUBLIC_SERVICE.md).
* Vendored copies of CLIProxyAPI. We integrate it and credit it; we do not redistribute it.

## Reporting a security issue

Do not open a public issue. See [`SECURITY.md`](SECURITY.md).