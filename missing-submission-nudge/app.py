from vumlo_plugins.http import create_plugin_app
from vumlo_plugins.protocol import manifest
MANIFEST = manifest("missing-submission-nudge", "1.0.0", subscribes=("submission.submitted",), settings={"webhook_url": {"type": "string", "secret": True, "description": "Reminder webhook"}, "hours_before": {"type": "number", "default": 24, "description": "Reminder lead time"}})
app = create_plugin_app(MANIFEST)
