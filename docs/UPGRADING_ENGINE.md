# Upgrading the engine (and retiring the shim)

The engine underneath yangble5 is **[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)**,
a third-party open-source Go project by `router-for-me`. **We did not write it.** yangble5 is a
config, a compatibility shim, and measurement tooling wrapped around it. This document is about
upgrading *their* binary inside *our* install, and about deleting one of our own workarounds once
their fix makes it unnecessary.

Everything measured below was produced on **2026-07-21**, on **one Windows 11 machine**, in a
**single run per configuration**, against **CLIProxyAPI 7.1.23** (windows/amd64).

---

## Why you would do this

`tools/claude_shim.py` exists for exactly one reason. CLIProxyAPI 7.1.23's antigravity
**streaming** translator passes `messages[].role` through verbatim. Claude Code >= 2.1.x injects a
mid-conversation `{"role":"system"}` message, and Gemini's `streamGenerateContent` answers that
with `400 "Request contains an invalid argument"`. The non-streaming path tolerates it, which is
why the bug hid for so long. The shim sits on `:8320`, maps `system` -> `user`, and forwards
everything else byte-identically to the engine on `:8318`.

Upstream fixed this in **v7.2.93**. After upgrading, the shim is dead weight: one extra hop, one
extra process, one more thing to explain. This document retires it.

**Status: Measured.** Here is the current live install answering all three probes, direct from
`engine/verify_shim_retirement.py` on 2026-07-21, engine 7.1.23:

```
  [A] CONTROL  stream:true, NO mid-conversation system message
      -> HTTP 200, 801 bytes of SSE, no error event  (7561 ms)

  [B] SUBJECT  stream:true, WITH a mid-conversation role:"system" message
      -> HTTP 400: Request contains an invalid argument.  (3233 ms)

  [C] SHIM     the same failing shape through the shim on :8320
      -> HTTP 200, 801 bytes of SSE, no error event  (4610 ms)

  VERDICT: SHIM STILL REQUIRED
```

Probe A is the control: it proves the endpoint, the model alias and the key all work, so the 400
in probe B is attributable to the mid-conversation system message and nothing else. Probe C shows
the shim currently masks it. That is the whole bug, reproduced in three requests.

---

## The two scripts

Both live in the live install's `engine/` directory, are **stdlib-only**, and never read, copy or
print anything from `auth/`, and never print an API key.

| Script | What it does |
|---|---|
| `engine/upgrade_engine.py` | `--check` / `--plan` / `--apply` / `--rollback` for the engine binary |
| `engine/verify_shim_retirement.py` | Sends the real failing request and reports the real status |

`--check` and `--plan` are strictly read-only. `--apply` **refuses to run** unless you also pass
`--i-approve-download`, because downloading and installing an executable is an operator decision,
not a script's.

---

## Procedure

Run everything from the live install's engine directory:

```bat
cd /d <install>\engine
```

### Step 0 — see where you stand (read-only)

```bat
python upgrade_engine.py --check
```

Real output from the live install on 2026-07-21:

```
  CURRENT ENGINE
    binary        : <install>\engine\cli-proxy-api.exe
    version       : 7.1.23  (commit e399edd3, built 2026-05-26T16:48:31Z)
    detected via  : binary probe (invalid-flag banner)
                    (authoritative: this is the file on disk, not a cached log)
    boot log says : 7.1.23 (agrees with the binary on disk)
    live on :8318  : UP (HTTP 200, 12 models, pid [7796])
    api key       : config.local.yaml api-keys[0] (value not shown)
    shim on :8320   : listening

  LATEST UPSTREAM
    repo          : router-for-me/CLIProxyAPI
    latest tag    : v7.2.93   (published 2026-07-21T05:18:17Z)
    asset for you : CLIProxyAPI_7.2.93_windows_amd64.zip  (15,478,972 bytes)
    checksums.txt : present

  VERDICT
    upgrade AVAILABLE : 7.1.23 -> 7.2.93
    shim          : engine 7.1.23 < 7.2.93 -> claude_shim.py (:8320) is STILL REQUIRED
```

