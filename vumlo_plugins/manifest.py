"""The manifest a plugin publishes at `GET /manifest`, and a port of the rules the core checks it with.

Source of truth: `vumlo-core/backend/crates/plugins/src/{manifest,model,settings}.rs` and
`access/src/plugin.rs`. `validate_manifest` raises `ManifestError` for everything the core would refuse at
`plugin install`; a test loads the manifest of every plugin in this repository through it. When the core
changes a rule, change it here too.
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any

API_VERSION = "1"
SUBSCRIBABLE = (
    "submission.submitted", "grade.published", "plugin.settings.changed", "assignment.created",
    "assignment.updated", "assignment.deadline_changed", "enrollment.changed", "quiz.attempt_submitted",
    "announcement.published",
)
SETTINGS_CHANGED = "plugin.settings.changed"
SCOPES = (
    "submissions:read", "reports:write", "settings:read", "assignments:read", "roster:read", "grades:read",
    "quizzes:read", "users:contact", "announcements:read", "mail:send",
)
SLOTS = ("assignment.tab",)
ROUTE_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
ROLES = ("admin", "teacher", "student")
COURSE_ROLES = ("teacher", "assistant", "student")
COURSE_SEGMENT = "course_id"

MAX_SUBSCRIPTIONS = 10
MAX_SLOTS = 4
MAX_TITLE_CHARS = 60
MAX_PATH_CHARS = 200
MAX_ROUTES = 20
MAX_SETTINGS = 40
MAX_KEY_CHARS = 40
MAX_DESCRIPTION_CHARS = 300
MAX_STRING_CHARS = 2000
MAX_LIST_ITEMS = 50
MAX_ENUM_OPTIONS = 50


class ManifestError(ValueError):
    """The core would stop the install with this message."""


def _shown(value: Any) -> str:
    text = value if isinstance(value, str) else str(value)
    return f"`{text[:40]}…`" if len(text) > 40 else f"`{text}`"


def _ascii_upper(text: str) -> str:
    """Rust's `to_ascii_uppercase`: Python's `upper()` also turns `ſ` into `S`, so `PoſT` would pass here only."""
    return "".join(c.upper() if c.isascii() else c for c in text)


def _is_control(char: str) -> bool:
    return unicodedata.category(char) == "Cc"


def _plain_text(text: str, maximum: int) -> bool:
    return len(text) <= maximum and not any(_is_control(c) for c in text)


# ---- builders -------------------------------------------------------------------------------------------------


def setting(
    kind: str, *, default: Any = None, description: str = "", secret: bool = False, scope: str = "institution",
    enum: list[str] | None = None, minimum: float | None = None, maximum: float | None = None,
    items: str | None = None,
) -> dict[str, Any]:
    """One entry of `settings`. `kind` is string, number, boolean or array (`items`: string or number)."""
    spec: dict[str, Any] = {"type": kind}
    if description:
        spec["description"] = description
    if default is not None:
        spec["default"] = default
    if secret:
        spec["secret"] = True
    if scope != "institution":
        spec["scope"] = scope
    if enum is not None:
        spec["enum"] = list(enum)
    if minimum is not None:
        spec["minimum"] = minimum
    if maximum is not None:
        spec["maximum"] = maximum
    if items is not None:
        spec["items"] = {"type": items}
    return spec


def route(
    method: str, path: str, *, roles: list[str] | tuple[str, ...] = (), course_roles: list[str] | tuple[str, ...] = (),
    auth: str | None = None,
) -> dict[str, Any]:
    spec: dict[str, Any] = {"method": _ascii_upper(method), "path": path}
    if auth:
        spec["auth"] = auth
    if roles:
        spec["roles"] = list(roles)
    if course_roles:
        spec["course_roles"] = list(course_roles)
    return spec


def manifest(plugin_id: str, version: str, *, subscribes=(), scopes=(), settings=None, ui=(), routes=()) -> dict[str, Any]:
    return {
        "id": plugin_id, "version": version, "api": API_VERSION,
        "subscribes": list(subscribes), "scopes": list(scopes), "ui": list(ui),
        "settings": dict(settings or {}), "routes": list(routes),
    }


# ---- the validator --------------------------------------------------------------------------------------------


def _shape(ok: bool, what: str) -> None:
    if not ok:
        raise ManifestError(f"not the JSON object of the protocol ({what})")


def _str_list(value: Any, field: str) -> list[str]:
    _shape(isinstance(value, list) and all(isinstance(v, str) for v in value), f"`{field}` must be a list of strings")
    return value


def _is_slug(text: str) -> bool:
    return bool(text) and text[0].isascii() and text[0].islower() and text[0].isalpha() and len(text) <= 32 \
        and all(c.isascii() and (c.islower() or c.isdigit() or c in "_-") for c in text[1:])


def _is_key(text: str) -> bool:
    return bool(text) and text[0].isascii() and text[0].islower() and text[0].isalpha() and len(text) <= MAX_KEY_CHARS \
        and all(c.isascii() and (c.islower() or c.isdigit() or c == "_") for c in text[1:])


def is_report_key(key: str) -> bool:
    """The name of a report (ADR-0077): lowercase letters, digits, `_` and `-`, starting with a letter, <= 40."""
    return bool(re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", key))


def _is_plain_segment(segment: str) -> bool:
    return bool(segment) and segment not in (".", "..") and all(
        c.isascii() and (c.isalnum() or c in "._~-") for c in segment)


def _is_param(name: str) -> bool:
    return 0 < len(name) <= 20 and all(c.isascii() and (c.islower() or c == "_") for c in name)


def validate_manifest(raw: Any) -> dict[str, Any]:
    """Returns the manifest as the core would store it (duplicates dropped), or raises `ManifestError`."""
    _shape(isinstance(raw, dict), "a JSON object is expected")
    for field in ("id", "version", "api"):
        _shape(isinstance(raw.get(field), str), f"`{field}` must be a string")
    plugin_id, version = raw["id"], raw["version"]
    if not _is_slug(plugin_id):
        raise ManifestError(
            f"id {_shown(plugin_id)} must be lowercase letters, digits, `_` and `-`, start with a letter, "
            "and have at most 32 characters")
    if not version or len(version) > 32 or any(_is_control(c) for c in version):
        raise ManifestError("version must be 1 to 32 characters")
    if raw["api"] != API_VERSION:
        raise ManifestError(f"api {_shown(raw['api'])} is not supported; this core speaks api `{API_VERSION}`")

    subscribes_in = _str_list(raw.get("subscribes", []), "subscribes")
    if len(subscribes_in) > MAX_SUBSCRIPTIONS:
        raise ManifestError(f"subscribes lists more than {MAX_SUBSCRIPTIONS} events")
    subscribes: list[str] = []
    for event in subscribes_in:
        if event not in SUBSCRIBABLE:
            raise ManifestError(
                f"cannot subscribe to {_shown(event)}; the events a plugin may subscribe to are {', '.join(SUBSCRIBABLE)}")
        if event not in subscribes:
            subscribes.append(event)

    scopes: list[str] = []
    for text in _str_list(raw.get("scopes", []), "scopes"):
        if text not in SCOPES:
            raise ManifestError(f"scope {_shown(text)} is unknown; the scopes are {', '.join(SCOPES)}")
        if text not in scopes:
            scopes.append(text)

    ui_in = raw.get("ui", [])
    _shape(isinstance(ui_in, list), "`ui` must be a list")
    if len(ui_in) > MAX_SLOTS:
        raise ManifestError(f"ui lists more than {MAX_SLOTS} pages")
    ui = [_slot(entry) for entry in ui_in]
    for at, page in enumerate(ui):
        if any(before["slot"] == page["slot"] for before in ui[:at]):
            raise ManifestError(f"ui slot {_shown(page['slot'])} has two pages; a plugin offers one page in a slot")

    settings_in = raw.get("settings", {})
    _shape(isinstance(settings_in, dict), "`settings` must be an object")
    settings = _settings(settings_in)
    routes_in = raw.get("routes", [])
    _shape(isinstance(routes_in, list), "`routes` must be a list")
    routes = _routes(routes_in)
    return {"id": plugin_id, "version": version, "api": API_VERSION, "subscribes": subscribes, "scopes": scopes,
            "ui": ui, "settings": settings, "routes": routes}


def _slot(entry: Any) -> dict[str, str]:
    _shape(isinstance(entry, dict) and all(isinstance(entry.get(k), str) for k in ("slot", "title", "path")),
           "a ui page needs `slot`, `title` and `path` as strings")
    if entry["slot"] not in SLOTS:
        raise ManifestError(f"ui slot {_shown(entry['slot'])} is unknown; the slots are {', '.join(SLOTS)}")
    title = entry["title"].strip()
    if not title or len(title) > MAX_TITLE_CHARS or any(_is_control(c) for c in title):
        raise ManifestError(f"the title of ui slot {_shown(entry['slot'])} must be 1 to {MAX_TITLE_CHARS} characters")
    path = entry["path"]
    plain = (path.startswith("/") and not path.startswith("//") and ".." not in path and "\\" not in path
             and len(path) <= MAX_PATH_CHARS and all(0x21 <= ord(c) <= 0x7E for c in path))
    if not plain:
        raise ManifestError(
            f"the path {_shown(path)} of ui slot {_shown(entry['slot'])} must start with a single `/` "
            "and hold plain characters only")
    return {"slot": entry["slot"], "title": title, "path": path}


def _routes(raw: list[Any]) -> list[dict[str, Any]]:
    if len(raw) > MAX_ROUTES:
        raise ManifestError(f"routes lists more than {MAX_ROUTES} routes")
    found: list[dict[str, Any]] = []
    for entry in raw:
        spec = _route(entry)
        if any(before["method"] == spec["method"] and before["path"] == spec["path"] for before in found):
            raise ManifestError(f"route {spec['method']} {_shown(spec['path'])} is declared twice")
        found.append(spec)
    return found


def _route(entry: Any) -> dict[str, Any]:
    _shape(isinstance(entry, dict) and isinstance(entry.get("method"), str) and isinstance(entry.get("path"), str),
           "a route needs `method` and `path` as strings")
    _shape(entry.get("auth") is None or isinstance(entry["auth"], str), "`auth` must be a string")
    roles_in = _str_list(entry.get("roles", []), "roles")
    course_roles_in = _str_list(entry.get("course_roles", []), "course_roles")
    method = _ascii_upper(entry["method"])
    if method not in ROUTE_METHODS:
        raise ManifestError(f"route method {_shown(entry['method'])} is not one of {', '.join(ROUTE_METHODS)}")
    path = entry["path"]

    def at(what: str) -> ManifestError:
        return ManifestError(f"route {method} {_shown(path)}: {what}")

    if not path.startswith("/") or len(path) > MAX_PATH_CHARS:
        raise at("the path must start with `/` and be at most 200 characters")
    if path == "/":
        raise at("the path needs at least one segment")
    names: list[str] = []
    for segment in path.split("/")[1:]:
        if segment.startswith("{") and segment.endswith("}"):
            name = segment[1:-1]
            if _is_param(name) and name not in names:
                names.append(name)
            else:
                raise at("a `{name}` segment holds lowercase letters and `_`, once")
        elif not _is_plain_segment(segment):
            raise at("a segment holds letters, digits, `.`, `_`, `~` and `-` only")
    auth = "session" if entry.get("auth") is None else entry["auth"]
    if auth not in ("session", "none"):
        raise at(f"auth {_shown(auth)} is not session or none")
    if auth == "none":
        if method != "GET":
            raise at('a public route (`auth = "none"`) is GET only')
        if roles_in or course_roles_in:
            raise at("a public route has no roles")
        if COURSE_SEGMENT in names:
            raise at("a public route has no `{course_id}` segment")
        return {"method": method, "auth": auth, "path": path, "roles": [], "course_roles": []}
    if not roles_in:
        raise at("roles must name at least one institution role")
    roles: list[str] = []
    for text in roles_in:
        if text not in ROLES:
            raise at(f"role {_shown(text)} is not admin, teacher or student")
        if text not in roles:
            roles.append(text)
    course_roles: list[str] = []
    for text in course_roles_in:
        if text not in COURSE_ROLES:
            raise at(f"course role {_shown(text)} is not teacher, assistant or student")
        if text not in course_roles:
            course_roles.append(text)
    if course_roles and COURSE_SEGMENT not in names:
        raise at("course_roles need a `{course_id}` segment in the path")
    return {"method": method, "auth": auth, "path": path, "roles": roles, "course_roles": course_roles}


_SPEC_FIELDS = {"type", "description", "default", "secret", "scope", "enum", "minimum", "maximum", "items"}
_KINDS = ("string", "number", "boolean", "array")


def _settings(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if len(raw) > MAX_SETTINGS:
        raise ManifestError(f"settings lists more than {MAX_SETTINGS} settings")
    out: dict[str, dict[str, Any]] = {}
    for key, spec in raw.items():
        if not _is_key(key):
            raise ManifestError(
                f"setting {_shown(key)} must be lowercase letters, digits and `_`, start with a letter, "
                "and have at most 40 characters")
        out[key] = _setting_spec(key, spec)
    return out


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _setting_spec(key: str, spec: Any) -> dict[str, Any]:
    def broken(why: str) -> ManifestError:
        return ManifestError(f"setting `{key}` is not valid ({why})")

    def bad(why: str) -> ManifestError:
        return ManifestError(f"setting `{key}`: {why}")

    if not isinstance(spec, dict):
        raise broken("invalid type: expected a map")
    unknown = sorted(set(spec) - _SPEC_FIELDS)
    if unknown:
        raise broken(f"unknown field `{unknown[0]}`")
    kind = spec.get("type")
    if kind not in _KINDS:
        raise broken("`type` must be string, number, boolean or array")
    description = spec.get("description", "")
    secret = spec.get("secret", False)
    scope = spec.get("scope", "institution")
    if not isinstance(description, str) or not isinstance(secret, bool) or scope not in ("institution", "course"):
        raise broken("`description` is text, `secret` a boolean, `scope` institution or course")
    options, minimum, maximum = spec.get("enum"), spec.get("minimum"), spec.get("maximum")
    items, default = spec.get("items"), spec.get("default")
    if options is not None and not (isinstance(options, list) and all(isinstance(o, str) for o in options)):
        raise broken("`enum` is a list of strings")
    if any(v is not None and not _number(v) for v in (minimum, maximum)):
        raise broken("`minimum` and `maximum` are numbers")
    if items is not None:
        if not (isinstance(items, dict) and set(items) == {"type"} and items["type"] in ("string", "number")):
            raise broken("`items` is {\"type\": \"string\" or \"number\"}")

    # SettingSpec::validate
    if not _plain_text(description, MAX_DESCRIPTION_CHARS):
        raise bad("the description must be plain text of at most 300 characters")
    if (minimum is not None or maximum is not None) and kind != "number":
        raise bad("`minimum` and `maximum` are for numbers")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise bad("`minimum` is above `maximum`")
    if any(isinstance(v, float) and not math.isfinite(v) for v in (minimum, maximum)):
        raise bad("`minimum` and `maximum` must be finite")
    if options is not None:
        if kind != "string":
            raise bad("`enum` is for strings")
        seen: list[str] = []
        for option in options:
            if not option or not _plain_text(option, 100) or option in seen:
                raise bad("`enum` lists distinct plain strings of 1 to 100 characters")
            seen.append(option)
        if not options or len(options) > MAX_ENUM_OPTIONS:
            raise bad("`enum` lists 1 to 50 values")
    if (items is not None) != (kind == "array"):
        raise bad("`items` goes with the type `array`, and only with it")
    if secret and (kind != "string" or options is not None):
        raise bad("only a string without `enum` can be a secret")
    if secret and default is not None:
        raise bad("a secret has no default")
    if default is not None:
        why = check_setting_value(spec, default)
        if why:
            raise ManifestError(f"setting `{key}`: its default {why}")
    out = {"type": kind}
    out.update({k: v for k, v in spec.items() if k != "type" and v is not None})
    return out


def check_setting_value(spec: dict[str, Any], value: Any) -> str | None:
    """`None` when `value` meets `spec`, else why not, completing "the value ..." (SettingSpec::check)."""
    kind = spec["type"]
    if kind == "string" and isinstance(value, str):
        if not _plain_text(value, MAX_STRING_CHARS):
            return f"must be plain text of at most {MAX_STRING_CHARS} characters"
        if spec.get("secret") and not value:
            return "must not be empty (unset it instead)"
        options = spec.get("enum")
        if options is not None and value not in options:
            return f"must be one of: {', '.join(options)}"
        return None
    if kind == "number" and _number(value):
        low, high = spec.get("minimum"), spec.get("maximum")
        if low is not None and value < low:
            return f"must be at least {low}"
        if high is not None and value > high:
            return f"must be at most {high}"
        return None
    if kind == "boolean" and isinstance(value, bool):
        return None
    if kind == "array" and isinstance(value, list):
        if len(value) > MAX_LIST_ITEMS:
            return f"must have at most {MAX_LIST_ITEMS} entries"
        entry = (spec.get("items") or {}).get("type", "string")
        for item in value:
            if entry == "string" and isinstance(item, str) and _plain_text(item, MAX_STRING_CHARS):
                continue
            if entry == "number" and _number(item):
                continue
            return f"must be a list of {entry}s"
        return None
    names = {"string": "string from the `enum`" if spec.get("enum") is not None else "string",
             "number": "number", "boolean": "boolean", "array": "list"}
    return f"must be a {names[kind]}"
