from vumlo_plugins.http import create_plugin_app
from vumlo_plugins.protocol import manifest
from vumlo_plugins.protocol import Notifier
MANIFEST = manifest("missing-submission-nudge", "1.0.0", subscribes=("assignment.deadline_changed",), scopes=("assignments:read", "roster:read", "submissions:read", "users:contact"), settings={"webhook_url": {"type": "string", "secret": True, "description": "Reminder webhook"}, "hours_before": {"type": "number", "default": 24, "description": "Reminder lead time"}})
notifier = Notifier()
def process(kind, event):
    if kind == "assignment.deadline_changed":
        notifier.send("Missing submission reminder", "A deadline is approaching.", event)
app = create_plugin_app(MANIFEST, process)
