from fastapi.testclient import TestClient
from app import app

def test_valid_ical(monkeypatch):
    monkeypatch.setenv("VUMLO_FEED_TOKEN", "t")
    assert TestClient(app).get("/feed.ics", params={"token": "t"}).content.startswith(b"BEGIN:VCALENDAR")

def test_feed_needs_its_token(monkeypatch):
    monkeypatch.setenv("VUMLO_FEED_TOKEN", "t")
    assert TestClient(app).get("/feed.ics").status_code == 401
    assert TestClient(app).get("/feed.ics", params={"token": "nope"}).status_code == 401
    monkeypatch.delenv("VUMLO_FEED_TOKEN")
    assert TestClient(app).get("/feed.ics", params={"token": ""}).status_code == 401, "no token configured: nothing is served"
