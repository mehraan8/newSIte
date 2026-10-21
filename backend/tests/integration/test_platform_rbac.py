"""Platform RBAC against real PostgreSQL (ADR-0011 §6, Phase 1D).

Exercises the ACTUAL resolution path the composition root uses:

    user_id -> user_roles -> roles.code -> role_permissions -> permissions.code
            -> Permission enum members -> Principal.platform_permissions

Nothing is mocked: the real SQL adapters and the real PrincipalFactory run
against the real schema, because the failure modes worth testing here (composite
PKs, CASCADE, unknown codes surviving in the database) only exist in Postgres.

Fixture rows are created per test and removed afterwards — no seed data is added
to the schema, and no migration carries test data (Phase 1E owns seeding).
"""

from __future__ import annotations

import uuid

import pytest

from src.api.security import PrincipalFactory
from src.modules.identity.application.ports import AccessTokenClaims
from src.modules.identity.application.roles import PlatformRole
from src.modules.identity.infrastructure.role_repository import (
    SqlPlatformRoleQuery,
    SqlRoleGrantsQuery,
)
from src.shared_kernel.application.permissions import Permission
from tests.integration import db

db.skip_if_unavailable()

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def schema():
    db.migrate()
    yield


@pytest.fixture(autouse=True)
def clean_rbac():
    """RBAC tables are shared reference data, so each test starts and ends empty.

    Deliberately narrow: only the tables this suite writes. Deleting users last
    cascades their grants.
    """
    def _wipe():
        db.execute("delete from user_roles")
        db.execute("delete from role_permissions")
        db.execute("delete from roles")
        db.execute("delete from permissions")
        db.execute("delete from users where mobile like '+98999%'")

    _wipe()
    yield
    _wipe()


# --- fixtures ---------------------------------------------------------------
def make_user() -> uuid.UUID:
    uid = uuid.uuid4()
    mobile = "+98999" + str(uuid.uuid4().int)[:7]
    db.execute(
        "insert into users (id, mobile, status, is_phone_verified) "
        "values (:i,:m,'active',true)", i=uid, m=mobile,
    )
    return uid


def make_role(code: str, name_fa: str = "نقش") -> int:
    db.execute(
        "insert into roles (code, name_fa, is_system) values (:c,:n,true)",
        c=code, n=name_fa,
    )
    return db.scalar("select id from roles where code=:c", c=code)


def make_permission(code: str) -> int:
    db.execute("insert into permissions (code, description) values (:c,'x')", c=code)
    return db.scalar("select id from permissions where code=:c", c=code)


def grant_permission(role_id: int, permission_id: int) -> None:
    db.execute(
        "insert into role_permissions (role_id, permission_id) values (:r,:p)",
        r=role_id, p=permission_id,
    )


def assign_role(user_id: uuid.UUID, role_id: int) -> None:
    db.execute(
        "insert into user_roles (user_id, role_id) values (:u,:r)", u=user_id, r=role_id
    )


def resolve(user_id: uuid.UUID):
    """The real adapters, on a real session."""
    async def _go():
        async with db.session_factory()() as session:
            codes = await SqlPlatformRoleQuery(session).role_codes_for_user(user_id)
            perms = await SqlRoleGrantsQuery(session).permission_codes_for_roles(list(codes))
            principal = await PrincipalFactory(SqlRoleGrantsQuery(session)).from_claims(
                AccessTokenClaims(user_id, None, codes)
            )
            return codes, perms, principal

    return db.run(_go())


# --- structure --------------------------------------------------------------
def test_rbac_tables_exist_with_expected_columns():
    expected = {
        "roles": {"id", "code", "name_fa", "is_system", "created_at"},
        "permissions": {"id", "code", "description"},
        "role_permissions": {"role_id", "permission_id"},
        "user_roles": {"user_id", "role_id", "assigned_at"},
    }
    for table, columns in expected.items():
        actual = {
            r["column_name"] for r in db.rows(
                "select column_name from information_schema.columns "
                "where table_name=:t", t=table,
            )
        }
        assert actual == columns, f"{table}: {actual}"


