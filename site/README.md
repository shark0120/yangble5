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
    add_header Content-Security-Policy "default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'sha256-azlzgFYelw1E8Ku3E8GqYH1fE6nmHMTP3Cy/CCWrGT8=' 'sha256-4FFG4w4T/7cQdRclDwWnwwb3pZxhyUhWrDX0fSl2niI=' ; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'" always;

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
index.html    'sha256-azlzgFYelw1E8Ku3E8GqYH1fE6nmHMTP3Cy/CCWrGT8='
verify.html   'sha256-4FFG4w4T/7cQdRclDwWnwwb3pZxhyUhWrDX0fSl2niI='
```

**These change whenever you edit a `<script>` block.** `python tools/sitecheck.py` checks them on
every push: it recomputes both hashes from the pages and reports any consumer that is missing one
or still carries a hash no page produces. Recompute them by hand with:

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

An instance is not required to issue keys. A self-hosted or BYOK-only deployment simply does not
expose `POST /auth/register`, and answers **404**.

**Do not look that up in this file — ask the instance.** `GET /health` reports the live value in
its `registration` field (`open`, `invite` or `closed`; `gateway/app.py`), and `GET /pool/status`
adds `registration_open`, which also goes false when the operator's cap is reached. A static
sentence in a README cannot track a runtime setting, and a stale one is worse than none: it tells
a reader — or an AI agent about to run the one-liner — to expect BYOK-with-no-key from an
instance that would have handed them a key. This paragraph named `yangble5.com` as an example of
an instance with no registration endpoint; that was wrong at the time it was read, because
registration on that host is **open**. The generic behaviour below is what this section is for.

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

## The transparency section, claim by claim

`index.html` `#installer` — plus the hero's `cmd-foot` and `verify.html` `#env` / `#files` /
`#backup` — is the part of this site that exists to make it safe to paste a one-liner into an AI
agent that has shell access. **Accuracy there is the product, not decoration around it.** So
every sentence in those sections is mapped below to the code that implements it.

> **The rule: if a sentence has no row in this table, delete the sentence.**
> A wrong short list is worse than no list — it teaches the reader that the section is decorative,
> and the next reader will not check the one claim that mattered.

Rows are anchored on **function names first, line numbers second**, because `site/install.sh` gets
edited and the numbers drift — they drifted by 3–4 lines while this table was being written.

Do **not** pin these numbers to `install.sh.sha256`. That digest is over raw bytes, so it changes
when a checkout flips CRLF↔LF even though not one line moved; it answers "same file?", not "same
line numbers?". Anchor them mechanically instead — each row's number must still land on the
construct it names:

```sh
python - <<'PY'
import pathlib, re
def load(p):
    return pathlib.Path(p).read_text(encoding="utf-8", errors="replace").splitlines()
L = load("site/install.sh")
P = load("site/install.ps1")
# (line cited in the table, a pattern that line must still match)
A = [(132,r'^YB5_HOME='),(151,r'^PRINT_KEY=0'),(127,r'^EX_VERIFY=8'),
     (184,r'^trap cleanup EXIT HUP INT TERM'),
     (274,r'^sanitize_remote\(\)'),(295,r'^print_remote\(\)'),
     (380,r'--no-bin-link\)'),(381,r'--show-key\)'),(404,r'^refuse_root\(\)'),
     (539,r'TMPD="\$\(mktemp -d'),(541,r'chmod 700 "\$TMPD"'),
     (556,r'^timestamp\(\)'),(564,r'chmod 700 "\$1"'),
     (577,r'wf_tmp="\$\{TMPD\}/write'),
     (591,r'if \[ -f "\$wf_dest" \]'),(597,r'wf_nobak.*!= "nobackup"'),
     (621,r'^ensure_machine_salt\(\)'),(636,r'> "\$ems_file"'),
     (656,r'^http_call\(\)'),(662,r'hc_out="\$\{TMPD\}/resp'),
     (663,r'hc_cfg="\$\{TMPD\}/curlrc'),
     (666,r'chmod 600 "\$hc_cfg"'),(694,r'curl --config'),
     (764,r'^CRED_FILE='),(882,r'^\s+reg_body='),(894,r'chmod 600 "\$reg_body"'),
     (987,r'ensure_dir "\$YB5_HOME"'),
     (1008,r'cred_tmp="\$\{TMPD\}/credentials'),(1043,r'env_tmp="\$\{TMPD\}/env'),
     (1216,r'toml_tmp="\$\{TMPD\}/codex'),(1302,r'info_tmp="\$\{TMPD\}/install_info'),
     (1532,r'v_body="\$\{TMPD\}/probe\.json'),
     (179,r'^cleanup\(\)'),(578,r'^\s+cat > "\$wf_tmp"'),
     (682,r'x-api-key: %s'),(683,r'authorization: Bearer %s'),
     (688,r'output = "%s"'),(1031,r'^\s+\} > "\$cred_tmp"'),
     (1034,r'write_file "\$CRED_FILE" 600'),(1072,r'yb5_load_credentials\(\)'),
     (1183,r'^export YANGBLE5_API YANGBLE5_API_KEY YANGBLE5_MODEL'),
     (1189,r'^export CLAUDE_CONFIG_DIR'),(1206,r'^unset ANTHROPIC_API_KEY'),
     (1209,r'^export CODEX_HOME'),(1212,r'write_file "\$\{YB5_HOME\}/env\.sh" 600'),
     (1237,r'codex/config\.toml" 600'),(1311,r'INSTALL_INFO" 600 nobackup'),
     (1331,r'for ll_name in yangble5-claude'),(1346,r'>> ~/\.profile'),
     (1486,r'http_call GET /health'),(1533,r'"max_tokens":16'),(1548,r'COLD request'),
     (1570,r'PRINT_KEY" -ne 1'),(1610,r'^print_backups\(\)'),
     (1621,r'Exempt on purpose'),(1704,r'verification FAILED')]
# install.ps1 is cited by four rows and drifts on its own schedule, so it needs
# its own anchors — a .sh-only checker reports "0 mismatches" while every
# Windows row is stale, which is precisely the shape of failure this table is
# supposed to prevent.
B = [(129,r'\[switch\] \$ShowKey'),(557,r'Copy-Item -LiteralPath \$Path'),
     (1073,r'^setlocal'),(1284,r'^set "CLAUDE_CONFIG_DIR'),
     (1285,r'^set "ANTHROPIC_BASE_URL'),(1297,r'^set "ANTHROPIC_API_KEY="'),
     (1307,r"yangble5-claude\.cmd'\) -Content"),(1320,r"yangble5-codex\.cmd'\) -Content"),
     (1351,r'^function Add-Yb5ToPath'),(1352,r'if \(-not \$AddToPath\)'),
     (1353,r"GetEnvironmentVariable\('Path', 'User'\)"),
     (1379,r"SetEnvironmentVariable\('Path', \$updated, 'User'\)"),
     (1382,r'^\}'),(1590,r'if \(-not \$ShowKey\)'),(1602,r'Pass -ShowKey if you'),
     (1609,r'shown once, and only once'),
     (1625,r'^function Show-Backups'),(1635,r'restore with:  Copy-Item -LiteralPath')]
# Everything above roughly install.sh:1050 and install.ps1:1100 has never moved;
# everything below it has moved twice. The lists below close that gap: they are
# the rest of the numbers the table cites in prose, which used to be checked by
# nothing at all. That asymmetry is why a "0 mismatches" reading was possible
# while half the table was stale — the checker only ever looked at the numbers
# somebody had remembered to add to it.
A += [(1086,r"YANGBLE5_KEY_ID=''"),(1139,r'YANGBLE5_KEY_ID\)\s+YANGBLE5_KEY_ID='),
      (1149,r'^case "\$YANGBLE5_API" in'),(1192,r'^export ANTHROPIC_MODEL'),
      (1200,r'CLAUDE_CODE_MAX_CONTEXT_TOKENS=%s'),(1202,r'API_TIMEOUT_MS=%s'),
      (1213,r'rm -f "\$env_tmp"'),(1224,r'^model_provider = "yangble5"'),
      (1228,r'model_context_window = %s'),(1229,r'model_max_output_tokens = %s'),
      (1231,r'base_url = "%s/v1"'),(1233,r'^env_key = "YANGBLE5_API_KEY"'),
      (1241,r'claude/README\.txt" 600'),(1247,r'^CLAUDEDIR'),
      (1250,r'yangble5-claude" 700'),(1256,r'^\. "\$\{HOME\}/\.yangble5/env\.sh"'),
      (1262,r'^exec claude "\$@"'),(1265,r'yangble5-codex" 700'),
      (1271,r'^\. "\$\{HOME\}/\.yangble5/env\.sh"'),(1276,r'^exec codex "\$@"'),
      (1279,r'yangble5-env" 700'),(1318,r'LINK_BIN" -eq 1'),
      (1333,r'\[ -e "\$ll_dest" \] && \[ ! -L "\$ll_dest" \]'),
      (1341,r'case ":\$\{PATH\}:" in'),(1344,r'is NOT on your PATH'),
      (1347,r'call the launcher by its full path'),
      (1353,r'yangble5-uninstall" 700'),(1367,r'uninstall\.sh" 700'),
      (1188,r'damage the credentials in ~/\.claude'),
      (1220,r'Your normal ~/\.codex is untouched'),
      (1245,r'here is separate from your real ~/\.claude'),
      (1401,r'It will NOT touch ~/\.claude'),
      (1511,r'http_call GET /v1/models'),(1537,r'http_call POST /v1/messages'),
      (1543,r'HTTP_STATUS" = "200"'),
      (1549,r'99\.53% figure applies to warm rounds'),(1554,r'^\s+v_msg='),
      (1575,r'mode 0600\) and nowhere else'),
      (1578,r"grep '\^YANGBLE5_API_KEY='"),(1590,r'^\s+cat <<KEY'),(1604,r'^KEY'),
      (1611,r'-z "\$BACKUPS"'),(1623,r'Nothing else is exempt'),
      (1629,r'^\s+print_backups'),(1707,r'exit "\$EX_VERIFY"')]
B += [(1370,r"GetEnvironmentVariable\('Path', 'User'\)")]
# A mismatch used to print only "this line no longer matches", which left the
# reader to re-derive 35 numbers by hand -- and the last time that happened the
# hand-derivation was done with a blind regex and made things worse (below).
# So the checker now says WHICH of the three things went wrong, and for the
# only benign one it prints the replacement number. Repair is copy-and-paste.
def scan(anchors, lines, tag):
    bad = []
    for n, rx in anchors:
        if re.search(rx, lines[n-1] if 0 < n <= len(lines) else ""):
            continue
        hits = [i + 1 for i, ln in enumerate(lines) if re.search(rx, ln)]
        if len(hits) == 1:
            bad.append((tag, n, f"MOVED -> {hits[0]}", rx))
        elif not hits:
            bad.append((tag, n, "GONE - read the diff, the BEHAVIOUR may have changed", rx))
        else:
            bad.append((tag, n, f"AMBIGUOUS -> {hits}, pattern no longer names one line", rx))
    return bad
bad = scan(A, L, "install.sh") + scan(B, P, "install.ps1")
print(f"anchors: {len(A) + len(B)}  mismatches: {len(bad)}")
for b in bad: print("  MISMATCH", b)
PY
```

