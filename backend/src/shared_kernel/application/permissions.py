"""The canonical permission catalogue (ADR-0011 §5) — code-first.

This enum is the SINGLE SOURCE OF TRUTH for what permissions exist. The
`permissions` table is a synchronized projection of it, written only by generated
migrations; nothing reads the catalogue from the database at runtime.

    Permission enum (here, version-controlled)
        -> generated delta migration (deterministic literals)
        -> permissions table (projection)
        -> role_permissions (DB-owned mappings)

Never the reverse.

STRING BOUNDARY RULE. A permission *string* may exist in exactly two places:
rows in the database, and the literals inside a generated migration. Everywhere
else — policies, routers, route declarations, tests — references are `Permission`
members. Comparing raw permission strings outside those two boundaries is a
defect: `ordering.orders.cancel.own` and `.cancel.any` differ by one word, and to
every tool a string typo is invisible while a wrong enum member is not.

Naming: the member name is the UPPER_SNAKE transform of its dotted code, so the
mapping is mechanical and diffs are readable. `test_authorization_primitives`
asserts that invariant rather than trusting it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class BoundedContext(str, Enum):
    """The context that owns a permission. Closed set, so a typo in a 40-member
    catalogue cannot silently create a new 'context'."""

    IDENTITY = "identity"
    MERCHANT = "merchant"
    CATALOG = "catalog"
    SELLING = "selling"
    BUYBOX = "buybox"
    INVENTORY = "inventory"
    ORDERING = "ordering"
    NOTIFICATION = "notification"
    OPS = "ops"


class PermissionScope(str, Enum):
    """Where a permission can be granted.

    PLATFORM         - grantable only through a platform role (`user_roles`).
    SELLER_SCOPABLE  - grantable within a Seller, through a SellerMember role.

    ADR-0011 §6: `seller` is NOT a platform role. Selling authority is a
    membership, so the permissions that carry it must be marked SELLER_SCOPABLE
    and are answered by `Principal.can_for_seller`, never by `Principal.can`.
    """

    PLATFORM = "platform"
    SELLER_SCOPABLE = "seller_scopable"


@dataclass(frozen=True)
class PermissionSpec:
    """The metadata a permission carries. Frozen and hashable so it can be an
    Enum value; codes are unique, so no two members alias each other."""

    code: str
    description: str
    context: BoundedContext
    scope: PermissionScope


class Permission(Enum):
    """Every permission in the system. Add members here, then regenerate the
    seed migration — never the other way round."""

    # --- Identity -----------------------------------------------------------
    IDENTITY_SESSION_CREATE = PermissionSpec(
        "identity.session.create", "Request and verify an OTP login challenge",
        BoundedContext.IDENTITY, PermissionScope.PLATFORM)
    IDENTITY_SESSION_REFRESH = PermissionSpec(
        "identity.session.refresh", "Exchange a refresh token for a new access token",
        BoundedContext.IDENTITY, PermissionScope.PLATFORM)
    IDENTITY_PROFILE_READ_OWN = PermissionSpec(
        "identity.profile.read.own", "Read one's own profile",
        BoundedContext.IDENTITY, PermissionScope.PLATFORM)
    IDENTITY_USERS_READ_ANY = PermissionSpec(
        "identity.users.read.any", "Read any user's profile",
        BoundedContext.IDENTITY, PermissionScope.PLATFORM)
    IDENTITY_USERS_BLOCK = PermissionSpec(
        "identity.users.block", "Block or unblock a user account",
        BoundedContext.IDENTITY, PermissionScope.PLATFORM)
    IDENTITY_ROLES_GRANT = PermissionSpec(
        "identity.roles.grant", "Grant a platform role to a user",
        BoundedContext.IDENTITY, PermissionScope.PLATFORM)
    IDENTITY_ROLES_REVOKE = PermissionSpec(
        "identity.roles.revoke", "Revoke a platform role from a user",
        BoundedContext.IDENTITY, PermissionScope.PLATFORM)

    # --- Merchant -----------------------------------------------------------
    MERCHANT_SELLERS_CREATE = PermissionSpec(
        "merchant.sellers.create", "Create a seller account",
        BoundedContext.MERCHANT, PermissionScope.PLATFORM)
    MERCHANT_SELLERS_READ_OWN = PermissionSpec(
        "merchant.sellers.read.own", "Read a seller one is a member of",
        BoundedContext.MERCHANT, PermissionScope.SELLER_SCOPABLE)
    MERCHANT_SELLERS_READ_ANY = PermissionSpec(
        "merchant.sellers.read.any", "Read any seller account",
        BoundedContext.MERCHANT, PermissionScope.PLATFORM)
    MERCHANT_SELLERS_UPDATE = PermissionSpec(
        "merchant.sellers.update", "Update a seller's own details",
        BoundedContext.MERCHANT, PermissionScope.SELLER_SCOPABLE)
    MERCHANT_SELLERS_SUSPEND = PermissionSpec(
        "merchant.sellers.suspend", "Suspend or reinstate a seller (trust and safety)",
        BoundedContext.MERCHANT, PermissionScope.PLATFORM)
    MERCHANT_SELLERS_MANAGE_ANY = PermissionSpec(
        "merchant.sellers.manage.any", "Administer any seller account",
        BoundedContext.MERCHANT, PermissionScope.PLATFORM)
    MERCHANT_MEMBERS_INVITE = PermissionSpec(
        "merchant.members.invite", "Add a member to a seller",
        BoundedContext.MERCHANT, PermissionScope.SELLER_SCOPABLE)
    MERCHANT_MEMBERS_REMOVE = PermissionSpec(
        "merchant.members.remove", "Remove a member from a seller",
        BoundedContext.MERCHANT, PermissionScope.SELLER_SCOPABLE)
    MERCHANT_MEMBERS_READ = PermissionSpec(
        "merchant.members.read", "List a seller's members",
        BoundedContext.MERCHANT, PermissionScope.SELLER_SCOPABLE)

    # --- Catalog ------------------------------------------------------------
    CATALOG_PRODUCTS_READ = PermissionSpec(
        "catalog.products.read", "Read the public product catalogue",
        BoundedContext.CATALOG, PermissionScope.PLATFORM)
    CATALOG_PRODUCTS_CREATE = PermissionSpec(
        "catalog.products.create", "Create a product",
        BoundedContext.CATALOG, PermissionScope.PLATFORM)
    CATALOG_PRODUCTS_UPDATE = PermissionSpec(
        "catalog.products.update", "Edit a product's attributes",
        BoundedContext.CATALOG, PermissionScope.PLATFORM)
    CATALOG_PRODUCTS_PUBLISH = PermissionSpec(
        "catalog.products.publish", "Publish a product to the storefront",
        BoundedContext.CATALOG, PermissionScope.PLATFORM)
    CATALOG_PRODUCTS_ARCHIVE = PermissionSpec(
        "catalog.products.archive", "Archive a product",
        BoundedContext.CATALOG, PermissionScope.PLATFORM)
    CATALOG_TAXONOMY_MANAGE = PermissionSpec(
        "catalog.taxonomy.manage", "Manage product types, categories and brands",
        BoundedContext.CATALOG, PermissionScope.PLATFORM)

    # --- Selling ------------------------------------------------------------
    SELLING_OFFERS_CREATE = PermissionSpec(
        "selling.offers.create", "Create an offer for one's seller",
        BoundedContext.SELLING, PermissionScope.SELLER_SCOPABLE)
    SELLING_OFFERS_READ_OWN = PermissionSpec(
        "selling.offers.read.own", "Read one's seller's offers",
        BoundedContext.SELLING, PermissionScope.SELLER_SCOPABLE)
    SELLING_OFFERS_READ_ANY = PermissionSpec(
        "selling.offers.read.any", "Read any seller's offers",
        BoundedContext.SELLING, PermissionScope.PLATFORM)
    SELLING_OFFERS_ACTIVATE = PermissionSpec(
        "selling.offers.activate", "Activate one's seller's offer",
        BoundedContext.SELLING, PermissionScope.SELLER_SCOPABLE)
    SELLING_OFFERS_SUSPEND = PermissionSpec(
        "selling.offers.suspend", "Suspend one's seller's offer",
        BoundedContext.SELLING, PermissionScope.SELLER_SCOPABLE)
    SELLING_OFFERS_PRICE_CHANGE = PermissionSpec(
        "selling.offers.price.change", "Change the price of one's seller's offer",
        BoundedContext.SELLING, PermissionScope.SELLER_SCOPABLE)
    SELLING_OFFERS_MANAGE_ANY = PermissionSpec(
        "selling.offers.manage.any", "Administer any seller's offers (moderation)",
        BoundedContext.SELLING, PermissionScope.PLATFORM)

    # --- BuyBox -------------------------------------------------------------
    # `buybox.variants.read`, not `buybox.read`: the catalogue in earlier ADR
    # revisions wrote the latter, which is two-part and breaks the stated
    # <context>.<resource>.<action> convention. The resource is the variant
    # buy-box state (GET /buybox/variants/{variant_id}); the projection is the
    # internal artefact that `buybox.projection.rebuild` acts on.
    BUYBOX_VARIANTS_READ = PermissionSpec(
        "buybox.variants.read", "Read the winning offer for a variant",
        BoundedContext.BUYBOX, PermissionScope.PLATFORM)
    BUYBOX_PROJECTION_REBUILD = PermissionSpec(
        "buybox.projection.rebuild", "Rebuild the buy-box projection from events",
        BoundedContext.BUYBOX, PermissionScope.PLATFORM)

    # --- Inventory ----------------------------------------------------------
    INVENTORY_STOCK_RECEIVE = PermissionSpec(
        "inventory.stock.receive", "Receive stock against an offer",
        BoundedContext.INVENTORY, PermissionScope.SELLER_SCOPABLE)
    INVENTORY_STOCK_READ_OWN = PermissionSpec(
        "inventory.stock.read.own", "Read one's seller's stock levels",
        BoundedContext.INVENTORY, PermissionScope.SELLER_SCOPABLE)
    INVENTORY_STOCK_READ_ANY = PermissionSpec(
        "inventory.stock.read.any", "Read any seller's stock levels",
        BoundedContext.INVENTORY, PermissionScope.PLATFORM)
    INVENTORY_RESERVATIONS_CREATE = PermissionSpec(
        "inventory.reservations.create", "Create a stock reservation directly",
        BoundedContext.INVENTORY, PermissionScope.PLATFORM)
    INVENTORY_RESERVATIONS_COMMIT = PermissionSpec(
        "inventory.reservations.commit", "Commit a stock reservation directly",
        BoundedContext.INVENTORY, PermissionScope.PLATFORM)
    INVENTORY_RESERVATIONS_RELEASE = PermissionSpec(
        "inventory.reservations.release", "Release a stock reservation directly",
        BoundedContext.INVENTORY, PermissionScope.PLATFORM)
    INVENTORY_RESERVATIONS_READ = PermissionSpec(
        "inventory.reservations.read", "Read stock reservations",
        BoundedContext.INVENTORY, PermissionScope.SELLER_SCOPABLE)
    INVENTORY_MANAGE_ANY = PermissionSpec(
        "inventory.manage.any", "Administer stock across all sellers",
        BoundedContext.INVENTORY, PermissionScope.PLATFORM)

    # --- Ordering -----------------------------------------------------------
    ORDERING_ORDERS_CREATE = PermissionSpec(
        "ordering.orders.create", "Place an order",
        BoundedContext.ORDERING, PermissionScope.PLATFORM)
    ORDERING_ORDERS_READ_OWN = PermissionSpec(
        "ordering.orders.read.own", "Read one's own order",
        BoundedContext.ORDERING, PermissionScope.PLATFORM)
    ORDERING_ORDERS_READ_ANY = PermissionSpec(
        "ordering.orders.read.any", "Read any customer's order",
        BoundedContext.ORDERING, PermissionScope.PLATFORM)
    ORDERING_ORDERS_CANCEL_OWN = PermissionSpec(
        "ordering.orders.cancel.own", "Cancel one's own order",
        BoundedContext.ORDERING, PermissionScope.PLATFORM)
    ORDERING_ORDERS_CANCEL_ANY = PermissionSpec(
        "ordering.orders.cancel.any", "Cancel any customer's order (support)",
        BoundedContext.ORDERING, PermissionScope.PLATFORM)

    # --- Notification -------------------------------------------------------
    NOTIFICATION_MESSAGES_READ_ANY = PermissionSpec(
        "notification.messages.read.any", "Read the notification log",
        BoundedContext.NOTIFICATION, PermissionScope.PLATFORM)

    # --- Operations ---------------------------------------------------------
    OPS_OUTBOX_READ = PermissionSpec(
        "ops.outbox.read", "Inspect outbox depth and lag",
        BoundedContext.OPS, PermissionScope.PLATFORM)
    OPS_DLQ_READ = PermissionSpec(
        "ops.dlq.read", "Inspect dead-lettered messages",
        BoundedContext.OPS, PermissionScope.PLATFORM)
    OPS_DLQ_REPLAY = PermissionSpec(
        "ops.dlq.replay", "Replay dead-lettered messages (re-emits real events)",
        BoundedContext.OPS, PermissionScope.PLATFORM)
    OPS_RECONCILIATION_RUN = PermissionSpec(
        "ops.reconciliation.run", "Run the inventory reconciliation job",
        BoundedContext.OPS, PermissionScope.PLATFORM)
    OPS_AUDIT_READ = PermissionSpec(
        "ops.audit.read", "Read the audit log",
        BoundedContext.OPS, PermissionScope.PLATFORM)

    # -- accessors ----------------------------------------------------------
    @property
    def code(self) -> str:
        """The dotted wire/database representation."""
        return self.value.code

    @property
    def description(self) -> str:
        return self.value.description

    @property
    def context(self) -> BoundedContext:
        return self.value.context

    @property
    def scope(self) -> PermissionScope:
        return self.value.scope

    @property
    def is_seller_scopable(self) -> bool:
        return self.value.scope is PermissionScope.SELLER_SCOPABLE

    def __str__(self) -> str:
        return self.value.code


# Built once at import. Not a cache to invalidate — the catalogue is code.
_BY_CODE: dict[str, Permission] = {p.code: p for p in Permission}


def from_code(code: str) -> Permission | None:
    """Parse a database permission code into a member, or None if unknown.

    Returns None rather than raising because this is the DB boundary and drift is
    expected: a deployment running older code than its database will see codes it
    does not know. ADR-0011 §5 requires those to FAIL CLOSED — the caller drops
    and logs them, so an unrecognised code can never confer authority.
    """
    return _BY_CODE.get(code)


def all_codes() -> frozenset[str]:
    """Every code in the catalogue. Used by the drift check and the seeder."""
    return frozenset(_BY_CODE)
