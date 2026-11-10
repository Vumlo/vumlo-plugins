"""Signatures between the core and a plugin. Byte for byte what `vumlo-core/backend/crates/plugins/src/secret.rs`
does (ADR-0066 events, ADR-0078 and ADR-0080 proxied calls); `tests/test_signing.py` holds the vectors of its tests.

- event:  `sha256=` + hex HMAC-SHA256(secret, timestamp "." body)
- call:   the same MAC over timestamp "." canonical, canonical being these lines joined by "\\n":
          `vumlo-proxy`, nonce, METHOD, request-target, `session`|`none`, user, role, course, course role,
          hex SHA-256 of the body (of the empty string when there is no body); `-` stands for a missing course or
          course role, and for the user and role of a call nobody signed in for.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass

from . import clock

MAX_AGE_SECONDS = 300
MAX_NONCES = 10_000


class Refused(Exception):
    """A request that must not be served. `status` is the HTTP answer, `why` a short reason without secrets."""

    def __init__(self, status: int, why: str):
        super().__init__(why)
        self.status = status
        self.why = why


def sign(secret: str, timestamp: int | str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def _fresh(headers: Mapping[str, str], now: float | None) -> str:
    """The timestamp as sent, if it is plain ASCII digits (what the core writes) and within 300 s of now."""
    timestamp = _header(headers, "x-vumlo-timestamp") or ""
    if not (timestamp.isascii() and timestamp.isdigit() and len(timestamp) <= 12):
        raise Refused(401, "bad timestamp")
    if abs((clock.timestamp() if now is None else now) - int(timestamp)) > MAX_AGE_SECONDS:
        raise Refused(401, "stale timestamp")
    return timestamp


def _same(expected: str, given: str | None) -> bool:
    """Constant-time comparison. On bytes: `compare_digest` raises on a `str` with a non-ASCII character, and a
    header can carry one."""
    return hmac.compare_digest(expected.encode(), (given or "").encode("utf-8", "surrogatepass"))


def verify_event(secret: str, headers: Mapping[str, str], body: bytes, now: float | None = None) -> None:
    """Returns when the event is genuine and fresh, else raises `Refused`."""
    if not secret:
        raise Refused(503, "no webhook secret configured")
    timestamp = _fresh(headers, now)
    if not _same(sign(secret, timestamp, body), _header(headers, "x-vumlo-signature")):
        raise Refused(401, "bad signature")


@dataclass(frozen=True)
class Identity:
    """Who the core says is calling. Never read from a body: only from a verified signature."""

    user_id: str | None = None
    role: str | None = None
    course_id: str | None = None
    course_role: str | None = None

    @property
    def anonymous(self) -> bool:
        return self.user_id is None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_staff_of_course(self) -> bool:
        return self.course_role in ("teacher", "assistant")


def proxy_canonical(
    *, nonce: str, method: str, target: str, user: str | None, role: str | None,
    course: str | None, course_role: str | None, body: bytes,
) -> bytes:
    session = user is not None
    return "\n".join([
        "vumlo-proxy", nonce, method.upper(), target, "session" if session else "none",
        user if session else "-", (role or "-") if session else "-", course or "-", course_role or "-",
        hashlib.sha256(body).hexdigest(),
    ]).encode()


def proxy_signature(
    secret: str, timestamp: int | str, *, nonce: str, method: str, target: str, user: str | None = None,
    role: str | None = None, course: str | None = None, course_role: str | None = None, body: bytes = b"",
) -> str:
    """`user=None` signs a call made by anyone (a public route)."""
    canonical = proxy_canonical(nonce=nonce, method=method, target=target, user=user, role=role,
                                course=course, course_role=course_role, body=body)
    return sign(secret, timestamp, canonical)


class NonceStore:
    """The nonces of proxied calls taken so far: the core sends a fresh one every time and a call is taken once."""

    def __init__(self, limit: int = MAX_NONCES):
        self._seen: dict[str, float] = {}
        self._limit = limit

    def take(self, nonce: str, now: float) -> None:
        # Oldest first (insertion order), so only the expired front is visited, not all of them on every call.
        while self._seen:
            oldest = next(iter(self._seen))
            if now - self._seen[oldest] <= 2 * MAX_AGE_SECONDS:
                break
            del self._seen[oldest]
        if nonce in self._seen:
            raise Refused(401, "a call seen before")
        if len(self._seen) >= self._limit:
            raise Refused(503, "too many calls to tell apart")
        self._seen[nonce] = now


def verify_proxy(
    secret: str, headers: Mapping[str, str], body: bytes, *, method: str, target: str,
    nonces: NonceStore | None = None, now: float | None = None,
) -> Identity:
    """The identity of a call the core forwarded, or `Refused`. Checks the signature (it covers the user, role,
    course, target and body), the time, and, given a store, that the nonce was not seen."""
    if not secret:
        raise Refused(503, "no webhook secret configured")
    moment = clock.timestamp() if now is None else now
    timestamp = _fresh(headers, moment)
    nonce = _header(headers, "x-vumlo-nonce") or ""
    anonymous = _header(headers, "x-vumlo-anonymous") is not None
    user = None if anonymous else _header(headers, "x-vumlo-user")
    if not anonymous and not user:
        raise Refused(401, "bad signature")
    role = None if anonymous else _header(headers, "x-vumlo-role")
    course = _header(headers, "x-vumlo-course") or None
    course_role = _header(headers, "x-vumlo-course-role") or None
    expected = proxy_signature(secret, timestamp, nonce=nonce, method=method, target=target, user=user,
                               role=role, course=course, course_role=course_role, body=body)
    if not nonce or not _same(expected, _header(headers, "x-vumlo-signature")):
        raise Refused(401, "bad signature")
    if nonces is not None:
        nonces.take(nonce, moment)
    return Identity(user, role, course, course_role)
