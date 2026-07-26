# Autonomous maintenance protocol for a public repository

> **Draft — user publishes, agents do not.** This repository copy is ready for review, but an
> agent must not submit, post or announce it.

Autonomy is permission to carry a bounded task through its ordinary implementation steps. It is
not permission to widen the task, rewrite shared history, touch credentials, publish under
somebody else's name or treat a green summary as evidence.

The protocol below is for maintenance where an agent may work for multiple rounds without waiting
for step-by-step approval. It assumes the repository has an explicit state record, repeatable
checks and a known boundary around production writes. If those do not exist, establish them before
claiming autonomous operation.

## Machine-locked rules

The table between the markers is generated from
[`docs/autonomy-rules.json`](../autonomy-rules.json). That JSON is the authority; prose below may
explain a rule but may not redefine it. A canonical JSON SHA-256 and the ordered rule IDs let a
private state record pin this contract without copying the principles into a second prose source.

<!-- AUTONOMY_RULES:BEGIN -->
| Rule ID | Required principle |
|---|---|
| `state-before-action` | Begin from one explicit state record that owns priority, completion and prior evidence. |
| `ci-first` | Inspect the latest default-branch CI before taking planned work; a real red result takes priority. |
| `one-complete-slice` | Finish one bounded item with implementation, tests and documentation instead of leaving several partial edits. |
| `mutation-before-claim` | Break the guarded behavior in a disposable copy and require the expected assertion to fail. |
| `exact-gates` | Run the repository's exact gate commands and scopes; a similar local command is not equivalent. |
| `isolated-reviewable-tree` | Keep parallel or adversarial work off the commit tree, review every diff hunk and verify the clone-visible tree. |
| `fetch-before-push` | Fetch before pushing, prove the remote default branch is an ancestor and never force-push through divergence. |
| `scoped-reversible-deploy` | Write to production only inside the authorized scope, with backup, atomic replacement and origin plus public verification. |
| `evidence-before-numbers` | Trace quantitative claims to evidence, carry their conditions and never edit the record to rescue the story. |
| `secrets-stay-out` | Do not read, print, commit or move credentials unless the user explicitly authorizes that exact secret operation. |
| `user-owns-publication` | Prepare release, issue and announcement drafts, but leave every public submission or publication action to the user. |
| `receipt-before-next` | Record the commit, verification and external status in the state record before starting the next item. |
<!-- AUTONOMY_RULES:END -->

## Before changing anything

Read the state record and select the first eligible item under its stated priority rule. Do not
reconstruct priority from memory or from an older status message. Then inspect the latest
default-branch CI anonymously or with read-only access. A current red result is work, not
background noise; either repair it first or record why it is unrelated and non-blocking.

Confirm the allowed mutation surface. Repository edits, production writes, credentials and public
actions are different authorities. Permission for one does not imply the others.

## Complete one slice

A slice is complete only when implementation, a test that would have failed before the change,
documentation and the repository's prescribed gates agree. Keep the item small enough to finish
and hand off cleanly. Starting several items makes it impossible to tell which change produced a
red result and encourages partial commits.

After the positive test passes, mutate the guarded behavior in a disposable copy. Require a
non-zero result at the assertion that names the planted defect. A syntax error, import failure or
unrelated red is not proof that the guard works. Remove the copy without restoring or cleaning the
real tree.

## Treat the tree as evidence

Parallel reviewers and adversarial tests must use read-only copies or isolated worktrees. A
mutation running in the same tree that is about to be committed can produce the worst false green:
the gates correctly validate a tree, but it is not the tree the maintainer thinks they wrote.

Before committing, inspect every hunk, check staged and unstaged state separately, and build a
fresh-clone or archive simulation from the exact staged tree when file discovery, ignore rules,
links or generated artifacts matter. The simulated tree identifier must equal the commit's tree
identifier.

## Gates are literal interfaces

Run the exact commands, arguments and directory scopes named by the repository. Linters and
formatters are different gates even when they share a brand. A repository-wide formatter is not a
substitute for a formatter intentionally scoped to two directories. Likewise, a locally convenient
regex is not the same as the credential scan CI actually executes.

When a gate is added, document it where contributors will look and add a tripwire for command
shapes the existing roster cannot classify. A checker must also prove it examined something and
must have a negative control.

## Push without rewriting history

Commit only the reviewed tree. Fetch the remote default branch, then prove it is an ancestor of the
local commit. If it is not, stop and reconcile explicitly. Never use force-push as a way to make the
ancestry check disappear.

An ordinary push is not a completion receipt by itself. Record the commit and the resulting CI run;
if a later push cancels an earlier run, distinguish cancellation from a test failure.

## Production and publication are separate boundaries

Production writes happen only inside the scope the user authorized. Back up the current bytes,
validate the candidate, replace atomically where possible, restore intended ownership, and verify
both the origin and the public path. A successful repository push does not prove deployment, and a
correct origin does not prove what an edge serves.

Public actions remain user actions unless separately and explicitly delegated. Prepare the exact
release body, issue report, tag plan and announcement copy, with unresolved receipts left as
placeholders. Do not press the release, submit, post or tag button on the user's behalf.

## Claims and secrets

Every number needs a record or cited source plus the conditions that bound it. If the record and
the prose disagree, fix or narrow the prose. Editing evidence to preserve a preferred narrative
destroys the only reason to trust the result.

Credentials are outside ordinary repository maintenance. Do not inspect them to “make sure,” echo
them into a transcript, move them into a new configuration, or include realistic secret material
in fixtures. A check can usually validate shape and wiring with reserved examples.

## Close the loop

Update the state record with the item, commit, tests, mutation result, deployment status and CI
status before taking the next item. State uncertainty plainly: queued is not green, source review
is not a live reproduction, and a draft is not published.

Stop when the queue is complete, when the remaining action belongs to the user, or when progress
requires new authority. A durable handoff contains the prepared artifact, the exact blocker and the
evidence already collected; it does not claim the blocked action happened.
