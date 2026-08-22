"""Seed migration 0020 and runtime drift, against real PostgreSQL (Phase 1E).

Exercises the migration itself in Alembic mode — not metadata creation — because
what is being validated is the seed DDL/DML and its idempotency, which only
exists in the migration.
"""

from __future__ import annotations

import os
import uuid

import pytest
from alembic import command
from alembic.config import Config

from src.modules.identity.application.rbac_baseline import (
    ROLE_PERMISSIONS,
    compute_drift,
    expected_state,
)
from src.modules.identity.application.roles import PlatformRole
from src.modules.identity.infrastructure.rbac_drift import (
    SqlRbacStateReader,
    check_drift,
)
from src.shared_kernel.application.permissions import Permission
from tests.integration import db

db.skip_if_unavailable()

pytestmark = pytest.mark.integration


def alembic_cfg() -> Config:
    cfg = Config(str(db.BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(db.BACKEND_ROOT / "migrations"))
    return cfg


def at_revision(revision: str) -> None:
    """Move the scratch database to an exact revision, sync driver."""
    restore = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = db.sync_dsn(db.async_dsn())
    try:
        if revision == "head":
            command.upgrade(alembic_cfg(), "head")
        else:
            command.downgrade(alembic_cfg(), revision)
    finally:
        if restore is not None:
            os.environ["DATABASE_URL"] = restore


@pytest.fixture(scope="module", autouse=True)
def schema():
    db.migrate()
    yield
    at_revision("head")          # never leave the scratch DB behind head


@pytest.fixture(autouse=True)
def head():
    """Every test starts from a pristine seed.

    Two reasons a bare `upgrade head` is not enough. It is a no-op when already
    at head, so seeded rows a test removed would stay removed; and `downgrade`
    deliberately preserves operator-created rows (that is its contract), so the
    unknown roles/grants the drift tests insert on purpose survive it. Hence:
    downgrade, wipe whatever the downgrade rightly kept, then re-seed.

    Reset on setup only — the next test's setup is the teardown.
    """
    at_revision("0019")
    db.execute("delete from role_permissions")
    db.execute("delete from permissions")
    db.execute("delete from roles")
    at_revision("head")
    yield


def read_state():
    async def _go():
        async with db.session_factory()() as session:
            return await SqlRbacStateReader(session).read()

    return db.run(_go())


# --- the seed lands ---------------------------------------------------------
def test_all_platform_roles_are_seeded():
    codes = {r["code"] for r in db.rows("select code from roles")}
    assert codes == {r.code for r in PlatformRole}


def test_seeded_roles_are_marked_system():
    assert db.scalar("select count(*) from roles where not is_system") == 0


def test_role_display_names_are_persian_and_non_empty():
    for row in db.rows("select code, name_fa from roles"):
        assert row["name_fa"].strip()
        assert row["name_fa"] == PlatformRole(
            next(r for r in PlatformRole if r.code == row["code"])
        ).name_fa


def test_all_permissions_are_seeded():
    codes = {r["code"] for r in db.rows("select code from permissions")}
    assert codes == {p.code for p in Permission}
    assert len(codes) == 50


def test_permission_descriptions_are_seeded():
    assert db.scalar(
        "select count(*) from permissions where description is null or description = ''"
    ) == 0


def test_all_baseline_grants_are_seeded():
    actual = {
        (r["role_code"], r["permission_code"]) for r in db.rows(
            "select r.code as role_code, p.code as permission_code "
            "from role_permissions rp "
            "join roles r on r.id = rp.role_id "
            "join permissions p on p.id = rp.permission_id"
        )
    }
    assert actual == set(expected_state().grants)


def test_super_admin_holds_every_platform_permission_in_the_database():
    granted = {
        r["code"] for r in db.rows(
            "select p.code from role_permissions rp "
            "join roles r on r.id = rp.role_id "
            "join permissions p on p.id = rp.permission_id "
            "where r.code = 'super_admin'"
        )
    }
    assert granted == {p.code for p in ROLE_PERMISSIONS[PlatformRole.SUPER_ADMIN]}


def test_no_seller_scopable_permission_is_granted_to_any_platform_role():
    """The database must not contradict the baseline invariant."""
    scopable = {p.code for p in Permission if p.is_seller_scopable}
    granted = {
        r["code"] for r in db.rows(
            "select p.code from role_permissions rp "
            "join permissions p on p.id = rp.permission_id"
        )
    }
    assert granted.isdisjoint(scopable)


# --- what the seed must NOT touch -------------------------------------------
def test_seed_creates_no_users_sellers_or_memberships():
    assert db.scalar("select count(*) from user_roles") == 0
    assert db.scalar("select count(*) from sellers") == 0
    assert db.scalar("select count(*) from seller_members") == 0


def test_no_seller_platform_role_was_seeded():
    assert db.scalar("select count(*) from roles where code = 'seller'") == 0
    assert db.scalar(
        "select count(*) from roles where code like 'seller%'"
    ) == 0


# --- idempotency ------------------------------------------------------------
def test_reapplying_the_seed_converges_to_the_same_state():
    """Empty database and already-correct database must end identically."""
    before = read_state()
    at_revision("0019")
    at_revision("head")
    after_second = read_state()
    assert before == after_second
    assert db.scalar("select count(*) from roles") == len(list(PlatformRole))
    assert db.scalar("select count(*) from permissions") == 50


def test_seed_is_safe_to_run_twice_without_duplicates():
    counts_before = (
        db.scalar("select count(*) from roles"),
        db.scalar("select count(*) from permissions"),
        db.scalar("select count(*) from role_permissions"),
    )
    # Re-run upgrade over an already-seeded database via a down/up cycle.
    at_revision("0019")
    at_revision("head")
    counts_after = (
        db.scalar("select count(*) from roles"),
        db.scalar("select count(*) from permissions"),
        db.scalar("select count(*) from role_permissions"),
    )
    assert counts_before == counts_after


def test_composite_keys_still_reject_duplicates_after_seeding():
    role_id = db.scalar("select id from roles where code='customer'")
    perm_id = db.scalar(
        "select id from permissions where code=:c", c=Permission.ORDERING_ORDERS_CREATE.code
    )
    with pytest.raises(Exception) as exc:
        db.execute(
            "insert into role_permissions (role_id, permission_id) values (:r,:p)",
            r=role_id, p=perm_id,
        )
    assert "role_permissions_pkey" in str(exc.value) or "duplicate" in str(exc.value)


# --- downgrade --------------------------------------------------------------
def test_downgrade_removes_exactly_what_it_seeded():
    at_revision("0019")
    assert db.scalar("select count(*) from roles") == 0
    assert db.scalar("select count(*) from permissions") == 0
    assert db.scalar("select count(*) from role_permissions") == 0
    # Schema survives — 0020 seeds data, it does not own the tables.
    assert db.scalar(
        "select count(*) from information_schema.tables "
        "where table_schema='public' and table_name in "
        "('roles','permissions','role_permissions','user_roles')"
    ) == 4


def test_downgrade_leaves_operator_created_rows_untouched():
    """Downgrade deletes by code, so a role the seed never created survives."""
    db.execute(
        "insert into roles (code, name_fa, is_system) values ('local_experiment','x',false)"
    )
    at_revision("0019")
    assert db.scalar("select count(*) from roles where code='local_experiment'") == 1
    db.execute("delete from roles where code='local_experiment'")


# --- runtime drift ----------------------------------------------------------
def test_drift_is_clean_against_a_freshly_seeded_database():
    drift = db.run(check_drift(db.session_factory()))
    assert drift.is_clean, drift.render()
    assert drift.render() == "RBAC DRIFT: CLEAN"


def test_drift_detects_a_missing_permission_row():
    db.execute(
        "delete from role_permissions rp using permissions p "
        "where rp.permission_id = p.id and p.code = :c",
        c=Permission.OPS_AUDIT_READ.code,
    )
    db.execute("delete from permissions where code = :c", c=Permission.OPS_AUDIT_READ.code)
    drift = db.run(check_drift(db.session_factory()))
    assert not drift.is_clean
    assert Permission.OPS_AUDIT_READ.code in drift.missing_permissions


def test_drift_detects_an_unknown_permission_row():
    db.execute(
        "insert into permissions (code, description) values "
        "('quantum.teleport.everything','from the future')"
    )
    drift = db.run(check_drift(db.session_factory()))
    assert drift.unknown_permissions == ("quantum.teleport.everything",)


def test_drift_detects_an_unknown_role():
    db.execute("insert into roles (code, name_fa, is_system) values ('wizard','x',false)")
    drift = db.run(check_drift(db.session_factory()))
    assert "wizard" in drift.unknown_roles


def test_drift_detects_an_unexpected_grant():
    db.execute(
        "insert into role_permissions (role_id, permission_id) "
        "select r.id, p.id from roles r, permissions p "
        "where r.code='customer' and p.code=:c",
        c=Permission.OPS_DLQ_REPLAY.code,
    )
    drift = db.run(check_drift(db.session_factory()))
    assert ("customer", Permission.OPS_DLQ_REPLAY.code) in drift.unexpected_grants


def test_drift_detects_a_missing_grant():
    db.execute(
        "delete from role_permissions rp using roles r, permissions p "
        "where rp.role_id=r.id and rp.permission_id=p.id "
        "and r.code='support_agent' and p.code=:c",
        c=Permission.ORDERING_ORDERS_CANCEL_ANY.code,
    )
    drift = db.run(check_drift(db.session_factory()))
    assert ("support_agent", Permission.ORDERING_ORDERS_CANCEL_ANY.code) in drift.missing_grants


# --- fail-closed end to end -------------------------------------------------
def test_an_unknown_database_permission_never_becomes_application_authority():
    """The whole point of code-first: the database cannot invent authority."""
    from src.api.security import PrincipalFactory
    from src.modules.identity.application.ports import AccessTokenClaims
    from src.modules.identity.infrastructure.role_repository import SqlRoleGrantsQuery

    db.execute(
        "insert into permissions (code, description) values "
        "('quantum.teleport.everything','from the future')"
    )
    db.execute(
        "insert into role_permissions (role_id, permission_id) "
        "select r.id, p.id from roles r, permissions p "
        "where r.code='customer' and p.code='quantum.teleport.everything'"
    )

    async def _assemble():
        async with db.session_factory()() as session:
            return await PrincipalFactory(SqlRoleGrantsQuery(session)).from_claims(
                AccessTokenClaims(uuid.uuid4(), None, ("customer",))
            )

    principal = db.run(_assemble())
    codes = {p.code for p in principal.platform_permissions}
    assert "quantum.teleport.everything" not in codes
    # The legitimate customer grants survive alongside the dropped one.
    assert codes == {p.code for p in ROLE_PERMISSIONS[PlatformRole.CUSTOMER]}
    assert all(isinstance(p, Permission) for p in principal.platform_permissions)


def test_seeded_roles_resolve_to_their_baseline_permissions():
    from src.api.security import PrincipalFactory
    from src.modules.identity.application.ports import AccessTokenClaims
    from src.modules.identity.infrastructure.role_repository import SqlRoleGrantsQuery

    async def _assemble(role_code: str):
        async with db.session_factory()() as session:
            return await PrincipalFactory(SqlRoleGrantsQuery(session)).from_claims(
                AccessTokenClaims(uuid.uuid4(), None, (role_code,))
            )

    for role in PlatformRole:
        principal = db.run(_assemble(role.code))
        assert principal.platform_permissions == ROLE_PERMISSIONS[role], role.code
        # Platform RBAC never confers seller authority.
        assert principal.memberships == {}
        assert not principal.can_for_seller(
            Permission.SELLING_OFFERS_ACTIVATE, uuid.uuid4()
        )
