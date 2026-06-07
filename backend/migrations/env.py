"""Alembic environment.

Reads the database URL from the ``DATABASE_URL`` environment variable so no
credentials live in version control. Supports both offline (``--sql``) and
online migration modes.

``target_metadata`` is left as ``None`` for now because these baseline
migrations are hand-authored. Once the SQLAlchemy 2.0 models exist under
``src/`` you can point ``target_metadata`` at the aggregated ``MetaData`` to
enable ``alembic revision --autogenerate``.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Resolve the DB URL at runtime (env var wins over alembic.ini).
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://app:app@localhost:5432/ecommerce",
)
config.set_main_option("sqlalchemy.url", DATABASE_URL)

# Hand-written migrations for the baseline; wire models here later.
target_metadata = None


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
