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
L = pathlib.Path("site/install.sh").read_text(encoding="utf-8", errors="replace").splitlines()
# (line cited in the table, a pattern that line must still match)
A = [(132,r'^YB5_HOME='),(151,r'^PRINT_KEY=0'),(127,r'^EX_VERIFY=8'),
     (274,r'^sanitize_remote\(\)'),(295,r'^print_remote\(\)'),
     (380,r'--no-bin-link\)'),(381,r'--show-key\)'),(404,r'^refuse_root\(\)'),
     (556,r'^timestamp\(\)'),(564,r'chmod 700 "\$1"'),
     (591,r'if \[ -f "\$wf_dest" \]'),(597,r'wf_nobak.*!= "nobackup"'),
     (621,r'^ensure_machine_salt\(\)'),(636,r'> "\$ems_file"'),
     (656,r'^http_call\(\)'),(666,r'chmod 600 "\$hc_cfg"'),(694,r'curl --config'),
     (749,r'^CRED_FILE='),(867,r'^\s+reg_body='),(972,r'ensure_dir "\$YB5_HOME"'),
     (1019,r'write_file "\$CRED_FILE" 600'),(1039,r'yb5_load_credentials\(\)'),
     (1102,r'^export YANGBLE5_API YANGBLE5_API_KEY YANGBLE5_MODEL'),
     (1108,r'^export CLAUDE_CONFIG_DIR'),(1125,r'^unset ANTHROPIC_API_KEY'),
     (1128,r'^export CODEX_HOME'),(1131,r'write_file "\$\{YB5_HOME\}/env\.sh" 600'),
     (1156,r'codex/config\.toml" 600'),(1230,r'INSTALL_INFO" 600 nobackup'),
     (1250,r'for ll_name in yangble5-claude'),(1265,r'>> ~/\.profile'),
     (1405,r'http_call GET /health'),(1452,r'"max_tokens":16'),(1467,r'COLD request'),
     (1489,r'PRINT_KEY" -ne 1'),(1529,r'^print_backups\(\)'),
     (1540,r'Exempt on purpose'),(1623,r'verification FAILED')]
bad = [(n, rx, (L[n-1] if 0 < n <= len(L) else "")[:64])
       for n, rx in A if not re.search(rx, L[n-1] if 0 < n <= len(L) else "")]
