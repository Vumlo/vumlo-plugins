"""`FakeCore`: an in-process ASGI stand-in for the core, enough for a plugin's tests to run offline.

    fake = FakeCore()
    course = fake.add_course()
    student = fake.add_user(role="student"); fake.enroll(course, student)
    work = fake.add_assignment(course); sub = fake.add_submission(work, student, text="hello world")
    fake.attach(plugin)                                    # client, state and secret of the plugin now point here
    await fake.deliver(plugin, "submission.submitted", fake.submission_event(sub))
    assert fake.report("submission", sub, "text-metrics")["summary"] == "2 words"

It serves `/api/v1/plugin-api/*` with the token, scope, course-grant, pagination and error rules of the core
(401/403/404/410/422/429/503), keeps reports and mail, and signs events and proxied calls like the core does
(`deliver`, `call`, `run_job`). With `validate=True` (the default when `jsonschema` is installed) every request and
answer is checked against the core's generated OpenAPI (`contract.py`), so the fake cannot drift from the core.
"""

from __future__ import annotations

import base64
import json
import math
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .. import clock
from ..manifest import check_setting_value, is_report_key
from ..plugin import ANSWER_TYPES, Plugin
from ..state import State
from . import contract
from .signed import signed_call_headers, signed_event

API = "/api/v1"
KEY_STRIPPED = {"correct", "answer", "tolerance", "accepted", "explanation", "partial_credit", "grading_notes"}
MAX_REPORTS_PER_SUBJECT = 20
REPORT_DATA_BYTES = 16 * 1024
KIND_RE = re.compile(r"[a-z][a-z0-9_-]{0,39}")
PLAIN = re.compile(r"[A-Za-z0-9._~-]+")


class Problem(Exception):
    def __init__(self, status: int, detail: str, headers: dict[str, str] | None = None,
                 errors: list[dict[str, str]] | None = None):
        super().__init__(detail)
        self.status, self.detail, self.headers, self.errors = status, detail, headers or {}, errors

    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"type": "about:blank", "title": self.detail, "status": self.status,
                                "detail": self.detail}
        if self.errors:
            body["errors"] = self.errors
        return body


@dataclass
class Call:
    method: str
    template: str
    query: list[str]
    status: int = 0


@dataclass
class _Failure:
    method: str
    contains: str
    status: int
    retry_after: float | None
    times: int


