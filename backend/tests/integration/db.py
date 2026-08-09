"""Live-Postgres plumbing for the integration suite.

Deliberately NOT SQLite. Everything this suite exists to prove — version CAS
under real concurrent transactions, FOR UPDATE SKIP LOCKED, partial indexes,
CHECK-constraint enforcement, cross-connection visibility — either behaves
differently or does not exist on SQLite. A green run on SQLite would be evidence
of nothing.

DSN: TEST_DATABASE_URL, which backend/conftest.py has already resolved through
src/config/env_bootstrap.py — derived as <POSTGRES_DB>_test from the repo-root
.env unless it is exported explicitly. The scratch database is created on first
use if it does not exist yet, so there is no manual setup step.

The target database is TRUNCATED between tests. Point it at a scratch database,
never at anything you care about.
"""

from __future__ import annotations

import asyncio
import os
import pathlib

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]

# Binding the global engine to the test database is backend/conftest.py's job —
# it runs before any test module is imported, which this module does not (the
# DB-free suites import src.* during collection first). See that file.

# Order matters: children before parents.
TABLES = (
    "order_saga_lines",
    "orders_saga",
    "inventory_reservation",
    "inventory_stock",
    "processed_events",
    "dead_letter_messages",
    "outbox_messages",
    "buy_box_variant_state",
    "buy_box_offer",
    "notifications",
    "offer_prices",
    "offers",
)

_LOOP: asyncio.AbstractEventLoop | None = None
_ENGINE = None
_STATE: dict = {"checked": False, "ok": False, "reason": "", "dsn": ""}


def async_dsn() -> str:
    dsn = os.getenv("TEST_DATABASE_URL", "").strip()
    if dsn:
        return dsn
    try:
        from src.config.settings import settings

        return str(settings.test_database_url)
    except Exception:  # pragma: no cover - settings import failure
        return ""


def sync_dsn(dsn: str) -> str:
    return dsn.replace("+asyncpg", "+psycopg")


def _ensure_database(dsn: str) -> None:
    """CREATE DATABASE the scratch target if it is missing.

    Keeps `pytest tests/integration` a single command on a fresh clone: compose
    only ever creates POSTGRES_DB, never the _test sibling. Connects to the
    `postgres` maintenance database with the same credentials; CREATE DATABASE
    cannot run inside a transaction, hence AUTOCOMMIT.
    """
    url = sa.engine.make_url(dsn)
    target = url.database
    if not target or target == "postgres":
        return

    admin = create_async_engine(
        url.set(database="postgres"),
        poolclass=sa.pool.NullPool,
        connect_args={"timeout": 5},
        isolation_level="AUTOCOMMIT",
    )

    async def _go():
        async with admin.connect() as conn:
            exists = (
                await conn.execute(
                    sa.text("select 1 from pg_database where datname = :name"),
                    {"name": target},
                )
            ).scalar()
            if not exists:
                await conn.execute(sa.text(f'create database "{target}"'))

    try:
        run(_go())
    finally:
        try:
            run(admin.dispose())
        except Exception:
            pass


def loop() -> asyncio.AbstractEventLoop:
    """One loop for the whole session: asyncpg connections are loop-bound, so a
    fresh asyncio.run() per test would poison the pool."""
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP


def run(coro):
    return loop().run_until_complete(coro)


def engine():
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = create_async_engine(async_dsn(), poolclass=sa.pool.NullPool)
    return _ENGINE


def session_factory() -> async_sessionmaker:
    return async_sessionmaker(bind=engine(), expire_on_commit=False, autoflush=False)


def _probe() -> tuple[bool, str]:
    dsn = async_dsn()
    if not dsn:
        return False, "no DSN: set TEST_DATABASE_URL"

    try:
        _ensure_database(dsn)
    except Exception as exc:
        detail = (str(exc).splitlines() or ["<no detail>"])[0][:160]
        return False, f"cannot reach server to create scratch DB — {type(exc).__name__}: {detail}"

    probe_engine = create_async_engine(
        dsn, poolclass=sa.pool.NullPool, connect_args={"timeout": 3}
    )

    async def ping():
        async with probe_engine.connect() as conn:
            await conn.execute(sa.text("select 1"))

    try:
        run(ping())
        return True, ""
    except Exception as exc:
        detail = (str(exc).splitlines() or ["<no detail>"])[0][:160]
        return False, f"{type(exc).__name__}: {detail}"
    finally:
        try:
            run(probe_engine.dispose())
        except Exception:
            pass


