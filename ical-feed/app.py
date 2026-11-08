import os
from fastapi import FastAPI, Query
from fastapi.responses import Response
from icalendar import Calendar

app = FastAPI(title="ical-feed")

@app.get("/manifest")
def manifest():
    return {"id": "ical-feed", "version": "1.0.0", "api": "1", "subscribes": ["assignment.created", "assignment.updated", "assignment.deadline_changed"], "scopes": ["assignments:read"], "ui": [], "settings": {"feed_token": {"type": "string", "secret": True, "description": "Private feed token"}}, "routes": [{"method": "GET", "path": "/feed.ics", "auth": "none"}]}

@app.get("/feed.ics")
def feed(token: str = Query("")):
    if not token or token != os.getenv("VUMLO_FEED_TOKEN", ""):
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="invalid feed token")
    calendar = Calendar()
    calendar.add("prodid", "-//Vumlo//Deadline feed//EN")
    calendar.add("version", "2.0")
    return Response(calendar.to_ical(), media_type="text/calendar")
