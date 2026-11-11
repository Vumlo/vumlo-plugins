"""Checks the fake core's JSON against the core's generated OpenAPI (`openapi_plugin_api.json`, cut from the core's
export by `tools/refresh_openapi.py`) and the mail contract of CONTRACTS.md (the core serves it from another branch, so its
schemas are written here until the export carries them).

Strict on purpose: an object the schema describes may not carry a property the schema does not list, because the
core serializes exactly its DTO.
"""

from __future__ import annotations

import copy
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    import jsonschema
except ImportError:  # pragma: no cover - jsonschema is a dev dependency
    jsonschema = None

SPEC_PATH = Path(__file__).with_name("openapi_plugin_api.json")
API_PREFIX = "/api/v1"
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:\d\d)")

# Contract 1, `POST /plugin-api/mail`.
MAIL_PATH = f"{API_PREFIX}/plugin-api/mail"
MAIL_REQUEST = {
    "type": "object", "required": ["to", "kind", "subject", "text"], "additionalProperties": False,
    "properties": {
        "to": {"oneOf": [
            {"type": "object", "required": ["user_id"], "additionalProperties": False,
             "properties": {"user_id": {"type": "string", "format": "uuid"}}},
            {"type": "object", "required": ["course_id", "audience"], "additionalProperties": False,
             "properties": {"course_id": {"type": "string", "format": "uuid"},
                            "audience": {"enum": ["students", "teachers", "all"]}}},
        ]},
        "kind": {"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,39}$"},
        "subject": {"type": "string", "minLength": 1, "maxLength": 200},
        "text": {"type": "string", "minLength": 1, "maxLength": 20000},
        "link_path": {"type": "string", "pattern": "^/(?!/)"},
        "dedupe_key": {"type": "string", "minLength": 1, "maxLength": 100},
    },
}
MAIL_RESPONSE = {
    "type": "object", "required": ["accepted", "skipped", "skipped_reasons"], "additionalProperties": False,
    "properties": {
        "accepted": {"type": "integer", "minimum": 0}, "skipped": {"type": "integer", "minimum": 0},
        "skipped_reasons": {"type": "object", "additionalProperties": False,
                            "required": ["opted_out", "no_address", "duplicate", "disabled", "rate_limited"],
                            "properties": {k: {"type": "integer", "minimum": 0}
                                           for k in ("opted_out", "no_address", "duplicate", "disabled",
                                                     "rate_limited")}},
    },
}


@lru_cache(maxsize=1)
def spec() -> dict[str, Any]:
    return json.loads(SPEC_PATH.read_text())


def _inline(node: Any, doc: dict[str, Any], depth: int = 0) -> Any:
    if depth > 20:
        raise RecursionError("schema nests too deep")
    if isinstance(node, dict):
        if "$ref" in node:
            target = doc["components"]["schemas"][node["$ref"].rsplit("/", 1)[1]]
            return _inline(copy.deepcopy(target), doc, depth + 1)
        out = {k: _inline(v, doc, depth + 1) for k, v in node.items()}
        if "properties" in out and "additionalProperties" not in out:
            out["additionalProperties"] = False
        return out
    if isinstance(node, list):
        return [_inline(v, doc, depth + 1) for v in node]
    return node


def _checker():
    checker = jsonschema.FormatChecker()
    checker.checks("uuid")(lambda v: not isinstance(v, str) or bool(_UUID.fullmatch(v)))
    checker.checks("date-time")(lambda v: not isinstance(v, str) or bool(_TIME.fullmatch(v)))
    checker.checks("int32")(lambda v: True)
    return checker


def available() -> bool:
    return jsonschema is not None


def check(schema: dict[str, Any], value: Any, where: str) -> None:
    """Raises AssertionError naming `where` when `value` does not match `schema`."""
    resolved = _inline(schema, spec())
    errors = sorted(jsonschema.Draft202012Validator(resolved, format_checker=_checker()).iter_errors(value),
                    key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        path = "/".join(str(p) for p in first.path) or "(root)"
        raise AssertionError(f"{where}: {first.message} at {path}")


def operation(method: str, template: str) -> dict[str, Any] | None:
    return spec()["paths"].get(API_PREFIX + template, {}).get(method.lower())


def check_request(method: str, template: str, query: list[str]) -> None:
    """The route exists in the core's OpenAPI and every query parameter the plugin sent is one it declares."""
    if template == "/plugin-api/mail":
        return
    op = operation(method, template)
    if op is None:
        raise AssertionError(f"the core has no {method} {template}")
    known = {p["name"] for p in op.get("parameters", []) if p["in"] == "query"}
    for name in query:
        if name not in known:
            raise AssertionError(f"{method} {template}: the core has no query parameter {name!r} (it has {sorted(known)})")


def check_body(method: str, template: str, body: Any) -> None:
    if template == "/plugin-api/mail":
        check(MAIL_REQUEST, body, "POST /plugin-api/mail request")
        return
    schema = (operation(method, template) or {}).get("requestBody", {}).get("content", {}).get(
        "application/json", {}).get("schema")
    if schema:
        check(schema, body, f"{method} {template} request")


def check_response(method: str, template: str, status: int, body: Any) -> None:
    if template == "/plugin-api/mail" and status == 202:
        check(MAIL_RESPONSE, body, "POST /plugin-api/mail response")
        return
    op = operation(method, template)
    if op is None:
        return
    declared = op["responses"].get(str(status))
    if declared is None:
        # The guard layer, infrastructure and axum's own rejection of a malformed path id answer on every route.
        if status in (400, 429) or status >= 500:
            return
        raise AssertionError(f"{method} {template}: the core documents no {status} answer")
    schema = declared.get("content", {}).get("application/json", declared.get("content", {}).get(
        "application/problem+json", {})).get("schema")
    if schema:
        check(schema, body, f"{method} {template} -> {status}")
