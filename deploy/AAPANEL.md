# Deploying behind aaPanel / BT panel nginx

For a host where a control panel owns nginx, a vhost for your domain already
exists, and its TLS certificate already works — and where **other people's
sites are on the same box**.

This document assumes you have read [`README.md`](README.md) → "Which file do
I use?" and landed on the **behind-proxy** column. If you have not, go back:
starting the standalone stack on this host stops every other site on it.

| | |
|---|---|
| Compose file | [`docker-compose.behind-proxy.yml`](docker-compose.behind-proxy.yml) |
| nginx snippet | [`nginx/yangble5.com.conf.example`](nginx/yangble5.com.conf.example) |
| Ordered procedure | [`GO_LIVE.md`](GO_LIVE.md) → "Path B" |

> **Nothing in this document has been run against a live panel.** The file
> paths below are the standard aaPanel/BT layout. §1 tells you how to confirm
> them on *your* host in one command instead of trusting this page. Do that
> first — a wrong path here means you edit a file nginx never loads, `nginx -t`
> passes, the reload succeeds, and nothing changes.

---

## 0. What you are adding, and what it must not touch

You are adding, to **one** vhost:

- a handful of `location` blocks that reverse-proxy to `127.0.0.1:8081`;
- server-level `proxy_*` and `add_header` defaults;
- a Cloudflare `real_ip` block.

You are **not** adding a `listen`, a `server_name`, a certificate, or a port
binding. The gateway container publishes on loopback only. Nothing about the
other sites' configuration changes, and no new port is opened to the internet.

The blast radius is still real, and it is this: **nginx has one configuration.**
A syntax error in your file makes `nginx -t` fail for the whole box, and if you
reload anyway with a broken config nginx refuses to apply it (the old workers
keep serving — see §5, this is the good case) — but a *semantically* wrong
directive at the wrong scope, such as a `gzip off` or a `client_max_body_size`
that lands in `http` instead of your `server`, applies to all 28 sites at once.

Everything in the snippet is scoped inside one `server { }`. Part 0 of the
snippet is the exception and is clearly marked optional; skip it and lose only
upstream keepalive and nginx-level rate limiting.

---

## 1. Find the files — do not trust the paths, print them

```sh
# Every configuration file nginx actually loads, in load order.
nginx -T 2>/dev/null | grep -n '^# configuration file'

# Narrow it to your domain.
nginx -T 2>/dev/null | grep -n 'configuration file.*yangble5'

# Which binary, and does it have the modules the snippet expects?
nginx -V 2>&1 | tr ' ' '\n' | grep -E 'realip|v2|prefix|conf-path'
```

`--with-http_realip_module` must appear, or Part 1a of the snippet does
nothing and every visitor is logged and rate-limited as a Cloudflare edge.

### The standard aaPanel layout

| Path | What it is | Panel rewrites it? |
|---|---|---|
| `/www/server/nginx/conf/nginx.conf` | main config, holds `http { }` | on panel/nginx upgrade |
| `/www/server/panel/vhost/nginx/<domain>.conf` | the vhost `server { }` | **yes — routinely** |
| `/www/server/panel/vhost/rewrite/<domain>.conf` | the "rewrite / 伪静态" file, `include`d from the vhost | no (it is yours) |
| `/www/server/panel/vhost/nginx/proxy/<domain>/*.conf` | files the panel's *reverse proxy* UI writes | yes, per proxy entry |
| `/www/wwwroot/<domain>/` | the webroot | no |
| `/www/wwwlogs/<domain>.log` | access log | no |

"Panel rewrites it" is the whole problem. The panel regenerates the vhost when
you renew a certificate, change PHP version, toggle a setting, or update the
panel. Anything you typed directly into `<domain>.conf` is gone at that moment,
without warning, and the first symptom is 404 on `/v1/messages` in production.

---

## 2. Where to put the snippet — pick one

### Option A — the rewrite include (recommended, survives panel edits)

The vhost already contains a line like
`include /www/server/panel/vhost/rewrite/yangble5.com.conf;`. Confirm it:

```sh
grep -n 'include' /www/server/panel/vhost/nginx/yangble5.com.conf
```

If it is there, put the snippet's contents in that rewrite file. The panel
treats it as user content, it is editable from **网站 → 设置 → 伪静态**, and it
is preserved when the vhost is regenerated.

```sh
cp -a /www/server/panel/vhost/rewrite/yangble5.com.conf \
      /root/yangble5-backup/rewrite.yangble5.com.conf.$(date +%s)

# then paste PART 1 - PART 3 of nginx/yangble5.com.conf.example into
# /www/server/panel/vhost/rewrite/yangble5.com.conf
```

