# cache_guard demo — catch a cache-breaking prompt change in CI

`tools/cache_guard.py` is an offline linter (no network, no API key) for the one
prompt-caching regression that costs real money and raises no error: a
**timestamp, UUID, request-id or rendered date sitting in the cacheable
prefix**. It changes every request, so the upstream can never reuse the cache,
and your bill quietly goes up. Nothing in a dashboard tells you which commit did
it. A CI check does.

## The two files here

* [`good_prompt.jsonl`](good_prompt.jsonl) — a stable system prompt. Two turns
  share a byte-identical prefix. This is what caches.
* [`bad_prompt.jsonl`](bad_prompt.jsonl) — the same prompt after someone added
  `Request <uuid> at <timestamp>` to the system message. It looks harmless; it
  destroys the cache.

## Run it

```bash
# Clean prompt: no volatile content in the cacheable prefix -> exit 0
python tools/cache_guard.py scan examples/cache_guard/good_prompt.jsonl

# Broken prompt: flags the uuid and the timestamp, with positions -> exit 1
python tools/cache_guard.py scan examples/cache_guard/bad_prompt.jsonl

# Before vs after: the stable prefix collapses 100% -> ~15%, with a cost estimate
python tools/cache_guard.py diff \
  --before examples/cache_guard/good_prompt.jsonl \
  --after  examples/cache_guard/bad_prompt.jsonl \
  --price-per-mtok 3
```

`scan` needs only one payload — a timestamp in a system prompt is self-evidently
cache-hostile without a second sample. `diff` needs a before and an after and
reports how much less of the prefix stays cacheable, plus an **estimated** cost
delta per 1,000 requests. Every dollar figure is an estimate from the byte diff
with its assumptions printed on the same line; the tool never invents a request
volume or claims a measured saving.

## Whichever API you target

The same guard reads the payload shapes a real client sends, so it drops into a CI
that talks to any of these — you feed it what your app already serializes:

* **Anthropic Messages** — `system` plus `messages` (the files above).
* **OpenAI chat.completions** — the system prompt is a `role: "system"` entry
  inside `messages`; the guard folds it in the same way.
* **OpenAI Responses** — `instructions` is the system prompt and `input` is the
  turn(s). See [`openai_responses.jsonl`](openai_responses.jsonl):

  ```bash
  python tools/cache_guard.py scan examples/cache_guard/openai_responses.jsonl
  ```

In every shape the *last* turn is treated as the fresh, allowed-to-vary input and
excluded from the cacheable prefix — put your stable content in `system` /
`instructions` / earlier turns, exactly where a real client would to get a cache
hit. A bare-string `prompt` or Responses `input` is scanned as-is.

## Use it as a CI gate

Fail the build when a pull request makes a prompt cache-hostile. Point it at
representative payloads your app would send (dump a couple to a `.jsonl`):

```yaml
# .github/workflows/cache-guard.yml (illustrative)
- name: Guard prompt caching
  run: python tools/cache_guard.py scan prompts/*.jsonl
```

Or assert it inside your own test suite:

```python
from tools import cache_guard

def test_system_prompt_stays_cacheable():
    payloads = load_the_payloads_my_app_sends()  # your fixtures
    report = cache_guard.scan_payloads(payloads)
    assert report.clean, report.findings
```

## What it does and does not claim

* It **does** catch volatile content in a cached prefix and quantify a
  prefix-stability regression from real bytes.
* It **does not** measure your upstream's actual cache accounting — that is what
  `tools/cache_bench.py` is for, against a live endpoint. `cache_guard` is the
  cheap, offline, every-PR check; `cache_bench` is the on-demand live proof.
* The token counts are a coarse `~4 chars/token` estimate, labelled as such. For
  an exact figure, feed it the payloads your tokenizer actually produced.
