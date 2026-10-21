"""Object-level authorization for Inventory (ADR-0011 §9).

Pure and synchronous, exactly like Selling's policy: every fact is already in the
`Principal` the composition root assembled, so a policy cannot introduce I/O into
a request path.

Stock is anchored on `offer_id` and carries no seller column of its own yet
(ADR-0011 §9 records `inventory_stock.seller_id` as TARGET STATE), so the owning
seller is resolved through the Selling ACL and passed in. The *decision* is made
here from the Principal; the *lookup* stays in the use case where the I/O belongs.

Two ways to be allowed, and they are different kinds of authority:

  * a SELLER MEMBER holding `inventory.stock.receive` for the owning seller —
    a merchant restocking their own goods;
  * a PLATFORM operator holding `inventory.manage.any` — back-office correction
    across all sellers.

The platform override is written out at the call site rather than hidden inside
`can_for_seller`, so every widening of authority is visible where it is granted
(ADR-0011 §4).
"""

from __future__ import annotations

import uuid

from src.shared_kernel.application.errors import ForbiddenError
from src.shared_kernel.application.permissions import Permission
from src.shared_kernel.application.security import Principal


def ensure_can_receive_stock(principal: Principal, seller_id: uuid.UUID) -> None:
    """Raise unless the principal may add stock to that seller's offer.

    Before this existed, `POST /inventory/stock` required only a valid token: any
    logged-in customer could inflate or create stock against any seller's offer,
    which feeds straight into BuyBox availability and lets an attacker sell goods
    that do not exist.

    One denial for every failing case, so the response never reveals whether the
    seller exists, is suspended, or simply is not yours.
    """
    if principal.can(Permission.INVENTORY_MANAGE_ANY):
        return
    if principal.can_for_seller(Permission.INVENTORY_STOCK_RECEIVE, seller_id):
        return
    raise ForbiddenError("شما مجاز به تغییر موجودی این فروشنده نیستید.")
