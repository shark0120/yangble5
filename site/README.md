# `site/` — the yangble5.com landing page and client installers

Static files only. No build step, no bundler, no package.json, no CDN, no fonts, no
analytics, no cookies. Copy them to a web root and you are done.

```
site/
├── index.html            the landing page (inline CSS + JS, fully self-contained)
├── verify.html           installer SHA256 + how to check it yourself
├── install.sh            POSIX client installer — the `curl … | sh` target
├── install.ps1           PowerShell client installer
├── uninstall.sh          removes exactly what install.sh created
├── uninstall.ps1         removes exactly what install.ps1 created
├── *.sha256             one digest file per installer, served next to it
└── README.md             this file
```

The pages and the installers ship together on purpose: `verify.html` reads the digest files
live, so a web root that has the HTML but not the `.sha256` files renders "尚未公告" instead of
a hash. Deploy the whole directory or the verification story does not work.

---

## THE ONE RULE

> **`index.html` must be served over HTTPS, with HSTS enabled.**

This is not a best-practice checkbox. It is the security model of the whole page.

The page's centrepiece is a one-liner the visitor copies and runs:

```
curl -fsSL https://yangble5.com/install.sh | sh
```

That command executes whatever bytes the server sends, on the visitor's machine, with the
visitor's privileges — often inside an AI agent that has tool access. Over plain HTTP, any
party on the path (public Wi-Fi, a hostile ISP, a compromised router) can replace those bytes
with anything they like, and the visitor has no way to notice. **The entire safety of that
one-liner is "the bytes came from us, unmodified" — and TLS is the only thing that provides
it.** `verify.html` publishes a SHA256 so users can check the script, but the digest is served
by the same origin: over HTTP an attacker rewrites the digest and the script together, and the
check silently passes.

HSTS matters on top of TLS because the first request is the one that gets stripped. A user who
types `yangble5.com` starts on HTTP; without HSTS an attacker downgrades that request and
serves their own page. HSTS makes the browser refuse to speak HTTP to this host at all after
the first successful visit.

If you cannot serve HTTPS, **do not deploy this page**. Serving it over HTTP is worse than not
serving it, because it teaches people to trust a pipe that is not trustworthy.

---

## Deploy

### Plain web root (the operator's target)

```sh
rsync -av --delete site/ user@host:/www/wwwroot/yangble5.com/
```

That single `rsync` puts everything in place, because all of it lives in this directory.
The paths the pages depend on:

| Path | Purpose |
|---|---|
| `/install.sh` | the POSIX one-liner target |
| `/install.ps1` | the PowerShell one-liner target |
| `/install.sh.sha256` | digest, read live by both pages |
| `/install.ps1.sha256` | digest, read live by both pages |
| `/uninstall.sh`, `/uninstall.ps1` (+ digests) | linked from the pages; not required for install |

The digest files are plain text; the first 64-hex-character token anywhere in the file is
used, so standard `shasum -a 256 install.sh > install.sh.sha256` output works as-is.

> **Recompute every digest after every edit to an installer**, in the same commit:
>
> ```sh
> cd site && for f in install.sh install.ps1 uninstall.sh uninstall.ps1; do
>     sha256sum "$f" | sed 's/ \*/  /' > "$f.sha256"
> done
> ```
>
> A stale digest is worse than no digest: a user who follows `verify.html`, gets a mismatch,
> and finds out the mismatch was our sloppiness learns that the check is noise. The next real
> mismatch will be ignored.

**Missing digests are handled, not faked.** If those files are absent, the pages render
"尚未公告" plus an instruction to verify against GitHub instead. They never display a
placeholder or a stale hard-coded value — there is no hash literal anywhere in the HTML.

### nginx

```nginx
server {
    listen 443 ssl http2;
    server_name yangble5.com;
    root /www/wwwroot/yangble5.com;
    index index.html;

    # ── the one rule ──
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains; preload" always;

    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "no-referrer" always;
    add_header X-Frame-Options "DENY" always;
    add_header Permissions-Policy "geolocation=(), microphone=(), camera=(), payment=()" always;
    add_header Content-Security-Policy "default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'sha256-YhSXRPWEEPURVaJsYXmkYxR+bfYx3vG0Qbm4th+2j8c=' 'sha256-4FFG4w4T/7cQdRclDwWnwwb3pZxhyUhWrDX0fSl2niI='; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'" always;

    # Never cache the digests or the installers.
    location ~ ^/install\.(sh|ps1)(\.sha256)?$ {
        add_header Cache-Control "no-store" always;
        default_type text/plain;
    }

    location / { try_files $uri $uri/ =404; }
}

# Redirect HTTP -> HTTPS. Do not serve any content on :80.
server {
    listen 80;
    server_name yangble5.com;
    return 301 https://$host$request_uri;
}
```

