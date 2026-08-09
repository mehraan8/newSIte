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

from src.modules.identity.domain.roles import Role

# Access levels
PUBLIC = "public"                # no credentials required, by design
AUTHENTICATED = "authenticated"  # any active logged-in user, no role needed

# Placeholders substituted into path templates. The values need not exist: these
# tests assert the AUTHORIZATION outcome, which must be decided before any lookup.
# A 404 leaking through to an anonymous caller would itself be the bug.
PATH_PARAMS = {
    "variant_id": "00000000-0000-0000-0000-0000000000v1".replace("v", "a"),
    "product_id": "00000000-0000-0000-0000-00000000p001".replace("p", "b"),
    "offer_id": "00000000-0000-0000-0000-00000000f001".replace("f", "c"),
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
        "variant_id": PATH_PARAMS["variant_id"], "condition": "new",
        "currency": "IRR", "amount": 1000, "scale": 0,
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
    # must not itself require a working session.
    ("POST", "/api/v1/auth/logout"): PUBLIC,
    ("GET", "/api/v1/auth/me"): AUTHENTICATED,

    # --- customer ---
    ("POST", "/api/v1/orders"): AUTHENTICATED,
    ("POST", "/api/v1/orders/{order_id}/cancel"): AUTHENTICATED,
    ("GET", "/api/v1/orders/{order_id}"): AUTHENTICATED,

    # --- seller (plus per-offer ownership, see test_selling_authorization) ---
    ("POST", "/api/v1/selling/offers"): Role.SELLER,
    ("POST", "/api/v1/selling/offers/{offer_id}/activate"): Role.SELLER,
    ("POST", "/api/v1/selling/offers/{offer_id}/suspend"): Role.SELLER,
    ("PATCH", "/api/v1/selling/offers/{offer_id}/price"): Role.SELLER,

    # --- admin / back office ---
    ("POST", "/api/v1/catalog/admin/products"): Role.ADMIN,
    ("PATCH", "/api/v1/catalog/admin/products/{product_id}"): Role.ADMIN,
    ("POST", "/api/v1/inventory/stock"): Role.ADMIN,
    ("POST", "/api/v1/inventory/reservations"): Role.ADMIN,
    ("POST", "/api/v1/inventory/reservations/{reservation_id}/commit"): Role.ADMIN,
    ("POST", "/api/v1/inventory/reservations/{reservation_id}/release"): Role.ADMIN,
}

# Documentation endpoints FastAPI adds for us; not part of the app's contract.
IGNORED_PATHS = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})


def concrete(path: str) -> str:
    for name, value in PATH_PARAMS.items():
        path = path.replace("{" + name + "}", value)
    return path


def body_for(method: str, path: str):
    return BODIES.get((method, path))
