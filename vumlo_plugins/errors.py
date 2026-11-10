"""What the core can answer to a plugin, as exceptions. All derive from `CoreError`, so a handler that does
not care can catch one class; the ones that matter (`Forbidden` for a scope that was not granted, `RateLimited`,
`Unavailable` for mail that is not configured) are told apart."""

from __future__ import annotations

from typing import Any


class CoreError(Exception):
    """The core answered with an error, or could not be reached (`status` 0)."""

    status = 0

    def __init__(self, status: int, problem: dict[str, Any] | None = None, retry_after: float | None = None):
        self.status = status
        self.problem = problem or {}
        self.retry_after = retry_after
        detail = self.problem.get("detail") or self.problem.get("title") or ""
        super().__init__(f"core answered {status}{': ' + str(detail) if detail else ''}")


class Unauthorized(CoreError):
    """401: the token is unknown or the plugin is disabled."""


class Forbidden(CoreError):
    """403: the scope was not granted, or the course is outside the plugin's grant."""


class NotFound(CoreError):
    """404: nothing there, or nothing a student could see."""


class Gone(CoreError):
    """410: `deleted_since` is older than the core keeps tombstones; read the list in full instead."""


class Invalid(CoreError):
    """400 or 422: the core refused what was sent. A bug of the plugin, not a thing to retry."""


class RateLimited(CoreError):
    """429, after the retries the client makes."""


class Unavailable(CoreError):
    """503: e.g. mail without `[mail]` configured."""


class ServerError(CoreError):
    """Other 5xx, after the retries the client makes."""


class Unreachable(CoreError):
    """The core did not answer (connection, timeout), after the retries."""


def error_for(status: int, problem: dict[str, Any] | None, retry_after: float | None) -> CoreError:
    kind = {401: Unauthorized, 403: Forbidden, 404: NotFound, 410: Gone, 400: Invalid, 422: Invalid,
            429: RateLimited, 503: Unavailable}.get(status)
    if kind is None:
        kind = ServerError if status >= 500 else CoreError
    return kind(status, problem, retry_after)