### Caddy

```
yangble5.com {
    root * /www/wwwroot/yangble5.com
    file_server
    # Caddy provisions TLS automatically; add HSTS explicitly.
    header Strict-Transport-Security "max-age=31536000; includeSubDomains; preload"
    header X-Content-Type-Options "nosniff"
    header Referrer-Policy "no-referrer"
    header /install.* Cache-Control "no-store"
}
```

> **Three files in this repo carry these hashes and all three must agree**: this README (the
> nginx block above and the list below), `deploy/Caddyfile`, and
> `deploy/nginx/yangble5.com.conf.example`. They are in sync as of the current `site/` contents.
> The page degrades safely if one of them drifts — with the script blocked, the content, links,
> commands and the honesty column all still render; only the copy buttons, OS detection, the
> live pool status and the live SHA256 lookup stop working — but that failure is *silent*, so
> fix it rather than shipping it.

### CSP hashes for the inline scripts

```
index.html    'sha256-YhSXRPWEEPURVaJsYXmkYxR+bfYx3vG0Qbm4th+2j8c='
verify.html   'sha256-4FFG4w4T/7cQdRclDwWnwwb3pZxhyUhWrDX0fSl2niI='
```

**These change whenever you edit a `<script>` block.** Recompute with:

```sh
python - <<'PY'
import base64, hashlib, pathlib
from html.parser import HTMLParser

class Scripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.grab = False
        self.out = []
    def handle_starttag(self, tag, attrs):
        self.grab = tag == "script" and not dict(attrs).get("src")
    def handle_data(self, data):
        if self.grab:
            self.out.append(data)
    def handle_endtag(self, tag):
        if tag == "script":
            self.grab = False

for f in ("index.html", "verify.html"):
    p = Scripts()
    p.feed(pathlib.Path("site", f).read_text(encoding="utf-8"))
    p.close()
    for s in p.out:
        d = base64.b64encode(hashlib.sha256(s.encode()).digest()).decode()
        print(f"{f:14} 'sha256-{d}'")
PY
```

> **Line endings do not affect these hashes**, so a Windows checkout that serves CRLF and a
> Linux checkout that serves LF both work. HTML input-stream preprocessing normalises CR and
> CRLF to LF before tokenisation, so the script element's text — the thing that gets hashed —
> is identical either way. This was checked by serving both byte sequences under this exact CSP
> and confirming zero violations, not assumed from the spec. (The `.sha256` digests of the
> *installers* are a different matter entirely: those hash raw file bytes, which is why
> `.gitattributes` pins `site/install.*` to `eol=lf`.)

> **Use a parser, not a regex.** The obvious `re.findall(r"<script[^>]*>(.*?)</script>")`
> silently hashes the wrong bytes the moment any *string* `<script` appears earlier in the file
> — a CSS comment mentioning the tag is enough. The resulting hash looks perfectly valid, is
> wrong, and the only symptom is that the deployed page's script is blocked. Both pages'
> comments now avoid writing the tag names literally, but the parser is what makes that
> irrelevant. **Verify the hash in a browser, not just in Python**: serve the page with the CSP
> header attached and check the console is free of `Refused to execute inline script`.

The alternative — `script-src 'unsafe-inline'` — is not acceptable on a page whose whole job is
to convince people it is safe to paste a command into a shell.

---

## The `/api/health` contract

The status widget on `index.html` fetches, in order, `/api/health` → `/health` → `/healthz`
(same origin only, `credentials: "omit"`, 6-second timeout, first success wins). Route at least
one of them to the gateway.

Today's gateway response — `gateway/app.py`, the `/health` route — is:

```json
{
  "status": "ok",
  "service": "yangble5-gateway",
  "version": "0.1.0",
  "uptime_seconds": 8123,
  "accepting_requests": true,
  "registration": "invite"
}
```

That endpoint is public and unauthenticated, and **deliberately reports no spend, no account
count and no capacity figure**. The widget therefore renders:

| Condition | What the page shows |
|---|---|
| `accepting_requests: true` | 「服務運作中,共用池開放使用」, green |
| `accepting_requests: false` or `status: "degraded"` | 「共用池已達上限」, amber, **and the BYOK panel opens** |
| fetch failed / non-2xx / bad JSON | 「狀態未知」 — and BYOK stays closed, because unknown is not the same as exhausted |
| no numeric capacity field | the bar stays hidden; the field reads 「未提供」 |

### If you want the percentage bar

Add **either** of these shapes to the health payload and the bar appears by itself — no page
change needed:

```json
{ "pool": { "remaining_pct": 8.4, "resets_at": "2026-07-22T00:00:00Z" } }
```

```json
{ "remaining_pct": 8.4, "resets_at": "2026-07-22T00:00:00Z" }
```

`remaining_pct` must be a JSON **number** in `[0, 100]`; anything else is ignored and the bar
stays hidden. `resets_at` is ISO-8601 and is rendered in the visitor's local timezone. The bar
turns amber at ≤30% and red at ≤10%.

> **Known gap — the bar does not render against the gateway as shipped, and that is fine.**
> Two things have to line up and currently neither does. (1) The capacity figures live on
> `GET /pool/status`, a route the page never fetches; `/health` deliberately carries no
> capacity field. (2) `/pool/status` spells the timestamp `reset_at`, while the page reads
> `resets_at`. So the page falls through to its documented "no numeric capacity field" branch
> and hides the bar — which is the safe outcome, and the one the gateway intends (see the note
> above about publishing live capacity to abusers). If you *want* the bar, the fix is on the
> serving side: have your edge merge `/pool/status` into the health payload under the shapes
> above, renaming `reset_at` to `resets_at`. Do not add a fallback number to the page.

> **Do not add a fallback value to make the bar always render.** The rule this page is built
> on is that it shows a real number or no number. A plausible-looking default is the one bug
> that would make everything else on the page untrustworthy. The relevant function in
> `index.html` is commented to that effect — read it before changing it.
>
> Also consider whether you want this public at all: a live capacity number tells an abuser
> exactly when to drain the pool. The gateway omits it on purpose. Shipping it is a choice,
> not an oversight to correct.

---

---

## What the installers do when there is no registration endpoint

An instance is not required to issue keys. A self-hosted or BYOK-only deployment — including
`yangble5.com` as it stands — simply does not expose `POST /auth/register`, and answers **404**.

Both installers treat that as a **normal outcome, not a failure**: `404` and `501` fall through
to BYOK mode exactly like `403` / `409` / `429` / `503` do. The installer still writes the
isolated client config, the launchers and the uninstaller under `~/.yangble5`, prints how to
supply a key, and exits `6` ("installed in BYOK mode with no key yet"). Nothing is rolled back;
dropping a key into `~/.yangble5/credentials` makes the existing install work.

The only statuses that abort the install are a transport failure (exit `5`) and a genuinely
unexpected HTTP status (exit `6` via a hard failure, with the first 400 bytes of the body
printed). If you add a status to the fall-through list, add it to **both** `install.sh` and
`install.ps1`, and recompute the digests.

---

## Editing notes

- **Numbers.** Every figure on the page is traceable to `docs/BENCHMARK.md` /
  `docs/FINDINGS.md` and carries its reproduce command plus the measurement conditions. If you
  change a number here, change it there first. Anything unmeasured says 「未量測」 and must
  keep saying it. Three specifics that keep getting broken: the hit rate is **warm-rounds-only
  and an upper bound** (the harness's session tail grows ~15 tokens per round — the most
  cache-favourable shape there is); the cold first request is **0.00%**; and **no page may claim
  a latency improvement**, because two of the three warm rounds were slower than the cold round
  and time-to-first-token was never measured at all. The results section prints the raw
  per-round record (prompt / `cache_read` / hit / uncached tail / round trip) and both totals —
  warm-only **99.53%** and all-four **74.6%** — so a reader can recompute either one.
- **Prefix-size dependence is a direction, not a number.** The hit rate rises as the prefix
  grows, and the page says so — but the released evidence set contains exactly **one** run, the
  748,918-token one. No figure for any other prefix size may appear on these pages.
- **The two halves of Finding 1 must stay separated.** The pool-rotation *mechanism* is
  **verified** in 7.1.23's source and in the binary that was run (`nextModelPoolOffset` ignores
  `routing.strategy` and session affinity). The **~50% ceiling is reasoned, not measured** —
  there is no pool-vs-direct A/B run in this repo. The `.claimbox` in the `#finding` section
  exists to hold that distinction; do not collapse it into one sentence.