Caveat, stated because it will bite someone: editing that file from the panel
UI afterwards is fine, but clicking a *preset* in the 伪静态 dropdown replaces
the whole file. Keep the backup.

### Option B — your own file, one `include` line in the vhost

More explicit, and it keeps yangble5's config in a file the panel has never
heard of. The cost is that the one `include` line lives in the vhost, so a
panel regeneration removes *the line* (your file survives untouched).

```sh
mkdir -p /www/server/panel/vhost/nginx/yangble5-extra
# write the snippet to:
#   /www/server/panel/vhost/nginx/yangble5-extra/yangble5-api.conf

# add ONE line inside the server { } of the vhost, before the panel's
# own location blocks:
#   include /www/server/panel/vhost/nginx/yangble5-extra/yangble5-api.conf;
```

Do **not** name the directory `/www/server/panel/vhost/nginx/anything.conf` —
the main config globs `/www/server/panel/vhost/nginx/*.conf`, so a `.conf`
file dropped there is loaded at `http` level as a *separate* config fragment
containing bare `location` blocks, and nginx refuses to start. A subdirectory
is not globbed; that is why Option B uses one.

### Option C — the panel's 反向代理 UI

Workable, and the one most people reach for. Read this before you do:

- It writes `proxy/<domain>/*.conf` and regenerates it whenever you touch that
  proxy entry in the UI, so hand edits are lost on the next save.
- Its generated block sets `proxy_buffering on` and often a `proxy_cache`
  zone. **Both break token streaming**, in the way that produces no error and
  no log line — the agent looks frozen and then dumps everything at once.
- It usually emits `add_header` inside the location, which (see the snippet,
  Part 2j) silently drops every server-level security header on the API paths.

If you use it anyway, diff what it wrote against the snippet before going
live, and re-check after every save.

### Which to pick

Option A. Option B if you want the config version-controlled next to this
repo. Option C only if you are going to audit its output every time.

---

## 3. Keep the checklist honest about ports

```sh
ss -ltnp | grep -E ':(80|443|8081|8318|8320|9000)\b'
```

Expected after `docker compose -f docker-compose.behind-proxy.yml up -d`:

| Port | Bound to | By |
|---|---|---|
| 80, 443 | `0.0.0.0` / `[::]` | **nginx — unchanged, still serving the other sites** |
| 8081 | `127.0.0.1` **only** | docker-proxy → gateway |
| 8318, 8320 | nothing on the host | engine and shim have no published port |

If 8081 shows `0.0.0.0:8081`, stop: someone changed the `ports:` line. A
gateway published on all interfaces is reachable without your TLS, without
your `/v0/*` block, and with `X-Forwarded-For` under the caller's control
while `YANGBLE5_TRUST_PROXY_HEADERS=true` — which is exactly how per-IP limits
get bypassed. Note that `ufw deny 8081` would **not** save you: Docker's
`DOCKER-USER` iptables chain is evaluated before UFW's rules. Binding to
loopback is the control; the firewall is not.

Do not open 8081 in the aaPanel 安全 (firewall) tab. There is nothing to open.

---

## 4. The aaPanel defaults that will fight you

Three of these are in the generated vhost of essentially every aaPanel site.

1. **`location ~ .*\.(js|css)?$ { expires 12h; }`**
   The `?` makes the extension optional, so this regex matches **every** URI.
   Regex locations beat plain prefix locations, so it would serve
   `/v1/messages` from the webroot as a 404. This is why every API block in
   the snippet uses `^~` or `=`, which outrank all regex locations regardless
   of file order. Do not rewrite them as plain prefixes.

2. **`location ~ /\.` → `deny all`**
   Harmless for us and already handled: the snippet's own dotfile rule uses a
   negative lookahead so `/.well-known/acme-challenge/` keeps working and your
   certificate keeps renewing.

3. **The panel's default `client_max_body_size`** (often `50m`, sometimes
   `1m` on tuned hosts) sits at `http` level. The snippet sets `32m` at server
   level, which wins for this vhost. If it is *smaller* somewhere that wins,
   large prompts get a bare nginx 413 HTML page instead of the gateway's JSON
   error, and no usage is recorded. Check with
   `nginx -T | grep -n client_max_body_size`.

4. **The free 网站防火墙 / WAF module**, if enabled, inspects request bodies.
   On a 32 MB prompt that is a real latency cost, and some builds buffer the
   body to do it — which defeats `proxy_request_buffering off`. Test a large
   streaming request with it on; disable it for this vhost if the stream
   buffers.

