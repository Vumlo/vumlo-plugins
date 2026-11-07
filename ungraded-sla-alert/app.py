from vumlo_plugins.http import create_plugin_app
from vumlo_plugins.protocol import manifest
MANIFEST = manifest("ungraded-sla-alert", "1.0.0", subscribes=("submission.submitted", "grade.published"), settings={"days": {"type": "number", "default": 3, "scope": "institution", "description": "Grading SLA days"}, "webhook_url": {"type": "string", "secret": True, "description": "Teacher alert webhook"}})
app = create_plugin_app(MANIFEST)