print(f"anchors: {len(A)}  mismatches: {len(bad)}")
for b in bad: print("  MISMATCH", b)
PY
```

Last run: **`anchors: 38  mismatches: 0`**. If it reports mismatches, the numbers moved — re-derive
them from the diff and update this table. **The claims do not expire when line numbers do.** Only
change the prose if the *behaviour* changed; a moved line is a bookkeeping fix, a changed behaviour
is a page edit.

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

### 1. Isolated directories — no shell profile, no PATH

| Sentence on the page | Implemented by | How it was verified |
|---|---|---|
| Creates `~/.yangble5/` plus `claude/`, `codex/`, `bin/`, all mode `700` | `write_config` → four `ensure_dir` calls (972–975); `ensure_dir` does `mkdir -p` + `chmod 700` (563–564) | ran `write_config`; the four directories appear and nothing else does |
| Four symlinks in `~/.local/bin` | `link_launchers` loop over exactly four names (1250–1257); `YB5_LINK_DIR` defined 134 | source read; the loop names are the only four |
| `--no-bin-link` turns that off | flag 380; guard 1237 | ran with `LINK_BIN=0` → `skipping ~/.local/bin symlinks (--no-bin-link)` |
| A same-named **non**-symlink there is left alone with a warning | 1252–1255 (`[ -e ] && [ ! -L ]` → `warn` + `continue`) | source read |
| Writes nowhere except those two locations | every write goes through `write_file` (10 call sites: 1019, 1131, 1156, 1160, 1169, 1184, 1198, 1230, 1272, 1286) or `ensure_machine_salt` (636); all target `$YB5_HOME` (132) or `$YB5_BIN` (133) | enumerated the call sites; `find` over the throwaway `$HOME` after a run shows nothing outside `.yangble5` |
| Does not look for, read or modify `.bashrc` / `.zshrc` / `.profile` | **absence.** `grep -nE 'bashrc\|zshrc\|\.profile' site/install.sh` → 3 hits, all of them text: header comment 30, banner 455, and the `info` at 1265 that *prints* a suggested line | grep output pasted under Validation below |
| Does not change `PATH`; only prints the line to add | `link_launchers` 1260 reads `":${PATH}:"`; 1263–1266 `warn`/`info` only | source read — there is no assignment to `PATH` anywhere in the file |
| Windows only touches the **user** PATH, and only with `-AddToPath` | `install.ps1` `Add-Yb5ToPath` 1198–1229: the no-flag branch 1199–1211 only reads and advises; the single write is 1226, scope `'User'` | source read |

### 2. The key is not printed

| Sentence on the page | Implemented by | How it was verified |
|---|---|---|
| **(hero `cmd-foot`)** The installer does not print the API key by default | `PRINT_KEY=0` (151); `print_key_once` 1489–1506 takes the "NOT printed" branch | ran `print_key_once` — real output pasted under Validation |
| Key is written to `~/.yangble5/credentials`, mode `0600` | `CRED_FILE` 749; `write_file "$CRED_FILE" 600` (1019) | ran `write_config`; the file contains the four `YANGBLE5_*` lines and nothing else |
| What is printed is the path, not the key | 1494–1497, which also prints the `grep '^YANGBLE5_API_KEY=' …` line | in the captured output |
| `--show-key` / `-ShowKey` opts back in, with a warning about the agent transcript | 381 / 1509–1523; `install.ps1` 129, 1437, 1456–1466 | source read |
| The key never appears in `argv`; curl reads it from a `0600` config file | `http_call` 656–691: `chmod 600 "$hc_cfg"` (666), headers written into the file (682–683), `curl --config "$hc_cfg"` (694) | source read — the key is never an argument to any command |

### 3. Eleven exports and one unset

| Sentence on the page | Implemented by | How it was verified |
|---|---|---|
| `~/.yangble5/env.sh` exports **eleven** variables and unsets one | generated 1028–1132: 1102 (3 names on one line), 1108–1111 (4), 1119–1121 (3), 1128 (1) = 11; `unset` at 1125 | generated the real file and counted: **`exports total: 11`, `unsets total: 1`** (output under Validation) |
| The eleven break down 3 `YANGBLE5_*` / 4 Claude Code / 3 numeric / 1 `CODEX_HOME` | same lines, in that order | the ordered name list is pasted under Validation |
| Values live only in the launcher's process | launchers source `env.sh` then `exec` (1175+1181, 1190+1195); they are never appended to any rc file | source read |
| `ANTHROPIC_API_KEY` is unset because it outranks `ANTHROPIC_AUTH_TOKEN` | 1123–1125 | source read |
| Windows uses the same names split across two `.cmd` launchers under `setlocal` | `install.ps1` 1069 (`setlocal`), 1131–1144 (claude), 1158 (codex), 1144 clears `ANTHROPIC_API_KEY` | source read |
| **(verify.html)** `YANGBLE5_KEY_ID` is assigned but not exported, so it is not one of the eleven | assigned 1058, absent from the `export` at 1102 | in the generated file: it appears in `yb5_load_credentials`, not in any `export` line |
| **(verify.html)** `credentials` is parsed as `KEY=VALUE`, never sourced | `yb5_load_credentials` 1039–1062 (`while IFS= read -r`) | in the generated file |
| **(verify.html)** three of those values are re-checked against the same allow-lists, exit `6` on failure | 1068–1100: `YANGBLE5_API` (twice), `YANGBLE5_MODEL`, `YANGBLE5_API_KEY` (twice) — `YANGBLE5_KEY_ID` is **not** re-checked | counted in the generated file. The page said "four" during drafting and was corrected to "three" by this row |

### 4. Its own Codex config — not yours

| Sentence on the page | Implemented by | How it was verified |
|---|---|---|
| Writes `~/.yangble5/codex/config.toml` (mode `600`) | 1135–1156 | generated file inspected |
| Sets `model_provider`, `base_url`, context/output ceilings, `env_key` | 1143, 1150, 1147, 1148, 1152 | all five appear in the generated TOML |
| Points Codex at it with `CODEX_HOME` | 1128 | in the generated `env.sh` |
| **Does not open, read or modify `~/.codex/config.toml`** | **absence.** `grep -nE '~/\.codex\|~/\.claude' site/install.sh` → 6 hits, all of them prose in comments or printed text (25, 452, 1107, 1139, 1164, 1320). No file operation names either path | grep output pasted under Validation |
| Plain `claude` keeps your login because `CLAUDE_CONFIG_DIR` is separate | 1108, plus the `claude/README.txt` marker 1160–1166 | generated file inspected |

### 5. Backups, and the one deliberate exemption

| Sentence on the page | Implemented by | How it was verified |
|---|---|---|
| Existing file with different content → `cp -p` to `<file>.bak-<timestamp>` | `write_file` 591–603; `timestamp()` 556 (`date +%Y%m%d-%H%M%S`) | ran `write_config` twice with changed values; two real `.bak-…` files were produced |
| Identical content → prints `unchanged`, no backup | 592–595 | in the captured second-run output |
| Every backup is printed at the end with the exact restoring command | `print_backups` 1529–1543, called from `next_steps` 1548 | real output pasted under Validation — one `restore with: cp -p "…" "…"` line per backup |
| Nothing backed up → says so instead of staying silent | 1530–1533 | ran `print_backups` with `BACKUPS=""` → `no existing file was overwritten, so nothing was backed up` |
| `INSTALL_INFO` is the **only** file exempt from backup, and the script says so | `write_file`'s third argument (571–572, 597); `grep -c 'nobackup'` on the call sites → the single site is 1230; the exemption text is printed at 1540–1542 | the second run rewrote `INSTALL_INFO` and it is **absent** from the printed backup list, with the exemption paragraph printed underneath |
| `machine-id` is created once and never overwritten, so it is never a backup candidate | `ensure_machine_salt` 621–638 returns early at 624–628 when the file exists | source read |
| Windows prints `Copy-Item -LiteralPath … -Destination … -Force` | `install.ps1` `Show-Backups` 1472–1486 | source read |

### 6. One real call, honestly reported

| Sentence on the page | Implemented by | How it was verified |
|---|---|---|
| `GET /health` → `GET /v1/models` → `POST /v1/messages` with `max_tokens` 16 | `verify()` 1405, 1430, 1451–1456 (`"max_tokens":16` at 1452) | source read |
| On success it prints the status and time **and says the call was cold, 0%** | 1462–1470; the cold-cache disclosure is 1467–1468 | source read |
| On failure it does not call it a success; exit code 8 | 1473–1478; `EX_VERIFY=8` (127); `main` 1623–1626 | source read |
| Server text is stripped of ANSI/control bytes, flattened, capped, prefixed `server says>` | `sanitize_remote` 274–292, `print_remote` 295–301 | unit-tested by `tests/test_installer_validation.py` against this same file |

### The "never" list

| Sentence on the page | Implemented by | How it was verified |
|---|---|---|
| Refuses to run as root or under `sudo`, exit 2 | `refuse_root` 404–425 (`id -u` = 0 or `$SUDO_USER` set) | source read |
| No background service, autostart or daemon | **absence.** `grep -nE 'systemd\|launchd\|launchctl\|crontab' site/install.sh` → no matches | grep output pasted under Validation |
| Downloads and executes no extra code; no `eval` | **absence.** `grep -nE '\beval\b' site/install.sh` → 2 hits, both comments (70, 707). The only network calls are `http_call`, whose URL is always `$YB5_API` + a fixed path | grep output pasted under Validation |
| Registration sends only the fingerprint, a label made of its first 32 chars, and any e-mail / invite you passed | `reg_body` 867–878 — a four-field JSON body, two of them optional | source read; the body is built with `printf`, field by field |
| Does not touch `~/.ssh`, browser data, keychains | follows from the two-destination row in §1 | — |

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

There is no test runner for static files. The check below is the whole of it: stdlib
`html.parser` only, no dependencies, run from the repo root. It verifies tag balance, duplicate
ids, resolution of every in-page anchor / `aria-controls` / `aria-labelledby` / `data-copy-*` /
`getElementById` target, absence of external subresources and inline event handlers, exactly one
`<style>` and one `<script>` per page, `<html lang>`, `<button type>`, and that every
3-or-more-digit figure rendered as page text is either in the authoritative measurement record —
recomputed here, including every derived total the results table prints — or in an explicit
allow-list where each entry carries its reason. It exits non-zero on any finding.

```python
# save as sitecheck.py at the repo root, then:  python sitecheck.py
import pathlib, re, sys
from html.parser import HTMLParser