Last run: **`anchors: 120  mismatches: 0`**, against the installers **as committed** — see the
next paragraph, which is the part that matters.

**These numbers describe `git show HEAD:site/install.sh`, not your working tree.** That is the
file a reader who clones the repo will open, so it is the only version the table can honestly be
about. The practical consequence: **if you edit an installer, this checker goes red until you
update the table in the same commit.** That is the intended behaviour, not a nuisance — run it,
paste the `MOVED -> n` numbers back into the lists above and into the prose rows, and commit the
two changes together. `GONE` and `AMBIGUOUS` are different: those mean a construct was renamed or
duplicated, and they need a human to decide whether the *claim* changed.

**Nothing runs this for you.** `.github/workflows/ci.yml` has six jobs — `test`,
`tools-are-stdlib-only`, `offline-self-checks`, `installer-digests`, `published-numbers`,
`no-secrets` — and not one of them executes the block above. It runs only when somebody
remembers, which is why 35 of 74 anchors were stale at one point while this file still read
"mismatches: 0". The fix is the one already applied to `tools/sitecheck.py`, which was extracted
out of this same README for this same reason: move the block to `tools/anchorcheck.py`, run it
from the `offline-self-checks` job, and let a red build do the remembering. **Until that lands,
treat "mismatches: 0" in this file as a claim about the last time a human ran it, not a
guarantee.**

**The claims do not expire when line numbers do.** Only change the prose if the *behaviour*
changed; a moved line is a bookkeeping fix, a changed behaviour is a page edit.

This has already earned its keep once: a fifteen-line comment added to `json_string` moved every
`install.sh` reference above 694, and the checker reported 28 mismatches in one run. Two lessons
were paid for there and are worth stating rather than re-learning. **Do not shift these numbers
with a blind regex** — a `+15` over "every 3-4 digit number" also rewrote `chmod 700` to `chmod
715` in six places, including inside two of the anchor patterns themselves, which then produced
*new* mismatches that looked like real drift. Re-derive each one with `grep -n` instead. And
**line numbers do not belong on the published pages at all**: `index.html` and `verify.html` cite
function names only, so a drift like that one can make this table stale without making the pages
lie. A stale line number reads exactly like a correct one; a function name that no longer exists
does not.

### How the rows below were verified

Four of them are absence claims, checked by grep — an absence cannot be demonstrated by running
the script, only by showing that the matching code is not there. The rest were checked by making the real script
generate the real files: `install.sh` defines every function and stops when sourced with
`YB5_SOURCE_ONLY=1` (that is how `tests/test_installer_validation.py` reaches the validators), so
`write_config` can be called directly against a throwaway `$HOME`.

```sh
mkdir -p /tmp/fh /tmp/td
cat > /tmp/h.sh <<'EOF'
YB5_SOURCE_ONLY=1 . "$PWD/site/install.sh"
set +e
HOME=/tmp/fh; YB5_HOME="$HOME/.yangble5"; YB5_BIN="$YB5_HOME/bin"
CRED_FILE="$YB5_HOME/credentials"; TMPD=/tmp/td
DRY_RUN=0; LINK_BIN=0; BACKUPS=""; MODE="registered"
OS_NAME=Linux; ARCH_NAME=x86_64
API_KEY="yb5_0123456789abcdef_EXAMPLEEXAMPLEEXAMPLE"; KEY_ID="0123456789abcdef"
write_config
EOF
NO_COLOR=1 sh /tmp/h.sh                       # writes the real client files
grep -E '^export ' /tmp/fh/.yangble5/env.sh   # then count them yourself
```

Two cautions if you re-run it. The script's `EXIT` trap does `rm -rf "$TMPD"`, so recreate
`/tmp/td` before a second run. And on a Windows checkout the `chmod` calls are no-ops against
NTFS, so **file modes must be read from the `write_file` call sites, not from `ls` output** — the
mode is the second argument at each call site and is listed per row below.

#### Why the "writes nowhere else" row needs a different method — and what the old one missed

That harness sets `TMPD` by hand and only ever calls `write_config`. It therefore never observes
what a real run does to the *temp* directory, and for a long time this table certified the
sentence "writes nowhere except those two locations" with **`find` over the throwaway `$HOME`
after a run**. That check cannot fail. The temp directory lives under `$TMPDIR` — `/tmp` on most
systems — which is not under `$HOME`, so a `$HOME`-scoped `find` could not have seen it no matter
what the installer did. The row was green because the instrument was pointed at the wrong place,
not because the claim was true. It was not true: a real run creates a `mktemp -d` directory and
puts ten files in it, four of them containing the API key in plaintext.

The replacement recipe watches the temp directory itself. Files there are created and deleted
inside the same run, so a `find` afterwards is also useless — it has to be a **concurrent**
observer:

```sh
# 1. own the temp root, so nothing else on the machine is in the sample
export TMPDIR=/tmp/yb5-audit; rm -rf "$TMPDIR"; mkdir -p "$TMPDIR"
export HOME=/tmp/yb5-home;   rm -rf "$HOME";   mkdir -p "$HOME"

# 2. record every path that ever exists under it, and keep a copy of the bytes
python3 - "$TMPDIR" /tmp/yb5-grab &          # poll at ~1ms; see below
WATCH=$!

# 3. a real end-to-end install
sh site/install.sh --api "$YB5_TEST_ENDPOINT"

# 4. what did the installer put in the temp directory, and did any of it hold the key?
wait $WATCH
grep -rl "$(grep '^YANGBLE5_API_KEY=' "$HOME/.yangble5/credentials" | cut -d= -f2)" /tmp/yb5-grab
```

The watcher is fifteen lines of `os.walk` + `open()` in a loop; any equivalent works
(`inotifywait -m -r -e create "$TMPDIR"` on Linux is shorter and does not race). For the
SIGKILL row, run the same install, wait until `probe.json` appears, `kill -9` the shell, and list
`$TMPDIR` — nothing has cleaned up, because nothing can.

**What this recipe proves and what it does not.** It proves what *this* run wrote, on *this*
platform, through *this* code path. It does not prove the set is closed: a path only reached by
an untaken branch (a `--reinstall`, a registration failure, a BYOK run) is not in the sample. Two
things narrow that gap rather than close it — every temp path in the script is built from `$TMPD`,
and `$TMPD` is assigned exactly once (539), so `grep -n '\${TMPD}' site/install.sh` enumerates the
candidates and the run confirms which of them fire. Combine the two; neither alone is an
enumeration.

A note on modes, because it bites on Windows checkouts: the watcher's `st_mode` is meaningless on
NTFS (every file reports `0o666` there, and `chmod 600` followed by `stat` returns `644`). Modes
must come from the `chmod` calls the run actually executed, which `sh -x` prints:
`sh -x site/install.sh … 2>&1 | grep -E '^\++ chmod'`. That trace is the source for the "extra
`chmod`" column in `verify.html`'s temp-file table: exactly two of the ten get one.

### 1. Isolated directories — no shell profile, no PATH

