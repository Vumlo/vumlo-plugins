from __future__ import annotations

import hashlib
import hmac
import json
import os
import smtplib
import time
from email.message import EmailMessage
from urllib.parse import urlparse
from typing import Any

import httpx

MAX_AGE_SECONDS = 300


def manifest(plugin_id: str, version: str, *, subscribes=(), scopes=(), settings=None, ui=()):
    return {
        "id": plugin_id,
        "version": version,
        "api": "1",
        "subscribes": list(subscribes),
        "scopes": list(scopes),
        "ui": list(ui),
        "settings": settings or {},
    }


def event_signature(secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_event(secret: str, headers: dict[str, str], body: bytes, now: float | None = None) -> tuple[int, str] | None:
    if not secret:
        return 503, "no webhook secret configured"
    timestamp = headers.get("x-vumlo-timestamp", headers.get("X-Vumlo-Timestamp", ""))
    try:
        sent_at = int(timestamp)
    except ValueError:
        return 401, "bad timestamp"
    if abs((time.time() if now is None else now) - sent_at) > MAX_AGE_SECONDS:
        return 401, "stale timestamp"
    expected = event_signature(secret, timestamp, body)
    actual = headers.get("x-vumlo-signature", headers.get("X-Vumlo-Signature", ""))
    if not hmac.compare_digest(expected, actual):
        return 401, "bad signature"
    return None


class PluginClient:
    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: float = 10):
        self.base_url = (base_url or os.getenv("VUMLO_API", "")).rstrip("/")
        self.token = token or os.getenv("VUMLO_PLUGIN_TOKEN", "")
        self.timeout = timeout

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = kwargs.pop("headers", {})
        headers.setdefault("Authorization", f"Bearer {self.token}")
        headers.setdefault("Content-Type", "application/json")
        with httpx.Client(base_url=self.base_url, timeout=self.timeout) as client:
            response = client.request(method, path, headers=headers, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else None

    def submission(self, submission_id: str) -> dict[str, Any]:
        return self.request("GET", f"/plugin-api/submissions/{submission_id}")

    def settings(self, course_id: str | None = None) -> dict[str, Any]:
        query = {"course_id": course_id} if course_id else {}
        return self.request("GET", "/plugin-api/settings", params=query)["values"]

    def report(self, submission_id: str, summary: str, data: dict[str, Any]) -> None:
        self.request("PUT", f"/plugin-api/submissions/{submission_id}/report", json={"summary": summary, "data": data})


class Notifier:
    def __init__(self, smtp_url: str | None = None, webhook_url: str | None = None):
        self.smtp_url = smtp_url or os.getenv("VUMLO_SMTP_URL")
        self.webhook_url = webhook_url or os.getenv("VUMLO_WEBHOOK_URL")

    def send(self, subject: str, message: str, payload: dict[str, Any] | None = None) -> None:
        if self.webhook_url:
            response = httpx.post(self.webhook_url, json={"subject": subject, "message": message, **(payload or {})}, timeout=10)
            response.raise_for_status()
        if self.smtp_url:
            parsed = urlparse(self.smtp_url)
            if parsed.scheme not in {"smtp", "smtps"} or not parsed.hostname:
                raise ValueError("VUMLO_SMTP_URL must be smtp://host:port")
            email = EmailMessage()
            email["Subject"] = subject
            email["From"] = os.getenv("VUMLO_SMTP_FROM", "vumlo-plugin@localhost")
            email["To"] = os.getenv("VUMLO_SMTP_TO", email["From"])
            email.set_content(message)
            smtp_type = smtplib.SMTP_SSL if parsed.scheme == "smtps" else smtplib.SMTP
            with smtp_type(parsed.hostname, parsed.port or 25, timeout=10) as server:
                username = parsed.username
                if username:
                    server.login(username, parsed.password or "")
                server.send_message(email)


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
