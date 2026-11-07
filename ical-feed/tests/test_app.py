from fastapi.testclient import TestClient
from app import app

def test_valid_ical():
    assert TestClient(app).get("/feed.ics").content.startswith(b"BEGIN:VCALENDAR")
