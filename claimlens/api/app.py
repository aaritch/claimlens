"""The REST API.

  POST /documents               upload + process (uploader)        201 created | 200 duplicate | 202 in progress
  GET  /documents/{id}          one document (reader)
  GET  /documents               list, filter by status, paginate (reader)
  GET  /claims                  the human review queue (reader)    default status=needs_review
  POST /documents/{id}/review   approve (with corrections) or reject (reviewer)
  GET  /healthz, /readyz        liveness / readiness (no auth)

Phase 5 replaces "upload bytes to the API" with "presigned S3 upload + queue + worker"; the resource shapes stay.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, Response, UploadFile
from sqlalchemy import text

from claimlens.api.auth import Actor, Authenticator
from claimlens.api.schemas import (DocumentList, DocumentOut, IngestResponse, ReviewIn, UploadRequest, UploadStatus,
                                   UploadTicket)
from claimlens.config import Settings
from claimlens.service import (ALLOWED_EXTENSIONS, ProcessingService, TransientProcessingError, UnsupportedUpload,
                               safe_basename)
from claimlens.storage import repository as repo

log = logging.getLogger(__name__)
def _default_s3():
    import boto3
    from botocore.config import Config
    return boto3.client("s3", config=Config(signature_version="s3v4"))


STATUSES = {"validated", "needs_review", "rejected", "needs_ocr", "error"}


def create_app(service: ProcessingService, session_factory, settings: Settings, authenticator: Authenticator,
               enable_docs: bool = True, s3_client=None) -> FastAPI:
    app = FastAPI(title="ClaimLens API", version="0.4.0",
                  docs_url="/docs" if enable_docs else None, redoc_url=None,
                  openapi_url="/openapi.json" if enable_docs else None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"          # responses can contain regulated data: never cache them
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def actor_for(min_role: str):
        def dependency(authorization: Optional[str] = Header(default=None)) -> Actor:
            actor = authenticator.authenticate(authorization)
            if not actor.allows(min_role):
                raise HTTPException(403, f"requires role '{min_role}' or higher")
            return actor
        return dependency

    # ---- health ----
    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        try:
            with session_factory() as s:
                s.execute(text("select 1"))
        except Exception:
            raise HTTPException(503, "database unavailable")
        return {"status": "ready"}

    # ---- helpers ----
    def load(document_id: str) -> DocumentOut:
        with session_factory() as s:
            doc = repo.get_document(s, document_id)
            if doc is None:
                raise HTTPException(404, "document not found")
            return DocumentOut(**repo.document_to_dict(doc))

    # ---- upload: asynchronous (production) ----
    @app.post("/uploads", response_model=UploadTicket, status_code=201)
    def request_upload(body: UploadRequest, actor: Actor = Depends(actor_for("uploader"))):
        """Step 1 of 2: get a short-lived, size-limited ticket to upload straight to S3. The API never touches the bytes."""
        if not settings.documents_bucket:
            raise HTTPException(503, "uploads are not configured on this server")
        name = safe_basename(body.filename)
        if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
            raise HTTPException(415, f"unsupported file type '{Path(name).suffix.lower()}'")
        upload_id = str(uuid.uuid4())
        key = f"{settings.upload_prefix}{upload_id}/{name}"
        client = s3_client or _default_s3()
        post = client.generate_presigned_post(
            Bucket=settings.documents_bucket, Key=key,
            Conditions=[["content-length-range", 1, settings.max_upload_bytes]],  # S3 itself rejects oversize/empty files
            ExpiresIn=settings.upload_url_ttl_seconds)
        with session_factory() as s:
            repo.create_upload(s, upload_id, key, name, actor.name)
            repo.audit(s, actor.name, "upload_requested", None, {"upload_id": upload_id})
            s.commit()
        return UploadTicket(upload_id=upload_id, url=post["url"], fields=post["fields"],
                            expires_in=settings.upload_url_ttl_seconds, max_bytes=settings.max_upload_bytes)

    @app.get("/uploads/{upload_id}", response_model=UploadStatus)
    def upload_status(upload_id: str, actor: Actor = Depends(actor_for("uploader"))):
        """Step 2 of 2: poll until state is processed / duplicate / failed, then GET /documents/{document_id}."""
        with session_factory() as s:
            row = repo.get_upload(s, upload_id)
            # Uploaders see only their own uploads; reviewers and admins see all. Others get 404 (not 403) so IDs can't be probed.
            if row is None or (row.requested_by != actor.name and not actor.allows("reviewer")):
                raise HTTPException(404, "upload not found")
            return UploadStatus(upload_id=row.id, state=row.state, document_id=row.document_id, error=row.error)

    # ---- upload: synchronous (local demos and tests; switch off in production) ----
    if settings.sync_upload_enabled:
        # ---- upload ----
        @app.post("/documents", response_model=IngestResponse, status_code=201)
        def upload(response: Response, file: UploadFile = File(...), content_length: Optional[int] = Header(default=None),
                   actor: Actor = Depends(actor_for("uploader"))):
            limit = settings.max_upload_bytes
            if content_length is not None and content_length > limit + 4096:  # small allowance for multipart framing
                raise HTTPException(413, f"file larger than {limit} bytes")
            data = file.file.read(limit + 1)
            if len(data) > limit:
                raise HTTPException(413, f"file larger than {limit} bytes")
            if not data:
                raise HTTPException(400, "empty file")
            try:
                outcome = service.ingest(data, file.filename or "upload", actor.name)
            except UnsupportedUpload as exc:
                raise HTTPException(415, str(exc))
            except TransientProcessingError as exc:
                raise HTTPException(503, "temporary processing failure, please retry",
                                    headers={"Retry-After": str(exc.retry_after)})
            if outcome.outcome == "in_progress":
                response.status_code = 202
                response.headers["Retry-After"] = "5"
                return IngestResponse(outcome="in_progress", detail="this document is being processed by another request")
            if outcome.outcome == "duplicate":
                response.status_code = 200
            response.headers["Location"] = f"/documents/{outcome.document_id}"
            return IngestResponse(outcome=outcome.outcome, document=load(outcome.document_id))


    # ---- reads (every read is audited: who looked at what) ----
    @app.get("/documents/{document_id}", response_model=DocumentOut)
    def get_document(document_id: str, actor: Actor = Depends(actor_for("reader"))):
        out = load(document_id)
        with session_factory() as s:
            repo.audit(s, actor.name, "view", document_id)
            s.commit()
        return out

    def _list(actor: Actor, status_filter: Optional[str], limit: int, cursor: Optional[str], action: str) -> DocumentList:
        if status_filter and status_filter not in STATUSES:
            raise HTTPException(422, f"status must be one of {sorted(STATUSES)}")
        with session_factory() as s:
            try:
                docs, next_cursor = repo.list_documents(s, status_filter, limit, cursor)
            except ValueError:
                raise HTTPException(400, "invalid cursor")
            repo.audit(s, actor.name, action, None, {"status": status_filter, "returned": len(docs)})
            s.commit()
            return DocumentList(items=[DocumentOut(**repo.document_to_dict(d)) for d in docs], next_cursor=next_cursor)

    @app.get("/documents", response_model=DocumentList)
    def list_documents(status_filter: Optional[str] = Query(default=None, alias="status"),
                       limit: int = Query(default=50, ge=1, le=200), cursor: Optional[str] = None,
                       actor: Actor = Depends(actor_for("reader"))):
        return _list(actor, status_filter, limit, cursor, "list")

    @app.get("/claims", response_model=DocumentList)
    def review_queue(status_filter: str = Query(default="needs_review", alias="status"),
                     limit: int = Query(default=50, ge=1, le=200), cursor: Optional[str] = None,
                     actor: Actor = Depends(actor_for("reader"))):
        return _list(actor, status_filter, limit, cursor, "list_claims")

    # ---- review ----
    @app.post("/documents/{document_id}/review", response_model=DocumentOut)
    def review(document_id: str, body: ReviewIn, actor: Actor = Depends(actor_for("reviewer"))):
        with session_factory() as s:
            try:
                repo.apply_review(s, document_id, actor.name, body.decision, settings,
                                  expected_version=body.expected_version, corrections=body.corrections, note=body.note)
                s.commit()
            except repo.NotFound:
                s.rollback()
                raise HTTPException(404, "document not found")
            except repo.VersionConflict as exc:
                s.rollback()
                raise HTTPException(409, f"{exc}. Reload the document and try again.")
            except repo.ReviewNotAllowed as exc:
                s.rollback()
                raise HTTPException(409, str(exc))
            except repo.CorrectionInvalid as exc:
                s.rollback()
                raise HTTPException(422, {"problems": exc.problems})
        return load(document_id)

    return app