| Sentence on the page | Implemented by | How it was verified |
|---|---|---|
| Creates `~/.yangble5/` plus `claude/`, `codex/`, `bin/`, all mode `700` | `write_config` → four `ensure_dir` calls (987–990); `ensure_dir` does `mkdir -p` + `chmod 700` (563–564) | ran `write_config`; the four directories appear and nothing else does |
| Four symlinks in `~/.local/bin` | `link_launchers` loop over exactly four names (1331–1338); `YB5_LINK_DIR` defined 134 | source read; the loop names are the only four |
| `--no-bin-link` turns that off | flag 380; guard 1318 | ran with `LINK_BIN=0` → `skipping ~/.local/bin symlinks (--no-bin-link)` |
| A same-named **non**-symlink there is left alone with a warning | 1333–1336 (`[ -e ] && [ ! -L ]` → `warn` + `continue`) | planted a plain file at `~/.local/bin/yangble5-env` and re-ran → `warn … exists and is not a symlink — leaving it alone`, and the planted file's contents were unchanged afterwards |
| Those two are the locations it **leaves things in** | every *persistent* write goes through `write_file` (10 call sites: 1034, 1212, 1237, 1241, 1250, 1265, 1279, 1311, 1353, 1367) or `ensure_machine_salt` (636); every one of those destinations is `$YB5_HOME` (132) or `$YB5_BIN` (133), and `link_launchers` adds the four symlinks in `$YB5_LINK_DIR` (134) | ran a real install against a stub gateway with a throwaway `$HOME`; `find` afterwards returns exactly 10 files under `.yangble5` plus the 4 links, and nothing else |
| There is a **third** location, a `mktemp -d` temp directory under `$TMPDIR`, and it is not permanent | `TMPD` assigned once at 539, `chmod 700` at 541; ten paths are built from it — `write.$$` 577, `resp.$$` 662, `curlrc.$$` 663, `curlerr.$$` 694, `register.json` 882, `credentials.$$` 1008, `env.$$` 1043, `codex.$$` 1216, `install_info.$$` 1302, `probe.json` 1532. Note 577: `write_file`'s **own** staging file is in `$TMPD`, so "every write goes through `write_file`, which targets `$YB5_HOME`" was never a closed argument | concurrent watcher over an owned `$TMPDIR` during a real install → **11 paths recorded: 1 directory + exactly those 10 files**, no others |
| Four of the ten hold the API key in plaintext | `curlrc.$$` gets `header = "x-api-key: %s"` and the `authorization` header (682–683); `resp.$$` is curl's `output` for `/auth/register` (688), i.e. where the key arrives; `credentials.$$` is the staged credentials file (1008–1031); `write.$$` receives its bytes on the way to disk (578) | the watcher kept a copy of every version of every temp file; `grep -l <the key>` over those copies matched **exactly four**: `curlrc`, `resp`, `credentials`, `write`. Confirming the count matters — reading the code suggests two |
| Only two of the ten get an extra `chmod 600`; the `700` directory is the real boundary | `chmod 600 "$hc_cfg"` (666) and `chmod 600 "$reg_body"` (894). The other eight inherit the process `umask` | `sh -x` trace of a full run: the only `chmod 600` lines naming a `$TMPD` path are those two (real output under Validation) |
| A `trap` removes the directory on `EXIT HUP INT TERM`, so a normal exit leaves nothing | `cleanup()` 179–183 (`rm -rf "$TMPD"`), `trap` 184 | after a successful run, `find "$TMPDIR" -mindepth 1 \| wc -l` → **0** |
| `SIGKILL` (and power loss) defeats it, and what is left can contain the key | `SIGKILL` is uncatchable; `register.json` and `probe.json` additionally have no per-file `rm -f` at all, so only the trap ever deletes them | `kill -9` during an in-flight HTTP call → **4 files left** (`curlrc`, `curlerr`, `register.json`, `probe.json`), and `grep` finds the full key in `curlrc`. Real output under Validation |
| Does not look for, read or modify `.bashrc` / `.zshrc` / `.profile` | **absence.** `grep -nE 'bashrc\|zshrc\|\.profile' site/install.sh` → 3 hits, all of them text: header comment 30, banner 455, and the `info` at 1346 that *prints* a suggested line | grep output pasted under Validation below |
| Does not change `PATH`; only prints the line to add | `link_launchers` 1341 reads `":${PATH}:"`; 1344–1347 `warn`/`info` only | source read — there is no assignment to `PATH` anywhere in the file |
| Windows only touches the **user** PATH, and only with `-AddToPath` | `install.ps1` `Add-Yb5ToPath` 1351–1382: the no-flag branch 1352–1364 only reads and advises; the single write is 1379, and every one of the three `[Environment]` calls in the function passes scope `'User'` (1353, 1370, 1379) | source read |

### 2. The key is not printed

| Sentence on the page | Implemented by | How it was verified |
|---|---|---|
| **(hero `cmd-foot`)** The installer does not print the API key by default | `PRINT_KEY=0` (151); `print_key_once` 1570–1587 takes the "NOT printed" branch | ran `print_key_once` — real output pasted under Validation |
| Key is written to `~/.yangble5/credentials`, mode `0600` | `CRED_FILE` 764; `write_file "$CRED_FILE" 600` (1034) | ran `write_config`; the file contains the four `YANGBLE5_*` lines and nothing else |
| What is printed is the path, not the key | 1575–1578, which also prints the `grep '^YANGBLE5_API_KEY=' …` line | in the captured output |
| `--show-key` / `-ShowKey` opts back in, with a warning about the agent transcript | 381 / 1590–1604; `install.ps1` 129, 1590, 1602, 1609 | ran the POSIX side with `--show-key`: the key is printed under `Your yangble5 API key — shown once, and only once (--show-key)`, followed by `You asked for this with --show-key. If an AI agent ran the installer, that key is now in its transcript.` The Windows equivalent is source-read |
| The key never appears in `argv`; curl reads it from a `0600` config file | `http_call` 656–691: `chmod 600 "$hc_cfg"` (666), headers written into the file (682–683), `curl --config "$hc_cfg"` (694) | source read — the key is never an argument to any command |

### 3. Eleven exports and one unset

| Sentence on the page | Implemented by | How it was verified |
|---|---|---|
| `~/.yangble5/env.sh` exports **eleven** variables and unsets one | generated 1043–1213: 1183 (3 names on one line), 1189–1192 (4), 1200–1202 (3), 1209 (1) = 11; `unset` at 1206 | generated the real file and counted: **`exports total: 11`, `unsets total: 1`** (output under Validation) |
| The eleven break down 3 `YANGBLE5_*` / 4 Claude Code / 3 numeric / 1 `CODEX_HOME` | same lines, in that order | the ordered name list is pasted under Validation |
| Values live only in the launcher's process | launchers source `env.sh` then `exec` (1256+1262, 1271+1276); they are never appended to any rc file | source read |
| `ANTHROPIC_API_KEY` is unset because it outranks `ANTHROPIC_AUTH_TOKEN` | 1204–1206 | source read |
| Windows uses the same names split across two `.cmd` launchers under `setlocal` | `install.ps1` 1073 (`setlocal`), 1284–1297 (the shared body: `CLAUDE_CONFIG_DIR` 1284, `ANTHROPIC_BASE_URL` 1285), written out at 1307 (claude) and 1320 (codex); 1297 clears `ANTHROPIC_API_KEY` with `set "ANTHROPIC_API_KEY="` | source read |
| **(verify.html)** `YANGBLE5_KEY_ID` is assigned but not exported, so it is not one of the eleven | assigned 1086 and 1139, absent from the `export` at 1183 | in the generated file: it appears in `yb5_load_credentials`, not in any `export` line |
| **(verify.html)** `credentials` is parsed as `KEY=VALUE`, never sourced | `yb5_load_credentials` 1072–1143 (`while IFS= read -r`) | in the generated file |
| **(verify.html)** three of those values are re-checked against the same allow-lists, exit `6` on failure | 1149–1181: `YANGBLE5_API` (twice), `YANGBLE5_MODEL`, `YANGBLE5_API_KEY` (twice) — `YANGBLE5_KEY_ID` is **not** re-checked | counted in the generated file. The page said "four" during drafting and was corrected to "three" by this row |

### 4. Its own Codex config — not yours

| Sentence on the page | Implemented by | How it was verified |
|---|---|---|
| Writes `~/.yangble5/codex/config.toml` (mode `600`) | 1216–1237 | generated file inspected |
| Sets `model_provider`, `base_url`, context/output ceilings, `env_key` | 1224, 1231, 1228, 1229, 1233 | all five appear in the generated TOML |
| Points Codex at it with `CODEX_HOME` | 1209 | in the generated `env.sh` |
| **Does not open, read or modify `~/.codex/config.toml`** | **absence.** `grep -nE '~/\.codex\|~/\.claude' site/install.sh` → 6 hits, all of them prose in comments or printed text (25, 452, 1188, 1220, 1245, 1401). No file operation names either path | grep output pasted under Validation |
| Plain `claude` keeps your login because `CLAUDE_CONFIG_DIR` is separate | 1189, plus the `claude/README.txt` marker 1241–1247 | generated file inspected |

### 5. Backups, and the one deliberate exemption

