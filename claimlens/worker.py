"""SQS worker: the process that runs on Fargate.

    S3 upload -> EventBridge -> SQS -> THIS WORKER -> ProcessingService (Phases 1-4)

Analogy: a mail-room clerk taking tickets off a rail. For each ticket the clerk:
  * keeps telling the rail "I'm still working on this one" (visibility heartbeat) so nobody else grabs it,
  * hands it back with a growing delay if something temporary went wrong (backoff),
  * throws it away only when it is finished or can never succeed,
  * and after N failed attempts SQS itself moves the ticket to the "problem pile" (the DLQ).

Safety properties:
  * At-least-once delivery is fine: the idempotency lease + unique constraint make reprocessing harmless.
  * A crashed or stopped worker loses nothing: its message simply becomes visible again.
  * Logs carry message IDs, upload IDs and outcomes only. S3 keys contain filenames, which can contain PHI.
"""
from __future__ import annotations

import json
import logging
import re
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import unquote_plus

from botocore.exceptions import BotoCoreError, ClientError

from claimlens.config import Settings
from claimlens.service import ProcessingService, TransientProcessingError, UnsupportedUpload
from claimlens.storage import repository as repo

log = logging.getLogger("claimlens.worker")

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MAX_BACKOFF_SECONDS = 900
IN_PROGRESS_RETRY_SECONDS = 30
NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound"}
LIVENESS_FILE = Path("/tmp/worker.alive")


@dataclass
class Outcome:
    delete: bool                 # True: remove the message. False: leave it for retry (after retry_delay)
    reason: str
    retry_delay: int = 0


def backoff_seconds(receive_count: int) -> int:
    """30s, 60s, 120s, 240s ... capped. Based on how many times SQS has already delivered this message."""
    return min(MAX_BACKOFF_SECONDS, 30 * 2 ** max(receive_count - 1, 0))


class _Heartbeat:
    """While a long job runs (OCR can take minutes), keep extending the message's invisibility."""

    def __init__(self, sqs, queue_url: str, receipt: str, visibility: int, interval: float):
        self._args = (sqs, queue_url, receipt, visibility)
        self._interval, self._stop = interval, threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self):
        sqs, queue_url, receipt, visibility = self._args
        while not self._stop.wait(self._interval):
            try:
                sqs.change_message_visibility(QueueUrl=queue_url, ReceiptHandle=receipt, VisibilityTimeout=visibility)
            except (ClientError, BotoCoreError) as exc:
                log.warning("heartbeat failed (%s)", exc.__class__.__name__,
                            extra={"event": "heartbeat_failed", "fields": {"operation": "heartbeat",
                                                                         "error_class": exc.__class__.__name__}})


