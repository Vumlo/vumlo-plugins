from fastapi.testclient import TestClient
from app import app
from vumlo_plugins.protocol import event_signature

def test_bad_signature_rejected(monkeypatch):
    monkeypatch.setenv("VUMLO_WEBHOOK_SECRET", "secret")
    client = TestClient(app)
    response = client.post("/events", content=b"{}", headers={"X-Vumlo-Timestamp": "1700000000", "X-Vumlo-Signature": "sha256=bad"})
    assert response.status_code == 401
