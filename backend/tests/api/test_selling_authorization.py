"""Object-level authorization for Selling over real HTTP — regression lock.

Two separate holes are covered here, and passing one does not imply the other:

  * IDENTITY BINDING — `seller_id` is a request field again, so the question is
    no longer "is it present" but "does sending it grant anything". It must not:
    the value selects a target, and the server proves the authority from the
    caller's memberships.
  * OWNERSHIP — activate/suspend/reprice used to look the offer up by id and
    never check who owned it, so one seller could suspend a competitor's offer or
    reprice it to 1.

The attackers here are not outsiders. They hold valid tokens and, in most cases,
full owner authority over a seller of their own. That is the point: authority is
per-seller, so "is a seller" is never the question.

Everything is real — the ASGI app, the router wiring, `PrincipalFactory`, the
Redis-backed membership cache, PostgreSQL. A denial is checked twice: the status,
and the database row.
"""

from __future__ import annotations

import uuid

import pytest

from tests.api.conftest import (
    SellerMemberRole,
    add_member,
    auth,
    call,
    forget,
    make_seller,
    make_user,
    member,
    principal,
    seed_offer,
    seed_variant,
)
from tests.integration import db

pytestmark = pytest.mark.integration

OFFERS = "/api/v1/selling/offers"


def create_body(seller_id: uuid.UUID, variant_id: uuid.UUID, amount: int = 5000) -> dict:
    return {
        "seller_id": str(seller_id), "variant_id": str(variant_id),
        "condition": "new", "currency": "IRR", "amount": amount, "scale": 0,
    }


def offer_status(offer_id) -> str | None:
    return db.scalar("select status from offers where id = :i", i=offer_id)


# ===========================================================================
# create — the seller must be one of yours
# ===========================================================================
def test_a_member_creates_an_offer_owned_by_their_seller(client):
    """MANDATORY regression test for the original User-as-Seller bug (brief §34).

    User U and Seller S are distinct rows with distinct ids. U is a legitimate
    owner member of S, and creates an offer through the real API. The offer must
    end up owned by S — and, just as importantly, NOT by U. The second assertion
    is the one that fails if `seller_id = user.id` ever returns.
    """
    seller, user, headers = member(SellerMemberRole.OWNER)
    assert seller != user, "the fixture must not conflate the two identities"
    variant = seed_variant()

    resp = call(client, "POST", OFFERS, headers=headers,
                json=create_body(seller, variant))

    assert resp.status_code == 201, resp.text
    assert resp.json()["seller_id"] == str(seller)
    assert resp.json()["seller_id"] != str(user)

    stored = db.scalar(
        "select seller_id from offers where id = :i", i=uuid.UUID(resp.json()["id"])
    )
    assert stored == seller
    assert stored != user
    # And the id that landed in offers is a real Seller, not a User that happens
    # to share the value: it resolves in `sellers`, and never in `users`.
    assert db.scalar("select count(*) from sellers where id = :i", i=stored) == 1
    assert db.scalar("select count(*) from users where id = :i", i=stored) == 0


def test_naming_another_sellers_id_is_denied_and_creates_nothing(client):
    """THE regression test for the original defect.

    `seller_id` was once taken from the body and trusted, which let any caller
    create offers under any seller's identity. It is back in the body — and worth
    exactly nothing without a membership behind it.
    """
    _mine, _user, headers = member(SellerMemberRole.OWNER)
    victim = make_seller()
    variant = seed_variant()

    resp = call(client, "POST", OFFERS, headers=headers,
                json=create_body(victim, variant))

    assert resp.status_code == 403, resp.text
    assert db.scalar("select count(*) from offers where seller_id = :s", s=victim) == 0
    assert db.scalar("select count(*) from outbox_messages") == 0


def test_a_caller_with_no_membership_creates_nothing(client):
    _uid, headers = principal()
    variant = seed_variant()

    resp = call(client, "POST", OFFERS, headers=headers,
                json=create_body(make_seller(), variant))

    assert resp.status_code == 403, resp.text
    assert db.scalar("select count(*) from offers") == 0


def test_naming_your_own_user_id_as_the_seller_is_denied(client):
    """`User != Seller`, asserted from the outside.

    If the old collapse existed anywhere, this request would succeed — it is the
    old behaviour spelled out as a payload.
    """
    _seller, user, headers = member(SellerMemberRole.OWNER)
    variant = seed_variant()

    resp = call(client, "POST", OFFERS, headers=headers,
                json=create_body(user, variant))

    assert resp.status_code == 403, resp.text
    assert db.scalar("select count(*) from offers") == 0


