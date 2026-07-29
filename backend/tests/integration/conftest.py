"""Fixtures for the live-Postgres integration suite."""

from __future__ import annotations

import uuid

import pytest

from tests.integration import db


@pytest.fixture(scope="session", autouse=True)
def schema():
    ok, _reason, _dsn = db.availability()
    if not ok:
        yield
        return
    db.migrate()
    yield


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    mode = db.SCHEMA_SOURCE["mode"]
    if mode == "unknown":
        return
    terminalreporter.write_sep("=", f"integration schema source: {mode}")
    terminalreporter.write_line(db.SCHEMA_SOURCE["detail"])
    if mode != "alembic":
        terminalreporter.write_line(
            "NOTE: migration DDL was NOT exercised; behaviour was still validated "
            "against real Postgres."
        )


@pytest.fixture(autouse=True)
def clean_db(schema):
    """Every test starts from an empty database — no ordering coupling."""
    db.truncate()
    yield


@pytest.fixture
def sessions():
    return db.session_factory()


@pytest.fixture
def offer_id(sessions) -> uuid.UUID:
    """A real row in Selling's `offers` table, so the Selling ACL is exercised
    for real rather than stubbed."""
    oid = uuid.uuid4()
    db.execute(
        "insert into offers (id, seller_id, variant_id, condition, currency, status) "
        "values (:id, :seller, :variant, 'new', 'IRR', 'active')",
        id=oid, seller=uuid.uuid4(), variant=uuid.uuid4(),
    )
    return oid


@pytest.fixture
def second_offer_id(sessions) -> uuid.UUID:
    oid = uuid.uuid4()
    db.execute(
        "insert into offers (id, seller_id, variant_id, condition, currency, status) "
        "values (:id, :seller, :variant, 'new', 'IRR', 'active')",
        id=oid, seller=uuid.uuid4(), variant=uuid.uuid4(),
    )
    return oid
