## What this changes

<!-- One paragraph. If it changes a documented number, say which and why. -->

## Why

<!-- The problem this solves. Link the issue if there is one. -->

## How it was verified

<!--
Code:   `pytest -q` output, and anything you ran by hand.
Claims: the exact command and its raw output. A number without a command that
        reproduces it will be asked to become "not measured".
-->

```

```

## Checklist

- [ ] `pytest -q` passes.
- [ ] `ruff check .` and `ruff format --check .` pass.
- [ ] New behaviour has a test; a bug fix has a test that failed before the fix.
- [ ] Docs updated in this PR if behaviour, flags or claims changed.
- [ ] Every new quantitative claim is labelled Measured / Verified / Observed / Reasoned and
      carries its reproduction command or citation.
- [ ] No secrets: no API key, management key, OAuth token, account e-mail address, or absolute
      filesystem path anywhere in the diff. New configuration reads from an environment variable
      with a safe default and is listed in `deploy/.env.example` as a placeholder.
- [ ] `tools/` still imports only the standard library.