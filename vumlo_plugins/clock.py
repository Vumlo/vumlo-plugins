"""The one place a plugin reads the time (Contract 5). Tests freeze it; nothing else calls `datetime.now`."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

_frozen: datetime | None = None


def now() -> datetime:
    """The current time, timezone-aware UTC."""
    return _frozen if _frozen is not None else datetime.now(UTC)


def timestamp() -> int:
    """Unix seconds, the unit of the signatures."""
    return int(now().timestamp())


def freeze(at: datetime | str) -> datetime:
    """Stop the clock at `at` (a datetime, naive means UTC, or an RFC 3339 string). Tests only."""
    global _frozen
    moment = parse_time(at) if isinstance(at, str) else at
    _frozen = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return _frozen


def advance(**delta: float) -> datetime:
    """Move a frozen clock forward, e.g. `advance(hours=2)`."""
    global _frozen
    if _frozen is None:
        raise RuntimeError("the clock is not frozen")
    _frozen = _frozen + timedelta(**delta)
    return _frozen


def unfreeze() -> None:
    global _frozen
    _frozen = None


@contextmanager
def frozen(at: datetime | str):
    previous = _frozen
    freeze(at)
    try:
        yield
    finally:
        globals()["_frozen"] = previous


def parse_time(text: str) -> datetime:
    """An RFC 3339 time as the core writes it (`2026-03-01T10:00:00Z` or with microseconds and an offset)."""
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def iso(moment: datetime) -> str:
    """RFC 3339 in UTC with a `Z`, the form the core reads and writes."""
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")
