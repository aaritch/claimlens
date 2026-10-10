"""Versioned migrations. Existing create_all databases require an explicit, schema-checked adoption."""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect

from claimlens.storage.db import Base


class MigrationError(ValueError):
    """Safe, fixed operational messages (never SQL, parameters or connection URLs)."""


def migrate(engine, adopt_legacy: bool = False) -> None:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "alembic"))
    with engine.begin() as connection:
        tables = set(inspect(connection).get_table_names())
        config.attributes["connection"] = connection
        if tables & set(Base.metadata.tables) and "alembic_version" not in tables:
            if not adopt_legacy:
                raise MigrationError("legacy database: back it up, then use db-migrate --adopt-legacy")
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            if compare_metadata(context, Base.metadata):
                raise MigrationError("legacy schema differs from the baseline; adoption refused")
            command.stamp(config, "0001")
        command.upgrade(config, "head")