| Sentence on the page | Implemented by | How it was verified |
|---|---|---|
| Existing file with different content → `cp -p` to `<file>.bak-<timestamp>` | `write_file` 591–603; `timestamp()` 556 (`date +%Y%m%d-%H%M%S`) | ran `write_config` twice with changed values; two real `.bak-…` files were produced |
| Identical content → prints `unchanged`, no backup | 592–595 | in the captured second-run output |
| Every backup is printed at the end with the exact restoring command | `print_backups` 1610–1624, called from `next_steps` 1629 | real output pasted under Validation — one `restore with: cp -p "…" "…"` line per backup |
| Nothing backed up → says so instead of staying silent | 1611–1614 | ran `print_backups` with `BACKUPS=""` → `no existing file was overwritten, so nothing was backed up` |
| `INSTALL_INFO` is the **only** file exempt from backup, and the script says so | `write_file`'s third argument (571–572, 597); `grep -c 'nobackup'` on the call sites → the single site is 1311; the exemption text is printed at 1621–1623 | the second run rewrote `INSTALL_INFO` and it is **absent** from the printed backup list, with the exemption paragraph printed underneath |
| `machine-id` is created once and never overwritten, so it is never a backup candidate | `ensure_machine_salt` 621–638 returns early at 624–628 when the file exists | source read |
| Windows prints `Copy-Item -LiteralPath … -Destination … -Force` | `install.ps1` `Show-Backups` 1625; the restore line is emitted at 1635 (the backup itself is taken at 557) | source read |

### 6. One real call, honestly reported

| Sentence on the page | Implemented by | How it was verified |
|---|---|---|
| `GET /health` → `GET /v1/models` → `POST /v1/messages` with `max_tokens` 16 | `verify()` 1486, 1511, 1532–1537 (`"max_tokens":16` at 1533) | source read |
| On success it prints the status and time **and says the call was cold, 0%** | 1543–1551; the cold-cache disclosure is 1548–1549 | source read |
| On failure it does not call it a success; exit code 8 | 1554–1559; `EX_VERIFY=8` (127); `main` 1704–1707 | pointed the installer at a stub answering `/auth/register`, `/health` and `/v1/models` with `200` and `/v1/messages` with `500` → `exit=8`, and the output says `the config was written, but the stack did NOT answer. Not calling this a success.` |
| Server text is stripped of ANSI/control bytes, flattened, capped, prefixed `server says>` | `sanitize_remote` 274–292, `print_remote` 295–301 | unit-tested by `tests/test_installer_validation.py` against this same file |

### The "never" list

| Sentence on the page | Implemented by | How it was verified |
|---|---|---|
| Refuses to run as root or under `sudo`, exit 2 | `refuse_root` 404–425 (`id -u` = 0 or `$SUDO_USER` set) | ran with `SUDO_USER=someone` → `exit=2`, ending `If you are an AI agent: do not retry this with sudo. Drop privileges instead.` The `id -u` half is source-read: this harness cannot be root |
| No background service, autostart or daemon | **absence.** `grep -nE 'systemd\|launchd\|launchctl\|crontab' site/install.sh` → no matches | grep output pasted under Validation |
| Downloads and executes no extra code; no `eval` | **absence.** `grep -nE '\beval\b' site/install.sh` → 2 hits, both comments (70, 707). The only network calls are `http_call`, whose URL is always `$YB5_API` + a fixed path | grep output pasted under Validation |
| Registration sends only the fingerprint, a label made of its first 32 chars, and any e-mail / invite you passed | `reg_body` 882–893 — a four-field JSON body, two of them optional | source read; the body is built with `printf`, field by field |
| Does not touch `~/.ssh`, browser data, keychains | **absence** for the tool names (`grep -nE 'security find-generic\|secret-tool\|Keychain\|\.ssh' site/install.sh` → no matches), plus the write enumeration in §1 | planted canary files at `~/.claude/settings.json`, `~/.codex/config.toml` and `~/.ssh/id_ed25519`, ran a full install, re-hashed: all three `sha256` values identical before and after. Real output below |
| Sends no prompt, code or file contents; registration carries only the two fields | `reg_body` 882–893 | captured the actual request body off the wire during a real run: `{"machine_id":"<64 hex>","label":"installer-<first 32 of it>"}` — no third field, because neither `--email` nor `--invite` was passed |

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
- **The transparency section is a contract, not copy.** Every sentence in `#installer`,
  in the hero's `cmd-foot`, and in `verify.html`'s `#env` / `#files` / `#backup` has a row in the
  claim-by-claim table above. That section's whole reason to exist is that the primary CTA tells
  people to paste a command into an agent with shell access, so a sentence there that is merely
  *approximately* true is a security bug, not a copy nit. **When `install.sh` or `install.ps1`
  changes, re-derive the rows before touching the prose** — regenerate the client files with the
  `YB5_SOURCE_ONLY=1` harness and count, rather than reading the script and believing yourself.
  Two counts are load-bearing and were both wrong at some point: the env-var count (it is
  **eleven exports plus one unset**, not four and not seven) and the number of values `env.sh`
  re-validates (**three**, not four). If a sentence loses its row, delete the sentence — never
  publish a short list in place of an accurate one, and never let the hero imply the key is
  printed, because it is not.
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
- **Theme.** Dark by default, light via `prefers-color-scheme`. Contrast is measured two ways and
  both must stay green: at the **palette** level, all 26 declared text pairs are ≥ 4.5:1 in both
  themes (lowest: **5.30:1**, `--accent` on `--surface-3` in light), and all 7 meaningful
  graphical objects — the diagram's lanes, routing paths, lineage arcs and the focus ring — are
  ≥ 3:1 (lowest: **3.26:1**). At the **rendered** level, the per-element browser sweep under
  Validation below covers every element with a visible text node in both themes and finds nothing
  under AA. The palette figure is the design constraint; the sweep is what catches a pair the
  palette list forgot. Both `<meta name="theme-color">` entries and `color-scheme` are set, so
  browser chrome and form controls follow.
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

There is no test runner for static files, so the checker is one: **`tools/sitecheck.py`**, a
committed, stdlib-only file that runs in CI on every push (job `published-numbers`) and under
`pytest` (`tests/test_sitecheck.py`). It verifies tag balance, duplicate ids, resolution of every
in-page anchor / `aria-controls` / `aria-labelledby` / `data-copy-*` / `getElementById` target,
absence of external subresources and inline event handlers, exactly one `<style>` and one
`<script>` per page, `<html lang>`, `<button type>`, that the CSP hashes in `deploy/` and in this
file are the hashes the current inline scripts actually produce, and that **every figure rendered
as page text — decimals included — is either recomputed from the authoritative measurement record
or in an explicit allow-list where each entry carries its reason**.

```
python tools/sitecheck.py              # self-test, then check the pages
python tools/sitecheck.py --self-test  # only the checker's own self-test
python tools/sitecheck.py --inventory  # every figure in scope and where it comes from
```

Exit codes are three, not two: `0` clean, `1` a page has a finding, **`2` the checker's own
self-test failed** — in which case no page result is printed at all, because a checker that cannot
be shown to fail certifies nothing.

### What this section used to say, and why it was false

The paragraph above used to describe a checker that lived **only as a fenced code block in this
file**. Nothing ever ran it, and its numeral guard could not fail:

```python
# The pattern this file replaces.  Kept ONLY so the self-test can prove the
# invariant assertion fires on it.  Never used to check a page.
HISTORICAL_BROKEN = re.compile(
    r"(?<![0-9A-Za-z_.])\d{1,3}(?:,\d{3})+(?![0-9A-Za-z_])"
    r"|(?<![0-9A-Za-z_.])\d+(?![0-9A-Za-z_])"
)
```

That negative lookbehind contains `.`, so a decimal was cut in half. In `99.53` it matched `99`
(discarded as under three digits) and then refused to match `53` because the character before it
was a `.`. **Every percentage on the site was outside the only automated check over published
numbers, including the headline 99.53% hit rate** — the class every claim that matters belongs
to — and the run still printed `OK`.

The tell was in the allow-list. It carried `"9953"`, `"000"` and `"7460"` with the comment
`# 99.53%, 0.00%, 74.6%` — entries written by someone who assumed the pattern stripped the dot
rather than split on it. No page text can ever produce those strings. Three unreachable entries
sat there because nobody had ever watched the guard fail. `749K`, the published prefix size,
escaped by a different route: it ends in a letter, so the identifier rule waved it through as a
name.

### Three properties that make it able to fail now

**1. The self-test runs before the check is trusted.** Ten must-fail cases and nine must-pass
cases, driven through the same `check_page()` the site run calls, against a structurally clean
synthetic page so the only findings that can appear are numeral findings. A must-fail case that
does not fail exits `2` and suppresses the page report entirely.

**2. The tokeniser has an invariant, and the invariant is itself tested.**
*The set of characters consumed must equal the set of characters checked.* `scan_figures` does not
hunt for number-shaped things; it partitions the page text into maximal `[0-9A-Za-z_.,]` atoms,
classifies every atom containing a digit, and then asserts that every ASCII digit landed inside an
atom it produced. A future edit that narrows the pattern cannot silently reopen the hole — the
digits it stops covering are reported by name, with their offsets. The self-test proves that
assertion is alive by running the scanner with the broken pattern above and requiring it to
complain.

