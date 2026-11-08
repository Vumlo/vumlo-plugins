from vumlo_plugins.http import create_plugin_app
from vumlo_plugins.protocol import manifest
from vumlo_plugins.protocol import Notifier
MANIFEST = manifest("deadline-change-alert", "1.0.0", subscribes=("assignment.deadline_changed",), scopes=("assignments:read", "users:contact"), settings={"webhook_url": {"type": "string", "secret": True, "description": "Notification webhook"}})
notifier = Notifier()
def process(kind, event):
    if kind == "assignment.deadline_changed":
        notifier.send("Assignment deadline changed", "An assignment deadline changed.", event)
app = create_plugin_app(MANIFEST, process)
