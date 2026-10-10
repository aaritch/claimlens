"""Fail-closed structured logging: free text, exception messages and request URLs are never rendered."""
from __future__ import annotations

import json
import logging
import math
from copy import copy
from datetime import datetime, timezone

EVENTS = {
    "unstructured_log_suppressed", "processing_finished", "heartbeat_failed", "worker_started",
    "worker_stopped", "queue_error", "message_retry", "queue_ack_failed", "message_finished",
    "bookkeeping_failed", "command_failed",
}
LABELS = {
    "status": {"validated", "needs_review", "rejected", "needs_ocr", "error"},
    "outcome": {"created", "duplicate", "in_progress", "transient_failure", "unexpected_error",
                "malformed_json", "not_an_object_created_event", "missing_bucket_or_key",
                "object_missing_or_outside_upload_prefix", "too_large", "unsupported_type", "in_progress_elsewhere"},
    "error_class": {"RuntimeError", "ClientError", "BotoCoreError", "ConnectionError", "TimeoutError",
                    "OSError", "ValueError", "UnknownError"},
    "operation": {"delete", "requeue", "heartbeat", "tag", "upload_status"},
}
NUMBERS = {"input_tokens", "output_tokens", "cost_usd", "elapsed_ms", "ocr_pages", "receives", "concurrency"}


class SafeLogFilter(logging.Filter):
    """Allow only known metadata, never try to guess whether arbitrary prose contains PHI."""

    def filter(self, record: logging.LogRecord) -> bool:
        event = getattr(record, "event", "unstructured_log_suppressed")
        if not isinstance(event, str) or event not in EVENTS:
            event = "unstructured_log_suppressed"
        payload = {
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "event": event,
        }
        fields = getattr(record, "fields", {})
        if isinstance(fields, dict):
            for key, value in fields.items():
                if key in NUMBERS and type(value) in (int, float) and math.isfinite(value) and value >= 0:
                    payload[key] = value
                elif key in LABELS and isinstance(value, str) and value in LABELS[key]:
                    payload[key] = value
        # Do not allow the formatter to append a traceback (SQL/HTTP exceptions can include secrets).
        record.msg, record.args = json.dumps(payload), ()
        record.exc_info, record.exc_text, record.stack_info = None, None, None
        return True


def configure_logging(level: int = logging.WARNING) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(SafeJsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)


class SafeJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Do not mutate a record shared with other handlers (including pytest's capture handler).
        safe = copy(record)
        SafeLogFilter().filter(safe)
        return safe.getMessage()