SITE = pathlib.Path("site")
FILES = ("index.html", "verify.html")

VOID = {"area","base","br","col","embed","hr","img","input","link","meta",
        "param","source","track","wbr"}

# ── the authoritative measurement record (the ONLY permitted measurements) ──
PROMPT = [748918, 748933, 748948, 748963]          # rounds 1-4
CACHED = [0, 745438, 745430, 745422]               # rounds 1-4
ROUND_MS = [21410, 10753, 23457, 22381]            # rounds 1-4

MEASURED = {str(n) for n in PROMPT + CACHED + ROUND_MS}
# every derived total the page prints, recomputed here rather than trusted
_warm_p = sum(PROMPT[1:]);  _warm_c = sum(CACHED[1:])
_all_p  = sum(PROMPT);      _all_c  = sum(CACHED)
for n in (_warm_p, _warm_c, _warm_p - _warm_c,
          _all_p,  _all_c,  _all_p - _all_c,
          *[PROMPT[i] - CACHED[i] for i in range(4)]):
    MEASURED.add(str(n))
MEASURED |= {
    "9953", "000", "7460",                         # 99.53%, 0.00%, 74.6%
    "749",                                         # ~749K prefix
    "2026",                                        # measurement date 2026-07-21
    "7123",                                        # CLIProxyAPI 7.1.23
    "7293",                                        # engine >= 7.2.93
}