```python
# A digit-leading atom ending in one of these is a figure with a unit, not an
# identifier.  Without this, `749K` — the published prefix size — would be
# waved through as a name, which is how it escaped the previous checker.
UNITS = ("KB", "MB", "GB", "ms", "K", "M", "G", "B", "h", "s", "x")

# Maximal run of identifier/number characters.  Not a number pattern: a
# partition.  See the module docstring.
ATOM = re.compile(r"[0-9A-Za-z_](?:[0-9A-Za-z_.,]*[0-9A-Za-z_])?")
UNIT_FIGURE = re.compile(r"^[0-9][0-9.,]*(" + "|".join(UNITS) + r")$")
COMMA_FORM = re.compile(r"^[0-9]{1,3}(,[0-9]{3})+(\.[0-9]+)?$")

# The pattern this file replaces.  Kept ONLY so the self-test can prove the
# invariant assertion fires on it.  Never used to check a page.
HISTORICAL_BROKEN = re.compile(
    r"(?<![0-9A-Za-z_.])\d{1,3}(?:,\d{3})+(?![0-9A-Za-z_])"
    r"|(?<![0-9A-Za-z_.])\d+(?![0-9A-Za-z_])"
)
```

**3. Measurements are recomputed, not listed.** A hit rate is accepted because the arithmetic
produces it, at the precision the page printed. Nothing rounds to 99.54, so 99.54 cannot pass — it
is not a string missing from a list, it is a number the record does not contain.

```python
# ── percentages, recomputed rather than allow-listed ────────────────────────
# A hit rate is accepted only if some authoritative cached/prompt pair, printed
# to the same number of decimal places the page used, is exactly that string.
# No pair rounds to 99.54 at any precision, so 99.54 cannot pass.
PERCENT: dict[str, str] = {}
_PAIRS = [(f"round {i + 1}", CACHED[i], PROMPT[i]) for i in range(4)] + [
    ("warm token-weighted", _WARM_C, _WARM_P),
    ("all four rounds", _ALL_C, _ALL_P),
]
for _label, _c, _p in _PAIRS:
    _r = 100.0 * _c / _p
    for _dp in (1, 2):
        PERCENT.setdefault(f"{_r:.{_dp}f}", f"{_label} hit rate = {_c}/{_p} = {_r:.4f}%")
```

The allow-list is held to the same standard from the other side: **an entry that nothing on the
site matches is reported as a finding**. A wish list cannot masquerade as a guard.

### Real output

```
$ python tools/sitecheck.py --self-test
self-test: the guard must fail on a bogus figure
    PASS  must-fail  bogus hit rate 99.54
              -> unaccounted figure: 99.54  (as written: 99.54)
    PASS  must-fail  bogus cold hit rate 0.01
              -> unaccounted figure: 0.01  (as written: 0.01)
    PASS  must-fail  bogus integer
              -> unaccounted figure: 12345  (as written: 12345)
    PASS  must-fail  bogus prompt total
              -> unaccounted figure: 748919  (as written: 748,919)
    PASS  must-fail  bogus derived warm cached total
              -> unaccounted figure: 2236291  (as written: 2,236,291)
    PASS  must-fail  bogus round-trip ms
              -> unaccounted figure: 21411  (as written: 21,411)
    PASS  must-fail  bogus prefix shorthand
              -> unaccounted figure: 750K  (as written: 750K)
    PASS  must-fail  bogus context claim
              -> unaccounted figure: 3M  (as written: 3M)
    PASS  must-fail  malformed thousands separator
              -> malformed thousands separator: '74,8918' — grouped digits must be 1-3 then groups of exactly 3
    PASS  must-fail  full-width digits evade an ASCII scanner
              -> INVARIANT: non-ASCII digit '９' (U+FF19) in page text at offset 2; the figure scanner only understands ASCII digits, so this numeral would never be checked
self-test: the guard must pass on the authoritative record
    PASS  must-pass  authoritative warm hit rate
    PASS  must-pass  authoritative cold hit rate
    PASS  must-pass  authoritative all-four hit rate
    PASS  must-pass  authoritative prompt/cached/ms
    PASS  must-pass  prefix shorthand
    PASS  must-pass  config figures
    PASS  must-pass  versions
    PASS  must-pass  identifiers are not figures
    PASS  must-pass  short bare integers are not figures
self-test: the consumed-equals-checked invariant must itself fail when the tokeniser narrows
    PASS  invariant  historical pattern flagged
              -> INVARIANT VIOLATED: the set of characters consumed is not the set checked — 2 digit(s) fell outside every token the scanner produced: '5' at offset 7 
self-test: an unmatched allow-list entry must be reported
    PASS  stale-allow  all 20 entries reported when nothing matches them
self-test: a stale or missing CSP hash must be reported
    PASS  csp  correct
    PASS  csp  a bare hash in prose is not a directive
    PASS  csp  stale hash beside the right one
              -> cfg: stale inline-script hash sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA= — no page produces it, so the deployed CSP would block the script it names
    PASS  csp  hash missing entirely
              -> cfg: missing the current index.html inline-script hash sha256-bhHHL3z2vDgxUt0W3dWQOrprscmda2Y5pLsLg4GF+pI= — recompute and update this file
self-test: 99.54 must not be reachable from the record at any printed precision
    PASS  record  99.53 provenance: round 2 hit rate = 745438/748933 = 99.5333%
self-test: OK
$ echo exit=$?
exit=0
```

```
$ python tools/sitecheck.py --quiet
index.html: OK
verify.html: OK
CSP hashes: OK
allow-list: OK (20 entries, all matched)
$ echo exit=$?
exit=0
```

The negative control, which CI runs on every push. It copies `site/` to a temp directory, plants
`99.54%` in the copy, and requires a red run naming it. The copy is why there is no restore step —
`git checkout -- site/index.html` as cleanup silently discards whatever else was uncommitted in
that file, which is a destructive way to run a read-only check.

```
$ work="$(mktemp -d)/site"; cp -r site "$work"
$ sed -i '0,/99.53%/s//99.54%/' "$work/index.html"
$ python tools/sitecheck.py --quiet --site "$work"
index.html: 1 PROBLEM(S)
    - unaccounted figure: 99.54  (as written: 99.54)
verify.html: OK
allow-list: OK (20 entries, all matched)
$ echo exit=$?
exit=1
```

(`--site` checks a copy, so the CSP row — whose consumers live in `deploy/` and are not copied —
is skipped there.)

And the other direction: with the historical pattern restored as the default tokeniser, the
invariant names the exact digits that stopped being checked, and the run refuses to report on the
pages at all.

```
$ python tools/sitecheck.py
...
SELF-TEST FAILED — the checker is not trustworthy, so no page result is reported.
  - MUST-PASS CASE FAILED: authoritative warm hit rate: payload '暖輪 99.53% 命中' produced
    ["INVARIANT VIOLATED: the set of characters consumed is not the set checked — 2 digit(s)
      fell outside every token the scanner produced: '5' at offset 8 ...; '3' at offset 9 ..."]
  - MUST-PASS CASE FAILED: prefix shorthand: payload '~749K 前綴' produced
    ["INVARIANT VIOLATED: ... 3 digit(s) fell outside every token ..."]
$ echo exit=$?
exit=2
```

### What the fixed tokeniser sees that the old one could not

Seven figures were structurally invisible. Six were decimals or dotted versions; the seventh wore
a unit suffix. Nothing moved the other way — the old pattern invented no tokens the new one
misses.

| Figure | Where it appears | Why it was invisible | Ruled in by |
|---|---|---|---|
| `99.53` | hero, diagram, results table, caveats | lookbehind `.` split it into `99` (too short) + nothing | recomputed: 745438/748933 = 99.5333% |
| `0.00` | cold row, diagram, caveats | same | recomputed: 0/748918 |
| `74.6` | all-four-rounds row, caveats | same | recomputed: 2236290/2995762 = 74.6485% |
| `749K` | hero, mechanism section | ends in a letter, so the identifier rule skipped it | derived: every round's prompt total rounds to 749K |
| `3.14.3` | measurement conditions | same as the decimals | allow-listed: the Python the run was made on |
| `7.1.23` | mechanism, measurement conditions | same | allow-listed: the engine version under test |
| `7.2.93` | claims table | same | allow-listed: the version that retired the shim workaround |

**A second hole, found by the self-test rather than by reading.** `"".join(text_nodes)` fused the
end of one text node against the start of the next, so a figure opening a node glued onto the
previous node's last letter and was reclassified as a name. The self-test's own scaffold page
tripped it: `<title>t</title>` followed by `12345` became the atom `t12345`, and six must-fail
cases came back clean. On the real pages it was hiding only a one-digit `5`, so nothing published
was affected — but the mechanism is the same consumed-is-not-checked failure in a different place,
and it was present in the version being replaced. Text nodes are now joined with a separator:
splitting a figure produces a loud unaccounted fragment, fusing one produces silence.

### Reconciliation: every figure now in scope

