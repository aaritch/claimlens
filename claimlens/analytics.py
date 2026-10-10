"""Bounded-memory Parquet exports of validated business fields, never originals, names, summaries or raw output."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from claimlens.storage import repository as repo
from claimlens.storage.db import Document

EXPORT_COLUMNS = (
    "id", "doc_type", "payer_name", "provider_npi", "service_date", "disputed_amount",
    "input_tokens", "output_tokens", "ocr_pages", "cost_usd", "elapsed_ms", "version", "updated_at",
)


def export_parquet(session, target: Path, actor: str = "analytics_export") -> int:
    import pyarrow as pa
    import pyarrow.parquet as pq
    schema = pa.schema([
        ("id", pa.string()), ("doc_type", pa.string()), ("payer_name", pa.string()),
        ("provider_npi", pa.string()), ("service_date", pa.date32()),
        ("disputed_amount", pa.decimal128(12, 2)), ("input_tokens", pa.int64()),
        ("output_tokens", pa.int64()), ("ocr_pages", pa.int64()),
        ("cost_usd", pa.decimal128(10, 6)), ("elapsed_ms", pa.int64()),
        ("version", pa.int64()), ("updated_at", pa.timestamp("us", tz="UTC")),
    ])
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    query = select(*(getattr(Document, name) for name in EXPORT_COLUMNS)).where(
        Document.status == "validated"
    ).order_by(Document.id).execution_options(yield_per=500)
    count = 0
    # Refuse to overwrite an existing export. Each run gets its own named snapshot.
    with target.open("xb") as output, pq.ParquetWriter(output, schema, compression="snappy") as writer:
        for partition in session.execute(query).mappings().partitions(500):
            rows = []
            for source in partition:
                row = dict(source)
                for name, precision in (("disputed_amount", "0.01"), ("cost_usd", "0.000001")):
                    if row[name] is not None:
                        row[name] = Decimal(str(row[name])).quantize(Decimal(precision))
                rows.append(row)
            writer.write_table(pa.Table.from_pylist(rows, schema=schema))
            count += len(rows)
    repo.audit(session, actor, "export_analytics", detail={"returned": count})
    return count


def upload_export(target: Path, bucket: str, key: str, kms_key: str, s3_client) -> None:
    if not bucket or not kms_key or not key.startswith("exports/") or not key.endswith(".parquet"):
        raise ValueError("exports require a bucket, KMS key, and exports/*.parquet destination")
    s3_client.upload_file(str(target), bucket, key,
                          ExtraArgs={"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": kms_key})
