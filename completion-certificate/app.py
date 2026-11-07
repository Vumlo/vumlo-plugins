from io import BytesIO
import qrcode
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

app = FastAPI(title="completion-certificate")

@app.get("/manifest")
def manifest():
    return {"id": "completion-certificate", "version": "1.0.0", "api": "1", "subscribes": [], "scopes": [], "ui": [], "settings": {"verification_base_url": {"type": "string", "description": "Verification URL base"}}}

@app.post("/certificate")
def certificate(data: dict):
    verification = str(data.get("verification_url", ""))
    qr = qrcode.make(verification)
    qr_buffer = BytesIO()
    qr.save(qr_buffer, format="PNG")
    qr_buffer.seek(0)
    output = BytesIO()
    canvas = Canvas(output, pagesize=A4)
    canvas.setFont("Helvetica-Bold", 24)
    canvas.drawCentredString(297, 700, "Certificate of Completion")
    canvas.setFont("Helvetica", 16)
    canvas.drawCentredString(297, 650, str(data.get("student", "")))
    canvas.drawImage(ImageReader(qr_buffer), 240, 430, width=110, height=110)
    canvas.save()
    output.seek(0)
    return StreamingResponse(output, media_type="application/pdf", headers={"Content-Disposition": "attachment; filename=certificate.pdf"})
