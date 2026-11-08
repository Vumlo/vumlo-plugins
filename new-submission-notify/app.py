from vumlo_plugins.http import create_plugin_app
from vumlo_plugins.protocol import manifest
from vumlo_plugins.protocol import Notifier
MANIFEST = manifest("new-submission-notify", "1.0.0", subscribes=("submission.submitted",), scopes=("roster:read", "users:contact"), settings={"webhook_url": {"type": "string", "secret": True, "description": "Teacher notification webhook"}})
notifier = Notifier()
def process(kind, event):
    if kind == "submission.submitted":
        notifier.send("New submission", "A student submitted work.", event)
app = create_plugin_app(MANIFEST, process)