class Worker:
    def __init__(self, service: ProcessingService, sqs, s3, session_factory, settings: Settings, *,
                 queue_url: Optional[str] = None, concurrency: Optional[int] = None, wait_seconds: int = 20,
                 heartbeat_seconds: Optional[float] = None):
        self.service, self.sqs, self.s3, self._sf, self.settings = service, sqs, s3, session_factory, settings
        self.queue_url = queue_url or settings.sqs_queue_url
        self.concurrency = max(1, concurrency or settings.worker_concurrency)
        self.wait_seconds = wait_seconds
        self.visibility = settings.queue_visibility_seconds
        self.heartbeat_seconds = heartbeat_seconds or max(self.visibility / 3, 1)
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="doc")
        self.processed = 0

    # ---- lifecycle ----------------------------------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        """Poll until asked to stop. Fargate sends SIGTERM on scale-in/deploy and waits `stopTimeout`:
        we finish the messages already in hand and exit (anything unfinished just reappears on the queue)."""
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: self.stop())
        log.info("worker started: concurrency=%d visibility=%ds", self.concurrency, self.visibility,
                 extra={"event": "worker_started", "fields": {"concurrency": self.concurrency}})
        while not self._stop.is_set():
            try:
                self.run_once()
            except (ClientError, BotoCoreError) as exc:
                log.error("queue error (%s); backing off", exc.__class__.__name__,
                          extra={"event": "queue_error", "fields": {"error_class": exc.__class__.__name__}})
                self._stop.wait(5)
            self._touch_liveness()
        self._pool.shutdown(wait=True)
        log.info("worker stopped cleanly after %d message(s)", self.processed, extra={"event": "worker_stopped"})

    @staticmethod
    def _touch_liveness() -> None:
        try:
            LIVENESS_FILE.touch()  # the ECS container health check looks at this file's age
        except OSError:
            pass

    # ---- one polling round ------------------------------------------------------------------------------
    def run_once(self) -> int:
        resp = self.sqs.receive_message(
            QueueUrl=self.queue_url, MaxNumberOfMessages=min(10, self.concurrency), WaitTimeSeconds=self.wait_seconds,
            VisibilityTimeout=self.visibility, AttributeNames=["ApproximateReceiveCount"])
        messages = resp.get("Messages", [])
        list(self._pool.map(self._handle, messages))
        self.processed += len(messages)
        return len(messages)

    def _handle(self, msg: dict) -> None:
        receipt, mid = msg["ReceiptHandle"], msg.get("MessageId", "?")
        receives = int(msg.get("Attributes", {}).get("ApproximateReceiveCount", "1"))
        started = time.monotonic()
        try:
            with _Heartbeat(self.sqs, self.queue_url, receipt, self.visibility, self.heartbeat_seconds):
                outcome = self._process(msg, receives)
        except Exception as exc:  # never let one bad message kill the worker
            log.error("message=%s unexpected %s; will retry", mid, exc.__class__.__name__,
                      extra={"event": "message_retry", "fields": {"error_class": exc.__class__.__name__}})
            outcome = Outcome(False, "unexpected_error", backoff_seconds(receives))
        try:
            if outcome.delete:
                self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=receipt)
            else:
                self.sqs.change_message_visibility(QueueUrl=self.queue_url, ReceiptHandle=receipt,
                                                   VisibilityTimeout=outcome.retry_delay)
        except (ClientError, BotoCoreError) as exc:
            log.error("message=%s could not %s (%s)", mid, "delete" if outcome.delete else "requeue", exc.__class__.__name__,
                      extra={"event": "queue_ack_failed", "fields": {"error_class": exc.__class__.__name__,
                                                                  "operation": "delete" if outcome.delete else "requeue"}})
        log.info("message=%s outcome=%s delete=%s receives=%d ms=%d", mid, outcome.reason, outcome.delete, receives,
                 int((time.monotonic() - started) * 1000),
                 extra={"event": "message_finished", "fields": {"outcome": outcome.reason, "receives": receives,
                                                               "elapsed_ms": int((time.monotonic() - started) * 1000)}})

    # ---- the actual work ----------------------------------------------------------------------------------
    def _process(self, msg: dict, receives: int) -> Outcome:
        try:
            event = json.loads(msg["Body"])
        except (ValueError, KeyError):
            return Outcome(True, "malformed_json")
        detail = event.get("detail") if isinstance(event, dict) else None
        if not isinstance(detail, dict) or event.get("detail-type") != "Object Created":
            return Outcome(True, "not_an_object_created_event")
        bucket = (detail.get("bucket") or {}).get("name")
        raw_key = (detail.get("object") or {}).get("key")
        if not bucket or not raw_key:
            return Outcome(True, "missing_bucket_or_key")

        located = self._locate(bucket, raw_key)
        if located is None:
            return Outcome(True, "object_missing_or_outside_upload_prefix")
        key, size = located
        upload_id = self._upload_id(key)

        if size > self.settings.max_upload_bytes:
            self._finish(bucket, key, upload_id, "failed", None, "too_large", tag="quarantined")
            return Outcome(True, "too_large")

        data = self.s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        try:
            result = self.service.ingest(data, Path(key).name, actor="worker")
        except UnsupportedUpload:
            self._finish(bucket, key, upload_id, "failed", None, "unsupported_type", tag="quarantined")
            return Outcome(True, "unsupported_type")
        except TransientProcessingError:
            return Outcome(False, "transient_failure", backoff_seconds(receives))

        if result.outcome == "in_progress":
            return Outcome(False, "in_progress_elsewhere", IN_PROGRESS_RETRY_SECONDS)
        state = "processed" if result.outcome == "created" else "duplicate"
        self._finish(bucket, key, upload_id, state, result.document_id, None, tag="processed")
        return Outcome(True, result.outcome)

    def _locate(self, bucket: str, raw_key: str) -> Optional[tuple[str, int]]:
        """Find the object. EventBridge keys are normally plain, but if one arrives URL-encoded (as in classic S3
        notifications) the decoded form is tried as a fallback."""
        for candidate in dict.fromkeys([raw_key, unquote_plus(raw_key)]):
            if not candidate.startswith(self.settings.upload_prefix):
                continue
            try:
                head = self.s3.head_object(Bucket=bucket, Key=candidate)
                return candidate, int(head["ContentLength"])
            except ClientError as exc:
                if exc.response["Error"]["Code"] not in NOT_FOUND_CODES:
                    raise
        return None

    def _upload_id(self, key: str) -> Optional[str]:
        parts = key[len(self.settings.upload_prefix):].split("/")
        return parts[0] if len(parts) >= 2 and UUID_RE.match(parts[0]) else None

    def _finish(self, bucket, key, upload_id, state, document_id, error, tag) -> None:
        """Best-effort bookkeeping. Failing to tag or update a status row must not fail (and so re-run) the document."""
        try:
            tags = [{"Key": "claimlens-status", "Value": tag}] + ([{"Key": "claimlens-document", "Value": document_id}] if document_id else [])
            self.s3.put_object_tagging(Bucket=bucket, Key=key, Tagging={"TagSet": tags})
        except (ClientError, BotoCoreError) as exc:
            log.warning("could not tag object (%s)", exc.__class__.__name__,
                        extra={"event": "bookkeeping_failed", "fields": {"operation": "tag",
                                                                        "error_class": exc.__class__.__name__}})
        if upload_id:
            try:
                with self._sf() as s:
                    repo.mark_upload(s, upload_id, state, document_id, error)
                    s.commit()
            except Exception as exc:
                log.warning("could not update upload status (%s)", exc.__class__.__name__,
                            extra={"event": "bookkeeping_failed", "fields": {"operation": "upload_status",
                                                                            "error_class": exc.__class__.__name__}})
