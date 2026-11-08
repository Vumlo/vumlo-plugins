from vumlo_plugins.http import create_plugin_app
from vumlo_plugins.protocol import manifest
from vumlo_plugins.protocol import Notifier
MANIFEST = manifest("ungraded-sla-alert", "1.0.0", subscribes=("submission.submitted", "grade.published"), scopes=("submissions:read", "roster:read", "users:contact"), settings={"days": {"type": "number", "default": 3, "scope": "institution", "description": "Grading SLA days"}, "webhook_url": {"type": "string", "secret": True, "description": "Teacher alert webhook"}})
notifier = Notifier()
def process(kind, event):
    if kind == "submission.submitted":
        notifier.send("Submission awaiting grading", "A submission is awaiting grading.", event)
app = create_plugin_app(MANIFEST, process)