# ── non-measurement numerals that legitimately appear as page text ──────────
NON_MEASUREMENT = {
    "1000000": "CLAUDE_CODE_MAX_CONTEXT_TOKENS / model_context_window default written by the installer",
    "65536":   "CLAUDE_CODE_MAX_OUTPUT_TOKENS default written by the installer",
    "600000":  "API_TIMEOUT_MS default written by the installer",
    "1102":    "install.sh line reference (start of the env.sh export block)",
    "1128":    "install.sh line reference (end of the env.sh export block)",
    "0600":    "file mode",
    "0700":    "file mode",
    "700":     "directory mode",
    "600":     "file mode",
    "256":     "SHA256 / sha256",
    "200":     "HTTP 200",
    "400":     "HTTP 400",
    "401":     "HTTP 401",
    "402":     "HTTP 402",
    "403":     "HTTP 403",
    "404":     "HTTP 404",
    "409":     "HTTP 409",
    "429":     "HTTP 429",
    "501":     "HTTP 501",
    "502":     "HTTP 502",
    "503":     "HTTP 503",
    "8320":    "local gateway port in the BYOK example",
    "8318":    "local engine port",
    "8319":    "stats sidecar port",
    "127":     "127.0.0.1",
    "2193":    "Claude Code v2.1.193 (env var availability)",
    "193":     "Claude Code v2.1.193 (env var availability)",
    "443":     "TLS port",
    "360":     "360px viewport note",
    "2023":    "anthropic-version: 2023-06-01",
    "2024":    "quoted wrong answer from the Gemini upstream (no live web search)",
    "2025":    "quoted wrong answer from the Grok upstream (no live web search)",
}

