from vumlo_plugins.http import create_plugin_app
from vumlo_plugins.protocol import manifest
MANIFEST = manifest("deadline-change-alert", "1.0.0", subscribes=("submission.submitted",), scopes=("submissions:read",), settings={"webhook_url": {"type": "string", "secret": True, "description": "Notification webhook"}})
app = create_plugin_app(MANIFEST)
