# new-submission-notify

Receives the core's signed `submission.submitted` event. It declares an
institution secret webhook setting for a batched notification implementation.

## Needs from core

The event contains IDs only and the plugin API has no teacher/course/member
directory or notification-recipient endpoint. Recipient resolution and a
reliable batch policy must be provided by core before notifications are sent.
