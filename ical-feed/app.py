from fastapi import FastAPI
from fastapi.responses import Response
from icalendar import Calendar

app = FastAPI(title="ical-feed")

@app.get("/manifest")
def manifest():
    return {"id": "ical-feed", "version": "1.0.0", "api": "1", "subscribes": [], "scopes": [], "ui": [], "settings": {"feed_token": {"type": "string", "secret": True, "description": "Private feed token"}}}

@app.get("/feed.ics")
def feed():
    calendar = Calendar()
    calendar.add("prodid", "-//Vumlo//Deadline feed//EN")
    calendar.add("version", "2.0")
    return Response(calendar.to_ical(), media_type="text/calendar")
