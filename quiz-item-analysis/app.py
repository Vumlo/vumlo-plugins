from vumlo_plugins.http import create_plugin_app
from vumlo_plugins.protocol import manifest
MANIFEST = manifest("quiz-item-analysis", "1.0.0", subscribes=("submission.submitted",), scopes=("submissions:read", "reports:write"))
app = create_plugin_app(MANIFEST)