`python tools/sitecheck.py --inventory` prints this. It exists because *0 problems* is also what a
checker that examined nothing prints, and looking at what was examined is the only way to tell
those two apart.

```
figure       as written     page                   ruled in by
--------------------------------------------------------------------------------------------------------------
1M           1M             index                  the 1,000,000-token context window the page is about
12h          12h            index                  session-affinity TTL written as 12h
256          256            index,verify           shasum -a 256 / sha256
400          400            index                  quoted upstream error 'API Error: 400'
402          402            index                  HTTP 402 returned by the gateway when the shared pool is exhausted
443          443            index                  80/443, the ports a pre-existing web server may already hold
600          600            index,verify           chmod 600, a file mode
700          700            index,verify           directory mode 700
0.00         0.00           index                  round 1 hit rate = 0/748918 = 0.0000%
0600         0600           index,verify           file mode 0600 as printed in the verify listing
0700         0700           verify                 file mode 0700 as printed in the verify listing
200K         200K           index                  the 200K window a client assumes for an unrecognised model name
2024         2024           index                  quoted wrong answer from the Gemini upstream (no live web search)
2025         2025           index                  quoted wrong answer from the Grok upstream (no live web search)
2026         2026           index                  2026-07-21, the measurement date
3495         3,495          index                  round 2 uncached tail = 748933 - 745438
3518         3,518          index                  round 3 uncached tail = 748948 - 745430
3541         3,541          index                  round 4 uncached tail = 748963 - 745422
74.6         74.6           index                  all four rounds hit rate = 2236290/2995762 = 74.6485%
749K         749K           index                  prefix shorthand for 748,918 tokens
10554        10,554         index                  warm uncached tail = warm prompt - warm cached
10753        10,753         index                  round 2 round-trip ms
21410        21,410         index                  round 1 round-trip ms
22381        22,381         index                  round 4 round-trip ms
23457        23,457         index                  round 3 round-trip ms
65536        65536          verify                 CLAUDE_CODE_MAX_OUTPUT_TOKENS default
99.53        99.53          index                  round 2 hit rate = 745438/748933 = 99.5333%
3.14.3       3.14.3         index                  Python 3.14.3 — the interpreter the run was made on
600000       600000         index,verify           --prefix-tokens 600000 in the bench command, and the API_TIMEOUT_MS default
7.1.23       7.1.23         index                  CLIProxyAPI 7.1.23 — the engine version under test
7.2.93       7.2.93         index                  engine 7.2.93 — the version that made the shim workaround unnecessary
745422       745,422        index                  round 4 tokens read from cache
745430       745,430        index                  round 3 tokens read from cache
745438       745,438        index                  round 2 tokens read from cache
748918       748,918        index                  round 1 prompt tokens
748933       748,933        index                  round 2 prompt tokens
748948       748,948        index                  round 3 prompt tokens
748963       748,963        index                  round 4 prompt tokens
759472       759,472        index                  all-four uncached tail
1000000      1000000        verify                 CLAUDE_CODE_MAX_CONTEXT_TOKENS / model_context_window default
2236290      2,236,290      index                  warm cached total = rounds 2+3+4
2246844      2,246,844      index                  warm prompt total = rounds 2+3+4
2995762      2,995,762      index                  all-four prompt total

43 distinct figures in scope.
```

Nothing on the pages needed correcting: all 43 are in the authoritative record, derived from it by
arithmetic the checker redoes, or allow-listed with a reason. The permitted measurements are
exactly the four rounds in `PROMPT` / `CACHED` / `ROUND_MS` at the top of `tools/sitecheck.py`.
**Two of the three warm rounds were slower than the cold round, so no latency-improvement claim is
permitted and none is made** — the results table labels rounds 3 and 4 「比冷輪慢」 and the note
under it calls that column anecdote, not conclusion.

### Audit: can each of the other recipes in this file fail?

The numeral guard was the third verification-that-cannot-fail found in this project. The other two
were a `$HOME`-scoped `find` certifying a claim about `/tmp`, and a secret-scan regex whose
branches fused into an impossible literal. Three is a pattern, not three accidents, so every
remaining recipe here was re-read with one question: **can this method observe a violation of the
claim it certifies?**

| Recipe | Can it fail? | Reasoning |
|---|---|---|
| Numeral guard (`tools/sitecheck.py`) | **Yes, demonstrated** | Ten must-fail cases run before every check; CI plants `99.54%` on every push. Was **no** until this pass. |
| CSP hashes (§CSP hashes after this change) | **Was effectively no — now yes** | The recipe printed the recomputed hash and then `grep -c`'d for a hash *spelled out in this file's prose*. Edit an inline script and the recompute prints something new while the grep, still carrying the old literal, reports `1 / 1 / 2` and looks green; the comparison only ever happened in the reader's head, and the symptom is a browser silently blocking the script. `sitecheck.csp_problems()` now makes the recomputed value the needle and reports any *other* script hash in a consumer as stale — the case a presence-only grep structurally cannot see. Proven by editing a script for real. |
| Line anchors (§transparency, claim by claim) | **Yes, demonstrated** | Reported 28 mismatches after a comment moved every reference past line 694. Out-of-range lines yield `""` and mismatch rather than crash, and the anchor count is printed, so an emptied list is visible rather than silently green. Limit: it certifies that a cited line still matches a pattern, not that the sentence beside it is still true. |
| Absence greps (§the four absence claims) | **Yes, but blind on one axis** | A grep can observe the string arriving, so it can fail. It runs over `site/install.sh` only, while the claims cover Windows too — the same `.sh`-only shape this file already warns about for the anchor table, where every Windows row can go stale while the checker reports zero. Run today over `install.ps1`, all four patterns plus the PowerShell equivalents (`$PROFILE`, `Register-ScheduledTask`, `HKCU:`/`HKLM:`, `Invoke-Expression`) match nothing outside comments, so there is no live discrepancy — the gap is that nothing would catch one tomorrow. |
| Canary hashes before/after (§the four absence claims) | **Yes, with a caveat** | Differing digests would show a modification. If the canary directories did not exist, `find` would print nothing and before/after would match vacuously — the recipe prints the three hashes, so a vacuous run is visible on the page instead of hidden in an exit code. It covers modification, not reading; the reading claim rests on the grep row above and inherits its `.sh`-only limit. |
| Temp-directory watcher (§the temp directory) | **Yes, demonstrated** | It is what falsified the `$HOME`-scoped `find`: eleven paths, ten files, four holding the key. A concurrent observer is required because the files are created and deleted inside one run, so any `find` afterwards is as blind as the one it replaced. Bounds stated in place: one platform, one code path, and MSYS signal emulation explicitly excluded from any `SIGTERM`/`SIGINT` conclusion. |
| Eleven-exports diff (§the eleven-exports claim) | **Yes, weakly** | A real diff of the generated file against the rendered DOM. Two extractions that both matched nothing would diff clean, which is why the recipe prints the line counts (`real=10 rendered=10`) — a `0 = 0` is visible. Manual, not wired into CI. |
| Contrast sweep (below) | **Yes, demonstrated twice** | Both of its false answers are documented in place: unparsed `color(srgb …)` produced 12 false failures, and sampling without a reload after a scheme switch produced a false 2.09:1. The instrument is self-tested against a known `color(srgb …)` value before any ratio is trusted, and an unparseable colour is a loud skip rather than a silent zero. |

Two of these — the line anchors and the eleven-exports diff — are still copy-paste recipes rather
than committed code, so they run when someone remembers to run them. That is exactly the condition
the numeral guard was in, and it is why the numeral guard stayed broken.


### Two things `sitecheck.py` cannot see, and how they were covered

Neither is reachable from a static parse: both need a real browser. They are listed here so the
boundary of the automated check is explicit — a reader should know what "index.html: OK" does
*not* cover.

- **Contrast** was re-measured in a real browser after this change, over every element that owns a
  visible text node, in both themes, compositing alpha backgrounds down the ancestor chain (an
  audit that ignores alpha reports false failures on `color-mix(… / .1)` pills and false passes on
  accent buttons — both were observed while writing this):

  | Page | Theme | Text-bearing elements | Below AA (4.5:1) | Tightest pair |
  |---|---|---|---|---|
  | `index.html` | dark | 511 | **0** | 6.16:1 (results-table `<th>`) |
  | `index.html` | light | 511 | **0** | 5.40:1 (results-table `<th>`) |
  | `verify.html` | dark | 287 | **0** | 6.16:1 (temp-file table `<th>`) |
  | `verify.html` | light | 287 | **0** | 5.40:1 (temp-file table `<th>`) |

  **Two ways this sweep lies, both of which produced a confident wrong answer during this pass.**
  Neither is hypothetical; both were caught only because a failure that made no design sense was
  chased instead of reported.

  1. **Parse `color(srgb …)`, not just `rgb()`.** A regex that grabs "the numbers" out of
     `color(srgb 0.951922 0.925412 0.935529)` yields `0.95, 0.93, 0.94` — it reads a near-white
     surface as near-black. That produced **12 false AA failures on `verify.html` in light theme**,
     including a `1.12:1` on dark text over a white warning box. Modern themes emit that syntax
     from `color-mix()`, so any sweep here will hit it. Self-test the parser on one known
     `color(srgb …)` value before trusting a single ratio, and make an unparseable colour a loud
     skip rather than a silent zero.
  2. **Reload after switching the emulated colour scheme.** `.tab` has
     `transition: background-color .15s, color .15s`. Flipping the scheme at runtime left those two
     buttons computing the *previous* theme's `--muted` indefinitely — `getComputedStyle` returned
     the dark `#98abbf` while `:root` and every untransitioned sibling had already moved to the
     light `#4e5f72`. That reads as a real **2.09:1** light-theme regression in the hero. It
     survives repeated sampling, so "wait and re-measure" does not clear it; only a reload does.
     The tell is that only transitioned properties are wrong. After a fresh load under the light
     scheme the same element measures correctly and the failure is gone.

