"""Sign events and proxied calls exactly as the core does, so a plugin's tests can talk to its own app."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from .. import clock
from ..signing import proxy_signature, sign


def signed_event(
    secret: str, event_type: str, data: dict[str, Any], *, event_id: str | None = None,
    occurred_at: datetime | None = None, timestamp: int | None = None,
) -> tuple[bytes, dict[str, str]]:
    """`(body, headers)` of a delivery (delivery.rs): body `{id, type, occurred_at, data}`, signed over the time."""
    body = json.dumps({
        "id": event_id or str(uuid.uuid4()), "type": event_type,
        "occurred_at": clock.iso(occurred_at or clock.now()), "data": data,
    }, separators=(",", ":")).encode()
    stamp = clock.timestamp() if timestamp is None else timestamp
    return body, {"content-type": "application/json", "x-vumlo-timestamp": str(stamp),
                  "x-vumlo-signature": sign(secret, stamp, body)}


def signed_call_headers(
    secret: str, method: str, target: str, *, user: str | None = None, role: str | None = None,
    course: str | None = None, course_role: str | None = None, body: bytes = b"", nonce: str | None = None,
    timestamp: int | None = None, content_type: str | None = None,
) -> dict[str, str]:
    """The headers the core adds to a call it forwards (proxy.rs). `user=None` is a public route (anyone)."""
    stamp = clock.timestamp() if timestamp is None else timestamp
    nonce = nonce or str(uuid.uuid4())
    headers = {
        "x-vumlo-timestamp": str(stamp), "x-vumlo-nonce": nonce,
        "x-vumlo-signature": proxy_signature(secret, stamp, nonce=nonce, method=method, target=target, user=user,
                                             role=role, course=course, course_role=course_role, body=body),
    }
    if user is None:
        headers["x-vumlo-anonymous"] = "1"
    else:
        headers["x-vumlo-user"] = user
        headers["x-vumlo-role"] = role or ""
    if course:
        headers["x-vumlo-course"] = course
    if course_role:
        headers["x-vumlo-course-role"] = course_role
    if content_type:
        headers["content-type"] = content_type
    return headers
