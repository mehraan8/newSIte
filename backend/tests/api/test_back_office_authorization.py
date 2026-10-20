"""Back-office authorization over real HTTP — Phase 1H.

`tests/api/test_admin_authorization.py` proved the security audit's finding: an
ordinary customer could reach these routes at all. This suite goes a layer
deeper, into the distinctions that only matter once the door is shut:

  * PUBLISH AND ARCHIVE ARE NOT UPDATE. `PATCH /products/{id}` accepts `status`,
    which puts a product on or off the storefront. It used to demand
    `catalog.products.update` for that, which made `.publish` and `.archive`
    decorative permissions.
  * THE USE CASE DECIDES, NOT THE ROUTER. A router guard cannot see the body, so
    it cannot tell an edit from a publish; and it protects a route, not an
    operation.
  * AUTHORIZATION PRECEDES EVERYTHING. No load, no mutation, no outbox row, and
    no existence oracle for an unauthorized caller.

Real ASGI app, real `PrincipalFactory`, real `user_roles` rows, real PostgreSQL.
Principals are never hand-constructed here.
"""

from __future__ import annotations

import uuid

import pytest

from tests.api.conftest import (
    PlatformRole,
    call,
    insert_returning,
    make_seller,
    member,
    principal,
    seed_offer,
    seed_variant,
)
from tests.integration import db

pytestmark = pytest.mark.integration

PRODUCTS = "/api/v1/catalog/admin/products"
RESERVATIONS = "/api/v1/inventory/reservations"
STOCK = "/api/v1/inventory/stock"


def taxonomy() -> tuple[int, int]:
    return (
        insert_returning(
            "insert into product_types (code, name_fa, is_active) "
            "values (:c, 'نوع', true) returning id",
            c=f"pt-{uuid.uuid4().hex[:8]}",
        ),
        insert_returning(
            "insert into categories (slug, name_fa, path, depth, sort_order, is_active) "
            "values (:s, 'دسته', :s, 0, 0, true) returning id",
            s=f"cat-{uuid.uuid4().hex[:8]}",
        ),
    )


def product_body() -> dict:
    ptid, catid = taxonomy()
    return {
        "product_type_id": ptid, "category_id": catid,
        "slug": f"p-{uuid.uuid4().hex[:8]}", "name_fa": "کالا", "variants": [],
    }


def a_product(client) -> str:
    """A real product, created through the real endpoint by a real catalog_admin."""
    _uid, headers = principal(PlatformRole.CATALOG_ADMIN.code)
    created = call(client, "POST", PRODUCTS, headers=headers, json=product_body())
    assert created.status_code == 201, created.text
    return created.json()["id"]


def status_of(product_id: str) -> str:
    return db.scalar("select status from products where id = :i", i=uuid.UUID(product_id))


def outbox_count() -> int:
    return db.scalar("select count(*) from outbox_messages") or 0


# ===========================================================================
# Catalog — update / publish / archive are three different authorities
# ===========================================================================
def test_catalog_admin_can_edit_publish_and_archive(client):
    """The counterweight. catalog_admin holds all three in the baseline, so the
    separation must not have locked the legitimate role out of any of them."""
    product = a_product(client)
    _uid, headers = principal(PlatformRole.CATALOG_ADMIN.code)

    edited = call(client, "PATCH", f"{PRODUCTS}/{product}", headers=headers,
                  json={"name_fa": "نام تازه"})
    assert edited.status_code == 200, edited.text

    published = call(client, "PATCH", f"{PRODUCTS}/{product}", headers=headers,
                     json={"status": "active"})
    assert published.status_code == 200, published.text
    assert status_of(product) == "active"

    archived = call(client, "PATCH", f"{PRODUCTS}/{product}", headers=headers,
                    json={"status": "archived"})
    assert archived.status_code == 200, archived.text
    assert status_of(product) == "archived"


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"status": "active"}, "publish"),
        ({"status": "archived"}, "archive"),
        ({"status": "draft"}, "unpublish"),
    ],
)
def test_a_status_change_is_not_an_attribute_edit(client, body, field):
    """A caller holding ONLY `catalog.products.update` must not change storefront
    visibility. No seeded role is update-only today, which is exactly why this is
    asserted at the permission level: the matrix is a contract for the roles that
    do not exist yet.

    Exercised over HTTP by granting the update permission through a throwaway
    role, so the real `PrincipalFactory` and `role_permissions` are in the path.
    """
    product = a_product(client)
    _uid, headers = temp_role_headers("catalog.products.update")
    before = status_of(product)

    resp = call(client, "PATCH", f"{PRODUCTS}/{product}", headers=headers, json=body)

    assert resp.status_code == 403, f"{field} was authorized by `update`: {resp.text[:160]}"
    assert status_of(product) == before


