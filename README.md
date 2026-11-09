# vumlo-plugins

Small independent Python 3.12 HTTP services for Vumlo LMS. Each directory is
one deployable plugin. The shared `vumlo_plugins` package implements the
Vumlo API version 1 manifest, HMAC event verification, replay-age checks,
plugin API client, settings reads, and notifications.

The contract was verified against `vumlo-core` ADR-0072 through ADR-0081,
`examples/hello-plugin`, the generated OpenAPI, and
`backend/crates/plugins`. The current contract includes nine signed event
types, course-scoped installation with `--courses`, paginated plugin reads,
tombstones, keyed report writes, and signed proxied routes. Plugins do not
invent unavailable endpoints.

Run with `uv sync`, then for example:

```sh
VUMLO_WEBHOOK_SECRET=... uv run uvicorn webhook-forwarder.app:app --port 9000
```

## Run in Docker

One image per plugin; `PLUGIN` is the directory name and is required:

```sh
docker build --build-arg PLUGIN=text-metrics -t vumlo-plugin-text-metrics .
docker run --rm -p 8000:8000 \
  -e VUMLO_API=http://core:8080 \
  -e VUMLO_PLUGIN_TOKEN=... \
  -e VUMLO_WEBHOOK_SECRET=... \
  vumlo-plugin-text-metrics
```

The container listens on port 8000 as a non-root user; `GET /manifest` returns
the plugin manifest. Images carry no system packages by default. Only
`pdf-optimizer` needs extra ones (OCR backend), passed via `APT_PACKAGES`:

```sh
docker build --build-arg PLUGIN=pdf-optimizer \
  --build-arg "APT_PACKAGES=tesseract-ocr tesseract-ocr-eng tesseract-ocr-ukr ghostscript" \
  -t vumlo-plugin-pdf-optimizer .
```

## Releases and `create-vumlo`

A release of this repository is what `npx create-vumlo` offers as the trusted
plugin list: every top-level directory containing an `app.py` is one plugin.
The installer copies the chosen plugin directories plus `vumlo_plugins/`,
`pyproject.toml`, `uv.lock`, `Dockerfile` and `LICENSE` and builds one image
per plugin with the `PLUGIN` build arg.
