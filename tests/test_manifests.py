"""Every plugin's manifest, checked with the rules the core applies at `plugin install`.

The source of truth is `backend/crates/plugins/src/manifest.rs` of vumlo-core (and the lists in `model.rs` and
`access/src/plugin.rs`). This is a port of those rules, so a plugin the core would refuse fails here, before a release
(pdf-optimizer once declared `GET /`, and the install of a whole set stopped at it). When the core changes a rule, change
it here too. Plugins are found by their `app.py`, so a new plugin is covered without touching this file.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PLUGINS = sorted(p.parent.name for p in ROOT.glob("*/app.py"))

SUBSCRIBABLE = {
    "submission.submitted", "grade.published", "plugin.settings.changed", "assignment.created", "assignment.updated",
    "assignment.deadline_changed", "enrollment.changed", "quiz.attempt_submitted", "announcement.published",
}
SCOPES = {
    "submissions:read", "reports:write", "settings:read", "assignments:read", "roster:read", "grades:read",
    "quizzes:read", "users:contact", "announcements:read",
}
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
ROLES = {"admin", "teacher", "student"}
COURSE_ROLES = {"teacher", "assistant", "student"}
SLOTS = {"assignment_tab"}
MAX_ROUTES, MAX_SUBSCRIPTIONS, MAX_SLOTS, MAX_PATH = 20, 10, 4, 200


def load_manifest(name: str) -> dict:
    spec = importlib.util.spec_from_file_location(f"plugin_{name.replace('-', '_')}_app", ROOT / name / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from fastapi.testclient import TestClient

    answer = TestClient(module.app).get("/manifest")
    assert answer.status_code == 200, f"{name}: GET /manifest answered {answer.status_code}"
    return answer.json()


def plain_segment(segment: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9._~-]+", segment)) and segment not in (".", "..")


def problems(m: dict) -> list[str]:
    """Everything the core would refuse in this manifest (empty: it installs)."""
    out: list[str] = []
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", str(m.get("id", ""))):
        out.append(f"id {m.get('id')!r}: lowercase letters, digits, _ and -, a letter first, 32 characters at most")
    version = m.get("version")
    if not isinstance(version, str) or not 1 <= len(version) <= 32:
        out.append("version must be 1 to 32 characters")
    if m.get("api") != "1":
        out.append(f"api {m.get('api')!r} is not '1'")
    subscribes = m.get("subscribes", [])
    if len(subscribes) > MAX_SUBSCRIPTIONS:
        out.append(f"more than {MAX_SUBSCRIPTIONS} events")
    out += [f"cannot subscribe to {e!r}" for e in subscribes if e not in SUBSCRIBABLE]
    out += [f"scope {s!r} is unknown" for s in m.get("scopes", []) if s not in SCOPES]
    ui = m.get("ui", [])
    if len(ui) > MAX_SLOTS:
        out.append(f"more than {MAX_SLOTS} pages")
    slots = [u.get("slot") for u in ui]
    out += [f"ui slot {s!r} is unknown" for s in slots if s not in SLOTS]
    if len(set(slots)) != len(slots):
        out.append("a plugin offers one page in a slot")
    routes = m.get("routes", [])
    if len(routes) > MAX_ROUTES:
        out.append(f"more than {MAX_ROUTES} routes")
    seen = set()
    for r in routes:
        method, path = str(r.get("method", "")).upper(), r.get("path", "")
        where = f"route {method} {path!r}"
        if method not in METHODS:
            out.append(f"{where}: method is not one of {sorted(METHODS)}")
        if not path.startswith("/") or len(path) > MAX_PATH:
            out.append(f"{where}: the path must start with / and be at most {MAX_PATH} characters")
            continue
        if path == "/":
            out.append(f"{where}: the path needs at least one segment")
            continue
        names: list[str] = []
        for segment in path.split("/")[1:]:
            if segment.startswith("{") and segment.endswith("}"):
                name = segment[1:-1]
                if not re.fullmatch(r"[a-z_]{1,20}", name) or name in names:
                    out.append(f"{where}: a {{name}} segment holds lowercase letters and _, once")
                names.append(name)
            elif not plain_segment(segment):
                out.append(f"{where}: a segment holds letters, digits, . _ ~ and - only")
        if (method, path) in seen:
            out.append(f"{where} is declared twice")
        seen.add((method, path))
        auth = r.get("auth", "session")
        if auth not in ("session", "none"):
            out.append(f"{where}: auth {auth!r} is not session or none")
        elif auth == "none":
            if method != "GET":
                out.append(f"{where}: a public route is GET only")
            if r.get("roles") or r.get("course_roles"):
                out.append(f"{where}: a public route has no roles")
            if "course_id" in names:
                out.append(f"{where}: a public route has no {{course_id}} segment")
        else:
            roles = r.get("roles", [])
            if not roles:
                out.append(f"{where}: roles must name at least one institution role")
            out += [f"{where}: role {x!r} is not admin, teacher or student" for x in roles if x not in ROLES]
            out += [f"{where}: course role {x!r} is not teacher, assistant or student" for x in r.get("course_roles", []) if x not in COURSE_ROLES]
            if r.get("course_roles") and "course_id" not in names:
                out.append(f"{where}: course_roles need a {{course_id}} segment")
    return out


def test_there_are_plugins():
    assert len(PLUGINS) >= 14, PLUGINS


@pytest.mark.parametrize("name", PLUGINS)
def test_the_core_accepts_the_manifest(name):
    manifest = load_manifest(name)
    assert manifest["id"] == name, "the folder and the id are the same name (it is also the compose service plugin-<name>)"
    assert problems(manifest) == []


def test_the_checks_themselves_refuse_what_the_core_refuses():
    base = {"id": "x", "version": "1", "api": "1", "routes": []}
    assert problems(base) == []
    bad = lambda **kw: problems({**base, **kw})
    assert any("at least one segment" in p for p in bad(routes=[{"method": "GET", "path": "/", "roles": ["teacher"]}]))
    assert any("declared twice" in p for p in bad(routes=[{"method": "GET", "path": "/a", "roles": ["teacher"]}] * 2))
    assert any("public route is GET only" in p for p in bad(routes=[{"method": "POST", "path": "/a", "auth": "none"}]))
    assert any("roles must name" in p for p in bad(routes=[{"method": "GET", "path": "/a"}]))
    assert any("course_roles need" in p for p in bad(routes=[{"method": "GET", "path": "/a", "roles": ["teacher"], "course_roles": ["teacher"]}]))
    assert any("scope" in p for p in bad(scopes=["grades:write"]))
    assert any("cannot subscribe" in p for p in bad(subscribes=["nope"]))
    assert any("api" in p for p in bad(api="2"))
    assert bad(routes=[{"method": "GET", "path": "/feed/{name}.ics", "auth": "none"}]) != [], "{name} must be the whole segment"
    assert bad(routes=[{"method": "GET", "path": "/c/{course_id}/x", "roles": ["teacher"], "course_roles": ["assistant"]}]) == []
