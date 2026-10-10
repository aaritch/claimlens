"""Frozen Phase 4 schema. Never import application models in a historical migration."""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    big_id = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    timestamp = sa.DateTime(timezone=True)
    op.create_table(
        "documents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(32), nullable=False),
        sa.Column("source_name", sa.String(255), nullable=False),
        sa.Column("file_type", sa.String(10)),
        sa.Column("page_count", sa.Integer()),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("doc_type", sa.String(40)),
        sa.Column("classification_confidence", sa.Float()),
        sa.Column("provider_npi", sa.String(10)),
        sa.Column("provider_name", sa.String(255)),
        sa.Column("payer_name", sa.String(120)),
        sa.Column("claim_reference", sa.String(40)),
        sa.Column("service_date", sa.Date()),
        sa.Column("disputed_amount", sa.Numeric(12, 2)),
        sa.Column("billing_violation_type", sa.String(30)),
        sa.Column("summary", sa.Text()),
        sa.Column("raw_claim", sa.JSON()),
        sa.Column("findings", sa.JSON()),
        sa.Column("ocr", sa.JSON()),
        sa.Column("prompt_versions", sa.JSON()),
        sa.Column("model_id", sa.String(200)),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("ocr_pages", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False),
        sa.Column("elapsed_ms", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("error_kind", sa.String(12)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
        sa.Column("reviewed_by", sa.String(100)),
        sa.Column("reviewed_at", timestamp),
        sa.UniqueConstraint("content_sha256", "fingerprint", name="uq_documents_hash_fingerprint"),
    )
    for name in ("content_sha256", "provider_npi", "payer_name"):
        op.create_index(f"ix_documents_{name}", "documents", [name])
    op.create_index("ix_documents_status_created", "documents", ["status", "created_at"])
    op.create_table(
        "review_events",
        sa.Column("id", big_id, primary_key=True, autoincrement=True),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("reviewer", sa.String(100), nullable=False),
        sa.Column("decision", sa.String(10), nullable=False),
        sa.Column("prior_status", sa.String(20), nullable=False),
        sa.Column("new_status", sa.String(20), nullable=False),
        sa.Column("note", sa.Text()), sa.Column("changes", sa.JSON()),
        sa.Column("created_at", timestamp, nullable=False),
    )
    op.create_index("ix_review_events_document_id", "review_events", ["document_id"])
    op.create_table(
        "audit_log",
        sa.Column("id", big_id, primary_key=True, autoincrement=True),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("document_id", sa.String(36)),
        sa.Column("detail", sa.JSON()),
        sa.Column("created_at", timestamp, nullable=False),
    )
    for name in ("actor", "document_id", "created_at"):
        op.create_index(f"ix_audit_log_{name}", "audit_log", [name])
    op.create_table(
        "uploads",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("s3_key", sa.String(1024), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("requested_by", sa.String(100), nullable=False),
        sa.Column("state", sa.String(12), nullable=False),
        sa.Column("document_id", sa.String(36)),
        sa.Column("error", sa.String(200)),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
    )
    op.create_index("ix_uploads_requested_by", "uploads", ["requested_by"])
    op.create_table(
        "idempotency",
        sa.Column("key", sa.String(120), primary_key=True),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("owner", sa.String(40), nullable=False),
        sa.Column("lease_expires", sa.BigInteger(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.String(36)),
        sa.Column("error", sa.Text()),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
    )


def downgrade():
    for table in ("idempotency", "uploads", "audit_log", "review_events", "documents"):
        op.drop_table(table)
