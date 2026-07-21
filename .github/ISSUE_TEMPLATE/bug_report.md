---
name: Bug report
about: Something behaves differently from what the docs claim
title: ""
labels: bug
assignees: ""
---

## What happened

<!-- One or two sentences. What did you expect, what did you get. -->

## Environment

| | |
|---|---|
| yangble5 commit | `git rev-parse --short HEAD` |
| CLIProxyAPI engine version | e.g. 7.1.23 (`cli-proxy-api --version`) |
| Upstream provider / channel | e.g. Gemini via `antigravity`, xAI, OpenAI-compatible |
| OS + Python | e.g. Windows 11, Python 3.12.4 |
| Client | Claude Code x.y.z / Codex / direct HTTP / none |
| Using `claude_shim.py`? | yes / no |

## Configuration shape

<!--
Paste the RELEVANT part of your engine config with every secret removed.
Especially: `routing:`, and the alias block (`oauth-model-alias:` or
`openai-compatibility:`) for the model you are calling.

DO NOT paste api-keys, management keys, OAuth tokens, or account e-mail addresses.
An issue containing a live key will be edited and the key should be considered
compromised - rotate it immediately.
-->

```yaml

```

## Reproduction

```bash
# the exact command(s) you ran
```

## Output

<!--
For cache/benchmark issues, the single most useful attachment is:

    python tools/cache_bench.py --model <alias> --prefix-tokens 600000 --rounds 4 --json

Paste the JSON. It contains token counts and latencies only - no prompt content.
-->

```

```

## Checked already

- [ ] I read the [Limitations](../../README.md#limitations) section and this is not one of them.
- [ ] My alias maps to exactly one upstream model (see
      [Finding 1](../../docs/FINDINGS.md#finding-1-a-two-member-model-pool-rotates-per-request-and-ignores-your-routing-policy)).
- [ ] The output above contains no keys, tokens, e-mail addresses or absolute paths.