"""Migration 0019 against real PostgreSQL (ADR-0011 §7, Phase 1C M1).

Validates the DDL itself — the constraints that are supposed to be database
backstops rather than application checks — plus the isolation rules: which
foreign keys must exist, and just as importantly which must NOT.
"""

from __future__ import annotations

import uuid

import pytest

from tests.integration import db

db.skip_if_unavailable()

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def schema():
    db.migrate()
    yield


# --- tables and columns -----------------------------------------------------
def test_merchant_tables_exist():
    present = {
        r["table_name"] for r in db.rows(
            "select table_name from information_schema.tables "
            "where table_schema='public' and table_name in ('sellers','seller_members')"
        )
    }
    assert present == {"sellers", "seller_members"}


def test_sellers_columns():
    cols = {
        r["column_name"]: (r["data_type"], r["is_nullable"]) for r in db.rows(
            "select column_name, data_type, is_nullable from information_schema.columns "
            "where table_name='sellers'"
        )
    }
    assert set(cols) == {
        "id", "code", "name", "status", "tenant_id", "version",
        "created_at", "updated_at",
    }
    assert cols["id"][0] == "uuid"
    assert cols["version"][0] == "integer" and cols["version"][1] == "NO"
    # Multi-tenant READY, not multi-tenant: nullable until a tenant model exists.
    assert cols["tenant_id"] == ("uuid", "YES")


def test_seller_members_columns():
    cols = {
        r["column_name"]: (r["data_type"], r["is_nullable"]) for r in db.rows(
            "select column_name, data_type, is_nullable from information_schema.columns "
            "where table_name='seller_members'"
        )
    }
    assert set(cols) == {
        "seller_id", "user_id", "member_role_code", "status",
        "created_at", "updated_at",
    }
    # No tenant_id: tenancy is inherited from the seller (ADR-0011 §16).
    assert "tenant_id" not in cols
    # No user profile data copied out of Identity.
    for forbidden in ("mobile", "email", "name", "full_name"):
        assert forbidden not in cols


# --- constraints and indexes ------------------------------------------------
def test_membership_uniqueness_is_enforced_by_the_database():
    pk = db.rows(
        "select conname, pg_get_constraintdef(oid) from pg_constraint "
        "where conrelid='seller_members'::regclass and contype='p'"
    )
    assert len(pk) == 1
    name, definition = pk[0]["conname"], pk[0]["pg_get_constraintdef"]
    assert name == "pk_seller_members"
    assert "seller_id" in definition and "user_id" in definition


def test_expected_indexes_exist():
    names = {r["indexname"] for r in db.rows(
        "select indexname from pg_indexes where tablename in ('sellers','seller_members')"
    )}
    assert "ix_seller_members_user" in names   # user -> sellers, the 1F lookup
    assert "ix_sellers_tenant" in names
    assert "uq_sellers_code" in names or any("code" in n for n in names)


def test_status_and_role_check_constraints():
    checks = {
        r["conname"]: r["pg_get_constraintdef"] for r in db.rows(
            "select conname, pg_get_constraintdef(oid) from pg_constraint "
            "where conrelid in ('sellers'::regclass,'seller_members'::regclass) "
            "and contype='c'"
        )
    }
    assert "ck_seller_status" in checks
    for value in ("pending", "active", "suspended"):
        assert value in checks["ck_seller_status"]
    assert "ck_seller_member_status" in checks
    assert "ck_seller_member_role" in checks
    for role in ("seller_owner", "seller_manager", "seller_staff", "seller_viewer"):
        assert role in checks["ck_seller_member_role"]


def test_membership_has_a_foreign_key_to_its_seller():
    """Referential integrity INSIDE Merchant is enforced."""
    fks = db.rows(
        "select pg_get_constraintdef(oid) from pg_constraint "
        "where conrelid='seller_members'::regclass and contype='f'"
    )
    assert any("sellers(id)" in f["pg_get_constraintdef"]
               and "seller_id" in f["pg_get_constraintdef"] for f in fks)


def test_no_cross_context_foreign_keys_were_introduced():
    """Merchant must not couple relationally to Identity, and no other context
    may gain an FK to sellers (ADR-0011 §9, §12)."""
    # seller_members.user_id must NOT reference users.
    member_fks = " ".join(
        r["pg_get_constraintdef"] for r in db.rows(
            "select pg_get_constraintdef(oid) from pg_constraint "
            "where conrelid='seller_members'::regclass and contype='f'"
        )
    )
    assert "users" not in member_fks

    # Nothing may FK INTO sellers except Merchant's own membership table.
    referencing = {
        r["conrelid"] for r in db.rows(
            "select conrelid::regclass::text as conrelid from pg_constraint "
            "where contype='f' and confrelid='sellers'::regclass"
        )
    }
    assert referencing <= {"seller_members"}, referencing

    # Explicitly: the local seller references stay local.
    for table in ("offers", "buy_box_offer"):
        fks = " ".join(
            r["pg_get_constraintdef"] for r in db.rows(
                "select pg_get_constraintdef(oid) from pg_constraint "
                f"where conrelid='{table}'::regclass and contype='f'"
            )
        )
        assert "sellers" not in fks, f"{table} gained an FK to sellers"


