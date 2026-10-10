import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

from claimlens.storage.db import Base, init_db, make_engine, make_session_factory
from claimlens.storage.migrations import migrate
from claimlens.storage import repository as repo
from tests.conftest import make_result


def test_fresh_migration_matches_runtime_schema_and_can_process_records(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
    migrate(engine)
    migrate(engine)
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
        assert connection.scalar(text("select version_num from alembic_version")) == "0001"
    with make_session_factory(engine)() as session:
        document = repo.save_result(session, make_result(), content_sha256="a" * 64, fingerprint="fp",
                                   source_name="opaque.pdf")
        session.commit()
        assert document.version == 1
    engine.dispose()


def test_legacy_adoption_is_explicit_and_preserves_records(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    init_db(engine)
    with make_session_factory(engine)() as session:
        document = repo.save_result(session, make_result(), content_sha256="b" * 64, fingerprint="fp",
                                   source_name="old.pdf")
        session.commit()
        identifier = document.id
    with pytest.raises(ValueError, match="legacy database"):
        migrate(engine)
    migrate(engine, adopt_legacy=True)
    with make_session_factory(engine)() as session:
        assert repo.get_document(session, identifier).source_name == "old.pdf"
    engine.dispose()


def test_modified_legacy_schema_is_never_silently_stamped(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'different.db'}")
    init_db(engine)
    with engine.begin() as connection:
        connection.execute(text("alter table documents add column unexpected TEXT"))
    with pytest.raises(ValueError, match="differs"):
        migrate(engine, adopt_legacy=True)
    assert "alembic_version" not in inspect(engine).get_table_names()
    engine.dispose()


def test_migration_cli_initializes_only_a_test_database(tmp_path, monkeypatch):
    from claimlens.cli import main
    monkeypatch.setenv("CLAIMLENS_DATABASE_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    assert main(["db-migrate"]) == 0
