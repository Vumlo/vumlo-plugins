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
