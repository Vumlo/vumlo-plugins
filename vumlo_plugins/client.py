"""The plugin's side of `/plugin-api/*`: async, paginated, retrying, typed errors.

`VUMLO_API` is the API root including `/api/v1`; `VUMLO_PLUGIN_TOKEN` is the bearer token printed by
`vumlo-cli plugin install`. A 429 is retried after its `Retry-After`; a 5xx or a lost connection is retried with a
back-off (not a POST: the one POST, mail, is retried only on 429, and `dedupe_key` makes a resend harmless).
"""

from __future__ import annotations

import asyncio
import os
import re
import unicodedata
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import httpx

from . import clock
from .errors import CoreError, Unreachable, error_for
from .manifest import is_report_key
from .report import MAX_DATA_BYTES, MAX_SUMMARY, Report, json_size

MAX_RETRY_AFTER = 60.0
MAX_PAGES = 10_000
KIND_RE = re.compile(r"[a-z][a-z0-9_-]{0,39}")
Subject = Literal["submission", "assignment", "course"]
_SUBJECT_PATH = {"submission": "submissions", "assignment": "assignments", "course": "courses"}


def _param(value: Any) -> str:
    if isinstance(value, datetime):
        return clock.iso(value)
    return str(value)


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return max(float(response.headers.get("retry-after", "")), 0.0)
    except ValueError:
        return None


class PluginClient:
    def __init__(
        self, base_url: str | None = None, token: str | None = None, *, timeout: float = 10.0, max_retries: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.base_url = (base_url if base_url is not None else os.getenv("VUMLO_API", "")).rstrip("/")
        self._token = token if token is not None else os.getenv("VUMLO_PLUGIN_TOKEN", "")
        self.max_retries = max_retries
        self._sleep = sleep
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)
        self.mail = Mail(self)
        self.reports = Reports(self)

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- plumbing ---------------------------------------------------------------------------------------------

    async def request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json: Any = None) -> Any:
        query = {k: _param(v) for k, v in (params or {}).items() if v is not None}
        headers = {"Authorization": f"Bearer {self._token}"}
        url = f"{self.base_url}{path}"
        attempt = 0
        while True:
            try:
                response = await self._http.request(method, url, params=query, json=json, headers=headers)
            except httpx.TransportError as error:
                if method == "POST" or attempt >= self.max_retries:
                    raise Unreachable(0, {"detail": type(error).__name__}) from None
                await self._sleep(min(0.5 * 2**attempt, MAX_RETRY_AFTER))
                attempt += 1
                continue
            status = response.status_code
            if status < 400:
                return response.json() if response.content else None
            try:
                problem = response.json()
            except ValueError:
                problem = {}
            problem_retry = _retry_after(response)
            retryable = status == 429 or (status >= 500 and method != "POST")
            # A wait longer than a minute is not slept through inside a request: the caller gets `RateLimited` with
            # `retry_after` and a job tries again on its next run.
            patient = problem_retry is None or problem_retry <= MAX_RETRY_AFTER
            if retryable and patient and attempt < self.max_retries:
                await self._sleep(problem_retry if problem_retry is not None else min(0.5 * 2**attempt, MAX_RETRY_AFTER))
                attempt += 1
                continue
            raise error_for(status, problem if isinstance(problem, dict) else {}, problem_retry)

    async def pages(self, path: str, **params: Any) -> AsyncIterator[dict[str, Any]]:
        """Every item of a paginated list, following `next_cursor`."""
        query = {k: v for k, v in params.items() if v is not None}
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            page = await self.request("GET", path, params=query)
            for item in page.get("items", []):
                yield item
            cursor = page.get("next_cursor")
            if not cursor or cursor in seen:
                return
            seen.add(cursor)
            query["cursor"] = cursor
        raise CoreError(0, {"detail": "too many pages"})

    async def all(self, path: str, **params: Any) -> list[dict[str, Any]]:
        return [item async for item in self.pages(path, **params)]

    # -- reads ------------------------------------------------------------------------------------------------

    async def assignments(self, course_id: str | UUID | None = None, *, updated_since: datetime | str | None = None):
        return await self.all("/plugin-api/assignments", course_id=course_id, updated_since=updated_since)

    async def assignment(self, assignment_id: str | UUID) -> dict[str, Any]:
        return await self.request("GET", f"/plugin-api/assignments/{assignment_id}")

    async def deleted_assignments(self, deleted_since: datetime | str, course_id: str | UUID | None = None):
        """Raises `Gone` when `deleted_since` is older than the core's retention: read `assignments()` in full."""
        return await self.all("/plugin-api/assignments/deleted", deleted_since=deleted_since, course_id=course_id)

    async def submissions(self, assignment_id: str | UUID, *, updated_since: datetime | str | None = None):
        """Submitted attempts of an assignment, without content."""
        return await self.all(f"/plugin-api/assignments/{assignment_id}/submissions", updated_since=updated_since)

    async def submission(self, submission_id: str | UUID) -> dict[str, Any]:
        return await self.request("GET", f"/plugin-api/submissions/{submission_id}")

    async def form_answers(self, submission_id: str | UUID) -> dict[str, Any]:
        return await self.request("GET", f"/plugin-api/submissions/{submission_id}/fields")

    async def members(self, course_id: str | UUID, role: str | None = None) -> list[dict[str, Any]]:
        return await self.all(f"/plugin-api/courses/{course_id}/members", role=role)

    async def grades(
        self, course_id: str | UUID | None = None, assignment_id: str | UUID | None = None, *,
        updated_since: datetime | str | None = None,
    ) -> list[dict[str, Any]]:
        return await self.all("/plugin-api/grades", course_id=course_id, assignment_id=assignment_id,
                              updated_since=updated_since)

    async def quiz(self, assignment_id: str | UUID) -> dict[str, Any]:
        return await self.request("GET", f"/plugin-api/assignments/{assignment_id}/quiz")

    async def attempts(self, assignment_id: str | UUID, *, updated_since: datetime | str | None = None):
        return await self.all(f"/plugin-api/assignments/{assignment_id}/attempts", updated_since=updated_since)

    async def announcements(self, course_id: str | UUID | None = None, *, updated_since: datetime | str | None = None):
        return await self.all("/plugin-api/announcements", course_id=course_id, updated_since=updated_since)

    async def deleted_announcements(self, deleted_since: datetime | str, course_id: str | UUID | None = None):
        return await self.all("/plugin-api/announcements/deleted", deleted_since=deleted_since, course_id=course_id)

    async def settings(self, course_id: str | UUID | None = None) -> dict[str, Any]:
        """The values an administrator configured (secrets in clear), defaults of the manifest where unset."""
        return (await self.request("GET", "/plugin-api/settings", params={"course_id": course_id}))["values"]


