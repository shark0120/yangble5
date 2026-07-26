# Supplying secrets to a yangble5 deployment

How upstream credentials get onto the server without ever passing through a
chat window, a git repository, or your shell history — plus how to rotate them
and what to do when one leaks.

Read this **before** step 5 of [`GO_LIVE.md`](GO_LIVE.md).

---

## The three rules

1. **A secret is typed once, on the server, into a prompt or an editor.** Never
   onto a command line, never into a message, never into a file that git can
   see.
2. **The server generates what it can generate itself.** Every internal secret
   in this deployment is created on the box by `install.sh` from the kernel
   CSPRNG. You never choose, transport, or know most of them.
3. **If you are unsure whether a secret leaked, treat it as leaked.** Rotation
   is cheap. The alternative is someone else's bill.

---

## Which secrets exist, and where they live

| Secret | Lives in | Permissions | Who creates it |
|---|---|---|---|
| `YANGBLE5_ENGINE_API_KEY` | `deploy/.env` **and** `deploy/engine/config.yaml` | `0600 root:root` / `0640 root:<service>` | `install.sh`, generated |
| `YANGBLE5_ENGINE_MANAGEMENT_KEY` | same two files | same | `install.sh`, generated |
| `YANGBLE5_ADMIN_API_KEY` | `deploy/.env` | `0600 root:root` | `install.sh`, generated |
| `YANGBLE5_KEY_PEPPER` | `deploy/.env` | `0600 root:root` | `install.sh`, generated |
| **Upstream provider credentials** | the `engine_auth` Docker volume, mounted at `/auth` | owned by the container UID | **you**, via the provider's own login flow |
| User keys (`yb5_…`) | nowhere — only a hash is stored | gateway database | `install.sh` / the gateway |

Two things worth internalising:

- **You do not choose the internal secrets.** They are 32 bytes of kernel
  randomness each. There is nothing for you to invent, transport, or write
  down. The only secret you ever handle by hand is the upstream credential.
- **User keys are never stored in plaintext.** A key is
  `yb5_<key_id>_<secret>`; the database holds a hash. If your database leaks,
  the keys in it cannot be used. This is also why a lost user key can only be
  reissued, never recovered.

The OAuth token store is a **named Docker volume**, not a directory in the
repo. `install.sh` warns you if a stray `deploy/engine/auth/` exists, because
that path looks like it should work and does not. Locate the real one with:

```sh
docker volume ls | grep engine_auth
docker volume inspect deploy_engine_auth --format '{{.Mountpoint}}'
```

The volume is declared as `engine_auth`; Docker prefixes it with the Compose
project name, which is the directory name `install.sh` passes
(`--project-directory .../deploy`), giving `deploy_engine_auth`. If you set
`COMPOSE_PROJECT_NAME` yourself, substitute your prefix in every command below.

---

## Whose credentials belong in a public pool

Personal-account OAuth credentials — the ones you get by logging in with your
own Google or X account — are not appropriate for a pool that serves the
public. They are issued to you as an individual, the provider's terms govern
them accordingly, and a shared public endpoint is not the use they were granted
for. The supported paths for a service other people use are **BYOK** (each user
brings their own credential — see [`../byok/README.md`](../byok/README.md)) and
**paid, licensed API keys** that permit serving third parties.

That is the whole of the warning. What you run on your own machine for yourself
is your business.

---

## Getting a credential onto the server

### Step 1 — do not carry it in the clear