**How the version is determined.** The script runs the binary with a flag it cannot define
(`-yangble5-version-probe`). CLIProxyAPI prints its version banner *before* Go's `flag` package
parses argv, then flag parsing fails and the process exits — so the probe reads the binary **on
disk** and can never bind a port or start a server, even while the engine is running. The
`boot.out.log` banner is only used as a cross-check: if the two disagree, the *running process*
predates the binary and you should restart the engine.

The latest tag comes from one read-only call to the GitHub releases API. Nothing is downloaded.

### Step 1 — read the plan (read-only)

```bat
python upgrade_engine.py --plan
```

This prints the exact download URL, the exact asset name and byte size, the exact backup path, and
the nine steps `--apply` would take, in order. It downloads nothing. Read it before approving
anything.

### Step 2 — apply (this downloads a binary — your call)

```bat
python upgrade_engine.py --apply --i-approve-download
```

Without the flag it refuses and exits 3. With it, one command does all of this:

1. Re-resolves the release, the asset for your OS/arch, and the asset's SHA256 from the release's
   `checksums.txt`. **If `checksums.txt` is absent, or has no line for your exact asset, it aborts.**
   It will not install an unverified binary.
2. Backs up `cli-proxy-api.exe` and `config.local.yaml` into
   `<install>\backups\engine-<timestamp>\`, with a `manifest.json` recording the old version and
   its SHA256.
3. Downloads the archive to a temp directory.
4. Verifies SHA256 against the published checksum. Mismatch -> delete, abort, nothing touched.
5. Extracts **only** the engine binary member (no path traversal, nothing else written), and
   checks the staged binary's own version banner matches the release tag.
6. Renames the running binary aside to `cli-proxy-api.exe.old-<version>` and moves the new one in.
   Renaming first is deliberate: Windows permits renaming a running executable, and doing it in
   this order removes any race against `watchdog.py`'s 3-8s restart backoff.
7. Stops the engine on `:8318`, then waits up to 25s for `watchdog.py` to restart it. Only if
   nothing does will the script start the engine itself — so you never get two engines fighting
   for the port.
8. Waits for `GET /v1/models` to return 200.
9. Smoke test: re-probes the binary version, confirms `/v1/models` is non-empty, then runs
   `verify_shim_retirement.py` for the shim verdict.

**Failure behaviour.** Any failure *before* the swap aborts with nothing on disk changed. Any
failure *at or after* the swap triggers an automatic rollback to the backed-up binary, restarts
the engine, and reports honestly. If the rollback itself fails, the script prints the exact manual
recovery commands rather than pretending it succeeded. The shim verdict is informational and never
triggers a rollback.

**Never touched:** the `auth/` directory, and the *contents* of `config.local.yaml`.
`config.local.yaml` is copied into the backup for safety and never edited.

To pin a specific release instead of the latest: add `--tag v7.2.93`.

### Step 3 — prove the shim is redundant (do not skip)

Version numbers are a claim; a request is evidence.

```bat
python verify_shim_retirement.py
```

It sends the **exact failing shape** — a Claude-format `POST /v1/messages` with `stream:true` and
a mid-conversation `role:"system"` message — **directly to `:8318`, bypassing the shim**, and
reports the real status.

Three things make the verdict trustworthy:

- It auto-selects an **antigravity-backed** alias from the engine's own `/v1/models`. The bug only
  ever affected the antigravity translator, so a pass on a grok- or openai-backed alias proves
  nothing. If it cannot get an antigravity model, it returns *inconclusive* rather than guessing.
- It runs a **control** request first (same shape, no system message). If the control fails, the
  run is *inconclusive* — a 400 could not be blamed on the system role.
- A `200` is not taken at face value. It reads a bounded prefix of the SSE stream and scans for an
  error event, because the engine can answer 200 and then fail inside the stream.

Exit codes: `0` = shim can be retired, `1` = shim still required, `2` = inconclusive.

**Only proceed to Step 4 if you get `VERDICT: SHIM CAN BE RETIRED`.** Inconclusive is not a yes.

### Step 4 — repoint Claude Code from :8320 to :8318

In the launcher's `:claude_yangble5` section, change one line:

```diff
- set "ANTHROPIC_BASE_URL=http://127.0.0.1:8320"
+ set "ANTHROPIC_BASE_URL=http://127.0.0.1:8318"
```

Leave everything else alone — in particular keep `CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000`, which
is what stops Claude Code assuming 200K for an unrecognised model name and auto-compacting early.

POSIX equivalent:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8318
```