def availability() -> tuple[bool, str, str]:
    if not _STATE["checked"]:
        ok, reason = _probe()
        _STATE.update(checked=True, ok=ok, reason=reason, dsn=async_dsn())
    return _STATE["ok"], _STATE["reason"], _STATE["dsn"]


def skip_if_unavailable() -> None:
    """Module-level guard. Skips — never silently passes — so an unrun suite can
    never be mistaken for a green one."""
    ok, reason, dsn = availability()
    if not ok:
        pytest.skip(
            f"LIVE POSTGRES REQUIRED AND UNAVAILABLE ({reason}). "
            f"These validations were NOT executed. Start Postgres and set "
            f"TEST_DATABASE_URL, then re-run.",
            allow_module_level=True,
        )


SCHEMA_SOURCE = {"mode": "unknown", "detail": ""}

# Every module's ORM models, so metadata.create_all() sees the whole schema.
_MODEL_MODULES = (
    "src.modules.selling.infrastructure.models",
    "src.modules.inventory.infrastructure.models",
    "src.modules.ordering.infrastructure.models",
    "src.modules.buybox.infrastructure.models",
    # merchant resolves under create_all: its only FK (seller_members ->
    # sellers) is internal to the context, unlike notification's FK to Identity.
    "src.modules.merchant.infrastructure.models",
    # notification.models is deliberately excluded: it FKs to Identity's `users`,
    # which is migration-only (no ORM model), so create_all cannot resolve it.
    # No validation here touches notifications.
    "src.shared_kernel.infrastructure.outbox.models",
)


def _create_all() -> None:
    import importlib

    from src.shared_kernel.infrastructure.database import Base

    for mod in _MODEL_MODULES:
        importlib.import_module(mod)

    async def _go():
        async with engine().begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    run(_go())


def migrate() -> None:
    """Prefer the real Alembic chain — it validates the migrations themselves.

    Falls back to metadata.create_all() when the server lacks contrib modules:
    migration 0003 builds a pricing no-overlap EXCLUDE constraint that genuinely
    requires btree_gist, which cannot be stubbed. The fallback creates the same
    tables, CHECKs, unique constraints and partial indexes from the ORM models,
    so every behavioural validation still runs against real Postgres — what is
    lost is coverage of the migration DDL itself. The mode is reported in the
    run output so a fallback can never pass unnoticed.
    """
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    # Alembic needs the SYNC driver; restore the async DSN afterwards so the
    # global engine the consumers use stays on asyncpg.
    restore = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = sync_dsn(async_dsn())
    try:
        command.upgrade(cfg, "head")
        SCHEMA_SOURCE.update(mode="alembic", detail="full migration chain applied")
    except Exception as exc:
        detail = (str(exc).splitlines() or ["<no detail>"])[0][:200]
        if "extension" not in detail and "operator class" not in detail.lower():
            raise
        _create_all()
        SCHEMA_SOURCE.update(
            mode="metadata",
            detail=f"alembic unavailable on this server ({detail})",
        )
    finally:
        if restore is not None:
            os.environ["DATABASE_URL"] = restore


def truncate() -> None:
    async def _truncate():
        async with engine().begin() as conn:
            existing = set(
                (
                    await conn.execute(
                        sa.text(
                            "select table_name from information_schema.tables "
                            "where table_schema='public'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            targets = [t for t in TABLES if t in existing]
            if targets:
                await conn.execute(
                    sa.text(f"truncate {', '.join(targets)} restart identity cascade")
                )

    run(_truncate())


def scalar(sql: str, **params):
    async def _q():
        async with engine().connect() as conn:
            return (await conn.execute(sa.text(sql), params)).scalar()

    return run(_q())


def rows(sql: str, **params) -> list:
    async def _q():
        async with engine().connect() as conn:
            return (await conn.execute(sa.text(sql), params)).mappings().all()

    return run(_q())


def execute(sql: str, **params) -> None:
    async def _x():
        async with engine().begin() as conn:
            await conn.execute(sa.text(sql), params)

    run(_x())
