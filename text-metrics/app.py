from vumlo_plugins.http import create_plugin_app
from vumlo_plugins.protocol import PluginClient, manifest

MANIFEST = manifest(
    "text-metrics", "1.0.0",
    subscribes=("submission.submitted",),
    scopes=("submissions:read", "reports:write"),
    settings={
        "minimum_words": {"type": "number", "scope": "course", "description": "Minimum word count"},
        "maximum_words": {"type": "number", "scope": "course", "description": "Maximum word count"},
    },
)
client = PluginClient()

def text(node):
    if isinstance(node, dict):
        return (" ".join([node.get("text", "")]) if node.get("type") == "text" else "") + " " + " ".join(text(v) for v in node.values())
    if isinstance(node, list):
        return " ".join(text(v) for v in node)
    return ""

def process(kind, event):
    if kind != "submission.submitted":
        return
    data = event["data"]
    submission = client.submission(data["submission_id"])
    value = text(submission["content"]).strip()
    words = len(value.split())
    client.report(data["submission_id"], f"{words} words", {"words": words, "characters": len(value), "minimum": None, "maximum": None})

app = create_plugin_app(MANIFEST, process)
