# completion-certificate

Serves `POST /certificate`, generating a PDF certificate with a QR code for a
caller-supplied verification URL.

## Needs from core

Core exposes no completion/grade aggregation endpoint and no certificate
verification route. The service does not assert completion; callers must
provide verified input and a verification URL.
