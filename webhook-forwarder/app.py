import os
import httpx
from vumlo_plugins.http import create_plugin_app
from vumlo_plugins.protocol import manifest

MANIFEST = manifest(
    "webhook-forwarder", "1.0.0",
    subscribes=("submission.submitted", "grade.published", "plugin.settings.changed", "assignment.created", "assignment.updated", "assignment.deadline_changed", "enrollment.changed", "quiz.attempt_submitted", "announcement.published"),
    settings={"webhook_url": {"type": "string", "secret": True, "description": "Destination webhook URL"}},
)

def forward(event_type, event):
    url = os.getenv("VUMLO_FORWARD_URL")
    if url:
        httpx.post(url, json=event, timeout=10).raise_for_status()

app = create_plugin_app(MANIFEST, forward)
