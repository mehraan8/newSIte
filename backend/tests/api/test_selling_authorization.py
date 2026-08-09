"""Object-level authorization for Selling — regression lock for CRITICAL 2.

Two separate holes are covered here, and passing one does not imply the other:

  * identity binding — `seller_id` used to be a request field, so any caller
    could list stock under any seller's identity;
  * ownership — activate/suspend/reprice looked the offer up by id and never
    checked who owned it, so one seller could suspend a competitor's offer or
    reprice it to 1.

The role guard alone fixes neither: both attackers here hold a valid `seller` role.
"""

from __future__ import annotations

import uuid

import pytest

from tests.api.conftest import Role, call, principal, seed_offer, seed_variant
from tests.integration import db

pytestmark = pytest.mark.integration

OFFERS = "/api/v1/selling/offers"


def test_created_offer_belongs_to_the_authenticated_seller(client):
    seller, headers = principal(Role.SELLER)
    variant = seed_variant()

    resp = call(client, "POST", OFFERS, headers=headers, json={
        "variant_id": str(variant), "condition": "new",
        "currency": "IRR", "amount": 5000, "scale": 0,
    })

    assert resp.status_code == 201, resp.text
    assert resp.json()["seller_id"] == str(seller)
    stored = db.scalar(
        "select seller_id from offers where id = :i", i=uuid.UUID(resp.json()["id"])
    )
    assert stored == seller


def test_seller_id_in_the_body_is_rejected_not_ignored(client):
    """A client still sending seller_id must get a 422.

    Silently ignoring it would hide an attempted impersonation; rejecting makes
    the attempt visible in logs and to the caller.
    """
    _seller, headers = principal(Role.SELLER)
    variant = seed_variant()
    victim = uuid.uuid4()

    resp = call(client, "POST", OFFERS, headers=headers, json={
        "seller_id": str(victim), "variant_id": str(variant),
        "condition": "new", "currency": "IRR", "amount": 5000, "scale": 0,
    })

    assert resp.status_code == 422, resp.text
    assert db.scalar("select count(*) from offers where seller_id = :s", s=victim) == 0


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("POST", "/activate", None),
        ("POST", "/suspend", None),
        ("PATCH", "/price", {"amount": 1, "currency": "IRR", "scale": 0}),
    ],
)
def test_a_seller_cannot_touch_another_sellers_offer(client, method, suffix, body):
    owner, _owner_headers = principal(Role.SELLER)
    _attacker, attacker_headers = principal(Role.SELLER)
    variant = seed_variant()
    offer = seed_offer(owner, variant)

    resp = call(
        client, method, f"{OFFERS}/{offer}{suffix}", headers=attacker_headers, json=body
    )

    assert resp.status_code == 403, (
        f"another seller reached {suffix} with {resp.status_code}: {resp.text[:200]}"
    )


def test_repricing_another_sellers_offer_leaves_the_price_untouched(client):
    """403 must mean nothing happened, not merely that the response was an error."""
    owner, _ = principal(Role.SELLER)
    _attacker, attacker_headers = principal(Role.SELLER)
    variant = seed_variant()
    offer = seed_offer(owner, variant)
    db.execute(
        "insert into offer_prices (offer_id, amount, currency, scale) "
        "values (:o, 50000, 'IRR', 0)",
        o=offer,
    )

    resp = call(client, "PATCH", f"{OFFERS}/{offer}/price", headers=attacker_headers,
                json={"amount": 1, "currency": "IRR", "scale": 0})

    assert resp.status_code == 403
    assert db.scalar(
        "select count(*) from offer_prices where offer_id = :o and amount = 1", o=offer
    ) == 0


def test_the_owner_can_still_manage_their_own_offer(client):
    """The ownership check must not lock out the legitimate owner."""
    owner, headers = principal(Role.SELLER)
    variant = seed_variant()
    offer = seed_offer(owner, variant, status="draft")
    db.execute(
        "insert into offer_prices (offer_id, amount, currency, scale) "
        "values (:o, 50000, 'IRR', 0)",
        o=offer,
    )

    activated = call(client, "POST", f"{OFFERS}/{offer}/activate", headers=headers)
    assert activated.status_code == 200, activated.text

    repriced = call(client, "PATCH", f"{OFFERS}/{offer}/price", headers=headers,
                    json={"amount": 42000, "currency": "IRR", "scale": 0})
    assert repriced.status_code == 200, repriced.text

    suspended = call(client, "POST", f"{OFFERS}/{offer}/suspend", headers=headers)
    assert suspended.status_code == 200, suspended.text


def test_offer_ids_are_not_enumerable_through_the_error_shape(client):
    """A non-existent offer and someone else's offer must be distinguishable only
    to the owner — 404 vs 403 both deny, and neither leaks the price or seller."""
    owner, _ = principal(Role.SELLER)
    _attacker, attacker_headers = principal(Role.SELLER)
    variant = seed_variant()
    real = seed_offer(owner, variant)

    missing = call(client, "POST", f"{OFFERS}/{uuid.uuid4()}/suspend",
                   headers=attacker_headers)
    foreign = call(client, "POST", f"{OFFERS}/{real}/suspend", headers=attacker_headers)

    assert missing.status_code == 404
    assert foreign.status_code == 403
    for resp in (missing, foreign):
        assert str(owner) not in resp.text
