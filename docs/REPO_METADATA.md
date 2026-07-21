# Repository metadata

Everything the operator pastes into GitHub's own settings, written out verbatim so nobody has to
compose it at the moment of publishing (which is exactly when an unqualified number gets typed
into a box).

Nothing here is applied by a script. Repository settings are an owner action.

Where to put each item:

| Item | Where in GitHub |
|---|---|
| Description, website, topics | Repository home -> **About** panel -> the gear icon |
| Social preview image | **Settings** -> **General** -> *Social preview* -> **Upload an image** |

---

## 1. Repository description

GitHub's limit is **350 characters**. The recommended text is **343**, so it fits with nothing to
spare - if you edit it, re-count.

**Recommended (paste exactly):**

```
A plausible-looking model-pool config in a popular OSS LLM proxy silently destroys prompt caching. yangble5 is the corrected config, a Claude-wire shim, and the benchmark that proves it: 99.53% token-weighted cache hit on warm rounds (cold round 0%), a 748,918-token prompt. One machine, one run, 2026-07-21. Not a model. Built on CLIProxyAPI.
```

Why it is written this way:

- **The hook is the bug, not the number.** "A config that looks right silently destroys your
  prompt cache" is the thing a reader has not heard before and can verify in their own setup in
  ten minutes. The 99.53% is evidence for it, not the pitch.
- **`(cold round 0%)` and `One machine, one run` are inside the description**, not in a footnote,
  because a repo description is quoted verbatim in search results, link previews and other
  people's tweets, where footnotes do not travel.
- **"Not a model"** is there because "1M context, 99.53% cache" reads like a model announcement to
  a skimmer. This is a proxy configuration and a measurement harness.
- **"Built on CLIProxyAPI"** because the Go engine doing the actual proxying is third-party MIT
  work by someone else, and it belongs in the first 350 characters, not only in the README.

**Alternative A (311 chars)** - lead with the practical fix rather than the discovery:

```
Stop a two-model alias pool from silently killing your LLM proxy's prompt cache. Corrected CLIProxyAPI config + Claude-wire shim + the benchmark that proves it: 99.53% token-weighted cache hit on warm rounds (cold round 0%), 748,918-token prompt. One machine, one run, 2026-07-21. Not a model, not free credits.
```

**Alternative B (307 chars)** - mirrors the README's opening line:

```
Make cheap 1M-context models behave like frontier coding agents. Corrected CLIProxyAPI config, a Claude-wire compatibility shim, and the prompt-cache benchmark that proves it: 99.53% token-weighted hit rate on warm rounds (cold round 0%), 748,918-token prompt, one machine, one run, 2026-07-21. Not a model.
```

Use B only if you are comfortable that "behave like frontier coding agents" reads as a statement
about *context handling and caching* rather than as a quality comparison against Claude or GPT.
It is not one, and no such comparison was measured. When in doubt, use the recommended text.

**Never put in this field:** any dollar figure of free credit; any claim of a Taiwan-trained or
homegrown model; "beats GPT/Claude/Gemini"; the 99.53% without both "warm" and the cold-round 0%;
or a bare "1M context" without the measured 748,918 that backs it.

---

## 2. Topics

GitHub allows up to 20 topics; each must be lowercase, may contain hyphens, and is limited to 50
characters. **19** are listed - one slot is deliberately left free for whatever term the first
wave of readers actually searches for.

Paste one per line into the topics box:

```
llm-proxy
prompt-caching
cliproxyapi
claude-code
codex
gemini
benchmark
api-gateway
fastapi
python
self-hosted
developer-tools
long-context
rate-limiting
docker
caddy
byok
observability
taiwan
```

Grouped by the job each one does:

| Group | Topics | Why |
|---|---|---|
| What it is | `llm-proxy`, `api-gateway`, `self-hosted`, `developer-tools` | the categories people browse |
| The actual subject | `prompt-caching`, `long-context`, `benchmark`, `observability` | the specific problem this repo is about |
| Who it plugs into | `cliproxyapi`, `claude-code`, `codex`, `gemini`, `byok` | how a user with the same stack finds it |
| How it is built and run | `python`, `fastapi`, `docker`, `caddy`, `rate-limiting` | filters for people evaluating the deployment bundle |
| Where it is from | `taiwan` | accurate provenance |

Deliberately **not** used: `ai`, `llm`, `gpt`, `chatgpt`, `openai`, `anthropic`, `agi`,
`free-api`, `unlimited`, `token-free`. The first group is noise at this point; the second implies
an affiliation that does not exist; the third implies free tokens, which this project does not
provide - every token is billed to the operator's own upstream account.