def _pick(obj: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    return {k: obj[k] for k in names if k in obj and not (k == "fields" and obj[k] is None)}


ASSIGNMENT = ("id", "course_id", "kind", "title", "due_at", "max_points", "fields", "updated_at")
GRADE = ("course_id", "assignment_id", "student_id", "score", "max_points", "published_by", "published_at")
MEMBER = ("user_id", "name", "surname", "role", "source")
SUBMISSION_ITEM = ("id", "student_id", "attempt", "submitted_at", "is_late", "graded_at")
SUBMISSION = ("id", "course_id", "assignment_id", "student_id", "attempt", "content", "submitted_at")
ATTEMPT = ("id", "student_id", "attempt", "scores", "started_at", "submitted_at")
ANNOUNCEMENT = ("id", "course_id", "author_id", "title", "body", "created_at", "updated_at")
DELETION = ("id", "course_id", "deleted_at")


def _strip_key(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip_key(v) for k, v in node.items() if k not in KEY_STRIPPED}
    if isinstance(node, list):
        return [_strip_key(v) for v in node]
    return node


def _ref(thing: Any) -> str:
    return str(thing["id"]) if isinstance(thing, dict) else str(thing)


def _time(value: datetime | str | None, default: datetime | None = None) -> str | None:
    if value is None:
        return clock.iso(default) if default is not None else None
    return clock.iso(clock.parse_time(value) if isinstance(value, str) else value)


class FakeCore:
    def __init__(self, *, plugin_id: str = "test-plugin", token: str = "vplg_" + "T" * 43,
                 secret: str = "fake-core-webhook-secret", validate: bool | None = None):
        self.plugin_id = plugin_id
        self.token = token
        self.secret = secret
        self.validate = contract.available() if validate is None else validate
        self.scopes: set[str] = {"submissions:read", "reports:write", "settings:read", "assignments:read",
                                 "roster:read", "grades:read", "quizzes:read", "users:contact",
                                 "announcements:read", "mail:send"}
        self.granted_courses: set[str] | None = None  # None: all
        self.calls: list[Call] = []
        self.slept: list[float] = []
        self.setting_specs: dict[str, dict[str, Any]] = {}
        self.tombstone_days = 30
        self.mail_configured = True
        # The names of `[mail]` in the core: calls (not messages) per plugin per hour, messages per person per day,
        # people per call.
        self.mail_limits = {"plugin_hourly_calls": 500, "recipient_daily": 20, "max_audience": 2000}
        self._mail_calls: list[datetime] = []
        self.mail_dedupe_days = 30
        self.teacher_audience = ("teacher", "assistant")
        self.mails: list[dict[str, Any]] = []
        self.opt_outs: set[tuple[str, str]] = set()  # (user id, kind)
        self._dedupe: dict[tuple[str, str], datetime] = {}
        self._failures: list[_Failure] = []
        self._counter = 0
        self.users: dict[str, dict[str, Any]] = {}
        self.courses: dict[str, dict[str, Any]] = {}
        self.members: list[dict[str, Any]] = []
        self.assignments: dict[str, dict[str, Any]] = {}
        self.submissions: dict[str, dict[str, Any]] = {}
        self.grades: list[dict[str, Any]] = []
        self.quizzes: dict[str, dict[str, Any]] = {}
        self.attempts: list[dict[str, Any]] = []
        self.announcements: dict[str, dict[str, Any]] = {}
        self.tombstones: list[dict[str, Any]] = []
        self.institution_values: dict[str, Any] = {}
        self.course_values: dict[str, dict[str, Any]] = {}
        self._reports: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.admin = self.add_user("Ada", "Admin", role="admin")
        self.app = self._build_app()

    # ---- ids and seed data ------------------------------------------------------------------------------------

    def _id(self) -> str:
        self._counter += 1
        return f"00000000-0000-4000-8000-{self._counter:012d}"

    def add_user(self, name: str = "Test", surname: str = "User", *, role: str = "student", address: bool = True,
                 status: str = "active") -> dict[str, Any]:
        user = {"id": self._id(), "name": name, "surname": surname, "role": role, "has_address": address,
                "status": status}
        self.users[user["id"]] = user
        return user

    def add_course(self, title: str = "Course") -> dict[str, Any]:
        course = {"id": self._id(), "title": title}
        self.courses[course["id"]] = course
        return course

    def enroll(self, course: Any, user: Any, role: str = "student", source: str = "direct") -> None:
        user_id, course_id = _ref(user), _ref(course)
        self.members = [m for m in self.members if not (m["course_id"] == course_id and m["user_id"] == user_id)]
        person = self.users[user_id]
        self.members.append({"course_id": course_id, "user_id": user_id, "name": person["name"],
                             "surname": person["surname"], "role": role, "source": source})

    def unenroll(self, course: Any, user: Any) -> None:
        self.members = [m for m in self.members if not (m["course_id"] == _ref(course) and m["user_id"] == _ref(user))]

    def add_assignment(self, course: Any, *, kind: str = "text", title: str = "Assignment",
                       due_at: datetime | str | None = None, max_points: Any = "100",
                       fields: list[dict[str, Any]] | None = None, published: bool = True,
                       updated_at: datetime | str | None = None) -> dict[str, Any]:
        item = {"id": self._id(), "course_id": _ref(course), "kind": kind, "title": title, "due_at": _time(due_at),
                "max_points": str(Decimal(str(max_points))), "fields": fields,
                "updated_at": _time(updated_at, clock.now()), "published": published}
        self.assignments[item["id"]] = item
        return item

    def change_assignment(self, assignment: Any, **changes: Any) -> dict[str, Any]:
        """Edit an assignment (e.g. `due_at=`, `title=`, `published=`); its `updated_at` moves to now."""
        item = self.assignments[_ref(assignment)]
        if "due_at" in changes:
            changes["due_at"] = _time(changes["due_at"])
        item.update(changes)
        item["updated_at"] = clock.iso(clock.now())
        return item

    def delete_assignment(self, assignment: Any) -> None:
        item = self.assignments.pop(_ref(assignment))
        if item["published"]:
            self.tombstones.append({"kind": "assignment", "id": item["id"], "course_id": item["course_id"],
                                    "deleted_at": clock.iso(clock.now())})

    def add_quiz(self, assignment: Any, config: dict[str, Any]) -> dict[str, Any]:
        item = self.assignments[_ref(assignment)]
        item["kind"] = "quiz"
        self.quizzes[item["id"]] = config
        return config

    def add_submission(self, assignment: Any, student: Any, *, text: str | None = None,
                       content: dict[str, Any] | None = None, attempt: int = 1,
                       submitted_at: datetime | str | None = None, is_late: bool | None = None) -> dict[str, Any]:
        work = self.assignments[_ref(assignment)]
        if content is None:
            paragraphs = [{"type": "paragraph", "content": [{"type": "text", "text": p}]}
                          for p in (text or "").split("\n\n")]
            content = {"type": "doc", "content": paragraphs}
        when = _time(submitted_at, clock.now())
        if is_late is None:
            is_late = bool(work["due_at"]) and clock.parse_time(when) > clock.parse_time(work["due_at"])
        sub = {"id": self._id(), "course_id": work["course_id"], "assignment_id": work["id"],
               "student_id": _ref(student), "attempt": attempt, "content": content, "submitted_at": when,
               "is_late": is_late}
        self.submissions[sub["id"]] = sub
        return sub

    def add_attempt(self, assignment: Any, student: Any, scores: dict[str, Any], *, attempt: int = 1,
                    started_at: datetime | str | None = None,
                    submitted_at: datetime | str | None = None) -> dict[str, Any]:
        submitted = _time(submitted_at, clock.now())
        row = {"id": self._id(), "assignment_id": _ref(assignment), "student_id": _ref(student), "attempt": attempt,
               "scores": {k: str(Decimal(str(v))) for k, v in scores.items()},
               "started_at": _time(started_at) or submitted, "submitted_at": submitted}
        self.attempts.append(row)
        return row

    def publish_grade(self, assignment: Any, student: Any, score: Any | None, *,
                      published_at: datetime | str | None = None, published_by: Any = None) -> dict[str, Any]:
        work = self.assignments[_ref(assignment)]
        self.grades = [g for g in self.grades if not (g["assignment_id"] == work["id"]
                                                      and g["student_id"] == _ref(student))]
        grade = {"course_id": work["course_id"], "assignment_id": work["id"], "student_id": _ref(student),
                 "score": None if score is None else str(Decimal(str(score))), "max_points": work["max_points"],
                 "published_by": None if published_by is None else _ref(published_by),
                 "published_at": _time(published_at, clock.now())}
        self.grades.append(grade)
        return grade

    def add_announcement(self, course: Any, title: str, body: str, author: Any = None) -> dict[str, Any]:
        now = clock.iso(clock.now())
        item = {"id": self._id(), "course_id": _ref(course), "author_id": _ref(author or self.admin), "title": title,
                "body": body, "created_at": now, "updated_at": now}
        self.announcements[item["id"]] = item
        return item

    def set_setting(self, key: str, value: Any, course: Any = None) -> None:
        spec = self.setting_specs.get(key)
        if spec is None:
            raise KeyError(f"the plugin declares no setting {key!r}")
        if (course is not None) != (spec.get("scope") == "course"):
            raise ValueError(f"{key} is a {spec.get('scope', 'institution')} setting")
        why = check_setting_value(spec, value)
        if why:
            raise ValueError(f"the value of {key} {why}")
        if course is None:
            self.institution_values[key] = value
        else:
            self.course_values.setdefault(_ref(course), {})[key] = value

    def opt_out(self, user: Any, kind: str) -> None:
        self.opt_outs.add((_ref(user), kind))

    def grant(self, *, scopes: set[str] | list[str] | None = None, courses: list[Any] | None = ...) -> None:  # type: ignore[assignment]
        """What an administrator granted: scopes, and `courses=None` for all or a list of courses."""
        if scopes is not None:
            self.scopes = set(scopes)
        if courses is not ...:
            self.granted_courses = None if courses is None else {_ref(c) for c in courses}

    def fail(self, method: str, path_contains: str, status: int, *, times: int = 1,
             retry_after: float | None = None) -> None:
        """The next `times` calls to a matching route fail with `status` (and a `Retry-After`), to test retries."""
        self._failures.append(_Failure(method.upper(), path_contains, status, retry_after, times))

    # ---- what a test reads back -------------------------------------------------------------------------------

    def report(self, kind: str, subject: Any, key: str) -> dict[str, Any] | None:
        return self._reports.get((kind, _ref(subject), key))

    def reports_for(self, kind: str, subject: Any) -> dict[str, dict[str, Any]]:
        return {k: v for (kd, sid, k), v in self._reports.items() if kd == kind and sid == _ref(subject)}

    def mails_to(self, user: Any) -> list[dict[str, Any]]:
        return [m for m in self.mails if m["to_user_id"] == _ref(user)]

    @staticmethod
    def submission_event(sub: dict[str, Any]) -> dict[str, Any]:
        return {k: sub[k] for k in ("assignment_id", "student_id", "course_id")} | {"submission_id": sub["id"]}

    # ---- the plugin API ---------------------------------------------------------------------------------------

    def _build_app(self) -> Starlette:
        r = []

        def add(method: str, template: str, scope: str | None, fn, early=None):
            r.append(Route(template, self._wrap(method, template, scope, fn, early), methods=[method]))

        p = "/plugin-api"
        add("GET", f"{p}/assignments", "assignments:read", self._assignments)
        add("GET", f"{p}/assignments/deleted", "assignments:read", self._deleted_assignments)
        add("GET", f"{p}/assignments/{{id}}", "assignments:read", self._assignment)
        add("GET", f"{p}/assignments/{{id}}/submissions", "submissions:read", self._assignment_submissions)
        add("GET", f"{p}/assignments/{{id}}/quiz", "quizzes:read", self._quiz)
        add("GET", f"{p}/assignments/{{id}}/attempts", "quizzes:read", self._attempts)
        add("GET", f"{p}/submissions/{{id}}", "submissions:read", self._submission)
        add("GET", f"{p}/submissions/{{id}}/fields", "assignments:read", self._fields)
        add("GET", f"{p}/courses/{{id}}/members", "roster:read", self._members)
        add("GET", f"{p}/grades", "grades:read", self._grades)
        add("GET", f"{p}/announcements", "announcements:read", self._announcements)
        add("GET", f"{p}/announcements/deleted", "announcements:read", self._deleted_announcements)
        add("GET", f"{p}/settings", "settings:read", self._settings)
        for kind in ("submissions", "assignments", "courses"):
            add("PUT", f"{p}/{kind}/{{id}}/reports/{{key}}", "reports:write", self._put_report, self._early_report)
            add("DELETE", f"{p}/{kind}/{{id}}/reports/{{key}}", "reports:write", self._delete_report,
                self._early_path_id)
            add("GET", f"/{kind}/{{id}}/plugin-reports", None, self._staff_reports)
        add("POST", f"{p}/mail", "mail:send", self._mail, self._early_mail)
        return Starlette(routes=[Route(API + route.path, route.endpoint, methods=list(route.methods - {"HEAD"}))
                                 for route in r])

    def _wrap(self, method: str, template: str, scope: str | None, fn, early=None):
        """The order of the core's extractors: the token (401), then the ids of the path (400) and the body (422)
        (`early`), and only then the scope (403) and what the handler looks up (404, 403)."""
        async def endpoint(request: Request) -> Response:
            call = Call(method, template, list(request.query_params.keys()))
            self.calls.append(call)
            try:
                if self.validate:
                    contract.check_request(method, template, call.query)
                self._injected(method, request.url.path)
                if scope is not None:
                    self._authenticate(request)
                parsed = await self._json(request, method)
                if scope is not None and early is not None:
                    early(request, parsed)
                if scope is not None and scope not in self.scopes:
                    raise Problem(403, f"the plugin was not granted `{scope}`")
                if self.validate and parsed is not None:
                    contract.check_body(method, template, parsed)
                status, body, headers = fn(request, parsed, **request.path_params)
            except Problem as problem:
                status, body, headers = problem.status, problem.body(), problem.headers
            call.status = status
            if self.validate:
                contract.check_response(method, template, status, body)
            if status == 204:
                return Response(status_code=204)
            media = "application/problem+json" if status >= 400 else "application/json"
            return JSONResponse(body, status_code=status, headers=headers, media_type=media)
        return endpoint

    @staticmethod
    async def _json(request: Request, method: str) -> Any:
        if method not in ("PUT", "POST"):
            return None
        try:
            return json.loads(await request.body())
        except ValueError:
            raise Problem(422, "the body is not JSON") from None

    def _injected(self, method: str, path: str) -> None:
        for failure in self._failures:
            if failure.times > 0 and failure.method == method and failure.contains in path:
                failure.times -= 1
                headers = {} if failure.retry_after is None else {"retry-after": str(failure.retry_after)}
                raise Problem(failure.status, "injected failure", headers)

    def _authenticate(self, request: Request) -> None:
        if request.headers.get("authorization") != f"Bearer {self.token}":
            raise Problem(401, "no token, or one that is unknown or disabled")

    def _uuid(self, text: str, name: str) -> str:
        """A malformed id in the path is axum's `400` (the name `id`); in a query or body it is the core's `422`."""
        try:
            return str(uuid.UUID(text))
        except ValueError:
            raise Problem(400 if name == "id" else 422, f"{name} is not a UUID") from None

    def _course(self, course_id: str, name: str = "course_id") -> dict[str, Any]:
        course = self.courses.get(self._uuid(course_id, name))
        if course is None:
            raise Problem(404, "no such course")
        return course

    def _reach(self, course_id: str) -> None:
        if self.granted_courses is not None and course_id not in self.granted_courses:
            raise Problem(403, "the plugin does not reach this course")

    def _reaches(self, course_id: str) -> bool:
        return self.granted_courses is None or course_id in self.granted_courses

    def _assignment_of(self, assignment_id: str) -> dict[str, Any]:
        item = self.assignments.get(self._uuid(assignment_id, "id"))
        if item is None or not item["published"]:
            raise Problem(404, "no such assignment, or none a student could see")
        self._reach(item["course_id"])
        return item

    def _page(self, request: Request, items: list[dict[str, Any]], fields: tuple[str, ...]):
        params = request.query_params
        try:
            limit = int(params.get("limit", "50"))
        except ValueError:
            raise Problem(422, "limit is not a number") from None
        if not 1 <= limit <= 100:
            raise Problem(422, "limit is 1 to 100")
        offset = 0
        if "cursor" in params:
            if not 1 <= len(params["cursor"]) <= 100:
                raise Problem(422, "cursor is 1 to 100 characters")
            try:
                tag, _, number = base64.urlsafe_b64decode(params["cursor"]).decode().partition(":")
                offset = int(number)
                assert tag == "o" and offset >= 0
            except (ValueError, AssertionError):
                raise Problem(422, "the cursor is not valid") from None
        window = items[offset:offset + limit]
        more = offset + limit < len(items)
        cursor = base64.urlsafe_b64encode(f"o:{offset + limit}".encode()).decode().rstrip("=") if more else None
        if cursor:
            cursor = cursor + "=" * (-len(cursor) % 4)
        return 200, {"items": [_pick(i, fields) for i in window], "next_cursor": cursor}, {}

    @staticmethod
    def _since(request: Request, name: str = "updated_since") -> datetime | None:
        text = request.query_params.get(name)
        if text is None:
            return None
        try:
            # No repair of a `+` that arrived as a space: the core's query parser would not repair it either.
            if not re.search(r"T.*(Z|[+-]\d\d:\d\d)$", text):  # RFC 3339 has a time and an offset
                raise ValueError(text)
            return clock.parse_time(text)
        except ValueError:
            raise Problem(422, f"{name} is not an RFC 3339 time") from None

    def _scoped(self, request: Request) -> str | None:
        course = request.query_params.get("course_id")
        if course is None:
            return None
        course_id = self._course(course)["id"]
        self._reach(course_id)
        return course_id

    # -- reads --

    def _assignments(self, request, _body):
        course_id, since = self._scoped(request), self._since(request)
        rows = [a for a in self.assignments.values() if a["published"]
                and (course_id == a["course_id"] if course_id else self._reaches(a["course_id"]))
                and (since is None or clock.parse_time(a["updated_at"]) >= since)]
        rows.sort(key=lambda a: (a["updated_at"], a["id"]))
        return self._page(request, rows, ASSIGNMENT)

    def _assignment(self, request, _body, id):
        return 200, _pick(self._assignment_of(id), ASSIGNMENT), {}

    def _tombstones(self, request, kind: str):
        since = self._since(request, "deleted_since")
        if since is None:
            raise Problem(422, "deleted_since is required")
        course = request.query_params.get("course_id")
        course_id = None
        if course is not None:
            course_id = self._uuid(course, "course_id")
            self._reach(course_id)
        if since < clock.now() - timedelta(days=self.tombstone_days):
            raise Problem(410, "deleted_since is older than the core keeps; read the list in full")
        rows = [t for t in self.tombstones if t["kind"] == kind and clock.parse_time(t["deleted_at"]) >= since
                and (course_id == t["course_id"] if course_id else self._reaches(t["course_id"]))]
        rows.sort(key=lambda t: (t["deleted_at"], t["id"]))
        return self._page(request, rows, DELETION)

    def _deleted_assignments(self, request, _body):
        return self._tombstones(request, "assignment")

    def _deleted_announcements(self, request, _body):
        return self._tombstones(request, "announcement")

    def _announcements(self, request, _body):
        course_id, since = self._scoped(request), self._since(request)
        rows = [a for a in self.announcements.values()
                if (course_id == a["course_id"] if course_id else self._reaches(a["course_id"]))
                and (since is None or clock.parse_time(a["updated_at"]) >= since)]
        rows.sort(key=lambda a: (a["updated_at"], a["id"]))
        return self._page(request, rows, ANNOUNCEMENT)

    def _submission(self, request, _body, id):
        sub = self.submissions.get(self._uuid(id, "id"))
        if sub is None:
            raise Problem(404, "no such submitted attempt")
        self._reach(sub["course_id"])
        return 200, _pick(sub, SUBMISSION), {}

    def _fields(self, request, _body, id):
        sub = self.submissions.get(self._uuid(id, "id"))
        if sub is None or self.assignments[sub["assignment_id"]]["kind"] != "form":
            raise Problem(404, "no such submitted attempt, or not at a form")
        self._reach(sub["course_id"])
        answers = (sub["content"] or {}).get("answers", {})
        return 200, {"submission_id": sub["id"], "course_id": sub["course_id"], "assignment_id": sub["assignment_id"],
                     "student_id": sub["student_id"], "answers": answers}, {}

    def _assignment_submissions(self, request, _body, id):
        work = self._assignment_of(id)
        since = self._since(request)
        graded = {g["student_id"]: g["published_at"] for g in self.grades if g["assignment_id"] == work["id"]}
        rows = [dict(s, graded_at=graded.get(s["student_id"])) for s in self.submissions.values()
                if s["assignment_id"] == work["id"]
                and (since is None or clock.parse_time(s["submitted_at"]) >= since)]
        rows.sort(key=lambda s: (s["submitted_at"], s["id"]))
        return self._page(request, rows, SUBMISSION_ITEM)

    def _members(self, request, _body, id):
        course_id = self._course(id, "id")["id"]
        self._reach(course_id)
        role = request.query_params.get("role")
        if role is not None and role not in ("teacher", "assistant", "student"):
            raise Problem(422, "role is teacher, assistant or student")
        rows = [m for m in self.members if m["course_id"] == course_id and (role is None or m["role"] == role)]
        rows.sort(key=lambda m: m["user_id"])
        return self._page(request, rows, MEMBER)

    def _grades(self, request, _body):
        course_id, since = self._scoped(request), self._since(request)
        assignment = request.query_params.get("assignment_id")
        if assignment is not None and self._uuid(assignment, "assignment_id") not in self.assignments:
            raise Problem(404, "no such assignment")
        rows = [g for g in self.grades
                if (course_id == g["course_id"] if course_id else self._reaches(g["course_id"]))
                and (assignment is None or g["assignment_id"] == assignment)
                and (since is None or clock.parse_time(g["published_at"]) >= since)]
        rows.sort(key=lambda g: (g["published_at"], g["student_id"], g["assignment_id"]))
        return self._page(request, rows, GRADE)

    def _quiz(self, request, _body, id):
        work = self._assignment_of(id)
        if work["kind"] != "quiz":
            raise Problem(404, "no such quiz")
        return 200, {"assignment_id": work["id"], "course_id": work["course_id"], "max_points": work["max_points"],
                     "config": _strip_key(self.quizzes.get(work["id"], {}))}, {}

    def _attempts(self, request, _body, id):
        work = self._assignment_of(id)
        since = self._since(request)
        rows = [a for a in self.attempts if a["assignment_id"] == work["id"]
                and (since is None or clock.parse_time(a["submitted_at"]) >= since)]
        rows.sort(key=lambda a: (a["submitted_at"], a["id"]))
        return self._page(request, rows, ATTEMPT)

    def _settings(self, request, _body):
        course = request.query_params.get("course_id")
        values = {k: s["default"] for k, s in self.setting_specs.items() if "default" in s}
        values.update(self.institution_values)
        if course is not None:
            course_id = self._course(course)["id"]
            values.update(self.course_values.get(course_id, {}))
        return 200, {"values": values}, {"cache-control": "no-store"}

    # -- reports --

    @staticmethod
    def _early_path_id(request, _body) -> None:
        FakeCore._uuid_of_path(request.path_params["id"])

    @staticmethod
    def _uuid_of_path(text: str) -> str:
        try:
            return str(uuid.UUID(text))
        except ValueError:
            raise Problem(400, "id is not a UUID") from None

    @classmethod
    def _early_report(cls, request, body) -> None:
        """What the extractors of a PUT answer before the handler runs: a malformed id (400), a body that is not a
        report (422)."""
        cls._uuid_of_path(request.path_params["id"])
        errors = []
        if not isinstance(body, dict) or not isinstance(body.get("summary"), str) \
                or not 1 <= len(body["summary"].encode()) <= 500:  # garde `length` counts bytes
            errors.append({"field": "summary", "message": "must be 1 to 500 bytes"})
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict) or len(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()) \
                > REPORT_DATA_BYTES:
            errors.append({"field": "data", "message": "must be a JSON object of at most 16 KiB"})
        if errors:
            raise Problem(422, "the report is not valid", errors=errors)

    def _subject(self, request, id: str) -> tuple[str, str, str]:
        # "/api/v1/plugin-api/<kind>/..." or "/api/v1/<kind>/...", after the empty segment before the first "/".
        segment = request.url.path.split("/")[4 if request.url.path.startswith(API + "/plugin-api") else 3]
        kind = {"submissions": "submission", "assignments": "assignment", "courses": "course"}[segment]
        sid = self._uuid(id, "id")
        if kind == "course" and request.url.path.startswith(API + "/plugin-api"):
            self._reach(sid)  # the grant is asked before the course is looked up
        table = {"submission": self.submissions, "assignment": self.assignments, "course": self.courses}[kind]
        subject = table.get(sid)
        if subject is None:
            raise Problem(404, "there is no such submitted attempt, assignment or course")
        return kind, sid, subject["id"] if kind == "course" else subject["course_id"]

    @staticmethod
    def _key(key: str) -> None:
        if not is_report_key(key):
            raise Problem(422, "the key is not valid", errors=[{"field": "key", "message": "must be a slug"}])

    def _put_report(self, request, body, id, key):
        kind, sid, course_id = self._subject(request, id)
        self._reach(course_id)
        self._key(key)  # after the subject, as in the core
        if (kind, sid, key) not in self._reports and len(self.reports_for(kind, sid)) >= MAX_REPORTS_PER_SUBJECT:
            raise Problem(422, f"a plugin may keep at most {MAX_REPORTS_PER_SUBJECT} reports on one subject",
                          errors=[{"field": "key", "message": "too many reports"}])
        self._reports[(kind, sid, key)] = {"summary": body["summary"], "data": body["data"],
                                           "updated_at": clock.iso(clock.now())}
        return 204, None, {}

    def _delete_report(self, request, _body, id, key):
        kind, sid, course_id = self._subject(request, id)
        self._reach(course_id)
        self._key(key)
        if self._reports.pop((kind, sid, key), None) is None:
            raise Problem(404, "there is no such report")
        return 204, None, {}

    def _staff_reports(self, request, _body, id):
        kind, sid, _ = self._subject(request, id)
        return 200, {"items": [{"plugin_id": self.plugin_id, "key": k, **v}
                               for k, v in sorted(self.reports_for(kind, sid).items())]}, {}

    # -- mail (Contract 1) --

    @classmethod
    def _early_mail(cls, request, body) -> None:
        cls._check_mail(body)  # the body is read before the scope is asked for

    def _mail(self, request, body):
        """In the order of the core's handler: body (422), scope (403), `[mail]` (503), the target (404, 403, 422 for
        an audience over `max_audience`), the hourly limit of calls (429), then per person: opt-out, the daily limit
        (an audience skips the person as `rate_limited`, one person is a 429), the dedupe claim."""
        if not self.mail_configured:
            raise Problem(503, "mail is not configured")
        to = self._check_mail(body)
        now = clock.now()
        if "user_id" in to:
            user = self.users.get(to["user_id"])
            if user is None:
                raise Problem(404, "no such user")
            reached = [m for m in self.members if m["user_id"] == user["id"] and self._reaches(m["course_id"])]
            if not reached:
                raise Problem(403, "the person is on no roster the plugin reaches")
            people, course_id = [user], None
        else:
            course_id = self._course(to["course_id"])["id"]
            self._reach(course_id)
            roles = {"students": ("student",), "teachers": self.teacher_audience,
                     "all": ("student", "teacher", "assistant")}[to["audience"]]
            people = [self.users[m["user_id"]] for m in self.members
                      if m["course_id"] == course_id and m["role"] in roles]
        limits = self.mail_limits
        if len(people) > limits["max_audience"]:
            raise Problem(422, "the audience is larger than one call may reach",
                          errors=[{"field": "to", "message": f"more than {limits['max_audience']} people"}])
        recent_calls = [at for at in self._mail_calls if at > now - timedelta(hours=1)]
        if len(recent_calls) >= limits["plugin_hourly_calls"]:
            wait = math.ceil((min(recent_calls) + timedelta(hours=1) - now).total_seconds())
            raise Problem(429, "the plugin made too many mail calls this hour", {"retry-after": str(wait)})
        reasons = {"opted_out": 0, "no_address": 0, "duplicate": 0, "disabled": 0, "rate_limited": 0}
        candidates = []
        for person in people:
            if person["status"] != "active":
                reasons["disabled"] += 1
            elif not person["has_address"]:
                reasons["no_address"] += 1
            elif (person["id"], body["kind"]) in self.opt_outs:
                reasons["opted_out"] += 1
            else:
                candidates.append(person)
        day = [m for m in self.mails if clock.parse_time(m["sent_at"]) > now - timedelta(days=1)]
        over, rested = None, []
        for person in candidates:
            mine = [clock.parse_time(m["sent_at"]) for m in day if m["to_user_id"] == person["id"]]
            if len(mine) >= limits["recipient_daily"]:
                reasons["rate_limited"] += 1
                over = math.ceil((min(mine) + timedelta(days=1) - now).total_seconds())
            else:
                rested.append(person)
        if over is not None and "user_id" in to:
            raise Problem(429, "a recipient got too much plugin mail today", {"retry-after": str(over)})
        sending = []
        for person in rested:
            if self._is_duplicate(body.get("dedupe_key"), person["id"], now):
                reasons["duplicate"] += 1
            else:
                sending.append(person)
        self._mail_calls.append(now)
        for person in sending:
            self.mails.append({"plugin": self.plugin_id, "to_user_id": person["id"], "course_id": course_id,
                               "kind": body["kind"], "subject": body["subject"], "text": body["text"],
                               "link_path": body.get("link_path"), "dedupe_key": body.get("dedupe_key"),
                               "sent_at": clock.iso(now)})
            if body.get("dedupe_key"):
                self._dedupe[(body["dedupe_key"], person["id"])] = now
        return 202, {"accepted": len(sending), "skipped": len(people) - len(sending), "skipped_reasons": reasons}, {}

    def _is_duplicate(self, key: str | None, user_id: str, now: datetime) -> bool:
        seen = self._dedupe.get((key, user_id)) if key else None
        return seen is not None and now - seen < timedelta(days=self.mail_dedupe_days)

    @staticmethod
    def _check_mail(body: Any) -> dict[str, Any]:
        errors: list[dict[str, str]] = []

        def bad(field_name: str, message: str) -> None:
            errors.append({"field": field_name, "message": message})

        if not isinstance(body, dict):
            raise Problem(422, "the body is not a mail", errors=[{"field": "body", "message": "must be an object"}])
        to = body.get("to")
        if isinstance(to, dict) and set(to) == {"user_id"} and isinstance(to["user_id"], str):
            try:
                to = {"user_id": str(uuid.UUID(to["user_id"]))}
            except ValueError:
                bad("to", "user_id is not a UUID")
        elif isinstance(to, dict) and set(to) == {"course_id", "audience"} and isinstance(to["course_id"], str) \
                and to["audience"] in ("students", "teachers", "all"):
            pass
        else:
            bad("to", "must be {user_id} or {course_id, audience: students|teachers|all}")
        if not isinstance(body.get("kind"), str) or not KIND_RE.fullmatch(body["kind"]):
            bad("kind", "must be a slug of at most 40 characters")
        subject = body.get("subject")
        if not isinstance(subject, str) or not 1 <= len(subject) <= 200 \
                or any(unicodedata.category(c) == "Cc" for c in subject):
            bad("subject", "must be 1 to 200 characters without control characters")
        text = body.get("text")
        if not isinstance(text, str) or not 1 <= len(text) <= 20000 \
                or any(unicodedata.category(c) == "Cc" and c not in "\n\r\t" for c in text):
            bad("text", "must be 1 to 20000 characters of plain text, no control character but line breaks and tabs")
        link = body.get("link_path")
        if link is not None and not (
            isinstance(link, str) and len(link.encode()) <= 500 and link.startswith("/") and "//" not in link
            and "\\" not in link and not any(c.isspace() or unicodedata.category(c) == "Cc" for c in link)
        ):
            bad("link_path", "must be a path of at most 500 characters: one leading /, no //, no backslash, "
                             "no whitespace")
        key = body.get("dedupe_key")
        if key is not None and not (isinstance(key, str) and 1 <= len(key) <= 100
                                    and not any(unicodedata.category(c) == "Cc" for c in key)):
            bad("dedupe_key", "must be 1 to 100 printable characters")
        if set(body) - {"to", "kind", "subject", "text", "link_path", "dedupe_key"}:
            bad("body", "has fields the core does not know")
        if errors:
            raise Problem(422, "the mail is not valid", errors=errors)
        return to

    # ---- wiring a plugin to the fake --------------------------------------------------------------------------

    async def _sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def client(self, **kwargs: Any):
        from ..client import PluginClient
        return PluginClient(f"http://core.test{API}", self.token, transport=httpx.ASGITransport(app=self.app),
                            sleep=self._sleep, **kwargs)

    def attach(self, plugin: Plugin, *, state: State | None = None):
        """Point `plugin` at this fake: its client, an in-memory state, the fake's webhook secret. The fake then
        grants what the plugin's manifest asks for. Returns the plugin's client."""
        self.plugin_id = plugin.id
        manifest = plugin.manifest()
        self.scopes = set(manifest["scopes"])
        self.setting_specs = manifest["settings"]
        client = self.client()
        plugin.configure(client=client, state=state or State(":memory:"), secret=self.secret)
        plugin.config.invalidate()
        return client

    def _plugin_http(self, plugin: Plugin) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=plugin.app), base_url="http://plugin.test")

    async def deliver(self, plugin: Plugin, event_type: str, data: dict[str, Any], *, event_id: str | None = None,
                      wait: bool = True, force: bool = False, **tamper: Any) -> httpx.Response:
        """Send the plugin a signed event the way the core does, then (by default) wait until it was processed.
        The core sends only subscribed events; `force=True` sends one anyway."""
        if not force and event_type not in plugin.manifest()["subscribes"]:
            raise AssertionError(f"the core would not send {event_type}: the plugin does not subscribe to it")
        course_id = data.get("course_id")
        if not force and isinstance(course_id, str) and not self._reaches(course_id):
            raise AssertionError(f"the core would not send an event about course {course_id}: the plugin does not "
                                 "reach that course (`fake.grant(courses=...)`)")
        body, headers = signed_event(self.secret, event_type, data, event_id=event_id, **tamper)
        async with self._plugin_http(plugin) as http:
            response = await http.post("/events", content=body, headers=headers)
        if wait:
            await plugin.drain()
        return response

    async def call(self, plugin: Plugin, method: str, path: str, *, user: Any = None, json: Any = None,
                   content: bytes | None = None, headers: dict[str, str] | None = None,
                   strict: bool = True) -> httpx.Response:
        """A person's call through the core to a route of the plugin (`path` includes the query). Does what the
        core does: finds the declared route (404), asks for a session unless the route is public (401), resolves the
        course (404 if the person cannot see it, 403 outside the grant), checks roles (403), signs, forwards, and
        (strict) fails the test where the core would answer 502 (a 5xx, or a type it does not pass on)."""
        route_path, _, query = path.partition("?")
        spec, params = self._match(plugin.manifest()["routes"], method.upper(), route_path)
        if spec is None:
            # A stranger is asked for a session before anything is said about what exists (proxy.rs).
            return httpx.Response(401 if user is None else 404, json=Problem(
                401 if user is None else 404, "not logged in" if user is None else "no such route").body())
        body = content if content is not None else (b"" if json is None else
                                                      __import__("json").dumps(json, separators=(",", ":")).encode())
        who: dict[str, str | None] = {}
        if spec["auth"] == "session":
            if user is None:
                return httpx.Response(401, json=Problem(401, "not logged in").body())
            person = self.users[_ref(user)]
            course_id = params.get("course_id")
            course_role = None
            if course_id is not None:
                if course_id not in self.courses:
                    return httpx.Response(404, json=Problem(404, "no such course").body())
                member = next((m for m in self.members if m["course_id"] == course_id
                               and m["user_id"] == person["id"]), None)
                if member is None and person["role"] != "admin":
                    return httpx.Response(404, json=Problem(404, "no such course").body())
                if not self._reaches(course_id):
                    return httpx.Response(403, json=Problem(403, "the plugin does not reach this course").body())
                course_role = member["role"] if member else None
            allowed = person["role"] in spec["roles"] and (
                not spec["course_roles"] or person["role"] == "admin" or course_role in spec["course_roles"])
            if not allowed:
                return httpx.Response(403, json=Problem(403, "not allowed").body())
            who = {"user": person["id"], "role": person["role"], "course": course_id, "course_role": course_role}
        target = route_path + (f"?{query}" if query else "")
        signed = signed_call_headers(self.secret, method.upper(), target, body=body,
                                     content_type="application/json" if json is not None else None,
                                     **{k: v for k, v in who.items()})
        async with self._plugin_http(plugin) as http:
            response = await http.request(method.upper(), target, content=body, headers={**signed, **(headers or {})})
        if strict:
            self._check_answer(response)
        return response

    @staticmethod
    def _match(routes: list[dict[str, Any]], method: str, path: str):
        for spec in routes:
            if spec["method"] != method:
                continue
            want, have, params = spec["path"].split("/"), path.split("/"), {}
            if len(want) != len(have):
                continue
            for w, h in zip(want[1:], have[1:]):
                if not PLAIN.fullmatch(h) or h in (".", ".."):
                    break
                if w.startswith("{"):
                    params[w[1:-1]] = h
                elif w != h:
                    break
            else:
                return spec, params
        return None, {}

    @staticmethod
    def _check_answer(response: httpx.Response) -> None:
        if not (200 <= response.status_code < 300 or 400 <= response.status_code < 500):
            raise AssertionError(f"the core would answer 502: the plugin answered {response.status_code} "
                                 "(only 2xx and 4xx are passed on, a redirect is not)")
        if response.content:
            media = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if media not in ANSWER_TYPES:
                raise AssertionError(f"the core would answer 502: it does not pass on {media or 'no content type'}")

    async def run_job(self, plugin: Plugin, name: str) -> dict[str, Any]:
        """What an operator does: `POST /jobs/{name}` as an administrator. Returns the JSON result."""
        response = await self.call(plugin, "POST", f"/jobs/{name}", user=self.admin)
        return response.json()
