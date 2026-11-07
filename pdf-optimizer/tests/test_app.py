from fastapi.testclient import TestClient
from app import app
def test_upload_page(): assert TestClient(app).get("/").status_code == 200