`taiwan` is accurate and is fine. It does **not** license any claim about a Taiwan-trained model.
This project trains nothing.

---

## 3. Website field (About panel)

The `site/` directory is written against `https://yangble5.com/` - its install one-liners and
canonical links all point there.

```
https://yangble5.com/
```

**Before pasting it, confirm the domain actually resolves to the page in `site/`.** At the time
this file was written that had not been verified from this machine. A 404 in the About panel is
worse than an empty field.

If the site is not live yet, use the repository's own documentation instead - both are valid and
neither over-promises:

```
https://github.com/shark0120/yangble5/blob/main/docs/FINDINGS.md
```

Whatever goes in this field must not imply a hosted service that anybody can use for free. If a
public instance ever runs, its page has to state whose upstream account is paying for the tokens
before its URL goes in this box.

---

## 4. Social preview image

GitHub renders this at **1280x640** and it is what appears in every link preview on X, Slack,
Discord, LinkedIn and Hacker News comment embeds. It is the single most-quoted surface this
project has, and the least likely to be read with its footnotes.

**Source asset:** [`assets/social-preview.svg`](../assets/social-preview.svg) - 1280x640, plain
SVG, system fonts only, no external references, no embedded raster data, no third-party logos.
See [`assets/README.md`](../assets/README.md) for what every number on the card means and where
it came from.

**Upload spec:**

| Property | Value |
|---|---|
| Dimensions | 1280 x 640 px exactly (2:1). GitHub's own recommendation. |
| Format | **PNG** (or JPG). GitHub's social-preview uploader does **not** accept SVG - the SVG in `assets/` is the editable source; a rendered PNG is what gets uploaded. |
| File size | Under 1 MB (GitHub's cap). A flat-colour card like this renders well under 100 KB. |
| Colour | sRGB. |
| Safe area | Keep text at least 64 px from every edge - link previews crop differently per platform, and Slack in particular trims the edges. |
| Legibility | Must stay readable at 320 px wide, which is roughly how it appears in a timeline. Nothing smaller than ~28 px in the source. |
| Text content | Only claims that carry their own qualifier on the card itself (see below). |

**Render the PNG (any one of these):**

```bash
# rsvg-convert (sharpest text, no browser needed)
rsvg-convert -w 1280 -h 640 assets/social-preview.svg -o social-preview.png

# Inkscape
inkscape assets/social-preview.svg -w 1280 -h 640 -o social-preview.png

# headless Chrome, if neither is installed
chrome --headless --screenshot=social-preview.png --window-size=1280,640 \
       --default-background-color=00000000 assets/social-preview.svg
```

The rendered PNG is **not** committed: it is a derived artifact, it would go stale the moment the
SVG changes, and a binary in git history cannot be removed later. Render it, upload it, delete it.

**Every number on the card must carry its qualifier in the same visual block, at a font size a
reader will actually see.** This is not a style rule; it is the whole reason the card is
self-authored rather than auto-generated:

- `99.53%` -> must be adjacent to "token-weighted, **warm rounds only** (2-4 of 4)".
- `748,918 tokens` -> must be adjacent to "ingested without truncation; recall at that size not
  tested".
- `0%` -> "round 1 of every session, by construction".
- Footer, on the card: measured 2026-07-21, one Windows 11 machine, one run per configuration,
  no repetitions; built on third-party CLIProxyAPI (MIT); **no live web search**.

If a redesign cannot fit the qualifiers, the fix is to drop the number, not the qualifier.

---

## 5. Other About-panel settings

| Setting | Value | Why |
|---|---|---|
| Include in the home page | Releases: **on**, Packages: **off**, Deployments: **off** | Nothing is published to a package registry and there are no environments. |
| Issues | **on** | The issue templates ask for engine version and streaming/non-streaming, the two facts that decide most reports. |
| Discussions | off until there is someone to answer them | An unanswered Discussions tab reads worse than an absent one. |
| Wiki | **off** | Documentation lives in `docs/`, versioned with the code that produced the numbers. A wiki drifts. |
| Projects | off | Not used. |
| Sponsor button | off | No funding file, and asking for money for a wrapper around somebody else's MIT engine needs a conversation first. |
| Default branch | `main` | `scripts/make_history.sh` builds the initial history on `main`. |

## 6. Attribution that must appear before anyone can star the repo

Not a GitHub setting, but it belongs in the same review: the README's first screen, the site's
first screen and the social card all have to say that the proxy engine
([CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI), MIT, Router-For.ME) is third-party
work that this project does not redistribute and is not affiliated with. If the About panel is
being filled in, check that too - it is the same five minutes and the same audience.
