# `deploy/caddy/conf.d/` — site-level drop-ins

Every `*.conf` file here is imported into the main site block of the
`Caddyfile`:

```
import /etc/caddy/conf.d/*.conf
```

The directory is mounted **read-only** into the Caddy container. A glob that
matches nothing produces a warning in Caddy's log, not an error, so it is fine
to leave this directory with no `.conf` files in it at all.

Files must contain **site-scoped directives only** — no global options block,
no site address. They are spliced into an existing site.

## What belongs here

Anything you want to change without editing the tracked `Caddyfile`:

- switching ACME to the Cloudflare DNS-01 challenge
  (`tls-cloudflare.conf.example` — rename to `tls-cloudflare.conf` to use it)
- enabling Cloudflare Authenticated Origin Pulls
- an extra route for something you host on the same name

## Reloading

Caddy does not watch this directory. After adding or editing a file:

```sh
docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile
```

A reload is graceful: in-flight requests, including streams that have been
open for minutes, are not dropped. `docker compose restart caddy` **does**
drop them — prefer reload.

Validate before reloading, so a typo does not take the edge down:

```sh
docker compose exec caddy caddy validate --config /etc/caddy/Caddyfile
```