def test_junction_tables_have_composite_primary_keys():
    """§4/§5 uniqueness is a DATABASE invariant, not an application check."""
    for table, cols in (
        ("user_roles", {"user_id", "role_id"}),
        ("role_permissions", {"role_id", "permission_id"}),
    ):
        pk = db.rows(
            "select pg_get_constraintdef(oid) as d from pg_constraint "
            "where conrelid = cast(:t as regclass) and contype='p'", t=table,
        )
        assert len(pk) == 1, table
        for col in cols:
            assert col in pk[0]["d"], f"{table} PK missing {col}"


def test_stable_code_columns_are_unique():
    for table in ("roles", "permissions"):
        uniques = " ".join(
            r["d"] for r in db.rows(
                "select pg_get_constraintdef(oid) as d from pg_constraint "
                "where conrelid = cast(:t as regclass) and contype='u'", t=table,
            )
        )
        assert "code" in uniques, table


def test_no_rbac_table_references_merchant():
    """RULE 6: platform RBAC must not couple to Merchant."""
    for table in ("roles", "permissions", "role_permissions", "user_roles"):
        fks = " ".join(
            r["d"] for r in db.rows(
                "select pg_get_constraintdef(oid) as d from pg_constraint "
                "where conrelid = cast(:t as regclass) and contype='f'", t=table,
            )
        )
        assert "sellers" not in fks and "seller_members" not in fks, table


def test_seller_members_has_no_foreign_key_to_roles():
    fks = " ".join(
        r["d"] for r in db.rows(
            "select pg_get_constraintdef(oid) as d from pg_constraint "
            "where conrelid='seller_members'::regclass and contype='f'"
        )
    )
    assert "roles" not in fks


# --- relationship integrity -------------------------------------------------
def test_duplicate_user_role_assignment_is_rejected():
    user, role = make_user(), make_role("customer")
    assign_role(user, role)
    with pytest.raises(Exception) as exc:
        assign_role(user, role)
    assert "user_roles_pkey" in str(exc.value) or "duplicate key" in str(exc.value)


def test_duplicate_role_permission_grant_is_rejected():
    role = make_role("catalog_admin")
    perm = make_permission(Permission.CATALOG_PRODUCTS_CREATE.code)
    grant_permission(role, perm)
    with pytest.raises(Exception) as exc:
        grant_permission(role, perm)
    assert "role_permissions_pkey" in str(exc.value) or "duplicate key" in str(exc.value)


def test_a_user_may_hold_several_roles_and_a_role_several_users():
    u1, u2 = make_user(), make_user()
    r1, r2 = make_role("customer"), make_role("support_agent")
    assign_role(u1, r1)
    assign_role(u1, r2)
    assign_role(u2, r1)
    assert db.scalar("select count(*) from user_roles where user_id=:u", u=u1) == 2
    assert db.scalar("select count(*) from user_roles where role_id=:r", r=r1) == 2


def test_a_role_may_grant_many_permissions_and_a_permission_belong_to_many_roles():
    r1, r2 = make_role("catalog_admin"), make_role("super_admin")
    p1 = make_permission(Permission.CATALOG_PRODUCTS_CREATE.code)
    p2 = make_permission(Permission.CATALOG_PRODUCTS_UPDATE.code)
    grant_permission(r1, p1)
    grant_permission(r1, p2)
    grant_permission(r2, p1)
    assert db.scalar("select count(*) from role_permissions where role_id=:r", r=r1) == 2
    assert db.scalar(
        "select count(*) from role_permissions where permission_id=:p", p=p1
    ) == 2


def test_revoking_a_role_cascades_its_grants():
    user, role = make_user(), make_role("customer")
    perm = make_permission(Permission.ORDERING_ORDERS_CREATE.code)
    assign_role(user, role)
    grant_permission(role, perm)
    db.execute("delete from roles where id=:r", r=role)
    assert db.scalar("select count(*) from user_roles where role_id=:r", r=role) == 0
    assert db.scalar("select count(*) from role_permissions where role_id=:r", r=role) == 0


# --- resolution through the real adapters -----------------------------------
def test_full_resolution_user_to_permission_enum():
    user = make_user()
    role = make_role(PlatformRole.CATALOG_ADMIN.code, PlatformRole.CATALOG_ADMIN.name_fa)
    for permission in (Permission.CATALOG_PRODUCTS_CREATE, Permission.CATALOG_PRODUCTS_UPDATE):
        grant_permission(role, make_permission(permission.code))
    assign_role(user, role)

    codes, perms, principal = resolve(user)
    assert codes == ("catalog_admin",)
    assert perms == {
        Permission.CATALOG_PRODUCTS_CREATE.code,
        Permission.CATALOG_PRODUCTS_UPDATE.code,
    }
    assert principal.platform_permissions == {
        Permission.CATALOG_PRODUCTS_CREATE, Permission.CATALOG_PRODUCTS_UPDATE
    }
    assert all(isinstance(p, Permission) for p in principal.platform_permissions)


