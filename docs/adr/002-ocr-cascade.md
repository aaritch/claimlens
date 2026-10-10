# ADR 002: Local-first OCR with confidence-gated Textract escalation

**Status:** accepted.

Native parsing is cheapest. Sparse scanned files go to Tesseract first when cascade mode is selected.
Escalate low confidence/density to Textract. Low-confidence/incomplete OCR cannot silently auto-accept.

Consequences: CPU usage and OCR quality must be measured on representative documents. Oracle/mock readers only
test routing and economics, never prove real OCR accuracy. Multi-page Textract uses an async temporary S3 copy.
