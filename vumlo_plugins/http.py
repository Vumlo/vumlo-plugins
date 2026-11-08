from __future__ import annotations

import json
import os
import threading

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .protocol import verify_event, verify_proxy


def create_plugin_app(plugin_manifest: dict, on_event=None) -> FastAPI:
    app = FastAPI(title=plugin_manifest["id"])
    seen: set[str] = set()
    lock = threading.Lock()
    app.state.received_events = []

    @app.get("/manifest")
    def get_manifest():
        return plugin_manifest

    @app.post("/events", status_code=202)
    async def receive_event(request: Request):
        body = await request.body()
        refused = verify_event(os.getenv("VUMLO_WEBHOOK_SECRET", ""), dict(request.headers), body)
        if refused:
            return JSONResponse({"error": refused[1]}, status_code=refused[0])
        try:
            event = json.loads(body)
            event_id = event["id"]
            event_type = event["type"]
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "not an event"}, status_code=400)
        with lock:
            first = event_id not in seen
            seen.add(event_id)
            if first:
                app.state.received_events.append(event)
        if first and on_event:
            on_event(event_type, event)
        return JSONResponse({}, status_code=202)

    @app.get("/health")
    def health():
        return {"plugin": plugin_manifest["id"], "received_events": len(app.state.received_events)}

    return app


async def signed_identity(request: Request) -> dict[str, str]:
    body = await request.body()
    identity = verify_proxy(
        os.getenv("VUMLO_WEBHOOK_SECRET", ""),
        dict(request.headers),
        body,
        method=request.method,
        target=request.url.path + (f"?{request.url.query}" if request.url.query else ""),
    )
    if identity is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="invalid Vumlo proxy signature")
    return identity
