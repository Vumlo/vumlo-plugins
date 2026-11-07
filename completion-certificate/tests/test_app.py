from fastapi.testclient import TestClient
from app import app

def test_pdf_certificate():
    response = TestClient(app).post("/certificate", json={"student": "A", "verification_url": "https://example.test/v"})
    assert response.headers["content-type"] == "application/pdf"
