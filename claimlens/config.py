"""Central settings.

Analogy: this is the thermostat panel for the whole project. Every knob lives
here (or in an environment variable) so you never hunt through code to change a
threshold, and secrets/model IDs never get hardcoded.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote


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
    documents_bucket: str        # S3 bucket that receives uploads (presigned POST targets it)
    upload_prefix: str           # only keys under this prefix are processed
    upload_url_ttl_seconds: int
    sqs_queue_url: str           # the worker's queue
    worker_concurrency: int      # messages processed in parallel by one worker task
    queue_visibility_seconds: int
    sync_upload_enabled: bool    # POST /documents (process inside the request). Off in production.


def _database_url() -> str:
    """Explicit URL wins. Otherwise assemble from parts (ECS injects host/user/password separately, and the
    password must be URL-escaped safely, which SQLAlchemy's URL builder does for us)."""
    explicit = os.getenv("CLAIMLENS_DATABASE_URL")
    if explicit:
        return explicit
    host = os.getenv("CLAIMLENS_DB_HOST")
    if host:
        from sqlalchemy.engine import URL
        return URL.create(
            "postgresql+psycopg", username=os.getenv("CLAIMLENS_DB_USER", "claimlens"),
            password=os.getenv("CLAIMLENS_DB_PASSWORD"), host=host, port=int(os.getenv("CLAIMLENS_DB_PORT", "5432")),
            database=os.getenv("CLAIMLENS_DB_NAME", "claimlens"),
            query={"sslmode": os.getenv("CLAIMLENS_DB_SSLMODE", "require")},
        ).render_as_string(hide_password=False)
    return "sqlite:///claimlens.db"


def _redis_url() -> str:
    explicit = os.getenv("CLAIMLENS_REDIS_URL")
    if explicit:
        return explicit
    host = os.getenv("CLAIMLENS_REDIS_HOST")
    if not host:
        return ""
    scheme = "rediss" if os.getenv("CLAIMLENS_REDIS_TLS", "1") == "1" else "redis"
    token = os.getenv("CLAIMLENS_REDIS_AUTH_TOKEN", "")
    auth = f":{quote(token, safe='')}@" if token else ""
    return f"{scheme}://{auth}{host}:{os.getenv('CLAIMLENS_REDIS_PORT', '6379')}/0"


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
        database_url=_database_url(),
        redis_url=_redis_url(),
        cache_ttl_seconds=int(os.getenv("CLAIMLENS_CACHE_TTL_SECONDS", "86400")),
        idempotency_backend=os.getenv("CLAIMLENS_IDEMPOTENCY", "sql"),
        dynamodb_table=os.getenv("CLAIMLENS_DYNAMODB_TABLE", "claimlens_idempotency"),
        dynamodb_endpoint=os.getenv("CLAIMLENS_DYNAMODB_ENDPOINT", ""),
        lease_seconds=int(os.getenv("CLAIMLENS_LEASE_SECONDS", "300")),
        max_upload_bytes=int(os.getenv("CLAIMLENS_MAX_UPLOAD_BYTES", str(20 * 1024 * 1024))),
        api_tokens=os.getenv("CLAIMLENS_API_TOKENS", ""),
        api_auth_disabled=os.getenv("CLAIMLENS_API_AUTH", "") == "disabled",
        documents_bucket=os.getenv("CLAIMLENS_DOCUMENTS_BUCKET", ""),
        upload_prefix=os.getenv("CLAIMLENS_UPLOAD_PREFIX", "incoming/"),
        upload_url_ttl_seconds=int(os.getenv("CLAIMLENS_UPLOAD_URL_TTL_SECONDS", "300")),
        sqs_queue_url=os.getenv("CLAIMLENS_SQS_QUEUE_URL", ""),
        worker_concurrency=int(os.getenv("CLAIMLENS_WORKER_CONCURRENCY", "4")),
        queue_visibility_seconds=int(os.getenv("CLAIMLENS_QUEUE_VISIBILITY_SECONDS", "900")),
        sync_upload_enabled=os.getenv("CLAIMLENS_SYNC_UPLOAD", "1") == "1",
    )
