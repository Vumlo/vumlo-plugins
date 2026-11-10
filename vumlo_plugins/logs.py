"""Structured JSON logs, one object per line on stdout, with no personal data: a field called `token`, `secret`,
`email` and the like is never written, and anything that looks like an address, a plugin token or a bearer header
is masked in every string. Log ids, counts and statuses; not names, not addresses."""

from __future__ import annotations

import json
import logging
import re
import sys
import traceback
from typing import Any

from . import clock

SENSITIVE = ("token", "secret", "password", "passwd", "authorization", "email", "address", "api_key", "apikey",
             "signature")
_PATTERNS = (
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
    (re.compile(r"vplg_[A-Za-z0-9_-]+"), "[token]"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [token]"),
    (re.compile(r"(?i)\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), "[token]"),
)


def scrub(text: str) -> str:
    for pattern, mask in _PATTERNS:
        text = pattern.sub(mask, text)
    return text


def _clean(name: str, value: Any) -> Any:
    if any(word in name.lower() for word in SENSITIVE):
        return "[redacted]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return scrub(str(value))


class _Json(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = {"time": clock.iso(clock.now()), "level": record.levelname.lower(), "plugin": record.name,
                "msg": scrub(record.getMessage())}
        line.update(getattr(record, "fields", {}))
        if record.exc_info:
            line["error"] = scrub("".join(traceback.format_exception_only(*record.exc_info[:2])).strip())
        return json.dumps(line, ensure_ascii=False, default=str)


class Log:
    """`plugin.log.info("synced", assignments=12)`."""

    def __init__(self, plugin_id: str, stream: Any = None):
        self._logger = logging.getLogger(plugin_id)
        self._logger.propagate = False
        if not self._logger.handlers:
            handler = logging.StreamHandler(stream or sys.stdout)
            handler.setFormatter(_Json())
            self._logger.addHandler(handler)
            self._logger.setLevel(logging.INFO)

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
