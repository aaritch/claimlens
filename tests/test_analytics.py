from decimal import Decimal
from unittest.mock import Mock

import pyarrow.parquet as pq
import pytest
from sqlalchemy import select

from claimlens.analytics import EXPORT_COLUMNS, export_parquet, upload_export
from claimlens.storage import repository as repo
from claimlens.storage.db import AuditLog
from tests.conftest import make_result


def test_export_includes_only_validated_allowlisted_columns(session_factory, tmp_path):
    with session_factory() as session:
        for i, status in enumerate(["validated", "needs_review", "rejected"]):
            repo.save_result(session, make_result(status), content_sha256=str(i) * 64, fingerprint="fp",
                             source_name="Jane_Example.pdf")
        session.commit()
        target = tmp_path / "snapshot.parquet"
        assert export_parquet(session, target) == 1
        session.commit()
        assert session.scalars(select(AuditLog)).one().action == "export_analytics"
    table = pq.read_table(target)
    assert set(table.column_names) == set(EXPORT_COLUMNS)
    row = table.to_pylist()[0]
    assert row["disputed_amount"] == Decimal("1450.50")
    assert row["cost_usd"] == Decimal("0.004000") and row["updated_at"].tzinfo is not None
    assert not {"source_name", "summary", "provider_name", "raw_claim", "reviewed_by"} & set(table.column_names)


def test_empty_exports_are_readable_and_never_overwrite_files(session_factory, tmp_path):
    target = tmp_path / "empty.parquet"
    with session_factory() as session:
        assert export_parquet(session, target) == 0
        assert pq.read_table(target).num_rows == 0
        original = target.read_bytes()
        with pytest.raises(FileExistsError):
            export_parquet(session, target)
        assert target.read_bytes() == original


def test_s3_export_requires_encryption_and_an_export_prefix(tmp_path):
    client = Mock()
    target = tmp_path / "export.parquet"
    for key, kms in [("incoming/a.parquet", "key"), ("exports/a.parquet", ""), ("exports/a.txt", "key")]:
        with pytest.raises(ValueError):
            upload_export(target, "bucket", key, kms, client)
    client.upload_file.assert_not_called()
    upload_export(target, "bucket", "exports/run/a.parquet", "kms-arn", client)
    assert client.upload_file.call_args.kwargs["ExtraArgs"] == {
        "ServerSideEncryption": "aws:kms", "SSEKMSKeyId": "kms-arn",
    }


def test_analytics_cli_never_calls_s3_without_explicit_upload(tmp_path, monkeypatch):
    from claimlens.cli import main
    import boto3
    monkeypatch.setenv("CLAIMLENS_DATABASE_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    assert main(["db-migrate"]) == 0
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: pytest.fail("unexpected network client"))
    assert main(["export-analytics", "--out", str(tmp_path / "cli.parquet")]) == 0
    assert main(["export-analytics", "--out", str(tmp_path / "cli.parquet")]) == 2
