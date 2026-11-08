from vumlo_plugins.http import create_plugin_app
from vumlo_plugins.protocol import manifest
from vumlo_plugins.protocol import Notifier
MANIFEST = manifest("at-risk-students", "1.0.0", subscribes=("submission.submitted", "grade.published", "enrollment.changed"), scopes=("assignments:read", "submissions:read", "roster:read", "grades:read", "users:contact"), settings={"webhook_url": {"type": "string", "secret": True, "description": "Report webhook"}})
notifier = Notifier()
def process(kind, event):
    if kind in {"submission.submitted", "grade.published", "enrollment.changed"}:
        notifier.send("At-risk student report updated", "Student risk data changed.", event)
app = create_plugin_app(MANIFEST, process)
