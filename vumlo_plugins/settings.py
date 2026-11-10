"""Reading the plugin's own settings with the defaults of its manifest, cached until the core says one changed.

The core merges what an administrator set with the manifest defaults and course values over institution ones
(`GET /plugin-api/settings?course_id=`). This adds three things: the plugin works without `settings:read` or
with the core down (the manifest defaults apply), a value that does not meet its spec is ignored, and one read
serves many events (cache per course, dropped on `plugin.settings.changed` and after `ttl` seconds anyway).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import clock
from .client import PluginClient
from .errors import CoreError
from .manifest import check_setting_value


class SettingsReader:
    def __init__(self, client: Callable[[], PluginClient], specs: dict[str, dict[str, Any]], ttl: float = 300.0):
        self._client = client
        self._specs = specs
        self._ttl = ttl
        self._cache: dict[str | None, tuple[float, dict[str, Any]]] = {}
        self._generation = 0

    @property
    def defaults(self) -> dict[str, Any]:
        return {key: spec["default"] for key, spec in self._specs.items() if "default" in spec}

    def invalidate(self) -> None:
        self._generation += 1  # a read already on its way holds the old value: it must not be kept
        self._cache.clear()

    async def get(self, course_id: str | None = None) -> dict[str, Any]:
        """The values for a course (or the institution when `course_id` is None). A key with neither a value nor a
        default is absent: use `.get(key)`."""
        key = str(course_id) if course_id else None
        now = clock.timestamp()
        hit = self._cache.get(key)
        if hit and hit[0] > now:
            return dict(hit[1])
        values = self.defaults
        generation = self._generation
        try:
            remote = await self._client().settings(key)
            if not isinstance(remote, dict):
                raise CoreError(0, {"detail": "settings are not an object"})
        except (CoreError, KeyError, TypeError):
            return values  # not cached: the next call asks again
        for name, value in remote.items():
            spec = self._specs.get(name)
            if spec is not None and check_setting_value(spec, value) is None:
                values[name] = value
        if generation == self._generation:
            self._cache[key] = (now + self._ttl, values)
        return dict(values)