def test_publish_authority_does_not_grant_attribute_edits(client):
    """The other direction. Someone allowed to put a product on the storefront is
    not thereby allowed to rewrite its copy."""
    product = a_product(client)
    _uid, headers = temp_role_headers("catalog.products.publish")

    resp = call(client, "PATCH", f"{PRODUCTS}/{product}", headers=headers,
                json={"name_fa": "بازنویسی"})

    assert resp.status_code == 403, resp.text
    assert db.scalar(
        "select name_fa from products where id = :i", i=uuid.UUID(product)
    ) == "کالا"


def test_publish_authority_does_not_grant_archiving(client):
    product = a_product(client)
    _uid, headers = temp_role_headers("catalog.products.publish")

    resp = call(client, "PATCH", f"{PRODUCTS}/{product}", headers=headers,
                json={"status": "archived"})

    assert resp.status_code == 403, resp.text
    assert status_of(product) == "draft"


def test_editing_and_publishing_together_needs_both(client):
    """A compound request must not slip past on the weaker of its two
    authorities."""
    product = a_product(client)
    body = {"name_fa": "نام و انتشار", "status": "active"}

    _u1, update_only = temp_role_headers("catalog.products.update")
    assert call(client, "PATCH", f"{PRODUCTS}/{product}",
                headers=update_only, json=body).status_code == 403

    _u2, publish_only = temp_role_headers("catalog.products.publish")
    assert call(client, "PATCH", f"{PRODUCTS}/{product}",
                headers=publish_only, json=body).status_code == 403

    _u3, both = temp_role_headers("catalog.products.update", "catalog.products.publish")
    ok = call(client, "PATCH", f"{PRODUCTS}/{product}", headers=both, json=body)
    assert ok.status_code == 200, ok.text
    assert status_of(product) == "active"


# ===========================================================================
# Authorization precedes the load — no oracle, no mutation
# ===========================================================================
def test_an_unauthorized_update_cannot_probe_which_products_exist(client):
    """A real id and a fabricated one must be indistinguishable to someone who
    may not update. If the load ran first, one would be 403 and the other 404."""
    real = a_product(client)
    _uid, headers = principal()  # authenticated, no platform role

    known = call(client, "PATCH", f"{PRODUCTS}/{real}", headers=headers,
                 json={"name_fa": "x"})
    unknown = call(client, "PATCH", f"{PRODUCTS}/{uuid.uuid4()}", headers=headers,
                   json={"name_fa": "x"})

    assert known.status_code == unknown.status_code == 403
    assert known.json()["title"] == unknown.json()["title"]


def test_an_unauthorized_create_cannot_probe_which_slugs_are_taken(client):
    """Slug collisions answer 409 to an authorized caller. An unauthorized one
    must not learn the difference."""
    _admin, admin_headers = principal(PlatformRole.CATALOG_ADMIN.code)
    body = product_body()
    assert call(client, "POST", PRODUCTS, headers=admin_headers,
                json=body).status_code == 201

    _uid, headers = principal()
    taken = call(client, "POST", PRODUCTS, headers=headers, json=body)
    free = call(client, "POST", PRODUCTS, headers=headers, json=product_body())

    assert taken.status_code == free.status_code == 403


def test_an_unauthorized_reservation_commit_cannot_probe_reservation_ids(client):
    _uid, headers = principal()

    resp = call(client, "POST", f"{RESERVATIONS}/{uuid.uuid4()}/commit", headers=headers)

    assert resp.status_code == 403, "a 404 here would confirm the id does not exist"


@pytest.mark.parametrize("action", ["commit", "release"])
def test_a_denied_reservation_action_changes_nothing(client, action):
    rid, _offer = a_pending_reservation(client)
    before = outbox_count()
    _uid, headers = principal()

    resp = call(client, "POST", f"{RESERVATIONS}/{rid}/{action}", headers=headers)

    assert resp.status_code == 403
    assert db.scalar(
        "select status from inventory_reservation where id = :i", i=rid
    ) == "pending"
    assert outbox_count() == before, "a denied request wrote to the outbox"


