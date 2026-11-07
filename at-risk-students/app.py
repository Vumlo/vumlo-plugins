from vumlo_plugins.http import create_plugin_app
from vumlo_plugins.protocol import manifest
MANIFEST = manifest("at-risk-students", "1.0.0", subscribes=("submission.submitted", "grade.published"), settings={"webhook_url": {"type": "string", "secret": True, "description": "Report webhook"}})
app = create_plugin_app(MANIFEST)
