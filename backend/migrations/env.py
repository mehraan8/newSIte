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
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# alembic.ini's prepend_sys_path is relative to the invocation CWD, so make
# `src` importable from __file__ instead — this file is also driven
# programmatically by the integration suite, from an arbitrary directory.
BACKEND_ROOT = str(Path(__file__).resolve().parents[1])
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

from src.config.env_bootstrap import load_env, sync_dsn  # noqa: E402

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False is required, not cosmetic. This module is
    # also driven in-process by the integration suite; with the default (True)
    # every logger created before `alembic upgrade` — i.e. the whole app — is
    # switched off for the rest of the process, and any later test asserting on
    # captured log output sees an empty string.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Resolve the DB URL at runtime (a real env var wins over the .env layers).
# No hardcoded fallback: a missing DATABASE_URL must fail loudly rather than
# quietly dial a guessed password against some other database.
load_env()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. It is derived from POSTGRES_USER / "
        "POSTGRES_PASSWORD / POSTGRES_DB in the repo-root .env (see "
        "src/config/env_bootstrap.py); set those, or export DATABASE_URL."
    )

# Alembic is synchronous: accept an asyncpg URL and swap the driver rather than
# making every caller remember to pass the psycopg form.
config.set_main_option("sqlalchemy.url", sync_dsn(DATABASE_URL))

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
