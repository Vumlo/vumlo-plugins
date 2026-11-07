# vumlo-plugins

Small independent Python 3.12 HTTP services for Vumlo LMS. Each directory is
one deployable plugin. The shared `vumlo_plugins` package implements the
Vumlo API version 1 manifest, HMAC event verification, replay-age checks,
plugin API client, settings reads, and notifications.

The contract was verified against `vumlo-core` ADR-0066, ADR-0072, ADR-0073,
`examples/hello-plugin`, and `backend/crates/plugins`. The core currently
supports only `submission.submitted`, `grade.published`, and
`plugin.settings.changed`; plugin API calls are submitted lookup, own settings,
and own report writes. Plugins do not invent unavailable endpoints.

Run with `uv sync`, then for example:

```sh
VUMLO_WEBHOOK_SECRET=... uv run uvicorn webhook-forwarder.app:app --port 9000
```
