"""Database tables (SQLAlchemy 2.0). Works on PostgreSQL (real use) and SQLite (quick local dev).

Analogy: Postgres is the filing cabinet of record: structured, queryable, permanent. We store
EXTRACTED FIELDS and the audit trail, never the document text itself.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import (JSON, BigInteger, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text,
                        TypeDecorator, UniqueConstraint, create_engine)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UtcDateTime(TypeDecorator):
    """Always store and return timezone-aware UTC (SQLite would silently drop the zone otherwise)."""
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


# Money as a 2-decimal NUMERIC column. asdecimal=False hands back floats so SQLite and Postgres behave the same
# in this learning project; a production billing system would keep Decimal end to end.
Money = Numeric(12, 2, asdecimal=False)
BigId = BigInteger().with_variant(Integer, "sqlite")  # SQLite only autoincrements INTEGER primary keys


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    content_sha256: Mapped[str] = mapped_column(String(64), index=True)
    fingerprint: Mapped[str] = mapped_column(String(32))  # which model+prompts+OCR+rules produced this row
    source_name: Mapped[str] = mapped_column(String(255))
    file_type: Mapped[Optional[str]] = mapped_column(String(10))
    page_count: Mapped[Optional[int]] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    doc_type: Mapped[Optional[str]] = mapped_column(String(40))
    classification_confidence: Mapped[Optional[float]] = mapped_column()

    # Extracted fields as real columns (this is why SQL is here: filter, join, aggregate, report).
    provider_npi: Mapped[Optional[str]] = mapped_column(String(10), index=True)
    provider_name: Mapped[Optional[str]] = mapped_column(String(255))
    payer_name: Mapped[Optional[str]] = mapped_column(String(120), index=True)
    claim_reference: Mapped[Optional[str]] = mapped_column(String(40))
    service_date: Mapped[Optional[date]] = mapped_column(Date)
    disputed_amount: Mapped[Optional[float]] = mapped_column(Money)
    billing_violation_type: Mapped[Optional[str]] = mapped_column(String(30))
    summary: Mapped[Optional[str]] = mapped_column(Text)

    raw_claim: Mapped[Optional[dict]] = mapped_column(JSON)   # exactly what the model said, before rules
    findings: Mapped[Optional[list]] = mapped_column(JSON)
    ocr: Mapped[Optional[dict]] = mapped_column(JSON)
    prompt_versions: Mapped[Optional[dict]] = mapped_column(JSON)
    model_id: Mapped[Optional[str]] = mapped_column(String(200))

    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    ocr_pages: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 6, asdecimal=False), default=0.0)
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)

    error: Mapped[Optional[str]] = mapped_column(Text)
    error_kind: Mapped[Optional[str]] = mapped_column(String(12))

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(100))
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime)

    # SQLAlchemy's built-in optimistic locking: every UPDATE says "...WHERE version = <what I loaded>".
    # If someone else changed the row first, the UPDATE matches 0 rows and we raise instead of overwriting.
    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (
        UniqueConstraint("content_sha256", "fingerprint", name="uq_documents_hash_fingerprint"),  # last line of defence against duplicates
        Index("ix_documents_status_created", "status", "created_at"),
    )


class ReviewEvent(Base):
    __tablename__ = "review_events"

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    reviewer: Mapped[str] = mapped_column(String(100))
    decision: Mapped[str] = mapped_column(String(10))           # approve | reject
    prior_status: Mapped[str] = mapped_column(String(20))
    new_status: Mapped[str] = mapped_column(String(20))
    note: Mapped[Optional[str]] = mapped_column(Text)
    changes: Mapped[Optional[dict]] = mapped_column(JSON)        # {field: [old, new]}; becomes ground truth later
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class AuditLog(Base):
    """Who looked at / changed what, and when. No foreign key on purpose: audit rows must outlive anything."""
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(100), index=True)
    action: Mapped[str] = mapped_column(String(40))
    document_id: Mapped[Optional[str]] = mapped_column(String(36), index=True)
    detail: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)


class Upload(Base):
    """One row per presigned upload. Lets a client ask "what happened to the file I sent?" even though processing
    is asynchronous, and even when two uploads turn out to be the same document."""
    __tablename__ = "uploads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    s3_key: Mapped[str] = mapped_column(String(1024))
    filename: Mapped[str] = mapped_column(String(255))
    requested_by: Mapped[str] = mapped_column(String(100), index=True)
    state: Mapped[str] = mapped_column(String(12), default="pending")  # pending | processed | duplicate | failed
    document_id: Mapped[Optional[str]] = mapped_column(String(36))
    error: Mapped[Optional[str]] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)


class IdempotencyRow(Base):
    """Used only by the SQL idempotency backend (local/dev). Production uses DynamoDB."""
    __tablename__ = "idempotency"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    status: Mapped[str] = mapped_column(String(12))
    owner: Mapped[str] = mapped_column(String(40))
    lease_expires: Mapped[int] = mapped_column(BigInteger)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    document_id: Mapped[Optional[str]] = mapped_column(String(36))
    error: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[int] = mapped_column(BigInteger)


def make_engine(url: str):
    if url.startswith("sqlite"):
        kwargs = {"connect_args": {"check_same_thread": False, "timeout": 30}}
        if ":memory:" in url or url == "sqlite://":
            kwargs["poolclass"] = StaticPool
        return create_engine(url, **kwargs)
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=10)


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def init_db(engine) -> None:
    """Create tables if missing. Fine for dev; production should use migrations (Alembic) so schema
    changes are reviewed, versioned and reversible."""
    Base.metadata.create_all(engine)