Codex never went through the shim — it already points at `:8318` — so nothing changes there.

### Step 5 — remove the shim from the launcher

Delete the shim's start line and its comment from `:ensure_proxy`:

```diff
- REM claude shim :8320 -> :8318 : fixes antigravity streaming 400 on Claude Code's
- REM mid-conversation system messages (engine 7.1.23; retire at engine >= 7.2.93)
- start "yangble5-shim" /min cmd /c "cd /d "%PROXY_DIR%" && python claude_shim.py"
```

Then close the existing `yangble5-shim` window, or kill whatever is listening on `:8320`.

Keep `claude_shim.py` in the repo. It documents the bug, and you need it again the moment you roll
back to an engine older than 7.2.93.

### Step 6 — confirm end to end

Launch Claude Code through the stack and complete a real multi-turn task that triggers the Agent
tool (that is what injects the mid-conversation system message). A clean run here is the only
thing that actually retires the shim; the probes just tell you it is worth trying.

---

## Rollback

```bat
python upgrade_engine.py --rollback
```

It picks the newest `backups\engine-*\` that has a `manifest.json`, verifies the backed-up
binary's SHA256 against the manifest (and refuses to install it on mismatch unless you pass
`--force`), stops the engine, restores the binary, restarts, and waits for health. Use
`--from-dir <path>` to pick a specific backup.

**If you had already retired the shim, undo Steps 4 and 5 as well** — an engine older than 7.2.93
needs the shim back:

1. Restore the `start "yangble5-shim" ...` line in `:ensure_proxy`.
2. Set `ANTHROPIC_BASE_URL` back to `http://127.0.0.1:8320`.
3. Start `python claude_shim.py`.

`--rollback` prints this reminder itself.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `--check` says `UP but rejected our API key` | The resolved key is not one the engine accepts | The scripts read `api-keys[0]` from `config.local.yaml`, or `YANGBLE5_API_KEY` / `PROXY_KEY`. They deliberately ignore `ANTHROPIC_AUTH_TOKEN`, which is often set to an unrelated provider's token in your shell. |
| Verifier says `INCONCLUSIVE`, control probe 502 `unknown provider` | A model alias was forced that the engine does not serve | Drop `--model` and let it auto-select, or set `YANGBLE5_MODEL` to a real alias. |
| `boot log says X (DIFFERS from the binary on disk)` | The running process predates the binary | Restart the engine. |
| GitHub API returns 403 | Anonymous rate limit | Retry later, or set `GITHUB_TOKEN`. |
| `--apply` aborts: no checksum entry | The release published no usable `checksums.txt` | Working as designed. Do not install an unverified binary. |
| Two engines fighting for `:8318` | An engine was started manually alongside `watchdog.py` | Kill both, start `watchdog.py` only. |

---

## What this document does not claim

- The upgrade path itself is **unverified end to end**. `--check`, `--plan`, the `--apply` refusal
  path, and `verify_shim_retirement.py` were all run for real against the live 7.1.23 install. The
  `--apply` and `--rollback` paths were **not** executed, because no binary was downloaded. Treat
  them as reviewed code, not as a tested procedure, and read `--plan` before approving.
- The 400 reproduction is one machine, one run per configuration, engine 7.1.23. It is consistent
  with the upstream source change in v7.2.93, but we did not run 7.2.93.
- No live web search reaches through this proxy. Measured: asked for the current date, Gemini
  answered "2024" and Grok "2025". Model knowledge is stale and the stack does not fix that.
- CLIProxyAPI is a third-party project. Its release cadence, asset names and checksum publication
  are outside our control; `--plan` re-resolves all three at run time rather than trusting anything
  hardcoded here.