5. **HTTP/3.** aaPanel may enable it on the listen line. It is unrelated to
   the snippet's `proxy_http_version 1.1`, which governs only the loopback hop
   to the gateway. People conflate the two and "fix" the wrong one.

---

## 5. Test, then reload — never restart

```sh
# 1. Syntax + semantics of the ENTIRE configuration, all 28 sites.
#    This does not touch the running server.
nginx -t
```

**ABORT IF** this prints anything other than `syntax is ok` /
`test is successful`. Fix the file, or restore the backup from §2, and run it
again. Do not proceed on a warning you have not read.

```sh
# 2. Graceful reload.
nginx -t && nginx -s reload
#   or, on aaPanel:
# nginx -t && /etc/init.d/nginx reload
```

Why `reload` and not `restart`, in one paragraph, because this is the step
that decides whether 27 other sites notice you:

> `reload` sends `SIGHUP`. The master process re-reads the config, starts new
> workers with it, and tells the old workers to stop accepting new connections
> and finish the ones they already have. Listening sockets are never closed.
> No connection is dropped, and if the new config fails to load the master
> keeps the old workers running. `restart` stops the master, closes the
> listening sockets, and starts again — every in-flight request dies and the
> box refuses connections for as long as the start takes. There is no upside.
> Never click the panel's **重启** button on a shared host; **重载** is the
> other one.

```sh
# 3. Confirm the reload actually took, and that nothing else broke.
systemctl status nginx --no-pager | head -5      # or: /etc/init.d/nginx status
nginx -T | grep -c 'server_name'                 # same count as before

# Two of the OTHER sites still answer:
curl -sS -o /dev/null -w '%{http_code}\n' https://<other-site-1>/
curl -sS -o /dev/null -w '%{http_code}\n' https://<other-site-2>/

tail -50 /www/wwwlogs/nginx_error.log
```

**ABORT IF** either other site changed its status code. Roll back (§6) before
you debug — the other sites are not yours to experiment on.

---

## 6. Rollback

Two commands, and they work whether or not the gateway is running:

```sh
cp -a /root/yangble5-backup/rewrite.yangble5.com.conf.<stamp> \
      /www/server/panel/vhost/rewrite/yangble5.com.conf
nginx -t && nginx -s reload
```

Option B: delete the `include` line from the vhost instead, then the same
`nginx -t && nginx -s reload`.

That restores the placeholder site and removes the proxy. The gateway keeps
running on loopback, unreachable from outside, with its database intact. To
stop it too:

```sh
cd /opt/yangble5/app/deploy
docker compose -f docker-compose.behind-proxy.yml down
```

Volumes, `.env`, the SQLite database and the engine's OAuth tokens all survive
a `down`. Nothing here is destructive.

**The kill switch** — if the API is live and misbehaving and you want it gone
in one second without touching nginx:

```sh
docker stop yangble5-gateway
```

nginx then returns 502 on the API paths and keeps serving the static site and
all 27 other sites normally.

---

## 7. After a panel update, or a certificate renewal

The panel regenerates the vhost. Check, do not assume:

```sh
# Is your config still loaded?
nginx -T | grep -c 'proxy_pass http://127.0.0.1:8081'    # expect 8, as shipped

# Is the API still reachable from outside? (run from your laptop)
curl -sS -o /dev/null -w '%{http_code}\n' https://yangble5.com/health
```

If the count is 0, the `include` line was dropped (Option B) or the rewrite
file was replaced (Option A). Restore from your backup, `nginx -t`, reload.

Worth automating as a cron check that emails you, because the failure is
silent and the gap between "panel updated itself at 04:00" and "a user tells
you the API is down" is otherwise however long it takes someone to complain.

---

## 8. What is still not covered here

- **Certificates.** Yours already work and this document never touches them.
  If you later move the API to its own hostname, issue that certificate
  through the panel as usual before adding the server block.
- **fail2ban.** `harden.sh` installs a jail that reads *Caddy's* JSON access
  log. There is no Caddy in this deployment. The nginx equivalent needs a
  different filter regex against `/www/wwwlogs/yangble5.com.log`, and it is
  not written yet — until it is, treat the gateway's own
  `YANGBLE5_AUTH_FAIL_LOCKOUT_*` settings and Cloudflare as your brute-force
  defence, and say so honestly in your own notes.
- **UFW.** `harden.sh` assumes it owns the firewall. On an aaPanel box the
  panel manages firewall rules too, and running both is a good way to lock
  yourself out of SSH. Read `harden.sh` before running it on this host, or
  skip it and use the panel's 安全 tab — but do not run both blind.