- **Behaviour** was verified in a browser against a mock gateway covering all three status states
  (healthy-without-capacity, exhausted, capacity-present), both copy paths (success and
  clipboard-denied fallback), keyboard tab navigation, and OS auto-detection. At a 360 px
  viewport, both pages report `documentElement.scrollWidth === clientWidth === 360` — **no
  horizontal page scroll** — with every overflowing element contained inside a `pre` or
  `.tablewrap` scroller. The two `<pre>` blocks added to `verify.html` for the verbatim `env.sh`
  listing and the backup-output shape carry `tabindex="0"` + `role="region"` + `aria-label`,
  matching how `index.html` exposes its scrollable tables, so the horizontal scroll they need at
  narrow widths is reachable from the keyboard. Console is clean on both pages.

  The temp-file table added to `verify.html` `#tmp` follows the same pattern and was checked the
  same way: at 360 px its `.tablewrap` measures 328 px wide against a 640 px `scrollWidth`, so it
  scrolls **inside itself** while the document stays at 360 — and the wrapper is `role="region"`,
  `aria-label`led, and reachable at tab position 13, so that scroll is not mouse-only. The
  「含金鑰」 column says 「是」/「否」 in text; the red is redundant, never the only carrier.
  The rendered rows were then diffed against the run evidence rather than proofread — the check
  asserts that the four 「是」 rows are exactly `curlrc`/`resp`/`credentials`/`write`, that the two
  `600` rows are exactly `curlrc`/`register.json`, and that the two trap-only rows are exactly
  `register.json`/`probe.json`. It caught one transcription error on first run (`probe.json` had
  the deletion value duplicated into the `chmod` column) and reports `mismatchesVsRunEvidence: []`
  now. A table of facts is worth exactly as much as its weakest cell, and eyes do not catch a
  wrong cell in a ten-row grid.

### The greps behind the four absence claims

```
$ grep -nE "bashrc|zshrc|\.profile" site/install.sh
30:#   * It does NOT edit .bashrc, .zshrc, .profile or your PATH.
455:    - modify your PATH, .bashrc, .zshrc or .profile
1280:            info "    echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.profile"

$ grep -nE "~/\.codex|~/\.claude" site/install.sh
25:#     your ~/.claude directory and your existing subscription are untouched.
452:    - touch your existing Claude Code login or ~/.claude (a separate
1122:# damage the credentials in ~/.claude.
1154:# Your normal ~/.codex is untouched.
1179:here is separate from your real ~/.claude. Deleting this directory logs out
1335:printf '\nIt will NOT touch ~/.claude, ~/.codex, your shell rc files, or anything\n'

$ grep -nE "systemd|launchd|launchctl|crontab" site/install.sh
(no matches)

$ grep -nE "\beval\b" site/install.sh
70:#   * No `eval`, and nothing the server sends is ever executed. The API key is
707:# Deliberately dumb: no eval, no shell expansion of server data, and every
```

Every hit is a comment or a string that gets *printed*. `install.sh:1346` is the `info` line that
shows the user the `>> ~/.profile` command to run themselves; it is not executed. There is no
`systemd` / `launchd` / `launchctl` / `crontab` match at all, and both `eval` matches are prose in
comments.

An absence claim that is only a grep is one rename away from being wrong, so the two that matter
most were also checked positively — canary files planted in the three directories the page
promises not to touch, then a full install on top of them:

```
$ grep -nE "security find-generic|secret-tool|Keychain|\.ssh" site/install.sh
(no matches)

$ (cd $HOME && find .claude .codex .ssh -type f -exec sha256sum {} \;) | sort   # before
605b7927ae2b2d04e7eaa0148151f0b6c267185706623129f5c4b293aa251e60  .codex/config.toml
833a3cf8a88533f95f4b290c59c7c71f007a21dce763919624ac4fc714f7b21f  .claude/settings.json
e89440086e51169b97161032cb64d88cc5b138f7efa2a47836edc1f32c38b082  .ssh/id_ed25519

$ sh site/install.sh --api "$YB5_TEST_ENDPOINT"
installer exit=0

$ ...same command                                                              # after
605b7927ae2b2d04e7eaa0148151f0b6c267185706623129f5c4b293aa251e60  .codex/config.toml
833a3cf8a88533f95f4b290c59c7c71f007a21dce763919624ac4fc714f7b21f  .claude/settings.json
e89440086e51169b97161032cb64d88cc5b138f7efa2a47836edc1f32c38b082  .ssh/id_ed25519
```

And the registration body, captured off the wire rather than read out of `reg_body`'s `printf`
block — the point of the row is what leaves the machine, so that is what was sampled:

```
{"machine_id":"e5c9dc62cbaad51154dd33fd29933c09ffcb2d09756e77cf6e78be3fe20c790c",
 "label":"installer-e5c9dc62cbaad51154dd33fd29933c09"}
```

Two fields, because no `--email` and no `--invite` were passed. The label is the first 32
characters of the same fingerprint, so it carries nothing the first field does not.

### The temp directory: what a real run puts there, and what survives

This is the evidence for the temp-directory rows in §1 and for `verify.html` `#tmp`. It replaces a
`$HOME`-scoped `find` that could not, by construction, have seen any of it. A stub gateway answers
`/auth/register`, `/health`, `/v1/models` and `/v1/messages` so the whole path runs; `$HOME` and
`$TMPDIR` are throwaway directories. Paths shortened to `/home/you` and `/tmp/yb5-audit`; pids and
the `mktemp` suffix collapsed to `$$` / `XXXXXXXX`; nothing else edited.

```
########## A. concurrent watcher over an owned $TMPDIR, one full install ##########
installer exit=0
recorded 11 paths, 39 content snapshots
  tmp.XXXXXXXX/
    tmp.XXXXXXXX/codex.$$
    tmp.XXXXXXXX/credentials.$$
    tmp.XXXXXXXX/curlerr.$$
    tmp.XXXXXXXX/curlrc.$$
    tmp.XXXXXXXX/env.$$
    tmp.XXXXXXXX/install_info.$$
    tmp.XXXXXXXX/probe.json
    tmp.XXXXXXXX/register.json
    tmp.XXXXXXXX/resp.$$
    tmp.XXXXXXXX/write.$$

paths recorded: 11

########## B. which of them held the API key (grep over the byte copies) ##########
$ grep -rl "$KEY" /tmp/yb5-grab | sed 's/\.v[0-9]*$//' | sort -u
  credentials.$$
  curlrc.$$
  resp.$$
  write.$$
matched: 4 of 10

########## C. every chmod the run actually executed (sh -x) ##########
        4 + chmod 600 $TMPD/curlrc.$$
        1 + chmod 600 $TMPD/register.json
        1 + chmod 600 /home/you/.yangble5/INSTALL_INFO
        1 + chmod 600 /home/you/.yangble5/claude/README.txt
        1 + chmod 600 /home/you/.yangble5/codex/config.toml
        1 + chmod 600 /home/you/.yangble5/credentials
        1 + chmod 600 /home/you/.yangble5/env.sh
        1 + chmod 600 /home/you/.yangble5/machine-id
        1 + chmod 700 $TMPD
        6 + chmod 700 /home/you/.yangble5
        5 + chmod 700 /home/you/.yangble5/bin
        1 + chmod 700 /home/you/.yangble5/bin/yangble5-claude
        1 + chmod 700 /home/you/.yangble5/bin/yangble5-codex
        1 + chmod 700 /home/you/.yangble5/bin/yangble5-env
        1 + chmod 700 /home/you/.yangble5/bin/yangble5-uninstall
        2 + chmod 700 /home/you/.yangble5/claude
        2 + chmod 700 /home/you/.yangble5/codex
        1 + chmod 700 /home/you/.yangble5/uninstall.sh

########## D. $TMPDIR and $HOME after that NORMAL exit ##########
$ find $TMPDIR -mindepth 1 | wc -l
0
$ find $HOME -mindepth 1 | sort
  ~/.local/bin/yangble5-claude      ~/.yangble5/INSTALL_INFO
  ~/.local/bin/yangble5-codex       ~/.yangble5/bin/yangble5-claude
  ~/.local/bin/yangble5-env         ~/.yangble5/bin/yangble5-codex
  ~/.local/bin/yangble5-uninstall   ~/.yangble5/bin/yangble5-env
                                    ~/.yangble5/bin/yangble5-uninstall
                                    ~/.yangble5/claude/README.txt
                                    ~/.yangble5/codex/config.toml
                                    ~/.yangble5/credentials
                                    ~/.yangble5/env.sh
                                    ~/.yangble5/machine-id
                                    ~/.yangble5/uninstall.sh

########## E. kill -9 during an in-flight HTTP call ##########
$ find $TMPDIR -mindepth 1 | sort
  /tmp/yb5-audit/tmp.XXXXXXXX
  /tmp/yb5-audit/tmp.XXXXXXXX/curlerr.$$
  /tmp/yb5-audit/tmp.XXXXXXXX/curlrc.$$
  /tmp/yb5-audit/tmp.XXXXXXXX/probe.json
  /tmp/yb5-audit/tmp.XXXXXXXX/register.json
$ grep -o 'x-api-key: yb5_.*' $TMPDIR/tmp.*/curlrc.*
  x-api-key: yb5_0123456789abcdef_TRACK2CA…<the rest of the key>
```