- **Positioning.** yangble5 is a proxy over third-party models. The page must never call it a
  model, never claim a Taiwanese-trained LLM, never quote a free-credit dollar figure, and
  never claim it beats any provider. CLIProxyAPI is a third party (MIT, Luis Pater /
  Router-For.ME) and is credited in the footer and in the 「這不是什麼」 column. The
  「這不是什麼」 column is as prominent as the 「你會得到」 column by design — same grid, same
  heading size, same body size — and a condensed version of it sits in the hero, above the fold.
  Keep both.
- **No external requests.** No CDN, webfont, tracker, or third-party image, ever. Both pages
  are self-contained; the only network calls at runtime are same-origin `/api/health` and the
  two `.sha256` files. The favicon and the pool-rotation diagram are inline (a `data:` SVG and
  a self-authored inline `<svg>`) precisely so this stays true.
- **Theme.** Dark by default, light via `prefers-color-scheme`. All 26 text pairs are ≥ 4.5:1 in
  both themes (lowest measured: **5.30:1**, `--accent` on `--surface-3` in light) and all 7
  meaningful graphical objects — the diagram's lanes, routing paths, lineage arcs and the focus
  ring — are ≥ 3:1 (lowest: **3.26:1**). Both `<meta name="theme-color">` entries and
  `color-scheme` are set, so browser chrome and form controls follow.
- **Typography.** One modular type scale (`--t--2` … `--t-5`) and one 4px spacing rhythm
  (`--s-1` … `--s-8`); no ad-hoc `px` values. Traditional-Chinese punctuation is full-width
  throughout the prose (`,。、()「」`), half-width only inside commands and code. Inline
  `<code>` inside prose gets `margin-inline` so CJK↔Latin runs do not collide, numbers use
  `font-variant-numeric: tabular-nums`, headings use `text-wrap: balance`.
- **Accessibility.** Real `<button>`s, a skip link, roving-tabindex tablists with arrow/Home/End
  keys, `aria-live="polite"` copy feedback (one live region per copy button, targeted by
  `data-copy-status`), scrollable table wrappers exposed as focusable `role="region"`, visible
  focus rings, `prefers-reduced-motion` honoured. Copy feedback is never colour-only — the
  button label changes too. Both diagrams are `role="img"` with a `<title>` and a `<desc>` that
  describes the finding in words, so the diagram is not the only way to receive it.
- **Print.** Both pages carry a print stylesheet: ink-on-paper palette, chrome and buttons
  removed, `<details>` forced open, link hrefs printed after the text, tables un-scrolled.

## Validation

There is no test runner for static files; the check that was run is a standalone script using
Python's stdlib `html.parser`, verifying tag balance, duplicate ids, anchor,
`aria-controls` / `aria-labelledby` / `data-copy-*` and `getElementById` target resolution,
absence of external subresources and inline event handlers, exactly one `<style>` and one
`<script>` per page, `<html lang>`, `<button type>`, and that every 3-or-more-digit figure
rendered as page text appears in the authoritative measurement record. Contrast is checked
separately for all 33 foreground/background pairs per theme (66 total). The tag-balance core,
which is the part most likely to be needed in a hurry:

```sh
python - <<'PY'
import pathlib
from html.parser import HTMLParser
VOID={"area","base","br","col","embed","hr","img","input","link","meta","param","source","track","wbr"}
class C(HTMLParser):
    def __init__(s): super().__init__(convert_charrefs=True); s.st=[]; s.err=[]
    def handle_starttag(s,t,a):
        if t not in VOID: s.st.append((t,s.getpos()))
    def handle_endtag(s,t):
        if t in VOID: return
        if not s.st: s.err.append(f"{s.getpos()[0]}: stray </{t}>"); return
        if s.st[-1][0]==t: s.st.pop()
        else: s.err.append(f"{s.getpos()[0]}: </{t}> vs open <{s.st[-1][0]}> line {s.st[-1][1][0]}")
for f in ("index.html","verify.html"):
    c=C(); c.feed(pathlib.Path("site",f).read_text(encoding="utf-8")); c.close()
    c.err += [f"<{t}> never closed (line {p[0]})" for t,p in c.st]
    print(f, "OK" if not c.err else c.err)
PY
```

Behaviour was additionally verified in a browser against a mock gateway covering all three
status states (healthy-without-capacity, exhausted, capacity-present), both copy paths
(success and clipboard-denied fallback), keyboard tab navigation, OS auto-detection, and a
360 px viewport with no horizontal page scroll.
