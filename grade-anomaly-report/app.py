from vumlo_plugins.http import create_plugin_app
from vumlo_plugins.protocol import manifest
MANIFEST = manifest("grade-anomaly-report", "1.0.0", subscribes=("grade.published",), scopes=(), settings={"webhook_url": {"type": "string", "secret": True, "description": "Teacher report webhook"}})
app = create_plugin_app(MANIFEST)
