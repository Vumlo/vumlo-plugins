import os
import subprocess
import tempfile
from pathlib import Path
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import Response

app = FastAPI(title="pdf-optimizer")

@app.get("/manifest")
def manifest():
    return {"id": "pdf-optimizer", "version": "1.0.0", "api": "1", "subscribes": [], "scopes": [], "ui": [], "settings": {}, "routes": [{"method": "POST", "path": "/optimize", "roles": ["teacher"]}]}

# Not a route of the manifest: the core refuses the path "/" and passes on no HTML. For running the service on its own.
@app.get("/")
def upload_page():
    return """<!doctype html><title>PDF optimizer</title><form method=post action=/optimize enctype=multipart/form-data><input type=file name=pdf accept=application/pdf required><label><input type=checkbox name=ocr> OCR (ukr+eng)</label><button>Optimize</button></form>"""

@app.post("/optimize")
async def optimize(pdf: UploadFile = File(...), ocr: bool = Form(False)):
    if pdf.content_type != "application/pdf":
        return {"error": "application/pdf is required"}
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "input.pdf"
        target = Path(directory) / "output.pdf"
        source.write_bytes(await pdf.read())
        command = ["ocrmypdf", "--optimize", "1"]
        if ocr:
            command += ["-l", "ukr+eng"]
        command += [str(source), str(target)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=300)
        if result.returncode:
            return {"error": result.stderr[-1000:]}
        return Response(target.read_bytes(), media_type="application/pdf", headers={"Content-Disposition": "attachment; filename=optimized.pdf"})
