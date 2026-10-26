"""Alembic environment: target metadata is aecom.db.Base.metadata, URL comes from aecom settings.

aecom.db.init_db() passes its live connection in ``config.attributes["connection"]`` so migrations run on the
same engine (SQLite pragmas included); the ``alembic`` CLI falls back to ``get_settings().database_url``.
"""

from __future__ import annotations

from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import create_engine

from aecom.config import get_settings
from aecom.db import Base

config = context.config
if config.config_file_name is not None and "connection" not in config.attributes:
    fileConfig(config.config_file_name, disable_existing_loggers=False)  # CLI only; don't reset app logging

target_metadata = Base.metadata


def _configure(**kw: Any) -> None:
    context.configure(target_metadata=target_metadata, render_as_batch=True, compare_type=True, **kw)


def run_migrations_offline() -> None:
    _configure(url=get_settings().database_url, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(get_settings().database_url)
    with engine.connect() as connection:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
