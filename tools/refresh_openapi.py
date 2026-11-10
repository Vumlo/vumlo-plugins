"""Cut the part of the core's OpenAPI that plugins talk to, for `vumlo_plugins/testing/openapi_plugin_api.json`.

    uv run python tools/refresh_openapi.py [path/to/openapi.json]

The input is the core's export (`web/openapi.json` of vumlo-core, or `GET /api/v1/openapi.json` of a running core).
Only `/plugin-api/*` and the staff reads of plugin reports are kept, with the schemas they reach. Run it when the core's
plugin API changes, then `uv run pytest tests/test_fake_core_contract.py`: the fake must still match.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

DEFAULT = Path(__file__).parents[2] / "vumlo-core" / "web" / "openapi.json"
OUT = Path(__file__).parents[1] / "vumlo_plugins" / "testing" / "openapi_plugin_api.json"


def refs(node, found: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str) and value.startswith("#/components/schemas/"):
                found.add(value.rsplit("/", 1)[1])
            else:
                refs(value, found)
    elif isinstance(node, list):
        for item in node:
            refs(item, found)


def main() -> None:
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    doc = json.loads(source.read_text())
    paths = {p: v for p, v in doc["paths"].items() if "/plugin-api/" in p or p.endswith("/plugin-reports")}
    needed: set[str] = set()
    refs(paths, needed)
    schemas = doc["components"]["schemas"]
    queue = list(needed)
    while queue:
        name = queue.pop()
        inner: set[str] = set()
        refs(schemas[name], inner)
        for extra in inner - needed:
            needed.add(extra)
            queue.append(extra)
    out = {"openapi": doc["openapi"], "info": doc["info"], "paths": paths,
           "components": {"schemas": {n: schemas[n] for n in sorted(needed)}}}
    OUT.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    print(f"{len(paths)} paths, {len(needed)} schemas -> {OUT}")


if __name__ == "__main__":
    main()
