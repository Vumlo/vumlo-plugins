from io import BytesIO
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from openpyxl import Workbook

app = FastAPI(title="gradebook-export")

@app.get("/manifest")
def manifest():
    return {"id": "gradebook-export", "version": "1.0.0", "api": "1", "subscribes": [], "scopes": ["grades:read", "roster:read"], "ui": [], "settings": {}, "routes": [{"method": "POST", "path": "/export", "roles": ["teacher"]}]}

@app.post("/export")
def export(rows: list[dict]):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Student", "Points", "5-point", "ECTS"])
    for row in rows:
        points = float(row.get("points", 0))
        scale = row.get("max_points", 100) or 100
        percent = points / scale * 100
        five = 5 if percent >= 90 else 4 if percent >= 75 else 3 if percent >= 60 else 2 if percent >= 50 else 1
        ects = "A" if percent >= 90 else "B" if percent >= 82 else "C" if percent >= 75 else "D" if percent >= 67 else "E" if percent >= 60 else "FX" if percent >= 35 else "F"
        sheet.append([row.get("student", ""), points, five, ects])
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment; filename=gradebook.xlsx"})