# ===========================================================================
# Inventory — each reservation verb needs its own permission
# ===========================================================================
def a_pending_reservation(client) -> tuple[uuid.UUID, uuid.UUID]:
    seller, _uid, seller_headers = member()
    offer = seed_offer(seller, seed_variant(), status="active", amount=1000)
    assert call(client, "POST", STOCK, headers=seller_headers,
                json={"offer_id": str(offer), "quantity": 10}).status_code == 200

    _op, op_headers = principal(PlatformRole.INVENTORY_MANAGER.code)
    rid = uuid.uuid4()
    created = call(client, "POST", RESERVATIONS, headers=op_headers, json={
        "reservation_id": str(rid), "offer_id": str(offer),
        "quantity": 4, "ttl_seconds": 900})
    assert created.status_code == 201, created.text
    return rid, offer


@pytest.mark.parametrize(
    ("held", "action"),
    [
        ("inventory.reservations.create", "commit"),
        ("inventory.reservations.create", "release"),
        ("inventory.reservations.commit", "release"),
        ("inventory.reservations.release", "commit"),
        ("inventory.manage.any", "commit"),
        ("inventory.manage.any", "release"),
    ],
)
def test_one_reservation_permission_does_not_authorize_another(client, held, action):
    """`manage.any` is included deliberately: "administer stock across all
    sellers" reads like a superset, and is not one."""
    rid, _offer = a_pending_reservation(client)
    _uid, headers = temp_role_headers(held)

    resp = call(client, "POST", f"{RESERVATIONS}/{rid}/{action}", headers=headers)

    assert resp.status_code == 403, f"{held} authorized {action}: {resp.text[:160]}"
    assert db.scalar(
        "select status from inventory_reservation where id = :i", i=rid
    ) == "pending"


def test_the_inventory_manager_can_run_the_whole_reservation_lifecycle(client):
    """The counterweight for inventory."""
    rid, _offer = a_pending_reservation(client)
    _uid, headers = principal(PlatformRole.INVENTORY_MANAGER.code)

    resp = call(client, "POST", f"{RESERVATIONS}/{rid}/commit", headers=headers)
    assert resp.status_code == 200, resp.text
    assert db.scalar(
        "select status from inventory_reservation where id = :i", i=rid
    ) == "committed"


def test_reservation_creation_needs_its_own_permission(client):
    seller, _uid, seller_headers = member()
    offer = seed_offer(seller, seed_variant(), status="active", amount=1000)
    call(client, "POST", STOCK, headers=seller_headers,
         json={"offer_id": str(offer), "quantity": 10})

    _uid2, headers = temp_role_headers("inventory.reservations.commit")
    resp = call(client, "POST", RESERVATIONS, headers=headers, json={
        "reservation_id": str(uuid.uuid4()), "offer_id": str(offer),
        "quantity": 1, "ttl_seconds": 900})

    assert resp.status_code == 403, resp.text
    assert db.scalar("select count(*) from inventory_reservation") == 0


# ===========================================================================
# A throwaway role, so permission-level tests run through the real stack
# ===========================================================================
def temp_role_headers(*permission_codes: str):
    """A user granted exactly these permissions via a real, disposable role.

    The seeded roles are coarse — catalog_admin holds update, publish AND
    archive — so asserting that one permission does not imply another needs a
    principal holding exactly one. Building it in the DATABASE keeps
    `PrincipalFactory`, `role_permissions` and the JWT path in the test, which
    hand-constructing a Principal would bypass.

    Rows are namespaced `qa_temp_*` and cleaned by the suite's TRUNCATE.
    """
    from tests.api.conftest import auth, make_user

    code = f"qa_temp_{uuid.uuid4().hex[:8]}"
    db.execute(
        "insert into roles (code, name_fa, is_system) values (:c, :n, false)",
        c=code, n="نقش آزمایشی",
    )
    role_id = db.scalar("select id from roles where code = :c", c=code)
    for permission_code in permission_codes:
        permission_id = db.scalar(
            "select id from permissions where code = :c", c=permission_code
        )
        assert permission_id is not None, f"{permission_code} is not seeded"
        db.execute(
            "insert into role_permissions (role_id, permission_id) values (:r, :p)",
            r=role_id, p=permission_id,
        )

    user = make_user()
    db.execute(
        "insert into user_roles (user_id, role_id) values (:u, :r)", u=user, r=role_id
    )
    # The token carries role CODES; permissions are resolved per request from
    # role_permissions, so the throwaway role behaves exactly like a seeded one.
    return user, auth(user, (code,))


