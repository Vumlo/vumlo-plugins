"""`Plugin`: one object that is the manifest, the FastAPI app, the event dispatcher, the signed routes, the jobs.

    plugin = Plugin("text-metrics", "1.0.0", scopes=["submissions:read", "reports:write"])

    @plugin.on("submission.submitted")
    async def submitted(event):
        sub = await plugin.client.submission(event.data["submission_id"])
        await plugin.client.reports.put("submission", sub["id"], "text-metrics", plugin.report("42 words"))

    app = plugin.app            # uvicorn <folder>.app:app

Environment: `VUMLO_API` (API root with `/api/v1`), `VUMLO_PLUGIN_TOKEN`, `VUMLO_WEBHOOK_SECRET` (printed by
`vumlo-cli plugin install`), `VUMLO_PLUGIN_DATA` (state directory), `VUMLO_TZ` (timezone of cron lines).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import unicodedata
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from . import clock
from .client import PluginClient
from .logs import Log
from .manifest import SETTINGS_CHANGED, manifest as build_manifest, route as build_route, validate_manifest
from .report import Report
from .scheduler import Scheduler
from .settings import SettingsReader
from .signing import Identity, NonceStore, Refused, verify_event, verify_proxy
from .state import State

# What the core passes on from a plugin's answer to a person (proxy.rs ANSWER_TYPES); anything else is a 502.
ANSWER_TYPES = (
    "application/json", "application/problem+json", "text/plain", "text/csv", "text/calendar", "application/pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
EVENT_MEMORY_SECONDS = 7 * 24 * 3600


@dataclass(frozen=True)
class Event:
    id: str
    type: str
    occurred_at: datetime | None
    data: dict[str, Any]


def attachment(content: bytes, filename: str, media_type: str) -> Response:
    """A download: the core passes `Content-Disposition` on only when it starts with `attachment`."""
    if media_type not in ANSWER_TYPES:
        raise ValueError(f"the core does not pass on {media_type}")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode())
    return Response(content, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{safe}"'})


Handler = Callable[[Event], Awaitable[None]]
RouteHandler = Callable[..., Awaitable[Any]]


class Plugin:
    def __init__(
        self, plugin_id: str, version: str = "1.0.0", *, scopes: list[str] | tuple[str, ...] = (),
        settings: dict[str, dict[str, Any]] | None = None, timezone: str | None = None,
    ):
        self.id = plugin_id
        self.version = version
        self.scopes = list(scopes)
        self.setting_specs = dict(settings or {})
        self.log = Log(plugin_id)
        self._client: PluginClient | None = None
        self._state: State | None = None
        self._secret: str | None = None
        self._handlers: dict[str, list[Handler]] = {}
        self._routes: list[dict[str, Any]] = []
        self._nonces = NonceStore()
        self._tasks: set[asyncio.Task] = set()
        self._handled = 0
        self.scheduler = Scheduler(self.log, timezone)
        self.config = SettingsReader(lambda: self.client, self.setting_specs)
        self.app = FastAPI(title=plugin_id, lifespan=self._lifespan, docs_url=None, redoc_url=None, openapi_url=None)
        self.app.state.plugin = self  # lets tools reach the Plugin from the `app` a module exposes
        self.app.add_api_route("/manifest", self._manifest_endpoint, methods=["GET"])
        self.app.add_api_route("/health", self._health_endpoint, methods=["GET"])
        self.app.add_api_route("/events", self._events_endpoint, methods=["POST"], status_code=202)

    # -- wiring (environment by default, tests inject) ---------------------------------------------------------

    @property
    def client(self) -> PluginClient:
        if self._client is None:
            self._client = PluginClient()
        return self._client

    @property
    def state(self) -> State:
        if self._state is None:
            self._state = State()
        return self._state

    @property
    def secret(self) -> str:
        return self._secret if self._secret is not None else os.getenv("VUMLO_WEBHOOK_SECRET", "")

    def configure(self, *, client: PluginClient | None = None, state: State | None = None,
                  secret: str | None = None) -> None:
        """Replace what the environment would give: the tests' way in (see `vumlo_plugins.testing`)."""
        if client is not None:
            self._client = client
        if state is not None:
            self._state = state
        if secret is not None:
            self._secret = secret
            self.log.protect(secret)

    # -- declaring --------------------------------------------------------------------------------------------

    def on(self, event_type: str) -> Callable[[Handler], Handler]:
        """Subscribe to an event. The handler runs after the core got its 202; it must be idempotent."""
        def decorate(fn: Handler) -> Handler:
            self._handlers.setdefault(event_type, []).append(fn)
            return fn
        return decorate

    def route(self, method: str, path: str, *, roles: tuple[str, ...] | list[str] = (),
              course_roles: tuple[str, ...] | list[str] = (), auth: str | None = None) -> Callable[[RouteHandler], RouteHandler]:
        """A route of this server that people reach through the core at `/api/v1/plugins/<id><path>`. The handler gets
        `(request, who, **path_params)`, `who` being the signed `Identity`, and returns a dict (JSON), a str
        (text/plain) or a `Response` (see `attachment`). `auth="none"` is a public GET: check your own token."""
        spec = build_route(method, path, roles=roles, course_roles=course_roles, auth=auth)

        def decorate(fn: RouteHandler) -> RouteHandler:
            self._add_route(spec, fn)
            return fn
        return decorate

    def job(self, name: str, *, cron: str | None = None, every: float | None = None) -> Callable:
        """Work on a clock. It is also run on demand by `POST /jobs/{name}` (admin), so it must be idempotent."""
        def decorate(fn):
            first = not self.scheduler.jobs
            self.scheduler.add(name, fn, cron=cron, every=every)
            if first:
                self._add_route(build_route("POST", "/jobs/{name}", roles=["admin"]), self._run_job_endpoint)
            return fn
        return decorate

    def report(self, summary: str, **kwargs: Any) -> Report:
        """`Report.build` with this plugin's id, so `links=[("Download", "/export/...")]` works."""
        return Report.build(summary, plugin_id=self.id, **kwargs)

    def manifest(self) -> dict[str, Any]:
        """The manifest, checked by the port of the core's validator (raises `ManifestError`)."""
        subscribes = list(self._handlers)
        if self.setting_specs and SETTINGS_CHANGED not in subscribes:
            subscribes.append(SETTINGS_CHANGED)
        return validate_manifest(build_manifest(
            self.id, self.version, subscribes=subscribes, scopes=self.scopes, settings=self.setting_specs,
            routes=list(self._routes)))

    # -- the endpoints ----------------------------------------------------------------------------------------

    def _add_route(self, spec: dict[str, Any], fn: RouteHandler) -> None:
        self._routes.append(spec)

        async def endpoint(request: Request) -> Response:
            body = await request.body()
            target = request.scope.get("raw_path", request.url.path.encode()).decode("latin-1")
            if request.scope.get("query_string"):
                target += "?" + request.scope["query_string"].decode("latin-1")
            try:
                who = verify_proxy(self.secret, request.headers, body, method=request.method, target=target,
                                   nonces=self._nonces)
            except Refused as refused:
                return JSONResponse({"error": refused.why}, status_code=refused.status)
            if not self._allowed(spec, who):
                return JSONResponse({"error": "not allowed"}, status_code=403)
            result = await fn(request, who, **request.path_params)
            if isinstance(result, Response):
                return result
            if isinstance(result, str):
                return PlainTextResponse(result)
            return JSONResponse(result)

        self.app.add_api_route(spec["path"], endpoint, methods=[spec["method"]], include_in_schema=False)

    @staticmethod
    def _allowed(spec: dict[str, Any], who: Identity) -> bool:
        """The core already decided; this is the second lock, so a plugin reached some other way still refuses."""
        if spec.get("auth") == "none":
            return who.anonymous
        if who.anonymous or who.role not in spec["roles"]:
            return False
        return not spec.get("course_roles") or who.is_admin or who.course_role in spec["course_roles"]

    async def _run_job_endpoint(self, request: Request, who: Identity, name: str):
        if name not in self.scheduler.jobs:
            return JSONResponse({"error": "no such job"}, status_code=404)
        result = await self.scheduler.run(name)
        return JSONResponse(result, status_code=200 if result["ok"] else 500)

    async def _manifest_endpoint(self) -> dict[str, Any]:
        return self.manifest()

    async def _health_endpoint(self) -> dict[str, Any]:
        configured = bool(self.secret and (self._client is not None or os.getenv("VUMLO_PLUGIN_TOKEN")))
        return {"plugin": self.id, "version": self.version, "status": "ok", "jobs": sorted(self.scheduler.jobs),
                "events_handled": self._handled, "configured": configured}

    async def _events_endpoint(self, request: Request) -> Response:
        body = await request.body()
        try:
            verify_event(self.secret, request.headers, body)
        except Refused as refused:
            return JSONResponse({"error": refused.why}, status_code=refused.status)
        try:
            raw = json.loads(body)
            event = Event(raw["id"], raw["type"], _when(raw.get("occurred_at")), raw.get("data") or {})
            valid = isinstance(event.id, str) and bool(event.id) and isinstance(event.type, str) \
                and isinstance(event.data, dict)
        except (ValueError, KeyError, TypeError, AttributeError):
            valid = False
        if not valid:
            return JSONResponse({"error": "not an event"}, status_code=400)
        if event.type == SETTINGS_CHANGED:
            self.config.invalidate()
        if event.type in self._handlers and self.state.seen("events", event.id):
            task = asyncio.create_task(self._dispatch(event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return JSONResponse({}, status_code=202)

    async def _dispatch(self, event: Event) -> None:
        try:
            for handler in self._handlers[event.type]:
                await handler(event)
            self._handled += 1
            self.log.info("event handled", event_type=event.type, event_id=event.id)
        except Exception:
            # Forget the id so a redelivery is processed again; the failure is logged, not swallowed.
            self.state.delete("events", event.id)
            self.log.exception("event handler failed", event_type=event.type, event_id=event.id)
        if self.state.seen("meta", f"pruned-{clock.timestamp() // 3600}"):
            self.state.prune("events", EVENT_MEMORY_SECONDS)

    async def drain(self) -> None:
        """Wait for the events being processed. The tests' way to see the work of a 202."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    @asynccontextmanager
    async def _lifespan(self, app: FastAPI):
        self.manifest()  # a manifest the core would refuse stops the server here, not at install
        self.scheduler.start()
        try:
            yield
        finally:
            self.scheduler.stop()
            await self.drain()
            if self._client is not None:
                await self._client.aclose()
            if self._state is not None:
                self._state.close()


def _when(text: Any) -> datetime | None:
    try:
        return clock.parse_time(text) if isinstance(text, str) else None
    except ValueError:
        return None