def test_an_unknown_seller_id_is_denied_without_revealing_that_it_is_unknown(client):
    """A nonexistent seller and a stranger's seller must look identical, or the
    endpoint becomes a seller-enumeration oracle."""
    _seller, _user, headers = member(SellerMemberRole.OWNER)
    variant = seed_variant()

    unknown = call(client, "POST", OFFERS, headers=headers,
                   json=create_body(uuid.uuid4(), variant))
    stranger = call(client, "POST", OFFERS, headers=headers,
                    json=create_body(make_seller(), variant))

    assert unknown.status_code == stranger.status_code == 403
    assert unknown.json()["title"] == stranger.json()["title"]
    assert unknown.json()["detail"] == stranger.json()["detail"]


@pytest.mark.parametrize("seller_status", ["pending", "suspended"])
def test_a_member_of_a_non_active_seller_creates_nothing(client, seller_status):
    _seller, _user, headers = member(SellerMemberRole.OWNER, seller_status=seller_status)
    variant = seed_variant()

    resp = call(client, "POST", OFFERS, headers=headers,
                json=create_body(_seller, variant))

    assert resp.status_code == 403, resp.text
    assert db.scalar("select count(*) from offers") == 0


def test_a_viewer_cannot_create(client):
    seller, _user, headers = member(SellerMemberRole.VIEWER)
    variant = seed_variant()

    resp = call(client, "POST", OFFERS, headers=headers, json=create_body(seller, variant))

    assert resp.status_code == 403, resp.text
    assert db.scalar("select count(*) from offers") == 0


def test_an_unexpected_field_is_still_rejected(client):
    """extra="forbid" survives: the schema names exactly what the server models."""
    seller, _user, headers = member(SellerMemberRole.OWNER)
    variant = seed_variant()

    resp = call(client, "POST", OFFERS, headers=headers,
                json={**create_body(seller, variant), "status": "active"})

    assert resp.status_code == 422, resp.text


# ===========================================================================
# existing offers — the OFFER names its owner
# ===========================================================================
OPERATIONS = [
    ("POST", "/activate", None),
    ("POST", "/suspend", None),
    ("PATCH", "/price", {"amount": 1, "currency": "IRR", "scale": 0}),
]


@pytest.mark.parametrize(("method", "suffix", "body"), OPERATIONS)
def test_a_member_of_another_seller_cannot_touch_an_offer(client, method, suffix, body):
    owner_seller = make_seller()
    _attacker_seller, _attacker, attacker_headers = member(SellerMemberRole.OWNER)
    offer = seed_offer(owner_seller, seed_variant(), status="active", amount=50_000)

    resp = call(client, method, f"{OFFERS}/{offer}{suffix}",
                headers=attacker_headers, json=body)

    assert resp.status_code == 403, (
        f"another seller reached {suffix} with {resp.status_code}: {resp.text[:200]}"
    )
    assert offer_status(offer) == "active"


@pytest.mark.parametrize(("method", "suffix", "body"), OPERATIONS)
def test_a_caller_with_no_membership_cannot_touch_an_offer(client, method, suffix, body):
    owner_seller = make_seller()
    _uid, headers = principal()
    offer = seed_offer(owner_seller, seed_variant(), status="active", amount=50_000)

    resp = call(client, method, f"{OFFERS}/{offer}{suffix}", headers=headers, json=body)

    assert resp.status_code == 403, resp.text
    assert offer_status(offer) == "active"


def test_repricing_another_sellers_offer_leaves_the_price_untouched(client):
    """403 must mean nothing happened, not merely that the response was an error."""
    owner_seller = make_seller()
    _s, _u, attacker_headers = member(SellerMemberRole.OWNER)
    offer = seed_offer(owner_seller, seed_variant(), status="active", amount=50_000)

    resp = call(client, "PATCH", f"{OFFERS}/{offer}/price", headers=attacker_headers,
                json={"amount": 1, "currency": "IRR", "scale": 0})

    assert resp.status_code == 403
    assert db.scalar(
        "select count(*) from offer_prices where offer_id = :o and amount = 1", o=offer
    ) == 0
    assert db.scalar("select count(*) from outbox_messages") == 0


def test_the_owning_sellers_member_can_still_manage_the_offer(client):
    """The ownership check must not lock out the legitimate member."""
    seller, _user, headers = member(SellerMemberRole.MANAGER)
    offer = seed_offer(seller, seed_variant(), status="draft", amount=50_000)

    activated = call(client, "POST", f"{OFFERS}/{offer}/activate", headers=headers)
    assert activated.status_code == 200, activated.text

    repriced = call(client, "PATCH", f"{OFFERS}/{offer}/price", headers=headers,
                    json={"amount": 42_000, "currency": "IRR", "scale": 0})
    assert repriced.status_code == 200, repriced.text

    suspended = call(client, "POST", f"{OFFERS}/{offer}/suspend", headers=headers)
    assert suspended.status_code == 200, suspended.text
    assert offer_status(offer) == "suspended"