class Reports:
    """`client.reports`: keyed reports on a submission, an assignment or a course (ADR-0077)."""

    def __init__(self, client: PluginClient):
        self._client = client

    @staticmethod
    def _path(subject: Subject, subject_id: str | UUID, key: str) -> str:
        if subject not in _SUBJECT_PATH:
            raise ValueError("subject is submission, assignment or course")
        if not is_report_key(key):
            raise ValueError("a report key is lowercase letters, digits, `_` and `-`, starts with a letter, <= 40")
        return f"/plugin-api/{_SUBJECT_PATH[subject]}/{subject_id}/reports/{key}"

    async def put(self, subject: Subject, subject_id: str | UUID, key: str, report: Report) -> None:
        """Replaces the plugin's report under `key`; sending the same twice leaves one."""
        path = self._path(subject, subject_id, key)
        if not 1 <= len(report.summary) <= MAX_SUMMARY or json_size(report.data) > MAX_DATA_BYTES:
            raise ValueError("the summary is 1 to 500 characters and the data at most 16 KiB; use Report.build")
        await self._client.request("PUT", path, json=report.body())

    async def delete(self, subject: Subject, subject_id: str | UUID, key: str) -> None:
        await self._client.request("DELETE", self._path(subject, subject_id, key))


@dataclass
class MailResult:
    accepted: int = 0
    skipped: int = 0
    skipped_reasons: dict[str, int] = field(default_factory=dict)


class Mail:
    """`client.mail.send(...)`: Contract 1, `POST /plugin-api/mail`. The plugin never sees an address."""

    def __init__(self, client: PluginClient):
        self._client = client

    async def send(
        self, *, kind: str, subject: str, text: str, user_id: str | UUID | None = None,
        course_id: str | UUID | None = None, audience: Literal["students", "teachers", "all"] | None = None,
        link_path: str | None = None, dedupe_key: str | None = None,
    ) -> MailResult:
        """Send to one person (`user_id`) or to a course audience (`course_id` + `audience`). `kind` groups mails for
        opt-out and statistics. The same `dedupe_key` to the same person is accepted once in 30 days."""
        if (user_id is None) == (course_id is None):
            raise ValueError("send to a user_id or to a course_id with an audience, not both or neither")
        if course_id is not None and audience not in ("students", "teachers", "all"):
            raise ValueError("audience is students, teachers or all")
        if user_id is not None and audience is not None:
            raise ValueError("audience goes with course_id")
        if not KIND_RE.fullmatch(kind):
            raise ValueError("kind is a slug of at most 40 characters")
        if not 1 <= len(subject) <= 200 or any(unicodedata.category(c) == "Cc" for c in subject):
            raise ValueError("subject is 1 to 200 characters without control characters")
        if not 1 <= len(text) <= 20000:
            raise ValueError("text is 1 to 20000 characters")
        if link_path is not None and not (link_path.startswith("/") and not link_path.startswith("//")):
            raise ValueError("link_path is a path starting with one `/`")
        if dedupe_key is not None and not 1 <= len(dedupe_key) <= 100:
            raise ValueError("dedupe_key is at most 100 characters")
        to = {"user_id": str(user_id)} if user_id is not None else {"course_id": str(course_id), "audience": audience}
        body: dict[str, Any] = {"to": to, "kind": kind, "subject": subject, "text": text}
        if link_path is not None:
            body["link_path"] = link_path
        if dedupe_key is not None:
            body["dedupe_key"] = dedupe_key
        answer = await self._client.request("POST", "/plugin-api/mail", json=body)
        return MailResult(answer["accepted"], answer["skipped"], dict(answer.get("skipped_reasons", {})))
