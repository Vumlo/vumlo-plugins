# webhook-forwarder

Forwards signed core events to a configured HTTP webhook. It subscribes to
`submission.submitted`, `grade.published`, and `plugin.settings.changed`; it
requests no plugin API scopes. Set `VUMLO_FORWARD_URL` for the process.

Settings: `webhook_url` is an institution-wide secret declaration. The current
core settings protocol returns secrets only to the plugin, but this reference
implementation uses the process environment for outbound delivery.

## Needs from core

None for event forwarding. The core currently sends event payloads containing
IDs only, as required by the plugin contract.
