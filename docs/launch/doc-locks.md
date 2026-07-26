# Doc-lock: make prose fail when the implementation changes

> **Draft — user publishes, agents do not.** This is a repository draft for review. The user
> decides whether and where to publish it; an agent must not post, submit or announce it.

Documentation drift is usually described as a discipline problem: somebody changed the code and
forgot the prose. That diagnosis is convenient and mostly useless. The two files often change at
different times, in different parts of the tree, for different reasons. Nothing in the later edit
points back to the earlier sentence.

A **doc-lock** turns that missing relationship into an executable contract. It parses the source
that is allowed to define a fact, parses the surface that repeats or inventories the fact, and
fails when the two disagree. It is deliberately narrower than “test the documentation.” A good
lock names one relationship it can justify and refuses to certify anything broader.

This pattern emerged here through seven implemented receipts. They cover measurements, release
versions, contributor instructions, workflow-native gates, a coarse workflow census, agent-facing
answers with executable verification commands and an autonomy contract shared with a private state
record. They are examples, not evidence that the pattern solves documentation in general.

## 1. Parse the authority; do not paste a second copy

Start by deciding which representation is allowed to win:

- A machine-captured record can own a published measurement.
- Project metadata can own a release version.
- The workflow can own the set of checks it actually executes.
- A public contract can own the response shape clients see.

Then parse that representation in the test. Do not copy its current value into another test
constant and compare two hand-maintained copies. That only creates two surfaces that can drift
together.

Use the format's parser when one exists: JSON for evidence, TOML for project metadata, AST for
Python assignments and YAML for workflows. A regex is reasonable only when the repository controls
the narrow syntax being recognized and the test loudly rejects an unfamiliar spelling.

## 2. Compare both directions

Most documentation tests check only one direction:

```text
everything implemented is documented
```

That catches an omitted instruction but accepts a phantom instruction for a check that no longer
runs. The reverse-only test has the opposite hole. A roster needs both:

```text
implemented - documented == empty
documented - implemented == empty
```

The failure messages should name which direction broke. “Undocumented gate” tells the author to
write the missing explanation. “Phantom gate” tells them either the implementation disappeared or
the parser no longer understands it. Those are different repairs.

Not every lock can discover a complete universe. When the reader-facing files themselves are a
manual set, say so and add a separate coverage tripwire where possible. Do not call a declared list
automatic discovery.

## 3. Lock numeric prose, including number words

Prose counts are contracts too. “Four gates” can become false while every command beside it stays
valid. If the sentence helps a reader know whether they ran everything, parse `four`, map it to an
integer and compare it with the derived roster.

Keep the supported vocabulary explicit. An unknown number word should fail with an instruction to
update the parser, not be treated as “no count found.” Deleting the count sentence must also fail;
otherwise rewriting a stale claim out of the parser's reach looks like a fix.

For measured figures, do not solve drift by rounding both sides until they agree. Parse the raw
record, derive the published representation using the same rule as the product, and reject both an
unrecorded value and a previously published but superseded transcription.

## 4. Prove the parser is not empty

An empty parser and a perfectly synchronized repository both produce an empty difference. That is
the most dangerous false green in this pattern.

Every parser therefore needs a non-vacuity assertion. Depending on the shape, that can mean:

- the record contains the expected rounds and required fields;
- one literal version assignment was found, not zero or several;
- known sentinel gates were discovered through different invocation forms;
- every signature pair occurs exactly once;
- the workflow contains a non-empty job and step set;
- the checker rejects a deliberately bad fixture as well as accepting the real one.

The point is not to freeze incidental size. It is to make “the checker examined nothing” observably
different from “the checker found no drift.”

## Run a negative control in a disposable copy

After the lock is green, break the relationship outside the working tree:

1. copy only the required files to a disposable directory;
2. change one authoritative value, remove one roster marker or add one unclassified step;
3. run the same test against that directory;
4. require a non-zero exit that names the planted defect;
5. remove the disposable directory without touching the real tree.

This proves the failure path, not just the success path. A red exit for an unrelated import error
does not count; the output must identify the mutation.

## Seven implemented receipts

<!-- doc-lock-example:released-measurements -->
### Receipt 1 — released measurements to reader surfaces

- Authority: [`evidence/run-749k-20260721.jsonl`](../../evidence/run-749k-20260721.jsonl)
- Parser: [`tools/sitecheck.py`](../../tools/sitecheck.py)
- Lock: [`tests/test_sitecheck.py`](../../tests/test_sitecheck.py)
- Receipt commit: `b39f869`

`load_measurement_record()` reads the committed JSONL, validates its round shape and derives the
prompt, cache-read and round-trip sequences. `LATENCY_SURFACES` names the reader-facing files and
which rounds each must show. The test also rejects the superseded transcription, so an old number
cannot survive merely because it used to be public.

Limit: files outside `site/` are manually declared in `LATENCY_SURFACES`; this lock does not claim
automatic discovery across the whole repository. The site checker has its own complete file
classification for `site/`.

<!-- doc-lock-example:release-version -->
### Receipt 2 — one release version and two necessary mirrors

