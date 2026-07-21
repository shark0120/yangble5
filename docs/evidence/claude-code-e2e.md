# Evidence: Claude Code end-to-end through the stack

The README/site reference a "Claude Code 3/3" result. An audit correctly flagged
that the raw record was not in the repository, so it is recorded here.

## What was run

Claude Code CLI **2.1.215** on Windows 11, pointed at the local stack:

```
CLAUDE_CONFIG_DIR=%USERPROFILE%\.yangble5-claude   (isolated; the normal login is untouched)
ANTHROPIC_BASE_URL=http://127.0.0.1:8320           (claude_shim -> engine on :8318)
ANTHROPIC_AUTH_TOKEN=<engine key>
ANTHROPIC_MODEL=yangble5
ANTHROPIC_SMALL_FAST_MODEL=grok-4.5
CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000
CLAUDE_CODE_MAX_OUTPUT_TOKENS=64000
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
```

Three consecutive non-interactive prompts, each asking for an exact literal reply:

```powershell
1..3 | ForEach-Object {
  $r = claude -p "reply with exactly: RUN$_-OK" --output-format json | ConvertFrom-Json
  "run $($_): is_error=$($r.is_error) result=$($r.result)"
}
```

## Raw output (2026-07-21)

```
run 1: is_error=False result=RUN1-OK
run 2: is_error=False result=RUN2-OK
run 3: is_error=False result=RUN3-OK
```

## What this does and does not show

**Does show:** the full client path works — Claude Code's Anthropic-format
requests (including its tool definitions, `thinking`, `output_config` and
`context_management` fields) survive translation through the shim and engine to
a Gemini upstream, and the model returns the exact requested string.

**Does not show:** anything about quality, long-session behaviour, or
reliability over time. It is n=3 on trivial prompts, run once, on one machine.
It is an operational smoke test, **not a benchmark**, and must never be
presented as one.

## Why it matters

The same three prompts failed **3/3 with `API Error: 400`** before the shim
existed. The 400 came from Claude Code >= 2.1.x injecting a mid-conversation
`role: "system"` message, which CLIProxyAPI 7.1.23's antigravity *streaming*
translator forwards verbatim and Gemini's `streamGenerateContent` rejects.
See [FINDINGS.md](../FINDINGS.md) for the bisection that located it and the
upstream v7.2.93 fix that `tools/claude_shim.py` backports.
