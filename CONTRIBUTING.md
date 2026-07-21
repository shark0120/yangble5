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

Python 3.11 or newer (`requires-python = ">=3.11"`; CI runs 3.11, 3.12, 3.13 and 3.14 on Ubuntu
and Windows). Those three places — the matrix, `requires-python`, and the `Programming Language ::
Python` classifiers — are checked against each other by the `offline-self-checks` job, so adding a
version means adding it in all of them or the build fails.

```bash
git clone https://github.com/shark0120/yangble5
cd yangble5
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"                          # pytest, ruff, and the gateway extras
pytest -q
```

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
* `ruff check .` and `ruff format --check .` must pass.

## Tests

`pytest -q` must pass before you open a PR. New behaviour needs a test; bug fixes need a test
that fails before the fix. The tests are offline by design - none of them touch a network or an
upstream account, so they run in CI without credentials.

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

or simply push to a branch and let CI answer.

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