# --- behaviour --------------------------------------------------------------
def test_the_many_to_many_actually_works_in_the_database():
    seller_a, seller_b = uuid.uuid4(), uuid.uuid4()
    user_1, user_2 = uuid.uuid4(), uuid.uuid4()
    for sid, code in ((seller_a, f"a-{sid_hex()}"), (seller_b, f"b-{sid_hex()}")):
        db.execute(
            "insert into sellers (id, code, name, status) values (:i,:c,'S','active')",
            i=sid, c=code,
        )
    # one user in two sellers
    db.execute("insert into seller_members (seller_id,user_id,member_role_code) "
               "values (:s,:u,'seller_owner')", s=seller_a, u=user_1)
    db.execute("insert into seller_members (seller_id,user_id,member_role_code) "
               "values (:s,:u,'seller_staff')", s=seller_b, u=user_1)
    # one seller with two users
    db.execute("insert into seller_members (seller_id,user_id,member_role_code) "
               "values (:s,:u,'seller_manager')", s=seller_a, u=user_2)

    assert db.scalar("select count(*) from seller_members where user_id=:u", u=user_1) == 2
    assert db.scalar("select count(*) from seller_members where seller_id=:s", s=seller_a) == 2


def test_duplicate_membership_is_rejected_by_the_database():
    seller, user = uuid.uuid4(), uuid.uuid4()
    db.execute("insert into sellers (id, code, name, status) values (:i,:c,'S','active')",
               i=seller, c=f"dup-{sid_hex()}")
    db.execute("insert into seller_members (seller_id,user_id,member_role_code) "
               "values (:s,:u,'seller_owner')", s=seller, u=user)
    with pytest.raises(Exception) as exc:
        db.execute("insert into seller_members (seller_id,user_id,member_role_code) "
                   "values (:s,:u,'seller_viewer')", s=seller, u=user)
    assert "pk_seller_members" in str(exc.value) or "duplicate key" in str(exc.value)


def test_invalid_status_and_role_are_rejected():
    seller = uuid.uuid4()
    with pytest.raises(Exception):
        db.execute("insert into sellers (id, code, name, status) values (:i,:c,'S','bogus')",
                   i=seller, c=f"bad-{sid_hex()}")
    db.execute("insert into sellers (id, code, name, status) values (:i,:c,'S','pending')",
               i=seller, c=f"ok-{sid_hex()}")
    with pytest.raises(Exception):
        db.execute("insert into seller_members (seller_id,user_id,member_role_code) "
                   "values (:s,:u,'wizard')", s=seller, u=uuid.uuid4())


def test_cascade_removes_memberships_with_their_seller():
    seller, user = uuid.uuid4(), uuid.uuid4()
    db.execute("insert into sellers (id, code, name, status) values (:i,:c,'S','active')",
               i=seller, c=f"cas-{sid_hex()}")
    db.execute("insert into seller_members (seller_id,user_id,member_role_code) "
               "values (:s,:u,'seller_owner')", s=seller, u=user)
    db.execute("delete from sellers where id=:i", i=seller)
    assert db.scalar("select count(*) from seller_members where seller_id=:s", s=seller) == 0


def test_migration_0019_is_reversible():
    """downgrade -> upgrade must return the schema to head.

    Restored in `finally`: a test that left the scratch database downgraded
    would break every test after it.
    """
    import os

    from alembic import command
    from alembic.config import Config

    cfg = Config(str(db.BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(db.BACKEND_ROOT / "migrations"))
    restore = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = db.sync_dsn(db.async_dsn())
    try:
        command.downgrade(cfg, "0018")
        gone = db.scalar(
            "select count(*) from information_schema.tables "
            "where table_schema='public' and table_name in ('sellers','seller_members')"
        )
        assert gone == 0, "downgrade left merchant tables behind"
        command.upgrade(cfg, "head")
        back = db.scalar(
            "select count(*) from information_schema.tables "
            "where table_schema='public' and table_name in ('sellers','seller_members')"
        )
        assert back == 2, "upgrade did not restore merchant tables"
    finally:
        command.upgrade(cfg, "head")
        if restore is not None:
            os.environ["DATABASE_URL"] = restore


def test_no_seller_rows_were_seeded_by_the_migration():
    """M1 is schema foundation only: no backfill, no fake sellers (ADR-0011 §7.2).

    Runs the assertion against a freshly migrated schema rather than the shared
    scratch state other tests write to.
    """
    import os

    from alembic import command
    from alembic.config import Config

    cfg = Config(str(db.BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(db.BACKEND_ROOT / "migrations"))
    restore = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = db.sync_dsn(db.async_dsn())
    try:
        command.downgrade(cfg, "0018")
        command.upgrade(cfg, "head")
        assert db.scalar("select count(*) from sellers") == 0
        assert db.scalar("select count(*) from seller_members") == 0
    finally:
        command.upgrade(cfg, "head")
        if restore is not None:
            os.environ["DATABASE_URL"] = restore


def sid_hex() -> str:
    return uuid.uuid4().hex[:8]