Five things to read out of that, in order of how much they change what the pages may say:

1. **A is the enumeration.** Eleven paths, one of them the directory. Ten files, not two. `D` shows
   the `$HOME` side is exactly what `#files` already listed, so the old row was right about what it
   looked at and wrong about where it looked.
2. **B contradicts the reading of the code.** `curlrc` and `write` are the two you predict from
   reading; `resp` (the register response — the key's *first* landing place on disk) and
   `credentials` are the two you miss. Four, not two. This is why the row is certified by grep over
   captured bytes and not by reading `http_call`.
3. **C is the mode evidence.** Two `chmod 600` inside `$TMPD`, both visible above; the other eight
   files inherit the `umask`. The directory's `700` is the boundary that matters, and the pages now
   say so instead of implying every temp file is `0600`.
4. **D is the trap working.** Zero paths left after a normal exit. `EXIT HUP INT TERM` all reach
   `cleanup()`, so Ctrl-C is covered too.
5. **E is the honest limit.** `kill -9` is not catchable, and what it leaves is not harmless: the
   curl config file holds the key until the call it belongs to returns, which under `kill -9` is
   never. `register.json` and `probe.json` are worse in one narrow sense — they have no per-file
   `rm -f` anywhere, so *only* the trap ever removes them — but neither contains a secret.

**Caveat on the harness, stated because it bounds what E proves.** These runs are on a Windows
checkout under MSYS, with a `uname` shim reporting `Linux/x86_64` so the platform gate (518–527)
takes the Linux branch. Two consequences. Modes cannot be measured there at all — `chmod 600`
followed by `stat` returns `644`, which is why C reads the `chmod` calls from the trace rather than
the filesystem. And MSYS's signal emulation does not run shell traps for `SIGTERM`/`SIGINT`: a
five-line control script with `trap cleanup EXIT HUP INT TERM; sleep 30` also fails to clean up
there, so **no conclusion about `SIGTERM`/`SIGINT` may be drawn from this box** — the D result
(normal `EXIT`) and the E result (`SIGKILL`, uncatchable by definition on every POSIX system) are
the two that carry. Re-run A–E on Linux or macOS before adding any signal claim beyond those.

### The counts behind the eleven-exports claim

Produced by the `YB5_SOURCE_ONLY=1` harness described in the claim-by-claim section above, which
makes the real `write_config` write the real files:

```
$ grep -E "^export " /tmp/fh/.yangble5/env.sh | sed "s/^export //; s/=.*//" | tr " " "\n" | grep -n .
1:YANGBLE5_API
2:YANGBLE5_API_KEY
3:YANGBLE5_MODEL
4:CLAUDE_CONFIG_DIR
5:ANTHROPIC_BASE_URL
6:ANTHROPIC_AUTH_TOKEN
7:ANTHROPIC_MODEL
8:CLAUDE_CODE_MAX_CONTEXT_TOKENS
9:CLAUDE_CODE_MAX_OUTPUT_TOKENS
10:API_TIMEOUT_MS
11:CODEX_HOME

exports total: 11
unsets  total: 1
```

`verify.html #env` claims to print those lines **verbatim**. That claim is itself checked: the
`export`/`unset` lines the page renders were diffed against the same lines in the generated file.

```
$ diff -u real_env_sh_lines.txt rendered_by_verify_html.txt
IDENTICAL — 0 differences

line counts: real=10  rendered=10
```

Normalise line endings before diffing (`tr -d '\r'`). A CRLF checkout makes every line look
changed while the text is identical — a false positive that is easy to mistake for a real drift,
and was hit once while writing this.

The rendered side was also read out of the live DOM (`pre` under `#env`) as well as out of the
file, so the comparison covers HTML escaping, not just content. Ten lines carry eleven names plus
the unset — the first `export` names three variables at once — which is why the page says *eleven
variables*, never *eleven lines*.

### The backup and key-printing behaviour, as actually printed

Second run of `write_config` against the same `$HOME` with a changed key and model, then
`print_backups`; and `print_key_once` at its default `PRINT_KEY=0`. Paths shortened to
`/home/you`; nothing else edited.

```
-- writing configuration
  warn backed up existing /home/you/.yangble5/credentials -> /home/you/.yangble5/credentials.bak-20260721-205151
  ok   wrote /home/you/.yangble5/credentials
       unchanged /home/you/.yangble5/env.sh
  warn backed up existing /home/you/.yangble5/codex/config.toml -> /home/you/.yangble5/codex/config.toml.bak-20260721-205151
  ok   wrote /home/you/.yangble5/codex/config.toml
       unchanged /home/you/.yangble5/claude/README.txt
       unchanged /home/you/.yangble5/bin/yangble5-claude
       unchanged /home/you/.yangble5/bin/yangble5-codex
       unchanged /home/you/.yangble5/bin/yangble5-env
       unchanged /home/you/.yangble5/bin/yangble5-uninstall
       unchanged /home/you/.yangble5/uninstall.sh
  ok   wrote /home/you/.yangble5/INSTALL_INFO
       skipping ~/.local/bin symlinks (--no-bin-link)

  Files replaced this run — each was copied first

      /home/you/.yangble5/credentials.bak-20260721-205151
        restore with:  cp -p "/home/you/.yangble5/credentials.bak-20260721-205151" "/home/you/.yangble5/credentials"
      /home/you/.yangble5/codex/config.toml.bak-20260721-205151
        restore with:  cp -p "/home/you/.yangble5/codex/config.toml.bak-20260721-205151" "/home/you/.yangble5/codex/config.toml"

      Exempt on purpose: ~/.yangble5/INSTALL_INFO is rewritten every run
      and is owned entirely by the installer, so it is not backed up.
      Nothing else is exempt.
```

Note what is *not* in that list: `INSTALL_INFO`, which the same run rewrote. That is the single
deliberate exemption, and the script prints the exemption itself rather than leaving the reader to
notice the gap.

```
  Your yangble5 API key was NOT printed

      It is at /home/you/.yangble5/credentials (mode 0600) and nowhere else.
      Read it yourself when you need it:

          grep '^YANGBLE5_API_KEY=' /home/you/.yangble5/credentials

      The launchers read it from that file, so you never need to paste it
      anywhere. Not printing is the default because this installer is meant to
      be run by an AI agent: printing a secret puts it in that agent's
      transcript and in your shell scrollback. Pass --show-key if you accept
      that and want it on screen anyway.

--- print_backups() with nothing backed up ---
       no existing file was overwritten, so nothing was backed up
```

### CSP hashes after this change

The inline scripts were **not** modified by the transparency rewrite, so both hashes are unchanged
and `deploy/Caddyfile` and `deploy/nginx/yangble5.com.conf.example` need no edit.

**This used to be certified by a check that could not fail.** The recipe printed the recomputed
hash and then ran `grep -c` for a hash *written out in this file's prose* — so if an inline script
had changed, the recompute would print something new while the grep, still carrying the old
literal, reported `1 / 1 / 2` and looked green. The comparison only ever happened in the reader's
head, and the failure it is supposed to catch is invisible until a browser blocks the script.

It is now `sitecheck.csp_problems()`, which runs on every push. The recomputed value *is* the
needle, so nothing is transcribed, and any *other* script hash present in a consumer is reported
as stale — the case a presence-only grep structurally cannot see.

```
$ python tools/sitecheck.py --quiet
index.html: OK
verify.html: OK
CSP hashes: OK
allow-list: OK (20 entries, all matched)
```

Proven to fail, by editing an inline script for real
(`tests/test_sitecheck.py::test_editing_an_inline_script_turns_the_csp_check_red`):

```
deploy/Caddyfile: stale inline-script hash sha256-YhSXRPWEEPURVaJsYXmkYxR+bfYx3vG0Qbm4th+2j8c=
    — no page produces it, so the deployed CSP would block the script it names
deploy/Caddyfile: missing the current index.html inline-script hash
    sha256-GyuNCIQSb5jUyd1Yit6M+UCOS+dADGKCxrmq4iHQF4g= — recompute and update this file
```

Three files are checked: both deploy configs and this one, which carries each hash twice (the
nginx block and the CSP-hashes list). Only the CSP source-expression form — quoted, `'sha256-…='`
— counts as a directive; a bare hash in running prose, such as the self-test output quoted under
Validation above, is text and is ignored. That distinction is itself a self-test case.
