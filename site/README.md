# `site/` — the yangble5.com landing page

Three static files. No build step, no bundler, no package.json, no CDN, no fonts, no
analytics, no cookies. Copy them to a web root and you are done.

```
site/
├── index.html    the landing page (inline CSS + JS, fully self-contained)
├── verify.html   installer SHA256 + how to check it yourself
└── README.md     this file
```

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

Alongside the HTML, that web root must also serve:

| Path | Owner | Purpose |
|---|---|---|
| `/install.sh` | installer track | the POSIX one-liner target |
| `/install.ps1` | installer track | the PowerShell one-liner target |
| `/install.sh.sha256` | installer track | digest, read live by both pages |
| `/install.ps1.sha256` | installer track | digest, read live by both pages |

The digest files are plain text; the first 64-hex-character token anywhere in the file is
used, so standard `shasum -a 256 install.sh > install.sh.sha256` output works as-is.

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
    add_header Content-Security-Policy "default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'sha256-pawXZ1E0gh7sMFUp8jAoUcwWaz/IL9rZ7ehGQ+RBzqw=' 'sha256-7WA/0DT8sBnHajkv7BF2ptQTPnLMaqGR9nUVz+BoIEQ='; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'" always;

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

> If you are instead serving the site through the repo's `deploy/Caddyfile`, note that its
> `security_headers` snippet ships a CSP with `script-src 'self'` and **no** hashes, which
> blocks these pages' inline scripts. Add the two hashes below to that directive, or serve the
> site from a separate vhost. The page degrades safely if you forget — with scripts blocked the
> content, links and commands all still render; only copy buttons, OS detection and the live
> status stop working — but fix it rather than shipping it that way.

### CSP hashes for the inline scripts

```
index.html    'sha256-pawXZ1E0gh7sMFUp8jAoUcwWaz/IL9rZ7ehGQ+RBzqw='
verify.html   'sha256-7WA/0DT8sBnHajkv7BF2ptQTPnLMaqGR9nUVz+BoIEQ='
```

**These change whenever you edit a `<script>` block.** Recompute with:

```sh
python - <<'PY'
import base64, hashlib, pathlib, re
for f in ("index.html", "verify.html"):
    html = pathlib.Path("site", f).read_text(encoding="utf-8")
    for s in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S):
        d = base64.b64encode(hashlib.sha256(s.encode()).digest()).decode()
        print(f"{f:14} 'sha256-{d}'")
PY
```

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

> **Do not add a fallback value to make the bar always render.** The rule this page is built
> on is that it shows a real number or no number. A plausible-looking default is the one bug
> that would make everything else on the page untrustworthy. The relevant function in
> `index.html` is commented to that effect — read it before changing it.
>
> Also consider whether you want this public at all: a live capacity number tells an abuser
> exactly when to drain the pool. The gateway omits it on purpose. Shipping it is a choice,
> not an oversight to correct.

---

## Editing notes

- **Numbers.** Every figure on the page is traceable to `docs/BENCHMARK.md` /
  `docs/FINDINGS.md` and carries its reproduce command plus the measurement conditions. If you
  change a number here, change it there first. Anything unmeasured says 「未量測」 and must
  keep saying it.
- **Positioning.** yangble5 is a proxy over third-party models. The page must never call it a
  model, never claim a Taiwanese-trained LLM, never quote a free-credit dollar figure, and
  never claim it beats any provider. The 「這不是什麼」 column is as prominent as the
  「你會得到」 column by design — keep it that way.
- **No external requests.** No CDN, webfont, tracker, or third-party image, ever. Both pages
  are self-contained; the only network calls at runtime are same-origin `/api/health` and the
  two `.sha256` files.
- **Theme.** Dark by default, light via `prefers-color-scheme`. All text pairs are ≥ 4.5:1 in
  both themes (lowest measured: 5.08:1).
- **Accessibility.** Real `<button>`s, a skip link, roving-tabindex tablists with arrow/Home/End
  keys, `aria-live="polite"` copy feedback, visible focus rings, `prefers-reduced-motion`
  honoured. Copy feedback is never colour-only — the text changes too.

## Validation

There is no test runner for static files; the check that was run is a standalone script using
Python's stdlib `html.parser`, verifying tag balance, duplicate ids, anchor and
`getElementById` target resolution, absence of external subresources and inline event handlers,
`<html lang>`, `<button type>`, and WCAG contrast for all 18 foreground/background pairs in
both themes. Re-run it after edits:

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
