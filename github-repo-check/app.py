import httpx
from vumlo_plugins.http import create_plugin_app
from vumlo_plugins.protocol import manifest
MANIFEST = manifest("github-repo-check", "1.0.0", subscribes=("submission.submitted",), scopes=("submissions:read", "reports:write"), settings={"github_token": {"type": "string", "secret": True, "description": "GitHub API token"}})
app = create_plugin_app(MANIFEST)
