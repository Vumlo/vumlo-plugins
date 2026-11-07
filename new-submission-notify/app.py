from vumlo_plugins.http import create_plugin_app
from vumlo_plugins.protocol import manifest
MANIFEST = manifest("new-submission-notify", "1.0.0", subscribes=("submission.submitted",), settings={"webhook_url": {"type": "string", "secret": True, "description": "Teacher notification webhook"}})
app = create_plugin_app(MANIFEST)
