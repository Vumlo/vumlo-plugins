"""Vumlo plugin SDK v2. See README.md for how to write a plugin in thirty lines."""

from . import clock
from .client import MailResult, PluginClient
from .errors import (CoreError, Forbidden, Gone, Invalid, NotFound, RateLimited, ServerError, Unauthorized,
                     Unavailable, Unreachable)
from .manifest import ManifestError, route, setting, validate_manifest
from .plugin import ANSWER_TYPES, Event, Plugin, attachment
from .report import Report
from .signing import Identity, Refused
from .state import State

__all__ = [
    "ANSWER_TYPES", "CoreError", "Event", "Forbidden", "Gone", "Identity", "Invalid", "MailResult", "ManifestError",
    "NotFound", "Plugin", "PluginClient", "RateLimited", "Refused", "Report", "ServerError", "State", "Unauthorized",
    "Unavailable", "Unreachable", "attachment", "clock", "route", "setting", "validate_manifest",
]
