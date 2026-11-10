"""Structured JSON logs, one object per line on stdout, with no personal data: a field called `token`, `secret`,
`email` and the like is never written, and anything that looks like an address, a plugin token or a bearer header
is masked in every string. Log ids, counts and statuses; not names, not addresses."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import traceback
from typing import Any

from . import clock

SENSITIVE = ("token", "secret", "password", "passwd", "authorization", "email", "address", "api_key", "apikey",
             "signature", "signing", "cookie", "credential", "private_key", "bearer", "session")
ENV_SECRETS = ("VUMLO_PLUGIN_TOKEN", "VUMLO_WEBHOOK_SECRET")
_PATTERNS = (
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@"), r"\1[credentials]@"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
    (re.compile(r"vplg_[A-Za-z0-9_-]+"), "[token]"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [token]"),
    (re.compile(r"(?i)\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), "[token]"),
)
_protected: set[str] = set()


def protect(*values: str) -> None:
    """Masks these exact strings in every line from now on: the secret settings an administrator typed (a webhook
    URL, a signing key, a GitHub token). The two secrets of the environment are always masked."""
    _protected.update(v for v in values if isinstance(v, str) and len(v) >= 6)


def scrub(text: str) -> str:
    for secret in sorted(_protected | {os.environ.get(name, "") for name in ENV_SECRETS}, key=len, reverse=True):
        if len(secret) >= 6:
            text = text.replace(secret, "[secret]")
    for pattern, mask in _PATTERNS:
        text = pattern.sub(mask, text)
    return text


def _clean(name: str, value: Any) -> Any:
    if any(word in name.lower() for word in SENSITIVE):
        return "[redacted]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        return {str(k): _clean(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_clean(name, v) for v in value]
    return scrub(str(value))


class _Json(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = {"time": clock.iso(clock.now()), "level": record.levelname.lower(), "plugin": record.name,
                "msg": scrub(record.getMessage())}
        line.update(getattr(record, "fields", {}))
        if record.exc_info:
            line["error"] = scrub("".join(traceback.format_exception_only(*record.exc_info[:2])).strip())
        return json.dumps(line, ensure_ascii=False, default=str)


class _Stdout(logging.StreamHandler):
    """Writes to whatever `sys.stdout` is now, so a redirected or captured stdout sees the lines."""

    def __init__(self):
        super().__init__(sys.stdout)

    @property
    def stream(self):
        return sys.stdout

    @stream.setter
    def stream(self, _value):
        pass


class Log:
    """`plugin.log.info("synced", assignments=12)`."""

    def __init__(self, plugin_id: str, stream: Any = None):
        self._logger = logging.getLogger(plugin_id)
        self._logger.propagate = False
        if not self._logger.handlers:
            handler = logging.StreamHandler(stream) if stream else _Stdout()
            handler.setFormatter(_Json())
            self._logger.addHandler(handler)
            self._logger.setLevel(logging.INFO)

    protect = staticmethod(protect)

    def _write(self, level: int, msg: str, fields: dict[str, Any], exc_info: bool = False) -> None:
        clean = {k: _clean(k, v) for k, v in fields.items() if k not in ("time", "level", "plugin", "msg")}
        self._logger.log(level, msg, extra={"fields": clean}, exc_info=exc_info or None)

    def info(self, msg: str, **fields: Any) -> None:
        self._write(logging.INFO, msg, fields)

    def warning(self, msg: str, **fields: Any) -> None:
        self._write(logging.WARNING, msg, fields)

    def error(self, msg: str, **fields: Any) -> None:
        self._write(logging.ERROR, msg, fields)

    def exception(self, msg: str, **fields: Any) -> None:
        """For an `except` block: logs the exception type and its scrubbed message, no traceback."""
        self._write(logging.ERROR, msg, fields, exc_info=True)
