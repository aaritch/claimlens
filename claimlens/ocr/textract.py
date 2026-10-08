"""AWS Textract engine.

Three code paths (Textract's own API limits force this):
  * image, or single-page PDF  -> synchronous detect_document_text with raw bytes
  * multi-page PDF + S3 bucket -> ASYNC start/get_document_text_detection (production path)
  * multi-page PDF, no bucket  -> split into single pages, one sync call each (handy locally)

Analogy: the sync call is a counter clerk who reads one page while you wait. The
async call is dropping a whole binder in the in-tray and coming back later for the
result: you must hand over the binder via S3, then poll (or get an SNS ping) until done.
"""
from __future__ import annotations

import io
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pypdf import PdfReader, PdfWriter

from claimlens.llm.pricing import estimate_ocr_cost
from claimlens.ocr.base import OcrError, OcrResult

SYNC_MAX_BYTES = 10 * 1024 * 1024  # Textract sync limit for bytes
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}
TRANSIENT_CODES = {"ThrottlingException", "ProvisionedThroughputExceededException", "InternalServerError",
                   "ServiceUnavailableException", "LimitExceededException"}


def blocks_to_text(blocks: list[dict]) -> tuple[str, Optional[float], int]:
    """LINE blocks -> (text in page order, mean confidence, page count)."""
    pages: dict[int, list[str]] = {}
    confidences: list[float] = []
    for b in blocks:
        if b.get("BlockType") != "LINE":
            continue
        pages.setdefault(b.get("Page", 1), []).append(b.get("Text", ""))
        if "Confidence" in b:
            confidences.append(float(b["Confidence"]))
    text = "\n\n".join("\n".join(pages[n]) for n in sorted(pages))
    mean = sum(confidences) / len(confidences) if confidences else None
    return text, mean, (max(pages) if pages else 0)


class TextractEngine:
    name = "textract"

    def __init__(self, region: str = "us-east-1", bucket: str = "", textract_client=None, s3_client=None,
                 sleep: Callable[[float], None] = time.sleep, poll_seconds: float = 2.0, timeout_seconds: float = 300):
        cfg = Config(retries={"max_attempts": 6, "mode": "adaptive"})
        self._textract = textract_client or boto3.client("textract", region_name=region, config=cfg)
        self._s3 = s3_client
        self._region = region
        self.bucket = bucket
        self._sleep, self._poll, self._timeout = sleep, poll_seconds, timeout_seconds

    # ---- public ----
    def extract(self, data: bytes, filename: str) -> OcrResult:
        ext = Path(filename).suffix.lower()
        try:
            if ext in IMAGE_EXTS:
                text, conf, pages = self._sync(data)
                pages = max(pages, 1)
            elif ext == ".pdf":
                text, conf, pages = self._pdf(data)
            else:
                raise OcrError(f"{filename}: Textract cannot read '{ext}' files")
        except (ClientError, BotoCoreError) as exc:
            code = exc.response["Error"]["Code"] if isinstance(exc, ClientError) else exc.__class__.__name__
            transient = code in TRANSIENT_CODES or not isinstance(exc, ClientError)
            raise OcrError(f"Textract call failed: {code}", transient=transient) from exc
        return OcrResult(text=text, pages=pages, engine=self.name, mean_confidence=conf,
                         cost_usd=estimate_ocr_cost(self.name, pages))

    # ---- internals ----
    def _sync(self, data: bytes):
        if len(data) > SYNC_MAX_BYTES:
            raise OcrError("File too large for synchronous Textract (10 MB). Configure an S3 bucket for async.")
        resp = self._textract.detect_document_text(Document={"Bytes": data})
        return blocks_to_text(resp.get("Blocks", []))

    def _pdf(self, data: bytes):
        try:
            n_pages = len(PdfReader(io.BytesIO(data)).pages)
        except Exception as exc:
            raise OcrError("PDF could not be read for OCR") from exc
        if n_pages <= 1:
            return self._sync(data)
        if self.bucket:
            return self._async_via_s3(data)
        return self._split_and_sync(data, n_pages)

    def _split_and_sync(self, data: bytes, n_pages: int):
        reader = PdfReader(io.BytesIO(data))
        texts, confs = [], []
        for page in reader.pages:
            writer = PdfWriter()
            writer.add_page(page)
            buf = io.BytesIO()
            writer.write(buf)
            t, c, _ = self._sync(buf.getvalue())
            texts.append(t)
            if c is not None:
                confs.append(c)
        return "\n\n".join(texts), (sum(confs) / len(confs) if confs else None), n_pages

    def _async_via_s3(self, data: bytes):
        s3 = self._s3 or boto3.client("s3", region_name=self._region)
        key = f"ocr-tmp/{uuid.uuid4().hex}.pdf"
        s3.upload_fileobj(io.BytesIO(data), self.bucket, key)  # bucket should enforce SSE-KMS by default
        try:
            job = self._textract.start_document_text_detection(
                DocumentLocation={"S3Object": {"Bucket": self.bucket, "Name": key}})
            return self._collect(job["JobId"])
        finally:
            s3.delete_object(Bucket=self.bucket, Key=key)  # never leave patient documents lying around

    def _collect(self, job_id: str):
        waited = 0.0
        while True:
            resp = self._textract.get_document_text_detection(JobId=job_id)
            status = resp.get("JobStatus")
            if status in ("SUCCEEDED", "PARTIAL_SUCCESS"):
                break
            if status == "FAILED":
                raise OcrError("Textract job failed")
            if waited >= self._timeout:
                raise OcrError("Textract job timed out", transient=True)
            self._sleep(self._poll)
            waited += self._poll
        blocks = list(resp.get("Blocks", []))
        token = resp.get("NextToken")
        while token:  # results come in pages of blocks
            more = self._textract.get_document_text_detection(JobId=job_id, NextToken=token)
            blocks.extend(more.get("Blocks", []))
            token = more.get("NextToken")
        text, conf, pages = blocks_to_text(blocks)
        pages = max(pages, int(resp.get("DocumentMetadata", {}).get("Pages", 0)))
        return text, conf, pages
