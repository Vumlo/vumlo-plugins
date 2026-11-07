from fastapi.testclient import TestClient
from app import app
def test_xlsx_export(): assert TestClient(app).post("/export", json=[{"student":"A","points":90}]).headers["content-type"].startswith("application/vnd.openxmlformats")
