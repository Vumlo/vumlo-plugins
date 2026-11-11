# vumlo-plugins

Small independent Python 3.12 HTTP services for Vumlo LMS, and the SDK they share. Each top-level directory with an
`app.py` is one deployable plugin (`uvicorn <folder>.app:app`); `vumlo_plugins/` is the SDK.

**Writing a plugin: [vumlo_plugins/README.md](vumlo_plugins/README.md)** (a plugin in 30 lines, the client, reports,
mail, jobs, and testing offline with `FakeCore`).

The contract is the core's: ADR-0066 and ADR-0072 to 0082 in `vumlo-core/docs/decisions`, `backend/crates/plugins`, and
its generated OpenAPI. The SDK signs and verifies exactly as `secret.rs` and `proxy.rs` do, ports the core's manifest
validator, and `FakeCore` is checked against the OpenAPI, so a plugin's tests fail here before an install would fail there.

## Develop

```sh
uv sync
uv run pytest                      # all plugins and the SDK, offline
VUMLO_WEBHOOK_SECRET=... uv run uvicorn text-metrics.app:app --port 9000
```

Environment of a running plugin (printed by `vumlo-cli plugin install`): `VUMLO_API`, `VUMLO_PLUGIN_TOKEN`,
`VUMLO_WEBHOOK_SECRET`; and `VUMLO_PLUGIN_DATA` for its small SQLite state (default `./data`).

## Run in Docker

One image per plugin; `PLUGIN` is the directory name and is required:

```sh
docker build --build-arg PLUGIN=text-metrics -t vumlo-plugin-text-metrics .
docker run --rm -p 8000:8000 -v text-metrics-data:/data \
  -e VUMLO_API=http://core:8080/api/v1 \
  -e VUMLO_PLUGIN_TOKEN=... \
  -e VUMLO_WEBHOOK_SECRET=... \
  -e VUMLO_PLUGIN_DATA=/data \
  vumlo-plugin-text-metrics
```

The container listens on port 8000 as a non-root user; `GET /manifest` returns the plugin manifest. Images carry no
system packages by default; a plugin that needs some lists them in `<plugin>/apt-packages.txt`, passed as
`--build-arg "APT_PACKAGES=..."`.

## Releases and `create-vumlo`

A release of this repository is what `npx create-vumlo` offers as the trusted plugin list: every top-level directory
containing an `app.py` is one plugin. The installer copies the chosen plugin directories plus `vumlo_plugins/`,
`pyproject.toml`, `uv.lock`, `Dockerfile` and `LICENSE` and builds one image per plugin with the `PLUGIN` build arg.
