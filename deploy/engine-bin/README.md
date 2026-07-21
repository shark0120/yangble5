# `deploy/engine-bin/` — you supply the engine binary

This directory is intentionally empty in the repository. Put a **linux/amd64
`cli-proxy-api` binary** here and `Dockerfile.engine` will wrap it:

```
deploy/engine-bin/cli-proxy-api
```

Then:

```sh
docker compose build engine
```

## Why yangble5 does not ship it

[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) is a separate
MIT-licensed project (copyright Luis Pater; Router-For.ME). yangble5 is a
configuration, a gateway and a set of
measurements *on top of* it — we did not write it. Vendoring someone else's
binary into our image would make its provenance, its upgrade path and its
licensing our problem and your risk. You should obtain it from upstream and
verify it yourself.

## Getting a binary

Either download the release artifact for the version you intend to run, or
build it from the tagged source:

```sh
git clone --branch v7.1.23 https://github.com/router-for-me/CLIProxyAPI
cd CLIProxyAPI
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o cli-proxy-api ./cmd/server
```

**Verify the checksum against the upstream release before you run it.** A
proxy binary sees every prompt and holds every upstream credential; it is the
single worst place in this stack to run something you have not checked.

```sh
sha256sum cli-proxy-api
```

## Which version

Every measurement in this repository was taken against **7.1.23**, and
`deploy/engine/config.example.yaml` matches its schema.

If you deploy **>= 7.2.93** you should also retire `tools/claude_shim.py`:
that release fixed the mid-conversation `role: "system"` bug natively, so the
shim becomes dead weight in the request path. See `deploy/runbook.md`,
"Upgrade the engine".

## Alternative: bring your own image

If you already have an image you trust, skip this directory entirely:

```sh
# in .env
ENGINE_IMAGE=ghcr.io/you/cli-proxy-api:7.2.93
```

and delete the `build:` key from the `engine` service in
`docker-compose.yml`.