def test_the_throwaway_role_helper_actually_grants_what_it_claims(client):
    """A fixture that silently granted nothing would make every denial above pass
    for the wrong reason — the most dangerous kind of green test."""
    product = a_product(client)
    _uid, headers = temp_role_headers("catalog.products.update")

    resp = call(client, "PATCH", f"{PRODUCTS}/{product}", headers=headers,
                json={"name_fa": "ویرایش مجاز"})

    assert resp.status_code == 200, resp.text


# ===========================================================================
# The remaining ID-shaped cases from the Phase 1H checklist
# ===========================================================================
# Each back-office route with an id in the path, paired with the role that is
# actually allowed to call it. Using the wrong role would make every case below
# a 403 from the router guard and prove nothing about id handling.
ID_ROUTES = [
    ("PATCH", PRODUCTS + "/{}", "catalog_admin", {"name_fa": "x"}),
    ("POST", RESERVATIONS + "/{}/commit", "inventory_manager", None),
    ("POST", RESERVATIONS + "/{}/release", "inventory_manager", None),
]
ID_ROUTE_IDS = [f"{m}-{t.split('/')[-1] or 'product'}" for m, t, _r, _b in ID_ROUTES]


@pytest.mark.parametrize(
    ("method", "template", "role", "body"), ID_ROUTES, ids=ID_ROUTE_IDS
)
@pytest.mark.parametrize("bad_id", ["not-a-uuid", "123", "%00", "../../etc/passwd"])
def test_a_malformed_id_is_a_validation_error_not_a_crash(
    client, method, template, role, body, bad_id
):
    """Path params are UUID-typed, so a malformed id is rejected by the schema
    before the handler runs. It must never reach the use case as a ValueError and
    surface as a 500, and it must never be mistaken for a real id.

    The caller here IS authorized, so what is being tested is id handling rather
    than the guard.
    """
    _uid, headers = principal(role)

    resp = call(client, method, template.format(bad_id), headers=headers, json=body)

    assert resp.status_code in (404, 422), resp.text


@pytest.mark.parametrize(
    ("method", "template", "role", "body"), ID_ROUTES, ids=ID_ROUTE_IDS
)
@pytest.mark.parametrize("bad_id", ["not-a-uuid", "123"])
def test_an_unauthorized_caller_cannot_even_learn_their_id_was_malformed(
    client, method, template, role, body, bad_id
):
    """Authorization outranks validation, and that ordering is worth keeping.

    A 422 here would tell an unauthorized caller that their id was well-formed
    or not — a free syntax oracle, and the first step of probing. They get the
    same 403 either way.
    """
    _uid, headers = principal()  # authenticated, no platform role

    resp = call(client, method, template.format(bad_id), headers=headers, json=body)

    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize(
    ("method", "template", "role", "body"), ID_ROUTES, ids=ID_ROUTE_IDS
)
def test_a_forged_but_well_formed_id_is_denied_before_it_is_looked_up(
    client, method, template, role, body
):
    """A syntactically perfect UUID that names nothing. For an unauthorized
    caller the answer must be 403 — not 404 — because authorization runs first;
    otherwise the endpoint confirms which ids are absent."""
    _uid, headers = principal()

    resp = call(client, method, template.format(uuid.uuid4()), headers=headers, json=body)

    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize(
    ("method", "template", "role", "body"), ID_ROUTES, ids=ID_ROUTE_IDS
)
def test_an_authorized_caller_still_gets_404_for_a_missing_resource(
    client, method, template, role, body
):
    """The other half of the contract. Hiding existence from everyone would mean
    an operator could not tell a typo from a deletion."""
    _uid, headers = principal(role)

    resp = call(client, method, template.format(uuid.uuid4()), headers=headers, json=body)

    assert resp.status_code == 404, resp.text


def test_a_seller_member_cannot_reach_any_back_office_id_route(client):
    """Seller authority never becomes platform authority, whatever id is used."""
    rid, _offer = a_pending_reservation(client)
    product = a_product(client)
    _seller, _uid, headers = member()

    assert call(client, "POST", f"{RESERVATIONS}/{rid}/commit",
                headers=headers).status_code == 403
    assert call(client, "PATCH", f"{PRODUCTS}/{product}", headers=headers,
                json={"status": "active"}).status_code == 403
    assert status_of(product) == "draft"
