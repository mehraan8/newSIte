"""Seller-scoped authorization for Selling (ADR-0011 §9, Phase 1G).

Pure and synchronous: no database, no Redis, no repository, no session. Every
fact needed to decide is already in the `Principal` that Phase 1F assembled, so
a policy cannot introduce I/O into a request path or make a decision the
composition root did not already have the data for.

The rule is the same for every operation, and deliberately has no special cases:

    target seller  ->  Principal.memberships[seller]  ->  SellerGrant.allows(p)

`SellerGrant.allows()` is what enforces the seller's own status, so Selling does
not re-implement a second seller-status policy that could drift from Merchant's.

Where the target seller comes from is the security-critical part:

  * existing offer  -> `offer.seller_id`, the authoritative resource owner. Never
                       the caller. Knowing an offer's UUID confers nothing.
  * creation        -> the caller nominates a target seller, and the nomination
                       is only honoured if it appears in their memberships. The
                       client names a resource; the server proves the authority.

Platform `.any` permissions are NOT consulted here. `selling.offers.manage.any`
is platform authority for moderation and belongs to a separate authorization
universe (ADR-0011 §9); wiring it into seller-member operations would let a
platform role act as a seller member, which the architecture forbids.
"""

from __future__ import annotations

import uuid

from src.shared_kernel.application.errors import ForbiddenError
from src.shared_kernel.application.permissions import Permission
from src.shared_kernel.application.security import Principal


def ensure_can_act_for_seller(
    principal: Principal, seller_id: uuid.UUID, permission: Permission
) -> None:
    """Raise unless the principal may exercise `permission` for that seller.

    One denial for every failing case — no membership, inactive membership,
    non-active seller, or a role that does not carry the permission — so the
    response never discloses which of those was true. A caller learns only that
    they may not do it.
    """
    grant = principal.memberships.get(seller_id)
    if grant is None or not grant.allows(permission):
        raise ForbiddenError("شما مجاز به انجام این عملیات برای این فروشنده نیستید.")
