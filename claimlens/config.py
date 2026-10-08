"""Central settings.

Analogy: this is the thermostat panel for the whole project. Every knob lives
here (or in an environment variable) so you never hunt through code to change a
threshold, and secrets/model IDs never get hardcoded.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    region: str
    model_id: str            # used for extraction
    classify_model_id: str   # used for classification (can be a cheaper model)
    min_chars_per_page: int  # below this, a PDF page is treated as "probably scanned"
    max_input_chars: int     # cap on document text sent to the LLM (cost guardrail)
    max_disputed_amount: float
    min_classification_confidence: float
    textract_bucket: str         # S3 bucket for async Textract (multi-page PDFs); empty = sync only
    min_ocr_confidence: float    # mean OCR confidence (0-100) below which a human should look
    ocr_poll_seconds: float
    ocr_timeout_seconds: float
    database_url: str            # SQLAlchemy URL. Postgres in real life; SQLite file for quick local dev.
    redis_url: str               # empty = no Redis (fast-path cache disabled)
    cache_ttl_seconds: int
    idempotency_backend: str     # 'sql' (local/dev) or 'dynamodb'
    dynamodb_table: str
    dynamodb_endpoint: str       # set for DynamoDB Local; empty for real AWS
    lease_seconds: int           # how long one worker may "own" a document before others may take over
    max_upload_bytes: int
    api_tokens: str              # "token:user:role,token2:user2:role2"  (roles: reader<uploader<reviewer<admin)
    api_auth_disabled: bool      # dev only!


def get_settings() -> Settings:
    """Read settings fresh each call (so tests can change env vars)."""
    model_id = os.getenv("CLAIMLENS_MODEL_ID", "")
    return Settings(
        region=os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1")),
        model_id=model_id,
        classify_model_id=os.getenv("CLAIMLENS_CLASSIFY_MODEL_ID", "") or model_id,
        min_chars_per_page=int(os.getenv("CLAIMLENS_MIN_CHARS_PER_PAGE", "100")),
        max_input_chars=int(os.getenv("CLAIMLENS_MAX_INPUT_CHARS", "60000")),
        max_disputed_amount=float(os.getenv("CLAIMLENS_MAX_DISPUTED_AMOUNT", "500000")),
        min_classification_confidence=float(os.getenv("CLAIMLENS_MIN_CONFIDENCE", "0.7")),
        textract_bucket=os.getenv("CLAIMLENS_TEXTRACT_BUCKET", ""),
        min_ocr_confidence=float(os.getenv("CLAIMLENS_MIN_OCR_CONFIDENCE", "80")),
        ocr_poll_seconds=float(os.getenv("CLAIMLENS_OCR_POLL_SECONDS", "2")),
        ocr_timeout_seconds=float(os.getenv("CLAIMLENS_OCR_TIMEOUT_SECONDS", "300")),
        database_url=os.getenv("CLAIMLENS_DATABASE_URL", "sqlite:///claimlens.db"),
        redis_url=os.getenv("CLAIMLENS_REDIS_URL", ""),
        cache_ttl_seconds=int(os.getenv("CLAIMLENS_CACHE_TTL_SECONDS", "86400")),
        idempotency_backend=os.getenv("CLAIMLENS_IDEMPOTENCY", "sql"),
        dynamodb_table=os.getenv("CLAIMLENS_DYNAMODB_TABLE", "claimlens_idempotency"),
        dynamodb_endpoint=os.getenv("CLAIMLENS_DYNAMODB_ENDPOINT", ""),
        lease_seconds=int(os.getenv("CLAIMLENS_LEASE_SECONDS", "300")),
        max_upload_bytes=int(os.getenv("CLAIMLENS_MAX_UPLOAD_BYTES", str(20 * 1024 * 1024))),
        api_tokens=os.getenv("CLAIMLENS_API_TOKENS", ""),
        api_auth_disabled=os.getenv("CLAIMLENS_API_AUTH", "") == "disabled",
    )