Do not paste an upstream credential into a chat with anyone, including an AI
assistant. Do not email it to yourself. Do not put it in a ticket. If it has
already been through any of those, it is burned — go to
[If a secret is believed leaked](#if-a-secret-is-believed-leaked).

### Step 2 — OAuth providers: never handle a long-lived secret at all

This is the best case and the default for the Gemini/antigravity channel.
CLIProxyAPI runs the login flow and writes the resulting token into `/auth`
itself. You authenticate in a browser; no long-lived secret is ever copied,
displayed, or pasted anywhere.

```sh
cd /opt/yangble5/app/deploy
docker compose run --rm engine <the login subcommand for your build>
docker compose restart engine
```

The exact subcommand depends on the CLIProxyAPI build you supplied — check its
own `--help`. yangble5 does not redistribute the engine, so we cannot promise
its CLI surface.

Verify a token landed without printing it:

```sh
docker compose exec engine sh -c 'ls -la /auth'
```

That lists filenames and sizes. Do not `cat` the files.

### Step 3 — API-key providers: type it into a prompt, never a command line

When a provider gives you a plain API key, the risk is the shell. This is wrong:

```sh
# WRONG — lands in ~/.bash_history and is visible in `ps` to every user
echo 'PROVIDER_KEY=sk-abc123' >> .env
```

Use `sudoedit`, which never touches history and writes through a temporary file
with the original ownership and mode:

```sh
sudo -e /opt/yangble5/app/deploy/engine/config.yaml
```

If you must script it, read the value into a variable through a **hidden
prompt**. A value typed at a `read` prompt is not a command, so it is not
recorded in history, and it never appears in `ps` because it is never an
argument:

```sh
read -rs -p 'Upstream API key (hidden): ' UPSTREAM_KEY; echo
printf 'PROVIDER_KEY=%s\n' "$UPSTREAM_KEY" | sudo tee -a /opt/yangble5/app/deploy/.env >/dev/null
unset UPSTREAM_KEY
sudo chmod 600 /opt/yangble5/app/deploy/.env
```

Then confirm the shape without revealing the value:

```sh
sudo grep -c '^PROVIDER_KEY=' /opt/yangble5/app/deploy/.env   # expect 1
```

> `HISTCONTROL=ignorespace` plus a leading space is often suggested as an
> alternative. It only works if the variable was already set before you typed
> the command, it does nothing about `ps`, and it silently does nothing at all
> in a shell that ignores it. Do not rely on it.

### Step 4 — check the permissions

`install.sh` sets these, but verify after any manual edit:

```sh
cd /opt/yangble5/app/deploy
sudo stat -c '%n %a %U:%G' .env engine/config.yaml
# .env               600 root:root
# engine/config.yaml 640 root:yangble5
```

`.env` is `0600 root:root` because only root and the Docker daemon need it.
`engine/config.yaml` is `0640` because the engine container's user must read
it — it holds the engine's inbound key, not an upstream credential.

Fix a bad mode immediately:

```sh
sudo chmod 600 .env && sudo chown root:root .env
```

### Step 5 — confirm nothing reached git

```sh
cd /opt/yangble5/app                      # or your working checkout
git status --porcelain                    # .env must NOT appear
git ls-files | grep -E '(^|/)\.env$|config\.yaml$'   # must print nothing
```

`deploy/.env` and `deploy/engine/config.yaml` are both gitignored. `git
ls-files` is the check that matters: `.gitignore` does not protect a file that
was committed before it was ignored. `preflight.sh` fails on this condition
too.

---

## Rotation

Rotate on a schedule you can actually keep, and immediately on any suspicion.
The internal secrets have documented procedures in the operations runbook;
this section covers the ordering and the upstream credential, which the runbook
does not.

| Secret | Procedure | User impact |
|---|---|---|
| Engine API key | [`runbook.md`](runbook.md) §5 | none, if `.env` and `engine/config.yaml` are changed together |
| Admin API key | `runbook.md` §5 | none |
| One user key | `runbook.md` §6 | that user re-keys |
| Key pepper | `runbook.md` §7 | **every user is locked out** — break-glass only |
| Upstream credential | below | brief outage while the engine reloads |

**The engine key and the engine config must change together.** They are the
same value in two files. Changing one leaves every request returning 401 with
nothing in the logs explaining why — which is exactly why `install.sh`
substitutes both from a single source.

### Rotating an upstream credential

Order matters: issue the new credential *before* revoking the old one, or you
take an outage for the length of the login flow.

```sh
cd /opt/yangble5/app/deploy

# 1. Back up the auth volume first. Losing it means re-authenticating every
#    upstream account by hand (runbook.md §8).
docker run --rm -v deploy_engine_auth:/auth:ro -v "$PWD":/backup alpine \
    tar czf /backup/engine_auth-$(date +%F).tgz -C /auth .
sudo chmod 600 engine_auth-*.tgz

# 2. Add the NEW credential (OAuth login flow, or sudoedit for an API key).

# 3. Restart and confirm the new credential is the one serving traffic.
docker compose restart engine
docker compose logs --tail=50 engine

# 4. Only now, revoke the OLD credential in the PROVIDER's console.
#    Revoking it anywhere else does not revoke it.
```

Step 4 is the one people skip. A credential you removed from your server but
did not revoke at the provider is still live, and still billable, for whoever
has a copy.

Afterwards, move the backup tarball off the server and delete it locally — it
contains live tokens.

---

## If a secret is believed leaked

Work down the table. "Believed" is enough; do not wait for proof.

| What leaked | Blast radius | Do this, in order |
|---|---|---|
| **An upstream credential** | your money, your provider account | 1. Revoke it **at the provider**, first, before anything else. 2. Issue a replacement (above). 3. Check the provider's usage dashboard for spend you did not make. |
| **`YANGBLE5_ENGINE_MANAGEMENT_KEY`** | can list and mint upstream credentials | 1. `docker compose down`. 2. Treat **every upstream credential as leaked** and revoke them all at the provider. 3. Rotate the management key, re-render the config, bring it back up. |
| **`YANGBLE5_ENGINE_API_KEY`** | free inference at your expense, if the engine is reachable | Rotate per `runbook.md` §5. Confirm port 8318 is not published (`smoke_test.sh` checks this). |
| **`YANGBLE5_ADMIN_API_KEY`** | can mint invites and suspend users | Rotate per `runbook.md` §5; audit invites and keys created since the leak. |
| **A single user key** | that user's quota | `runbook.md` §6 — suspend, then reissue. |
| **`YANGBLE5_KEY_PEPPER`** | offline attack on stored key hashes becomes cheaper | `runbook.md` §7. Tell users first: rotating it locks out **everyone**. |
| **The whole `.env`** | all of the above at once | Kill switch (`GO_LIVE.md`), then work the entire table. |

### If it reached a git repository

Deleting the file in a new commit does **not** remove it. It is in the history,
in every clone, and — if the repo was ever public — in GitHub's fork network
and in whatever scraped it within minutes.

1. **Rotate first.** Every secret in that file, before touching the repository.
   Rewriting history is slow; a scraper is not.
2. Then rewrite history (`git filter-repo`) if you still want to, and force-push.
3. Assume the old value is permanently public regardless.

### If it went through a chat window

Same conclusion, fewer steps: rotate it. Retention, logging and training
policies vary by provider and are not under your control. It does not matter
whether the transcript was later deleted.

---

## Quick self-audit

Run this on the server whenever you have touched configuration. It prints
statuses, never values.

```sh
cd /opt/yangble5/app/deploy
sudo stat -c '%n %a %U:%G' .env engine/config.yaml
git -C .. ls-files | grep -E '(^|/)\.env$' && echo 'PROBLEM: .env is tracked' || echo 'ok: .env not tracked'
sudo grep -c '__GENERATE__' .env   # 0 = every placeholder was filled in
docker compose config | grep -iE 'sk-|yb5_|Bearer ' && echo 'PROBLEM: secret literal in compose output' || echo 'ok: no secret literals'
history | grep -iE 'sk-[A-Za-z0-9]|yb5_[A-Za-z0-9]' && echo 'PROBLEM: secret in shell history' || echo 'ok: history clean'
```

If the last line finds something, **rotate that secret first, then** clear the
history. Clearing first only destroys your own evidence; it does nothing about
the copy that already leaked.

```sh
history -d <line-number>     # or: history -c && rm -f ~/.bash_history
```
