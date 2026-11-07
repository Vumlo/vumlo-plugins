# gradebook-export

Serves `POST /export`, producing an XLSX gradebook with a 5-point scale and
ECTS conversion. The endpoint accepts explicit JSON rows so it can also be
used independently.

## Needs from core

The plugin API exposes no gradebook read endpoint, so it cannot fetch a Vumlo
gradebook automatically. PDF export is not claimed until a PDF layout and
gradebook data contract are supplied.
