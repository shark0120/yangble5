# yangble5 operations runbook

Everything here assumes:

```sh
cd /opt/yangble5/app/deploy      # or wherever you passed --prefix
```

**Why every database command goes through Python.** The gateway image is
`python:3.12-slim`, which has no `sqlite3` command-line tool. It does have
Python's `sqlite3` module and the `gateway.storage` package, which is a better
interface anyway: `Storage` opens the database with the same pragmas the
application uses, and its methods enforce invariants that hand-written SQL
would let you break.

Read-only inspection can use raw SQL. Anything that writes should go through
`Storage`.

---

## Contents

1. [Daily / weekly checks](#1-daily--weekly-checks)
2. [Check spend](#2-check-spend)
3. [Invite codes and issuing keys](#3-invite-codes-and-issuing-keys)
4. [Suspend an abusive user](#4-suspend-an-abusive-user)
5. [Rotate the engine key](#5-rotate-the-engine-key)
6. [Rotate a user key](#6-rotate-a-user-key)
7. [Rotate the key pepper (break-glass)](#7-rotate-the-key-pepper-break-glass)
8. [Back up the database](#8-back-up-the-database)
9. [Restore](#9-restore)
10. [Upgrade the engine, and retire the shim](#10-upgrade-the-engine-and-retire-the-shim)
11. [Upgrade the stack](#11-upgrade-the-stack)
12. [When the global budget cap trips](#12-when-the-global-budget-cap-trips)
13. [Incidents](#13-incidents)
14. [Where things are](#14-where-things-are)

---

## 1. Daily / weekly checks

```sh
docker compose ps                      # everything Up and healthy?
docker compose logs --tail=100 gateway
docker compose logs --tail=50 caddy
df -h /                                # logs and images fill disks
sudo fail2ban-client status            # jails alive; any bans?
```

Certificate expiry — Caddy renews automatically, so this is a "did automation
work" check, not a task:

```sh
echo | openssl s_client -servername "$YANGBLE5_DOMAIN" \
        -connect "$YANGBLE5_DOMAIN:443" 2>/dev/null \
  | openssl x509 -noout -dates
```

Behind Cloudflare this shows *Cloudflare's* edge certificate, not yours. To
check the origin's own certificate you have to bypass the proxy:

```sh
echo | openssl s_client -servername "$YANGBLE5_DOMAIN" \
        -connect <origin-ip>:443 2>/dev/null | openssl x509 -noout -dates
```

Weekly: skim the top spenders (§2) and confirm the monthly total is tracking
where you expect against your cap.

---

## 2. Check spend

> **The `cost_usd` column is only as good as your price table.** The gateway
> ships a **placeholder** price table, so until you point
> `YANGBLE5_PRICE_TABLE_FILE` at real per-model rates, `cost_usd` is an
> internal accounting unit for enforcing *relative* budgets — not money. The
> authoritative number is always your upstream provider's billing console.
> Token counts, by contrast, are counted from the responses and are real.

### This month, whole deployment

```sh
docker compose exec -T gateway python - <<'PY'
import os, sqlite3
db = os.environ.get("YANGBLE5_DB_PATH", "/data/yangble5.db")
c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
row = c.execute("""
    SELECT month,
           COUNT(*)                  AS requests,
           SUM(total_tokens)         AS tokens,
           SUM(cached_input_tokens)  AS cached_in,
           SUM(input_tokens)         AS input_in,
           ROUND(SUM(cost_usd), 4)   AS cost_usd
    FROM usage_records
    WHERE month = strftime('%Y-%m', 'now')
    GROUP BY month
""").fetchone()
if not row:
    print("no usage recorded this month")
else:
    print(dict(row))
    total_in = (row["input_in"] or 0) + (row["cached_in"] or 0)
    if total_in:
        # Token-weighted, and it includes cold first requests -- so it will sit
        # below the 99.53% figure measured on warm rounds only.
        print(f"cache read share of input tokens: {row['cached_in']/total_in:.2%}")
PY
```

### Top spenders this month

```sh
docker compose exec -T gateway python - <<'PY'
import os, sqlite3
db = os.environ.get("YANGBLE5_DB_PATH", "/data/yangble5.db")
c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
for r in c.execute("""
    SELECT u.key_id, k.status, us.email,
           COUNT(*) AS reqs,
           SUM(u.total_tokens) AS tokens,
           ROUND(SUM(u.cost_usd), 4) AS cost_usd
    FROM usage_records u
    JOIN api_keys k ON k.key_id = u.key_id
    JOIN users   us ON us.id    = k.user_id
    WHERE u.month = strftime('%Y-%m', 'now')
    GROUP BY u.key_id
    ORDER BY SUM(u.cost_usd) DESC
    LIMIT 20
"""):
    print(r)
PY
```

### One key, today

```sh
docker compose exec -T gateway python - <<'PY'
import os
from gateway.storage import Storage
KEY_ID = "yb5_xxxxxxxx"          # <-- edit
s = Storage(os.environ.get("YANGBLE5_DB_PATH", "/data/yangble5.db"))
print("today:      ", s.usage_for_day(KEY_ID))
print("this month: ", s.usage_for_month(KEY_ID))
s.close()
PY
```

### Upstream's own view

The engine keeps a short usage queue of its own. It is **consume-on-read**:
whoever reads it first gets the records and every other reader gets nothing.
`tools/cache_stats_sidecar.py` is designed to be that single consumer and to
fold the records into a durable `stats.json`. If you query the queue by hand
while the sidecar is running, you are stealing its data.

---

## 3. Invite codes and issuing keys

### The admin key

`YANGBLE5_ADMIN_API_KEY` authenticates you to the gateway's `/admin/*`
endpoints. **In `invite` registration mode it is not optional**: without it the
gateway disables `/admin/invites`, no invite code can be minted, and therefore
nobody can register. The gateway says exactly that in its startup warnings —
worth reading them after any config change:

```sh
docker compose logs gateway | grep -i warn
```

`install.sh` generates the key and prints it once. To read it back:

```sh
sudo grep '^YANGBLE5_ADMIN_API_KEY=' .env
```

It is an operator credential. Never issue it to a user, and never send it to
the gateway from a client machine you do not control.

### Mint an invite code (HTTP)

```sh
ADMIN_KEY="$(sudo sed -n 's/^YANGBLE5_ADMIN_API_KEY=//p' .env)"

curl -sS -X POST "https://${YANGBLE5_DOMAIN}/admin/invites" \
     -H "Authorization: Bearer ${ADMIN_KEY}" \
     -H 'Content-Type: application/json' \
     -d '{"label":"friend-of-a-friend","max_uses":1}'

unset ADMIN_KEY
```

Check the exact request and response shape against `gateway/app.py` for the
version you are running — the admin surface is the newest part of this stack
and the one most likely to have moved.

### Mint an invite code (direct, always works)

```sh
docker compose exec -T gateway python - <<'PY'
import os, secrets
from gateway.storage import Storage

code = "yb5-" + secrets.token_hex(8)          # 64 bits, single use
s = Storage(os.environ.get("YANGBLE5_DB_PATH", "/data/yangble5.db"))
s.create_invite(code, label="friend-of-a-friend", max_uses=1)
s.close()
print("invite code:", code)
PY
```

Only a hash of the code is stored, so this output is the only time you will
see it. Send it over something you trust.

### Issue a key directly (no invite round-trip)

```sh
docker compose exec -T gateway python - <<'PY'
import os
from gateway.storage import Storage

s = Storage(os.environ.get("YANGBLE5_DB_PATH", "/data/yangble5.db"))
issued = s.issue_key(
    email="user@example.com",
    label="early tester",
    pepper=os.environ.get("YANGBLE5_KEY_PEPPER", ""),
    daily_token_budget=1_000_000,     # None = fall back to the global default
    daily_cost_budget_usd=1.0,
)
s.close()
print("key_id:", issued.key_id)
print("KEY (shown once):", issued.plaintext)
PY
```

**`pepper=` is not optional.** Issue a key without it and the stored
`pepper_fp` will not match the running configuration, so the key will fail
verification on first use and you will spend an afternoon on it.

### Revoke an invite

```sh
docker compose exec -T gateway python - <<'PY'
import os
from gateway.storage import Storage
s = Storage(os.environ.get("YANGBLE5_DB_PATH", "/data/yangble5.db"))
print("revoked:", s.revoke_invite("yb5-...."))
s.close()
PY
```

---

## 4. Suspend an abusive user

Statuses: `active`, `suspended`, `revoked`. Suspension is reversible; use it
first. Reserve `revoked` for a key you never intend to re-enable.

### Find the key

```sh
docker compose exec -T gateway python - <<'PY'
import os
from gateway.storage import Storage
s = Storage(os.environ.get("YANGBLE5_DB_PATH", "/data/yangble5.db"))
for row in s.list_keys(limit=50):
    print(dict(row))
s.close()
PY
```

### Suspend it

```sh
docker compose exec -T gateway python - <<'PY'
import os
from gateway.storage import Storage
KEY_ID = "yb5_xxxxxxxx"                    # <-- edit
s = Storage(os.environ.get("YANGBLE5_DB_PATH", "/data/yangble5.db"))
print("suspended:", s.set_key_status(KEY_ID, "suspended", "abuse: shared key, 40 distinct IPs"))
s.close()
PY
```

Always give a reason. It is stored in `suspended_reason`, and in three months
it will be the only record of why you did it.

### How fast does it take effect?

The gateway caches successful authentications for `YANGBLE5_AUTH_CACHE_TTL_SECONDS`
(default 300) so it does not run scrypt on every proxied request. A suspension
therefore takes up to that long to bite. To make it immediate:

```sh
docker compose restart gateway
```

That drops in-flight streams. For a genuine abuse incident, do it; for
routine housekeeping, let the cache expire.

### Check the sharing signal first

`ip_observations` is why you might suspect a shared key in the first place:

```sh
docker compose exec -T gateway python - <<'PY'
import os
from gateway.storage import Storage
KEY_ID = "yb5_xxxxxxxx"                    # <-- edit
s = Storage(os.environ.get("YANGBLE5_DB_PATH", "/data/yangble5.db"))
print("distinct IPs in 24h:", s.distinct_ip_count(KEY_ID, 24))
s.close()
PY
```

IPs are stored **hashed**, so you can count distinct sources but cannot
recover who they were. That is intentional — the count is the operational
signal; the addresses are not yours to keep.

A high count is evidence, not proof: mobile networks, VPNs and CI runners all
produce it legitimately. This is also why `YANGBLE5_ABUSE_AUTO_SUSPEND`
defaults to `false`.

---

## 5. Rotate the engine key

The engine key is the credential the gateway presents to the engine. Users
never see it. Rotate it if it may have leaked, or on a schedule.

**It lives in two places and both must change together.** A mismatch means
every request returns 401.

```sh
NEW_KEY="$(openssl rand -hex 32)"

# 1. .env
sudo sed -i "s|^YANGBLE5_ENGINE_API_KEY=.*|YANGBLE5_ENGINE_API_KEY=${NEW_KEY}|" .env

# 2. the engine's own config
sudo sed -i "s|^  - \".*\"|  - \"${NEW_KEY}\"|" engine/config.yaml
sudo grep -A2 '^api-keys:' engine/config.yaml      # eyeball it before restarting

# 3. recreate both (the engine reads config at startup)
docker compose up -d --force-recreate engine gateway

unset NEW_KEY
```

Verify:

```sh
docker compose exec -T gateway python - <<'PY'
import os, urllib.request
req = urllib.request.Request(
    os.environ["YANGBLE5_ENGINE_URL"] + "/v1/models",
    headers={"Authorization": "Bearer " + os.environ["YANGBLE5_ENGINE_API_KEY"]},
)
print(urllib.request.urlopen(req, timeout=10).status)      # expect 200
PY
```

`401` means the two copies still disagree. Nothing else produces that symptom
in this stack, so do not go looking further.

Rotating `YANGBLE5_ENGINE_MANAGEMENT_KEY` is the same shape: change `.env` and
`remote-management.secret` in `engine/config.yaml`, then recreate the engine.

### Rotating the admin key

Cheap and low-risk — it lives in one place and no user key depends on it:

```sh
sudo sed -i "s|^YANGBLE5_ADMIN_API_KEY=.*|YANGBLE5_ADMIN_API_KEY=$(openssl rand -hex 32)|" .env
docker compose up -d --force-recreate gateway
sudo grep '^YANGBLE5_ADMIN_API_KEY=' .env      # the new value
```

Existing user keys and invite codes are unaffected: the admin key authenticates
*you*, not them.

---

## 6. Rotate a user key

There is no in-place rotation: keys are stored as hashes, so a "new key" is a
new record.

```sh
docker compose exec -T gateway python - <<'PY'
import os
from gateway.storage import Storage

OLD = "yb5_xxxxxxxx"                        # <-- edit
EMAIL = "user@example.com"                  # <-- edit

s = Storage(os.environ.get("YANGBLE5_DB_PATH", "/data/yangble5.db"))
issued = s.issue_key(email=EMAIL, label="rotation",
                     pepper=os.environ.get("YANGBLE5_KEY_PEPPER", ""))
print("NEW KEY (shown once):", issued.plaintext)
# Suspend rather than revoke, so the user has a window to switch over.
s.set_key_status(OLD, "suspended", "rotated")
s.close()
PY
```

If `YANGBLE5_ALLOW_MULTIPLE_KEYS_PER_EMAIL` is `false`, suspend the old key
*before* issuing the new one, or the issue will be refused.

---

## 7. Rotate the key pepper (break-glass)

**This invalidates every issued API key.** The pepper is mixed into every key
hash; each key stores a `pepper_fp` fingerprint, and after rotation none of
them match, so every key fails verification.

Only do this if the pepper itself is compromised — which in practice means
your `.env` leaked, in which case the engine key and everything else needs
rotating too.

```sh
# 1. Tell your users first. Every one of them is about to be locked out.

# 2. Back up (§8).

# 3. Rotate.
NEW_PEPPER="$(openssl rand -hex 32)"
sudo sed -i "s|^YANGBLE5_KEY_PEPPER=.*|YANGBLE5_KEY_PEPPER=${NEW_PEPPER}|" .env
docker compose up -d --force-recreate gateway
unset NEW_PEPPER

# 4. Re-issue every key (§3) and distribute them.

# 5. Revoke the orphans, so the table does not fill with keys that can
#    never verify again.
docker compose exec -T gateway python - <<'PY'
import os
from gateway.storage import Storage
from gateway.storage import pepper_fingerprint
s = Storage(os.environ.get("YANGBLE5_DB_PATH", "/data/yangble5.db"))
current = pepper_fingerprint(os.environ.get("YANGBLE5_KEY_PEPPER", ""))
for row in s.list_keys(limit=10000):
    rec = s.get_key(row["key_id"])
    if rec and rec.pepper_fp != current and rec.status != "revoked":
        s.set_key_status(rec.key_id, "revoked", "pepper rotation")
        print("revoked", rec.key_id)
s.close()
PY
```

---

## 8. Back up the database

**Do not `cp` a live SQLite database.** The gateway runs in WAL mode, so the
`.db` file on its own is an incomplete snapshot — you can restore it and lose
the most recent writes, or get a corrupt file. Use SQLite's backup API, which
takes a consistent snapshot of a database that is being written to.

```sh
STAMP="$(date +%Y%m%d-%H%M%S)"

docker compose exec -T gateway python - <<PY
import os, sqlite3
src = os.environ.get("YANGBLE5_DB_PATH", "/data/yangble5.db")
dst = f"/data/backup-${STAMP}.db"
s = sqlite3.connect(src)
d = sqlite3.connect(dst)
with d:
    s.backup(d)          # consistent hot snapshot, WAL and all
d.close(); s.close()
print("wrote", dst)
PY

# Pull it out of the volume onto the host, then off the host entirely.
docker compose cp "gateway:/data/backup-${STAMP}.db" "/opt/yangble5/backups/yangble5-${STAMP}.db"
docker compose exec -T gateway rm -f "/data/backup-${STAMP}.db"

sudo chmod 600 "/opt/yangble5/backups/yangble5-${STAMP}.db"
```

### What else to back up

| Item | Why |
|---|---|
| `deploy/.env` | the pepper. **Without it, a restored database authenticates nobody.** |
| `deploy/engine/config.yaml` | engine key and aliases |
| the `engine_auth` volume | upstream OAuth tokens; losing it means re-authenticating every account |

```sh
docker run --rm -v yangble5_engine_auth:/auth:ro -v /opt/yangble5/backups:/out \
    debian:bookworm-slim tar czf /out/engine-auth-$(date +%Y%m%d).tar.gz -C /auth .
```

(Volume name comes from `docker volume ls` — Compose prefixes it with the
project directory name.)

### Retention

Keep 7 daily and 4 weekly copies. A cron entry:

```
17 4 * * * root /opt/yangble5/app/deploy/backup.sh >> /var/log/yangble5-backup.log 2>&1
```

`backup.sh` is not shipped — write it from the commands above once you have
decided where backups go. **A backup you have never restored is a hypothesis.**
Test §9 on a throwaway host before you need it.

> Backups contain hashed keys, hashed IPs and full usage history. Treat them
> as personal data: encrypt them at rest and delete them on a schedule.

---

## 9. Restore

```sh
docker compose stop gateway            # never restore under a live writer

docker compose cp /opt/yangble5/backups/yangble5-20260721-040000.db \
                  gateway:/data/restore.db

docker compose run --rm -T --entrypoint python gateway - <<'PY'
import os, shutil
db = os.environ.get("YANGBLE5_DB_PATH", "/data/yangble5.db")
# Move the current file aside rather than deleting it: if the restore turns
# out to be the wrong snapshot you still have the newer data.
for suffix in ("", "-wal", "-shm"):
    p = db + suffix
    if os.path.exists(p):
        shutil.move(p, p + ".pre-restore")
shutil.move("/data/restore.db", db)
print("restored", db)
PY

docker compose start gateway
docker compose logs --tail=50 gateway
```

Then confirm the pepper in `.env` is the one that was in force when the backup
was taken. If it is not, every key in the restored database is dead and you
are in §7.

---

## 10. Upgrade the engine, and retire the shim

### Upgrade

```sh
# 1. Back up the auth volume (§8) — token loss means re-authenticating
#    every upstream account by hand.

# 2. Drop in the new binary and CHECK IT.
sudo cp ~/cli-proxy-api-7.2.93 engine-bin/cli-proxy-api
sha256sum engine-bin/cli-proxy-api     # compare against the upstream release

# 3. Rebuild and restart just the engine.
docker compose build engine
docker compose up -d engine

# 4. Watch it come up.
docker compose logs -f engine
```

Verify the engine answers and your aliases still resolve:

```sh
docker compose exec -T gateway python - <<'PY'
import os, json, urllib.request
req = urllib.request.Request(
    os.environ["YANGBLE5_ENGINE_URL"] + "/v1/models",
    headers={"Authorization": "Bearer " + os.environ["YANGBLE5_ENGINE_API_KEY"]},
)
data = json.load(urllib.request.urlopen(req, timeout=15))
print([m.get("id") for m in data.get("data", [])])
PY
```

A model alias missing from that list is a config schema change between
versions. Diff your `engine/config.yaml` against the new release's
`config.example.yaml` before you go looking anywhere else.

### Retire `claude_shim.py` (once you are on >= 7.2.93)

The shim exists for exactly one bug: CLIProxyAPI 7.1.23's antigravity
**streaming** translator forwards `messages[].role` verbatim, so a Claude Code
>= 2.1.x client that injects a mid-conversation `role: "system"` message gets
`400 Request contains an invalid argument` from Gemini. The non-streaming path
tolerates the same message, which is why the failure looked intermittent.
Upstream fixed it in **7.2.93** by mapping `system` → `user`.

Once the engine is past that version the shim is a pointless extra hop:

```sh
# 1. Point the gateway straight at the engine.
sudo sed -i 's|^YANGBLE5_ENGINE_URL=.*|YANGBLE5_ENGINE_URL=http://engine:8318|' .env

# 2. Recreate the gateway and stop the shim.
docker compose up -d --force-recreate gateway
docker compose --profile shim stop shim
docker compose --profile shim rm -f shim
```

Then confirm the bug really is fixed upstream rather than assuming it: run a
Claude Code session that uses the Agent tool (that is what injects the
mid-conversation system message) **with streaming on**. If it completes, the
shim is genuinely redundant. If you get a 400, put the shim back — set the URL
to `http://shim:8320` and `docker compose --profile shim up -d`.

**Clients do not need to change.** They talk to the gateway, and the gateway
is what moves.

---

## 11. Upgrade the stack

```sh
cd /opt/yangble5/app
sudo git -C ~/yangble5-oss pull                    # your checkout
sudo bash ~/yangble5-oss/deploy/install.sh \
     --domain "$YANGBLE5_DOMAIN" --email "$ACME_EMAIL"
```

`install.sh` is idempotent: it re-copies the code, adds any new `.env` keys
introduced by the release, and leaves every existing value — including every
secret — alone.

Caddy config changes do not need a restart:

```sh
docker compose exec caddy caddy validate --config /etc/caddy/Caddyfile
docker compose exec caddy caddy reload   --config /etc/caddy/Caddyfile
```

`reload` is graceful — streams that have been open for minutes survive it.
`docker compose restart caddy` kills them. Validate first: a reload with a bad
config leaves the old one running, but a *restart* with a bad config leaves
you with no edge at all.

---

## 12. When the global budget cap trips

`YANGBLE5_GLOBAL_MONTHLY_USD_BUDGET` is a ceiling on the accounted spend for
the calendar month. When it is reached the gateway stops serving inference.
That is the system working: the alternative is an unbounded bill.

### First: is it real?

```sh
# What we think we spent:
docker compose exec -T gateway python - <<'PY'
import os
from gateway.storage import Storage
s = Storage(os.environ.get("YANGBLE5_DB_PATH", "/data/yangble5.db"))
print("accounted this month: $", round(s.global_cost_for_month(), 4))
s.close()
PY
```

Then compare against your provider's billing console. If your price table is
still the placeholder, the two numbers will not match and only the provider's
is real.

### Then pick one

| Situation | Action |
|---|---|
| Legitimate growth, you are happy to pay | Raise `YANGBLE5_GLOBAL_MONTHLY_USD_BUDGET`, `docker compose up -d --force-recreate gateway` |
| One key ran away | Suspend it (§4), leave the cap alone |
| You do not know yet | `YANGBLE5_REGISTRATION_MODE=closed` and recreate — existing users keep working, no new keys are minted while you investigate |
| Price table is wrong | Fix `YANGBLE5_PRICE_TABLE_FILE`; the cap is measuring the wrong thing until you do |

Raising the cap because the number looks scary, without finding out which key
caused it, is how the same thing happens again next month at a higher number.

### Prevention

- Set per-key ceilings (`YANGBLE5_DAILY_TOKEN_BUDGET`,
  `YANGBLE5_DAILY_COST_USD_BUDGET`) so one key cannot drain the global cap.
- Keep `YANGBLE5_GLOBAL_BUDGET_WARN_RATIO` at 0.9 and actually watch for the
  warning in the logs.
- Stay on `invite` registration until you trust your numbers.

---

## 13. Incidents

### A user key leaked

```sh
# Immediate, plus flush the auth cache.
docker compose exec -T gateway python -c "
import os
from gateway.storage import Storage
s = Storage(os.environ.get('YANGBLE5_DB_PATH','/data/yangble5.db'))
s.set_key_status('yb5_xxxxxxxx', 'revoked', 'leaked publicly')
s.close()"
docker compose restart gateway
```

Then check what it did while it was out: §2 "one key", and
`distinct_ip_count` for where from.

### `.env` leaked

Everything in it is compromised. In order:

1. Rotate the engine key (§5).
2. Rotate the engine management key (§5).
3. Rotate the pepper (§7) — this locks out every user, so tell them first.
4. Rotate the Cloudflare API token if you use DNS-01.
5. Rotate **the upstream provider credentials in the engine's auth volume**.
   yangble5 cannot do this for you; it is each provider's own flow.

### Upstream credentials compromised

Not a yangble5 problem in the technical sense, and entirely your problem in
the practical sense: those credentials are yours, the spend is yours. Revoke
at the provider, re-authenticate the engine, then rotate the engine key so any
cached client state is invalidated too.

### Everything is 502

```sh
docker compose ps                      # which service is not healthy?
docker compose logs --tail=100 gateway
docker compose logs --tail=100 engine
```

Usual causes, most common first:

1. engine unhealthy — bad config, or upstream credentials expired
2. `YANGBLE5_ENGINE_API_KEY` mismatch (§5) — this shows as 401 at the engine
   and 502 at the edge
3. gateway OOM-killed — check `docker compose ps` for a restart loop and raise
   `GATEWAY_MEM`
4. engine still starting after a restart — `start_period` is 60s for a reason

### Streams cut off at ~100 seconds

Cloudflare's origin timeout. See `cloudflare.md` §4. Check
`nonstream-keepalive-interval` is still set in `engine/config.yaml` — an
engine upgrade that resets the config is the usual way this comes back.

---

## 14. Where things are

| What | Where |
|---|---|
| Compose project | `/opt/yangble5/app/deploy` |
| Secrets | `/opt/yangble5/app/deploy/.env` (0600, root) |
| Engine config | `/opt/yangble5/app/deploy/engine/config.yaml` |
| Engine binary | `/opt/yangble5/app/deploy/engine-bin/cli-proxy-api` |
| Caddy access log | `/opt/yangble5/logs/caddy/access.log` |
| Backups | `/opt/yangble5/backups` |
| Database | `gateway_data` volume, `/data/yangble5.db` inside the container |
| Upstream OAuth tokens | `engine_auth` volume, `/auth` inside the container |
| Certificates | `caddy_data` volume |
| fail2ban jail | `/etc/fail2ban/jail.d/yangble5.local` |
| sysctl | `/etc/sysctl.d/99-yangble5.conf` |
| sshd hardening | `/etc/ssh/sshd_config.d/99-yangble5.conf` |

Container logs go to Docker's json-file driver, capped at 10 MB x 5 files per
service, so they cannot fill the disk on their own.
