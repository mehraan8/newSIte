"""The declared access level of every API operation.

This table is the executable counterpart of the guard declarations in
src/api/v1/router.py. test_auth_matrix.py cross-checks it against the live
OpenAPI schema in BOTH directions, so:

  * an operation that exists but is not listed here fails the suite, and
  * an entry here for an operation that no longer exists fails the suite.

That two-way check is the control that stops CRITICAL 1 from recurring. Adding an
endpoint now forces an explicit, reviewed decision about who may call it — the
previous failure mode was ten endpoints whose protection lived only in a
docstring.
"""

from __future__ import annotations

# Access levels
PUBLIC = "public"                # no credentials required, by design
AUTHENTICATED = "authenticated"  # any active logged-in user, no role needed

# Seller-scoped: authentication PLUS a SellerMember grant carrying the operation's
# permission. There is no `seller` platform role and never will be — authority
# here is per-seller, so the gate cannot be expressed as "has role X" (ADR-0011
# §7). Which member roles suffice per operation is asserted in
# test_selling_authorization.py; this table asserts only that the gate exists.
SELLER_MEMBER = "seller_member"

# Platform-permission gated. The value is the PLATFORM ROLE a caller must hold to
# pass the route's guard, so the matrix can grant it and prove the gate both
# refuses and admits. Replaces the Phase 1G `PLATFORM_GUARD_PENDING` marker: the
# guards these operations were waiting for now exist, because the security audit
# confirmed any logged-in customer could reach every one of them.
PLATFORM_ROLE_REQUIRED = {
    ("POST", "/api/v1/catalog/admin/products"): "catalog_admin",
    ("PATCH", "/api/v1/catalog/admin/products/{product_id}"): "catalog_admin",
    ("POST", "/api/v1/inventory/reservations"): "inventory_manager",
    ("POST", "/api/v1/inventory/reservations/{reservation_id}/commit"): "inventory_manager",
    ("POST", "/api/v1/inventory/reservations/{reservation_id}/release"): "inventory_manager",
}
PLATFORM_GUARDED = "platform_guarded"

# Placeholders substituted into path templates. The values need not exist: these
# tests assert the AUTHORIZATION outcome, which must be decided before any lookup.
# A 404 leaking through to an anonymous caller would itself be the bug.
PATH_PARAMS = {
    "variant_id": "00000000-0000-0000-0000-0000000000v1".replace("v", "a"),
    "product_id": "00000000-0000-0000-0000-00000000p001".replace("p", "b"),
    "offer_id": "00000000-0000-0000-0000-00000000f001".replace("f", "c"),
    "seller_id": "00000000-0000-0000-0000-0000000000s1".replace("s", "a"),
    "reservation_id": "00000000-0000-0000-0000-00000000r001".replace("r", "d"),
    "order_id": "00000000-0000-0000-0000-00000000o001".replace("o", "e"),
    "slug": "some-slug",
}

# Minimal well-formed bodies, so a 422 never masks the 401/403 under test.
BODIES = {
    ("POST", "/api/v1/auth/otp/request"): {"mobile": "09121234567"},
    ("POST", "/api/v1/auth/otp/verify"): {"mobile": "09121234567", "code": "123456"},
    ("POST", "/api/v1/auth/token/refresh"): {"refresh_token": "x.y.z"},
    ("POST", "/api/v1/auth/logout"): {"refresh_token": "x.y.z"},
    ("POST", "/api/v1/catalog/admin/products"): {
        "product_type_id": 1, "category_id": 1, "slug": "s", "name_fa": "ن", "variants": [],
    },
    ("PATCH", "/api/v1/catalog/admin/products/{product_id}"): {"name_fa": "ن"},
    ("POST", "/api/v1/selling/offers"): {
        "seller_id": PATH_PARAMS["seller_id"], "variant_id": PATH_PARAMS["variant_id"],
        "condition": "new", "currency": "IRR", "amount": 1000, "scale": 0,
    },
    ("PATCH", "/api/v1/selling/offers/{offer_id}/price"): {
        "amount": 1000, "currency": "IRR", "scale": 0,
    },
    ("POST", "/api/v1/inventory/stock"): {
        "offer_id": PATH_PARAMS["offer_id"], "quantity": 1,
    },
    ("POST", "/api/v1/inventory/reservations"): {
        "reservation_id": PATH_PARAMS["reservation_id"],
        "offer_id": PATH_PARAMS["offer_id"], "quantity": 1, "ttl_seconds": 900,
    },
    ("POST", "/api/v1/orders"): {
        "lines": [{"offer_id": PATH_PARAMS["offer_id"], "quantity": 1}],
    },
}

# (method, path template) -> required access
ACCESS: dict[tuple[str, str], str] = {
    # --- system / public storefront ---
    ("GET", "/health"): PUBLIC,
    ("GET", "/api/v1/catalog/products"): PUBLIC,
    ("GET", "/api/v1/catalog/products/{slug}"): PUBLIC,
    ("GET", "/api/v1/buybox/variants/{variant_id}"): PUBLIC,

    # --- auth: these are how a caller BECOMES authenticated ---
    ("POST", "/api/v1/auth/otp/request"): PUBLIC,
    ("POST", "/api/v1/auth/otp/verify"): PUBLIC,
    ("POST", "/api/v1/auth/token/refresh"): PUBLIC,
    # Possession of the refresh token is the credential; burning a leaked token
    # must not itself require a working access token. Restored in Phase 1G.1.
    ("POST", "/api/v1/auth/logout"): PUBLIC,
    ("GET", "/api/v1/auth/me"): AUTHENTICATED,

    # --- customer ---
    ("POST", "/api/v1/orders"): AUTHENTICATED,
    ("POST", "/api/v1/orders/{order_id}/cancel"): AUTHENTICATED,
    ("GET", "/api/v1/orders/{order_id}"): AUTHENTICATED,

    # --- seller (plus per-offer ownership, see test_selling_authorization) ---
    ("POST", "/api/v1/selling/offers"): SELLER_MEMBER,
    ("POST", "/api/v1/selling/offers/{offer_id}/activate"): SELLER_MEMBER,
    ("POST", "/api/v1/selling/offers/{offer_id}/suspend"): SELLER_MEMBER,
    ("PATCH", "/api/v1/selling/offers/{offer_id}/price"): SELLER_MEMBER,

    # --- admin / back office ---
    ("POST", "/api/v1/catalog/admin/products"): PLATFORM_GUARDED,
    ("PATCH", "/api/v1/catalog/admin/products/{product_id}"): PLATFORM_GUARDED,
    # Seller-scoped: the stock belongs to the OFFER's seller. A platform
    # operator with `inventory.manage.any` may also act, which the
    # seller-scoped tests cover via the override.
    ("POST", "/api/v1/inventory/stock"): SELLER_MEMBER,
    ("POST", "/api/v1/inventory/reservations"): PLATFORM_GUARDED,
    ("POST", "/api/v1/inventory/reservations/{reservation_id}/commit"): PLATFORM_GUARDED,
    ("POST", "/api/v1/inventory/reservations/{reservation_id}/release"): PLATFORM_GUARDED,
}

# Documentation endpoints FastAPI adds for us; not part of the app's contract.
IGNORED_PATHS = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})


def concrete(path: str) -> str:
    for name, value in PATH_PARAMS.items():
        path = path.replace("{" + name + "}", value)
    return path


def body_for(method: str, path: str):
    return BODIES.get((method, path))