class Doc(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.errors = [], []
        self.ids, self.dupes = set(), []
        self.refs = []          # (kind, target)
        self.external = []
        self.inline_handlers = []
        self.styles = self.scripts = 0
        self.lang = None
        self.buttons_no_type = 0
        self.text = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html":
            self.lang = a.get("lang")
        if tag == "style":
            self.styles += 1
        if tag == "script":
            self.scripts += 1
            if a.get("src"):
                self.external.append(f"script src={a['src']}")
        if tag in ("style", "script"):
            self._skip += 1
        if tag == "button" and "type" not in a:
            self.buttons_no_type += 1
        if a.get("id"):
            if a["id"] in self.ids:
                self.dupes.append(a["id"])
            self.ids.add(a["id"])
        for k, v in a.items():
            if k.startswith("on"):
                self.inline_handlers.append(f"<{tag} {k}=>")
        for k in ("aria-controls", "aria-labelledby", "data-copy-target",
                  "data-copy-status"):
            if a.get(k):
                for tok in a[k].split():
                    self.refs.append((k, tok))
        href = a.get("href", "")
        if href.startswith("#") and len(href) > 1:
            self.refs.append(("href", href[1:]))
        # only *subresources* count: a canonical/alternate <link> and ordinary
        # <a href> are navigations, not fetches the browser makes for us.
        subresource = tag in ("img", "script", "iframe", "source", "video", "audio") or (
            tag == "link" and a.get("rel", "").lower() not in ("canonical", "alternate", "author", "license"))
        for k in ("src", "href"):
            v = a.get(k, "")
            if subresource and re.match(r"^(https?:)?//", v):
                self.external.append(f"<{tag} {k}={v}>")
        if tag not in VOID:
            self.stack.append((tag, self.getpos()))

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if tag in ("style", "script"):
            self._skip -= 1
        if not self.stack:
            self.errors.append(f"line {self.getpos()[0]}: stray </{tag}>")
            return
        if self.stack[-1][0] == tag:
            self.stack.pop()
        else:
            t, p = self.stack[-1]
            self.errors.append(f"line {self.getpos()[0]}: </{tag}> closes <{t}> opened line {p[0]}")

    def handle_data(self, data):
        if self._skip == 0:
            self.text.append(data)

def check(fname):
    src = (SITE / fname).read_text(encoding="utf-8")
    d = Doc(); d.feed(src); d.close()
    problems = []

    problems += d.errors
    problems += [f"<{t}> never closed (line {p[0]})" for t, p in d.stack]
    problems += [f"duplicate id: {i}" for i in d.dupes]

    for kind, target in d.refs:
        if target not in d.ids:
            problems.append(f"{kind}=\"{target}\" has no matching id")

    for m in re.finditer(r'getElementById\(\s*"([^"]+)"\s*\)', src):
        if m.group(1) not in d.ids:
            problems.append(f'getElementById("{m.group(1)}") has no matching id')

    problems += [f"external subresource: {e}" for e in d.external]
    problems += [f"inline event handler: {h}" for h in d.inline_handlers]
    for m in re.finditer(r"@import|url\(\s*['\"]?https?:", src):
        problems.append(f"external CSS reference: {m.group(0)}")

    if d.styles != 1:
        problems.append(f"expected exactly 1 <style>, found {d.styles}")
    if d.scripts != 1:
        problems.append(f"expected exactly 1 <script>, found {d.scripts}")
    if not d.lang:
        problems.append("<html> has no lang attribute")
    if d.buttons_no_type:
        problems.append(f"{d.buttons_no_type} <button> without type=")

    text = "".join(d.text)
    unknown = {}
    # A "figure" is a standalone numeral: not glued to a letter (i5-11400H,
    # shark0120, sha256 are identifiers, not figures) and thousands separators
    # are part of the number, not a boundary.
    NUM = re.compile(r"(?<![0-9A-Za-z_.])\d{1,3}(?:,\d{3})+(?![0-9A-Za-z_])"
                     r"|(?<![0-9A-Za-z_.])\d+(?![0-9A-Za-z_])")
    for m in NUM.finditer(text):
        raw = m.group(0)
        part = raw.replace(",", "")
        if len(part) < 3:
            continue
        bare = part.lstrip("0") or part
        if part in MEASURED or bare in MEASURED:
            continue
        if part in NON_MEASUREMENT or bare in NON_MEASUREMENT:
            continue
        unknown.setdefault(part, raw)
    for n, ctx in sorted(unknown.items()):
        problems.append(f"unaccounted 3+ digit figure: {n}  (as written: {ctx})")

    return problems

rc = 0
for f in FILES:
    p = check(f)
    if p:
        rc = 1
        print(f"{f}: {len(p)} PROBLEM(S)")
        for x in p:
            print(f"    - {x}")
    else:
        print(f"{f}: OK")
sys.exit(rc)
```

Real output, current `site/` contents:

```
$ python sitecheck.py
index.html: OK
verify.html: OK
exit=0
```

Two things that check cannot see, and how they were covered:

- **Contrast** was re-measured in a real browser after this change, over every element that owns a
  visible text node, in both themes, compositing alpha backgrounds down the ancestor chain (an
  audit that ignores alpha reports false failures on `color-mix(… / .1)` pills and false passes on
  accent buttons — both were observed while writing this):

  | Page | Theme | Text-bearing elements | Below AA (4.5:1) | Worst pair |
  |---|---|---|---|---|
  | `index.html` | dark | 490 | **0** | 6.16:1 (results-table `<th>`) |
  | `index.html` | light | 490 | **0** | 5.40:1 (results-table `<th>`) |
  | `verify.html` | dark | 174 | **0** | 6.61:1 (`<h3>` 對不上怎麼辦) |
  | `verify.html` | light | 174 | **0** | 5.53:1 (GitHub issues link) |

  Reload the page after switching the emulated colour scheme before trusting any number: a
  scheme change without a reload leaves part of the tree styled from the previous theme's custom
  properties, and produces convincing-looking failures that vanish on reload.

- **Behaviour** was verified in a browser against a mock gateway covering all three status states
  (healthy-without-capacity, exhausted, capacity-present), both copy paths (success and
  clipboard-denied fallback), keyboard tab navigation, and OS auto-detection. At a 360 px
  viewport, both pages report `documentElement.scrollWidth === clientWidth === 360` — **no
  horizontal page scroll** — with every overflowing element contained inside a `pre` or
  `.tablewrap` scroller. The two `<pre>` blocks added to `verify.html` for the verbatim `env.sh`
  listing and the backup-output shape carry `tabindex="0"` + `role="region"` + `aria-label`,
  matching how `index.html` exposes its scrollable tables, so the horizontal scroll they need at
  narrow widths is reachable from the keyboard. Console is clean on both pages.

### The greps behind the four absence claims

```
$ grep -nE "bashrc|zshrc|\.profile" site/install.sh
30:#   * It does NOT edit .bashrc, .zshrc, .profile or your PATH.
455:    - modify your PATH, .bashrc, .zshrc or .profile
1265:            info "    echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.profile"

$ grep -nE "~/\.codex|~/\.claude" site/install.sh
25:#     your ~/.claude directory and your existing subscription are untouched.
452:    - touch your existing Claude Code login or ~/.claude (a separate
1107:# damage the credentials in ~/.claude.
1139:# Your normal ~/.codex is untouched.
1164:here is separate from your real ~/.claude. Deleting this directory logs out
1320:printf '\nIt will NOT touch ~/.claude, ~/.codex, your shell rc files, or anything\n'

$ grep -nE "systemd|launchd|launchctl|crontab" site/install.sh
(no matches)

$ grep -nE "\beval\b" site/install.sh
70:#   * No `eval`, and nothing the server sends is ever executed. The API key is
707:# Deliberately dumb: no eval, no shell expansion of server data, and every
```

Every hit is a comment or a string that gets *printed*. `install.sh:1265` is the `info` line that
shows the user the `>> ~/.profile` command to run themselves; it is not executed. There is no
`systemd` / `launchd` / `launchctl` / `crontab` match at all, and both `eval` matches are prose in
comments.

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
and `deploy/Caddyfile` and `deploy/nginx/yangble5.com.conf.example` need no edit. Verified rather
than assumed:

```
$ python recompute-csp.py   # the parser-based snippet above
index.html     'sha256-YhSXRPWEEPURVaJsYXmkYxR+bfYx3vG0Qbm4th+2j8c='
verify.html    'sha256-4FFG4w4T/7cQdRclDwWnwwb3pZxhyUhWrDX0fSl2niI='

$ grep -c "sha256-YhSXRPWEEPURVaJsYXmkYxR+bfYx3vG0Qbm4th+2j8c=" deploy/Caddyfile deploy/nginx/yangble5.com.conf.example site/README.md
deploy/Caddyfile:1
deploy/nginx/yangble5.com.conf.example:1
site/README.md:2
$ grep -c "sha256-4FFG4w4T/7cQdRclDwWnwwb3pZxhyUhWrDX0fSl2niI=" deploy/Caddyfile deploy/nginx/yangble5.com.conf.example site/README.md
deploy/Caddyfile:1
deploy/nginx/yangble5.com.conf.example:1
site/README.md:2
```

`site/README.md:2` is expected: this file carries each hash twice, once in the nginx block and
once in the CSP-hashes list. All three files agree, which is the property that matters.