def test_multiple_roles_union_their_permissions():
    user = make_user()
    r1 = make_role("catalog_admin")
    r2 = make_role("support_agent")
    grant_permission(r1, make_permission(Permission.CATALOG_PRODUCTS_CREATE.code))
    grant_permission(r2, make_permission(Permission.ORDERING_ORDERS_READ_ANY.code))
    assign_role(user, r1)
    assign_role(user, r2)

    _codes, _perms, principal = resolve(user)
    assert principal.can(Permission.CATALOG_PRODUCTS_CREATE)
    assert principal.can(Permission.ORDERING_ORDERS_READ_ANY)


def test_a_user_with_no_roles_gets_no_authority():
    _codes, _perms, principal = resolve(make_user())
    assert _codes == ()
    assert principal.platform_permissions == frozenset()


# --- fail closed ------------------------------------------------------------
def test_unknown_permission_code_in_the_database_confers_nothing():
    """A database ahead of this build must not grant authority the code does not
    define (ADR-0011 §5). The known permission alongside it must survive."""
    user = make_user()
    role = make_role("catalog_admin")
    grant_permission(role, make_permission(Permission.CATALOG_PRODUCTS_CREATE.code))
    grant_permission(role, make_permission("quantum.teleport.everything"))
    assign_role(user, role)

    _codes, perms, principal = resolve(user)
    # The adapter faithfully returns what the database holds ...
    assert "quantum.teleport.everything" in perms
    # ... and assembly drops what the enum does not know.
    assert principal.platform_permissions == {Permission.CATALOG_PRODUCTS_CREATE}
    assert all(isinstance(p, Permission) for p in principal.platform_permissions)


def test_unknown_role_code_confers_nothing():
    user = make_user()
    role = make_role("wizard")          # not in the PlatformRole vocabulary
    grant_permission(role, make_permission(Permission.OPS_DLQ_REPLAY.code))
    assign_role(user, role)

    codes, _perms, principal = resolve(user)
    assert codes == ("wizard",)
    # The role is unknown to the vocabulary, but authority still comes only from
    # what the database actually grants it — and nothing is invented.
    from src.modules.identity.application.roles import from_code

    assert from_code("wizard") is None
    assert principal.platform_permissions == {Permission.OPS_DLQ_REPLAY}


def test_a_role_with_no_grants_confers_nothing():
    user = make_user()
    assign_role(user, make_role("readonly_operator"))
    _codes, perms, principal = resolve(user)
    assert perms == frozenset()
    assert principal.platform_permissions == frozenset()


# --- separation from seller membership --------------------------------------
def test_platform_role_resolution_never_reads_seller_membership():
    """A user with a platform role and no membership must resolve platform
    permissions and ZERO seller authority."""
    user = make_user()
    role = make_role("super_admin")
    grant_permission(role, make_permission(Permission.SELLING_OFFERS_MANAGE_ANY.code))
    assign_role(user, role)

    _codes, _perms, principal = resolve(user)
    # Platform-wide override: held.
    assert principal.can(Permission.SELLING_OFFERS_MANAGE_ANY)
    # Seller-scoped: nothing, because membership is a different system entirely.
    assert principal.memberships == {}
    assert not principal.can_for_seller(
        Permission.SELLING_OFFERS_MANAGE_ANY, uuid.uuid4()
    )


def test_platform_grants_create_no_seller_membership_rows():
    user = make_user()
    role = make_role("super_admin")
    grant_permission(role, make_permission(Permission.MERCHANT_SELLERS_MANAGE_ANY.code))
    assign_role(user, role)
    resolve(user)
    assert db.scalar(
        "select count(*) from seller_members where user_id=:u", u=user
    ) == 0


def test_principal_subject_remains_the_user_identity():
    user = make_user()
    assign_role(user, make_role("customer"))
    _codes, _perms, principal = resolve(user)
    assert principal.subject_id == user
