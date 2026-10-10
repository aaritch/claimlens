"""The orchestrator for ONE upload: fast-path check -> database check -> lease -> process -> save.

Three layers of duplicate protection, each with a different job (defense in depth):
  1. Redis    : fast path. "Seen this before?" No database round-trip. Optional; may be wrong or down.
  2. DynamoDB : the lease. Guarantees only ONE worker processes a given document at a time, and that a
                crashed worker's job can be safely taken over. Correctness under concurrency.
  3. Postgres : the record + a UNIQUE(content_sha256, fingerprint) constraint. Even if layers 1-2 both
                failed, the database refuses a second copy.

"Same document" means same bytes AND same recipe (model, prompts, OCR, rules). Re-uploading a file after
you improve the prompt SHOULD reprocess it; re-uploading it unchanged should not cost a cent.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from sqlalchemy.exc import IntegrityError

from claimlens.config import Settings, get_settings
from claimlens.llm.prompts import load_prompt
from claimlens.pipeline import CLASSIFY_PROMPT, EXTRACT_PROMPT, process_file
from claimlens.rules.engine import RULES_VERSION
from claimlens.observability import Metrics
from claimlens.storage import repository as repo
from claimlens.storage.cache import NullCache, ResultCache
from claimlens.storage.idempotency import DONE, IN_PROGRESS, IdempotencyStore

log = logging.getLogger(__name__)
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".eml", ".txt", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}


class TransientProcessingError(Exception):
    """A retryable failure (throttling, timeout). Nothing was saved; the caller should retry later."""

    def __init__(self, message: str, retry_after: int = 30):
        super().__init__(message)
        self.retry_after = retry_after


class UnsupportedUpload(Exception):
    pass


@dataclass
class IngestOutcome:
    outcome: Literal["created", "duplicate", "in_progress"]
    document_id: Optional[str]


def safe_basename(filename: str) -> str:
    name = Path((filename or "upload").replace("\\", "/")).name
    return re.sub(r"[\x00-\x1f\x7f]", "", name)[:255] or "upload"


class ProcessingService:
    def __init__(self, session_factory, llm, idempotency: IdempotencyStore, *, classify_llm=None, ocr=None,
                 cache: Optional[ResultCache] = None, settings: Optional[Settings] = None,
                 classify_prompt: str = CLASSIFY_PROMPT, extract_prompt: str = EXTRACT_PROMPT, ocr_spec: str = "none"):
        self._sf, self.llm, self.idempotency = session_factory, llm, idempotency
        self.classify_llm, self.ocr = classify_llm or llm, ocr
        self.cache = cache or NullCache()
        self.settings = settings or get_settings()
        self.classify_prompt, self.extract_prompt, self.ocr_spec = classify_prompt, extract_prompt, ocr_spec
        self.metrics = Metrics(self.settings.environment, self.settings.metrics_enabled)

    @property
    def fingerprint(self) -> str:
        """Short hash of everything that could change the answer. Prompt TEXT is hashed (not just its name),
        so editing a prompt file automatically makes old results 'a different recipe'."""
        recipe = {
            "model": getattr(self.llm, "model_id", ""), "classify_model": getattr(self.classify_llm, "model_id", ""),
            "classify_prompt": hashlib.sha256(load_prompt(self.classify_prompt).encode()).hexdigest()[:12],
            "extract_prompt": hashlib.sha256(load_prompt(self.extract_prompt).encode()).hexdigest()[:12],
            "ocr": self.ocr_spec, "rules": RULES_VERSION,
        }
        return hashlib.sha256(json.dumps(recipe, sort_keys=True).encode()).hexdigest()[:16]

    def ingest(self, data: bytes, filename: str, actor: str) -> IngestOutcome:
        source_name = safe_basename(filename)
        ext = Path(source_name).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise UnsupportedUpload("unsupported file type")
        if self.settings.opaque_source_names:
            source_name = f"{uuid.uuid4().hex}{ext}"

        sha, fp = hashlib.sha256(data).hexdigest(), self.fingerprint
        key = f"{sha}:{fp}"

        # Layer 1: Redis fast path (may be stale: verify the row still exists)
        hit = self.cache.get(key)
        if hit:
            with self._sf() as s:
                if repo.get_document(s, hit["document_id"]):
                    return IngestOutcome("duplicate", hit["document_id"])

        # Layer 3 (checked early, it's the source of truth): already stored?
        with self._sf() as s:
            existing = repo.find_by_hash(s, sha, fp)
            if existing:
                self.cache.put(key, {"document_id": existing.id, "status": existing.status})
                return IngestOutcome("duplicate", existing.id)

        # Layer 2: take the lease
        owner = uuid.uuid4().hex
        lease = self.idempotency.begin(key, owner, self.settings.lease_seconds)
        if not lease.acquired:
            if lease.status == DONE and lease.document_id:
                return IngestOutcome("duplicate", lease.document_id)
            return IngestOutcome("in_progress", None)  # another worker owns it right now

        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / f"upload{ext}"  # fixed name on disk: the client's filename never touches the filesystem
                path.write_bytes(data)
                result = process_file(path, self.llm, self.settings, self.classify_llm,
                                      classify_prompt=self.classify_prompt, extract_prompt=self.extract_prompt,
                                      ocr=self.ocr)
            self.metrics.record(result)

            if result.status == "error" and result.error_kind == "transient":
                self.idempotency.fail(key, owner, result.error or "transient error")
                raise TransientProcessingError(result.error or "temporary failure")

            doc_id = self._save(result, sha, fp, source_name, actor)
            self.idempotency.complete(key, owner, doc_id)
            self.cache.put(key, {"document_id": doc_id, "status": result.status})
            return IngestOutcome("created" if doc_id else "duplicate", doc_id)
        except TransientProcessingError:
            raise
        except _RaceLost as lost:
            self.idempotency.complete(key, owner, lost.document_id)
            self.cache.put(key, {"document_id": lost.document_id, "status": "unknown"})
            return IngestOutcome("duplicate", lost.document_id)
        except Exception as exc:
            self.idempotency.fail(key, owner, exc.__class__.__name__)  # class name only: messages could echo data
            raise

    def _save(self, result, sha, fp, source_name, actor) -> str:
        with self._sf() as s:
            try:
                doc = repo.save_result(s, result, content_sha256=sha, fingerprint=fp, source_name=source_name)
                repo.audit(s, actor, "ingest", doc.id, {"status": result.status})
                s.commit()
                return doc.id
            except IntegrityError:
                s.rollback()
                existing = repo.find_by_hash(s, sha, fp)
                if existing is None:
                    raise
                raise _RaceLost(existing.id)


class _RaceLost(Exception):
    """Another writer inserted the same document first; the unique constraint caught it."""

    def __init__(self, document_id: str):
        super().__init__(document_id)
        self.document_id = document_id
