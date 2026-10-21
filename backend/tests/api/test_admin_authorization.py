"""Back-office authorization over real HTTP — regression lock for the audit.

Every assertion here corresponds to an exploit that SUCCEEDED against the live
application before this suite existed. The audit probe, run against the real
ASGI app with an ordinary customer's token, got:

    POST  /catalog/admin/products              -> 201  (created a product)
    PATCH /catalog/admin/products/{id}         -> 200  (edited someone else's)
    POST  /inventory/stock                     -> 200  (stock for another seller)
    POST  /inventory/reservations              -> 201  (reserved their stock)
    POST  /inventory/reservations/{id}/commit  -> 200  (committed it)

None of those callers held any role or membership. Authentication was the only
gate, and authentication is not authorization.

Nothing is mocked: the real routers, the real `PrincipalFactory`, real
`user_roles` and `seller_members` rows, real PostgreSQL and Redis.
"""

from __future__ import annotations

import uuid

import pytest

from tests.api.conftest import (
    PlatformRole,
    SellerMemberRole,
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
STOCK = "/api/v1/inventory/stock"
RESERVATIONS = "/api/v1/inventory/reservations"


def taxonomy() -> tuple[int, int]:
    """A product type and category that really exist, so a 422/404 from a
    dangling foreign key cannot be mistaken for an authorization refusal."""
    ptid = insert_returning(
        "insert into product_types (code, name_fa, is_active) "
        "values (:c, 'نوع', true) returning id",
        c=f"pt-{uuid.uuid4().hex[:8]}",
    )
    catid = insert_returning(
        "insert into categories (slug, name_fa, path, depth, sort_order, is_active) "
        "values (:s, 'دسته', :s, 0, 0, true) returning id",
        s=f"cat-{uuid.uuid4().hex[:8]}",
    )
    return ptid, catid


def product_body() -> dict:
    ptid, catid = taxonomy()
    return {
        "product_type_id": ptid, "category_id": catid,
        "slug": f"p-{uuid.uuid4().hex[:8]}", "name_fa": "کالا", "variants": [],
    }


# ===========================================================================
# Catalog admin
# ===========================================================================
def test_an_ordinary_customer_cannot_create_a_product(client):
    """EXPLOIT: this returned 201. The catalogue is shared by every seller, so a
    customer able to write to it can inject listings sitewide."""
    _uid, headers = principal()

    resp = call(client, "POST", PRODUCTS, headers=headers, json=product_body())

    assert resp.status_code == 403, resp.text
    assert db.scalar("select count(*) from products") == 0


def test_an_ordinary_customer_cannot_edit_a_product(client):
    """EXPLOIT: this returned 200 — any user could rewrite any product."""
    _admin, admin_headers = principal(PlatformRole.CATALOG_ADMIN.code)
    created = call(client, "POST", PRODUCTS, headers=admin_headers, json=product_body())
    assert created.status_code == 201, created.text
    product_id = created.json()["id"]

    _uid, headers = principal()
    resp = call(client, "PATCH", f"{PRODUCTS}/{product_id}", headers=headers,
                json={"name_fa": "تغییر توسط مهاجم"})

    assert resp.status_code == 403, resp.text
    assert db.scalar(
        "select name_fa from products where id = :i", i=uuid.UUID(product_id)
    ) == "کالا"


def test_a_seller_owner_cannot_write_to_the_shared_catalogue(client):
    """Seller authority stops at the seller's own shop (ADR-0011 §7). The
    catalogue belongs to the platform, not to any merchant."""
    _seller, _uid, headers = member(SellerMemberRole.OWNER)

    resp = call(client, "POST", PRODUCTS, headers=headers, json=product_body())

    assert resp.status_code == 403, resp.text


def test_the_catalog_admin_role_can_still_do_its_job(client):
    """The counterweight. A guard that locks out the role whose entire purpose is
    this router is as much a defect as one that lets everyone in — and this one
    nearly happened: `catalog.products.read` belongs to super_admin alone, so a
    router-wide read guard would have blocked catalog_admin from every route."""
    _uid, headers = principal(PlatformRole.CATALOG_ADMIN.code)

    created = call(client, "POST", PRODUCTS, headers=headers, json=product_body())
    assert created.status_code == 201, created.text

    updated = call(client, "PATCH", f"{PRODUCTS}/{created.json()['id']}",
                   headers=headers, json={"name_fa": "نام تازه"})
    assert updated.status_code == 200, updated.text


def test_an_unmodelled_field_is_rejected_rather_than_ignored(client):
    """A client sending `id` or `status` on create used to be accepted with the
    field silently dropped. Safe, but it hides the attempt; now it is a 422."""
    _uid, headers = principal(PlatformRole.CATALOG_ADMIN.code)

    resp = call(client, "POST", PRODUCTS, headers=headers,
                json={**product_body(), "status": "active", "owner_id": str(uuid.uuid4())})

    assert resp.status_code == 422, resp.text


# ===========================================================================
# Inventory — stock is the offer's seller's
# ===========================================================================
def test_a_stranger_cannot_inject_stock_for_another_sellers_offer(client):
    """EXPLOIT: this returned 200 and created 999999 units.

    Stock feeds BuyBox availability, so inventing it lets an attacker make a
    competitor appear to sell goods that do not exist — and every order placed
    against that phantom stock fails at fulfilment.
    """
    victim = make_seller()
    offer = seed_offer(victim, seed_variant(), status="active", amount=1000)
    _uid, headers = principal()

    resp = call(client, "POST", STOCK, headers=headers,
                json={"offer_id": str(offer), "quantity": 999_999})

    assert resp.status_code == 403, resp.text
    assert db.scalar(
        "select count(*) from inventory_stock where offer_id = :o", o=offer
    ) == 0


def test_a_seller_member_cannot_inject_stock_into_a_rivals_offer(client):
    """Holding full authority over your own shop is not authority over theirs."""
    rival = make_seller()
    offer = seed_offer(rival, seed_variant(), status="active", amount=1000)
    _mine, _uid, headers = member(SellerMemberRole.OWNER)

    resp = call(client, "POST", STOCK, headers=headers,
                json={"offer_id": str(offer), "quantity": 500})

    assert resp.status_code == 403, resp.text
    assert db.scalar(
        "select count(*) from inventory_stock where offer_id = :o", o=offer
    ) == 0


def test_a_seller_member_can_receive_stock_for_their_own_offer(client):
    """The legitimate flow the approved matrix grants (`inventory.stock.receive`).
    Closing the hole must not close the shop."""
    seller, _uid, headers = member(SellerMemberRole.OWNER)
    offer = seed_offer(seller, seed_variant(), status="active", amount=1000)

    resp = call(client, "POST", STOCK, headers=headers,
                json={"offer_id": str(offer), "quantity": 7})

    assert resp.status_code == 200, resp.text
    assert db.scalar(
        "select on_hand from inventory_stock where offer_id = :o", o=offer
    ) == 7


def test_a_seller_viewer_cannot_receive_stock(client):
    """Read-only membership is read-only. `inventory.stock.receive` is not in the
    viewer's approved permission set."""
    seller, _uid, headers = member(SellerMemberRole.VIEWER)
    offer = seed_offer(seller, seed_variant(), status="active", amount=1000)

    resp = call(client, "POST", STOCK, headers=headers,
                json={"offer_id": str(offer), "quantity": 7})

    assert resp.status_code == 403, resp.text


# ===========================================================================
# Inventory — reservations are platform/internal
# ===========================================================================
def existing_reservation(client) -> tuple[uuid.UUID, uuid.UUID]:
    """A real PENDING reservation, created the legitimate way."""
    seller, _uid, seller_headers = member(SellerMemberRole.OWNER)
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


@pytest.mark.parametrize("action", ["commit", "release"])
def test_a_stranger_cannot_touch_a_reservation_by_id(client, action):
    """EXPLOIT: both returned 200 to a third party who knew only the id.

    Committing someone else's reservation consumes their stock; releasing it
    hands stock back mid-order and breaks the saga's accounting.
    """
    rid, _offer = existing_reservation(client)
    _uid, headers = principal()

    resp = call(client, "POST", f"{RESERVATIONS}/{rid}/{action}", headers=headers)

    assert resp.status_code == 403, resp.text
    assert db.scalar(
        "select status from inventory_reservation where id = :i", i=rid
    ) == "pending"


def test_a_stranger_cannot_create_a_reservation(client):
    """EXPLOIT: this returned 201 — an attacker could drain a seller's available
    stock into holds without ever placing an order."""
    victim = make_seller()
    offer = seed_offer(victim, seed_variant(), status="active", amount=1000)
    _seller_uid, seller_headers = member(SellerMemberRole.OWNER, seller_id=victim)[1:]
    call(client, "POST", STOCK, headers=seller_headers,
         json={"offer_id": str(offer), "quantity": 10})

    _uid, headers = principal()
    resp = call(client, "POST", RESERVATIONS, headers=headers, json={
        "reservation_id": str(uuid.uuid4()), "offer_id": str(offer),
        "quantity": 5, "ttl_seconds": 900})

    assert resp.status_code == 403, resp.text
    assert db.scalar("select count(*) from inventory_reservation") == 0


def test_a_seller_member_cannot_reach_the_reservation_routes(client):
    """Reservations are not a merchant-owned resource. In normal operation the
    Ordering saga creates them in-process; these routes are back-office only."""
    seller, _uid, headers = member(SellerMemberRole.OWNER)
    offer = seed_offer(seller, seed_variant(), status="active", amount=1000)
    call(client, "POST", STOCK, headers=headers,
         json={"offer_id": str(offer), "quantity": 10})

    resp = call(client, "POST", RESERVATIONS, headers=headers, json={
        "reservation_id": str(uuid.uuid4()), "offer_id": str(offer),
        "quantity": 1, "ttl_seconds": 900})

    assert resp.status_code == 403, resp.text


def test_the_inventory_manager_role_can_still_operate(client):
    """The counterweight for the reservation routes."""
    rid, _offer = existing_reservation(client)
    _uid, headers = principal(PlatformRole.INVENTORY_MANAGER.code)

    resp = call(client, "POST", f"{RESERVATIONS}/{rid}/commit", headers=headers)

    assert resp.status_code == 200, resp.text
    assert db.scalar(
        "select status from inventory_reservation where id = :i", i=rid
    ) == "committed"


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("POST", PRODUCTS, {"product_type_id": 1, "category_id": 1,
                            "slug": "x", "name_fa": "x", "variants": []}),
        ("POST", STOCK, {"offer_id": str(uuid.uuid4()), "quantity": 1}),
        ("POST", RESERVATIONS, {"reservation_id": str(uuid.uuid4()),
                                "offer_id": str(uuid.uuid4()), "quantity": 1}),
    ],
)
def test_anonymous_callers_are_refused_before_authorization(client, method, url, body):
    """401, never 403: authorization must not be reachable without authentication,
    and a 403 would confirm to an anonymous prober that the resource exists."""
    assert call(client, method, url, json=body).status_code == 401
