# pdf-optimizer

Standalone `POST /optimize` multipart service and minimal upload page at `/`.
It uses OCRmyPDF/Tesseract with `ukr+eng` when OCR is selected; OCR is an
option separate from optimization. The OCR backend is the `ocrmypdf` command.

## Needs from core

Core has no pre-upload hook. Submit-flow integration is intentionally out of
scope; callers upload directly to this service.