- Authority: [`pyproject.toml`](../../pyproject.toml)
- Mirrors: [`gateway/__init__.py`](../../gateway/__init__.py) and
  [`site/README.md`](../../site/README.md)
- Lock: [`tests/test_release_version.py`](../../tests/test_release_version.py)
- Receipt commit: `1ea70b5`

The test parses TOML, the Python assignment AST and the documented health-response JSON. It also
checks the Docker premise that makes the runtime mirror necessary, the two public health routes,
and the release checklist's list of bump locations.

Limit: this proves the declared authority and mirrors agree and remain justified. It is not a
repository-wide detector for every version-looking string.

<!-- doc-lock-example:python-gate-roster -->
### Receipt 3 — Python-file CI gates to contributor instructions

- Authority: [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)
- Surface: [`CONTRIBUTING.md`](../../CONTRIBUTING.md)
- Lock: [`tests/test_contributing_gates.py`](../../tests/test_contributing_gates.py)
- Receipt commit: `17af463`

The parser derives non-help `python tools/x.py` invocations from CI. Forward and reverse tests
compare that set with the scripts named in the contributor section. Separate assertions reject
interpreter spellings the parser cannot see, prove sentinel invocations were found and lock the
word that states the roster count.

Limit: a workflow check without a `tools/*.py` filename is intentionally outside this parser; the
next receipt handles that different shape.

<!-- doc-lock-example:workflow-native-gates -->
### Receipt 4 — workflow-native gates without filenames

- Authority: [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)
- Surface: [`CONTRIBUTING.md`](../../CONTRIBUTING.md)
- Lock: [`tests/test_contributing_gates.py`](../../tests/test_contributing_gates.py)
- Receipt commit: `cafb697`

Each native gate has two signatures taken from its executable body rather than its display name.
The derived set is compared in both directions with `workflow-gate:<id>` markers in the prose.
Seeing half a signature pair is an explicit parser failure; seeing none makes the documented
marker a phantom. The prose count is locked separately.

Limit: the signature registry can classify known shapes, not imagine a wholly new one.

<!-- doc-lock-example:ci-step-inventory -->
### Receipt 5 — a coarse census for unknown workflow shapes

- Authority: [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)
- Lock: [`tests/test_ci_step_inventory.py`](../../tests/test_ci_step_inventory.py)
- Receipt commit: `ac131a6`

YAML parsing derives the job set plus each job's total and named step counts. A new job, a new
unnamed `uses:` step or a new named check turns the suite red and asks for classification. Step
names themselves are not pinned, so a wording-only rename stays green.

Limit: this is a tripwire, not a semantic classifier. Replacing one step with another while
preserving both counts is a same-count replacement it cannot detect. Review and the shape-specific
locks still matter.

<!-- doc-lock-example:agent-interview-answers -->
### Receipt 6 — agent-facing answers to executable verification commands

- Surface: [`docs/AGENT_INTERVIEW.md`](../AGENT_INTERVIEW.md)
- Lock: [`tests/test_agent_interview_answers.py`](../../tests/test_agent_interview_answers.py)

Each normative answer block has a machine-readable `interview-answer:<id>` marker and exactly one
`python -m pytest -q` command. The test compares the known answer headings and markers in both
directions, rejects shell-composed commands, and proves every target file and `-k` term selects a
real test. Two answer-specific locks also keep the “one question” summary and the known installer
gaps true.

Limit: selecting a real test proves the command has an executable target; the full suite proves
that target passes. It does not certify editorial judgement or prove that an advice sentence is
the only humane way to explain the underlying behaviour.

<!-- doc-lock-example:autonomy-protocol -->
### Receipt 7 — public autonomy rules to a private state record

- Authority: [`docs/autonomy-rules.json`](../autonomy-rules.json)
- Surface: [`docs/launch/autonomous-maintenance-protocol.md`](autonomous-maintenance-protocol.md)
- Lock: [`tests/test_autonomy_protocol.py`](../../tests/test_autonomy_protocol.py)

The public table is parsed and compared exactly with the ordered manifest. A private state record
pins the same IDs plus a SHA-256 of canonical JSON, so it can detect changed principles without
copying them into another narrative. The local test exercises that external boundary when the
state record is available; CI still locks the public table and runs parser negative controls in a
standalone clone.

Limit: this proves the two declared surfaces share one contract. It does not prove that every
operational detail belongs in the generic rules, or make a private state file available to public
CI.

## What a doc-lock does not prove

A doc-lock proves correspondence, not semantic truth. If the chosen authority is wrong, faithfully
mirroring it spreads the error. Authority selection therefore needs an evidence argument before it
needs code.

It also does not make every paragraph executable. Trying to parse nuanced prose with broad regexes
creates false positives; noisy gates get disabled. Lock exact claims with operational consequences:
bump locations, public response fields, measured values, inventories and commands that block a
pull request.

Finally, a passing lock is not a substitute for the negative control. The claim worth making is
small: this relationship was parsed, compared in the directions stated above, shown to be
non-empty, and shown to fail on a named mutation. Nothing more.
