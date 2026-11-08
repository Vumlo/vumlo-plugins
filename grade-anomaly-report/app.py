from vumlo_plugins.http import create_plugin_app
from vumlo_plugins.protocol import manifest
from vumlo_plugins.protocol import Notifier
MANIFEST = manifest("grade-anomaly-report", "1.0.0", subscribes=("grade.published",), scopes=("grades:read", "roster:read", "reports:write"), settings={"webhook_url": {"type": "string", "secret": True, "description": "Teacher report webhook"}})
notifier = Notifier()
def process(kind, event):
    if kind == "grade.published":
        notifier.send("Grade anomaly report updated", "A grade was published.", event)
app = create_plugin_app(MANIFEST, process)