@pytest.mark.parametrize(("method", "suffix", "body"), OPERATIONS)
def test_staff_of_the_owning_seller_cannot_activate_suspend_or_reprice(
    client, method, suffix, body
):
    """The in-seller boundary (approval decisions A and B). The membership is
    real and active; the ROLE is what stops it."""
    seller, _user, headers = member(SellerMemberRole.STAFF)
    offer = seed_offer(seller, seed_variant(), status="active", amount=50_000)

    resp = call(client, method, f"{OFFERS}/{offer}{suffix}", headers=headers, json=body)

    assert resp.status_code == 403, resp.text
    assert offer_status(offer) == "active"


def test_a_colleague_in_the_same_seller_can_act_on_the_offer(client):
    """Offers belong to the seller, not to the user who created them."""
    seller, _creator, creator_headers = member(SellerMemberRole.MANAGER)
    colleague = make_user()
    add_member(seller, colleague, SellerMemberRole.MANAGER)
    variant = seed_variant()

    created = call(client, "POST", OFFERS, headers=creator_headers,
                   json=create_body(seller, variant))
    assert created.status_code == 201, created.text
    offer = created.json()["id"]

    activated = call(client, "POST", f"{OFFERS}/{offer}/activate",
                     headers=auth(colleague))
    assert activated.status_code == 200, activated.text


def test_multi_membership_is_scoped_per_seller(client):
    """Staff in A, manager in B: the same caller, two different answers."""
    seller_a, user, headers = member(SellerMemberRole.STAFF)
    seller_b = make_seller()
    add_member(seller_b, user, SellerMemberRole.MANAGER)
    variant = seed_variant()
    offer_a = seed_offer(seller_a, variant, status="draft", amount=1000)
    offer_b = seed_offer(seller_b, variant, status="draft", amount=1000)

    denied = call(client, "POST", f"{OFFERS}/{offer_a}/activate", headers=headers)
    allowed = call(client, "POST", f"{OFFERS}/{offer_b}/activate", headers=headers)

    assert denied.status_code == 403, denied.text
    assert allowed.status_code == 200, allowed.text
    assert offer_status(offer_a) == "draft"


def test_revoking_a_membership_ends_authority_without_a_new_token(client):
    """Authority is resolved per request from the rows, not carried in the token.

    `forget` stands in for the cache invalidation the membership write path must
    perform; without it the caller keeps the authority until the 5-minute entry
    expires, which is the documented trade-off of ADR-0011 §10, not a bypass.
    """
    seller, user, headers = member(SellerMemberRole.MANAGER)
    offer = seed_offer(seller, seed_variant(), status="draft", amount=1000)

    assert call(client, "POST", f"{OFFERS}/{offer}/activate",
                headers=headers).status_code == 200

    db.execute("delete from seller_members where user_id = :u", u=user)
    forget(user, seller)

    after = call(client, "POST", f"{OFFERS}/{offer}/suspend", headers=headers)
    assert after.status_code == 403, after.text
    assert offer_status(offer) == "active"


def test_suspending_the_seller_ends_authority_over_its_own_offers(client):
    """A suspended seller confers nothing, even to its owner with an intact
    membership row (ADR-0011 §10.8 case I)."""
    seller, user, headers = member(SellerMemberRole.OWNER)
    offer = seed_offer(seller, seed_variant(), status="active", amount=1000)

    db.execute("update sellers set status = 'suspended' where id = :i", i=seller)
    forget(user, seller)

    resp = call(client, "POST", f"{OFFERS}/{offer}/suspend", headers=headers)

    assert resp.status_code == 403, resp.text
    assert offer_status(offer) == "active"


# ===========================================================================
# denial shape
# ===========================================================================
def test_offer_ids_are_not_enumerable_through_the_error_shape(client):
    """A missing offer answers 404 and someone else's answers 403; neither may
    disclose the owning seller."""
    owner_seller = make_seller()
    _s, _u, attacker_headers = member(SellerMemberRole.OWNER)
    real = seed_offer(owner_seller, seed_variant(), status="active", amount=1000)

    missing = call(client, "POST", f"{OFFERS}/{uuid.uuid4()}/suspend",
                   headers=attacker_headers)
    foreign = call(client, "POST", f"{OFFERS}/{real}/suspend", headers=attacker_headers)

    assert missing.status_code == 404
    assert foreign.status_code == 403
    for resp in (missing, foreign):
        assert str(owner_seller) not in resp.text


def test_anonymous_callers_are_refused_before_authorization(client):
    """401, not 403 — a missing credential is not a permission problem, and a 403
    would confirm to an anonymous prober that the named seller exists."""
    seller = make_seller()
    variant = seed_variant()
    offer = seed_offer(seller, variant, status="active", amount=1000)

    assert call(client, "POST", OFFERS,
                json=create_body(seller, variant)).status_code == 401
    assert call(client, "POST", f"{OFFERS}/{offer}/suspend").status_code == 401
    assert offer_status(offer) == "active"
