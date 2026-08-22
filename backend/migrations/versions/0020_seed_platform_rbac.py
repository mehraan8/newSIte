"""identity: seed the platform RBAC baseline (roles, permissions, grants)

Phase 1E. Adds NO schema — migration 0001 already created `roles`,
`permissions`, `role_permissions` and `user_roles` with their composite primary
keys. This migration only populates the authoritative platform vocabulary.

The literals below are GENERATED, never hand-typed:

    python -m src.modules.identity.infrastructure.rbac_drift --emit

They are frozen here on purpose. A migration must keep reproducing the history
it recorded even after the code moves on, so it must never import the live
Permission/PlatformRole vocabulary (ADR-0011 SS12). tests/test_rbac_seed_sync.py
compares these literals against the code baseline and fails the build if the
vocabulary changes without a regenerated migration.

Safety:
  * Idempotent - every write is ON CONFLICT DO NOTHING / DO UPDATE, so applying
    it to an empty database and to an already-correct one converge to the same
    state.
  * Joined on stable CODES, never surrogate ids, which differ per environment.
  * Seeds ONLY platform roles, permissions and their grants. No users, no
    sellers, no seller members, no test data.
  * `is_system` marks these rows as not-deletable by a future admin UI.

Downgrade removes exactly what it seeded, by code, leaving operator-created rows
untouched. `user_roles` assignments cascade from `roles`, so downgrade discards
role assignments held by real users - a development-only operation.

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# --- GENERATED BASELINE (do not edit by hand) ------------------------------
# Baseline fingerprint: 50d1af270bf518da20fcbb9562d3e0b9868574823fa11cba5668372aaacaf770
ROLES = (
    ('catalog_admin', 'مدیر کاتالوگ', True),
    ('customer', 'مشتری', True),
    ('inventory_manager', 'مدیر موجودی', True),
    ('readonly_operator', 'ناظر فقط\u200cخواندنی', True),
    ('super_admin', 'مدیر ارشد', True),
    ('support_agent', 'کارشناس پشتیبانی', True),
)

PERMISSIONS = (
    ('buybox.projection.rebuild', 'Rebuild the buy-box projection from events'),
    ('buybox.variants.read', 'Read the winning offer for a variant'),
    ('catalog.products.archive', 'Archive a product'),
    ('catalog.products.create', 'Create a product'),
    ('catalog.products.publish', 'Publish a product to the storefront'),
    ('catalog.products.read', 'Read the public product catalogue'),
    ('catalog.products.update', "Edit a product's attributes"),
    ('catalog.taxonomy.manage', 'Manage product types, categories and brands'),
    ('identity.profile.read.own', "Read one's own profile"),
    ('identity.roles.grant', 'Grant a platform role to a user'),
    ('identity.roles.revoke', 'Revoke a platform role from a user'),
    ('identity.session.create', 'Request and verify an OTP login challenge'),
    ('identity.session.refresh', 'Exchange a refresh token for a new access token'),
    ('identity.users.block', 'Block or unblock a user account'),
    ('identity.users.read.any', "Read any user's profile"),
    ('inventory.manage.any', 'Administer stock across all sellers'),
    ('inventory.reservations.commit', 'Commit a stock reservation directly'),
    ('inventory.reservations.create', 'Create a stock reservation directly'),
    ('inventory.reservations.read', 'Read stock reservations'),
    ('inventory.reservations.release', 'Release a stock reservation directly'),
    ('inventory.stock.read.any', "Read any seller's stock levels"),
    ('inventory.stock.read.own', "Read one's seller's stock levels"),
    ('inventory.stock.receive', 'Receive stock against an offer'),
    ('merchant.members.invite', 'Add a member to a seller'),
    ('merchant.members.read', "List a seller's members"),
    ('merchant.members.remove', 'Remove a member from a seller'),
    ('merchant.sellers.create', 'Create a seller account'),
    ('merchant.sellers.manage.any', 'Administer any seller account'),
    ('merchant.sellers.read.any', 'Read any seller account'),
    ('merchant.sellers.read.own', 'Read a seller one is a member of'),
    ('merchant.sellers.suspend', 'Suspend or reinstate a seller (trust and safety)'),
    ('merchant.sellers.update', "Update a seller's own details"),
    ('notification.messages.read.any', 'Read the notification log'),
    ('ops.audit.read', 'Read the audit log'),
    ('ops.dlq.read', 'Inspect dead-lettered messages'),
    ('ops.dlq.replay', 'Replay dead-lettered messages (re-emits real events)'),
    ('ops.outbox.read', 'Inspect outbox depth and lag'),
    ('ops.reconciliation.run', 'Run the inventory reconciliation job'),
    ('ordering.orders.cancel.any', "Cancel any customer's order (support)"),
    ('ordering.orders.cancel.own', "Cancel one's own order"),
    ('ordering.orders.create', 'Place an order'),
    ('ordering.orders.read.any', "Read any customer's order"),
    ('ordering.orders.read.own', "Read one's own order"),
    ('selling.offers.activate', "Activate one's seller's offer"),
    ('selling.offers.create', "Create an offer for one's seller"),
    ('selling.offers.manage.any', "Administer any seller's offers (moderation)"),
    ('selling.offers.price.change', "Change the price of one's seller's offer"),
    ('selling.offers.read.any', "Read any seller's offers"),
    ('selling.offers.read.own', "Read one's seller's offers"),
    ('selling.offers.suspend', "Suspend one's seller's offer"),
)

GRANTS = (
    ('catalog_admin', 'catalog.products.archive'),
    ('catalog_admin', 'catalog.products.create'),
    ('catalog_admin', 'catalog.products.publish'),
    ('catalog_admin', 'catalog.products.update'),
    ('catalog_admin', 'catalog.taxonomy.manage'),
    ('customer', 'ordering.orders.cancel.own'),
    ('customer', 'ordering.orders.create'),
    ('customer', 'ordering.orders.read.own'),
    ('inventory_manager', 'inventory.manage.any'),
    ('inventory_manager', 'inventory.reservations.commit'),
    ('inventory_manager', 'inventory.reservations.create'),
    ('inventory_manager', 'inventory.reservations.release'),
    ('inventory_manager', 'inventory.stock.read.any'),
    ('readonly_operator', 'inventory.stock.read.any'),
    ('readonly_operator', 'merchant.sellers.read.any'),
    ('readonly_operator', 'notification.messages.read.any'),
    ('readonly_operator', 'ops.audit.read'),
    ('readonly_operator', 'ops.dlq.read'),
    ('readonly_operator', 'ops.outbox.read'),
    ('readonly_operator', 'ordering.orders.read.any'),
    ('readonly_operator', 'selling.offers.read.any'),
    ('super_admin', 'buybox.projection.rebuild'),
    ('super_admin', 'buybox.variants.read'),
    ('super_admin', 'catalog.products.archive'),
    ('super_admin', 'catalog.products.create'),
    ('super_admin', 'catalog.products.publish'),
    ('super_admin', 'catalog.products.read'),
    ('super_admin', 'catalog.products.update'),
    ('super_admin', 'catalog.taxonomy.manage'),
    ('super_admin', 'identity.profile.read.own'),
    ('super_admin', 'identity.roles.grant'),
    ('super_admin', 'identity.roles.revoke'),
    ('super_admin', 'identity.session.create'),
    ('super_admin', 'identity.session.refresh'),
    ('super_admin', 'identity.users.block'),
    ('super_admin', 'identity.users.read.any'),
    ('super_admin', 'inventory.manage.any'),
    ('super_admin', 'inventory.reservations.commit'),
    ('super_admin', 'inventory.reservations.create'),
    ('super_admin', 'inventory.reservations.release'),
    ('super_admin', 'inventory.stock.read.any'),
    ('super_admin', 'merchant.sellers.create'),
    ('super_admin', 'merchant.sellers.manage.any'),
    ('super_admin', 'merchant.sellers.read.any'),
    ('super_admin', 'merchant.sellers.suspend'),
    ('super_admin', 'notification.messages.read.any'),
    ('super_admin', 'ops.audit.read'),
    ('super_admin', 'ops.dlq.read'),
    ('super_admin', 'ops.dlq.replay'),
    ('super_admin', 'ops.outbox.read'),
    ('super_admin', 'ops.reconciliation.run'),
    ('super_admin', 'ordering.orders.cancel.any'),
    ('super_admin', 'ordering.orders.cancel.own'),
    ('super_admin', 'ordering.orders.create'),
    ('super_admin', 'ordering.orders.read.any'),
    ('super_admin', 'ordering.orders.read.own'),
    ('super_admin', 'selling.offers.manage.any'),
    ('super_admin', 'selling.offers.read.any'),
    ('support_agent', 'ordering.orders.cancel.any'),
    ('support_agent', 'ordering.orders.read.any'),
)
# --- end generated ---------------------------------------------------------


def upgrade() -> None:
    bind = op.get_bind()

    # Roles: refresh the display name so a rename converges; nothing else about
    # an existing row is touched.
    for code, name_fa, is_system in ROLES:
        bind.execute(
            sa.text(
                "INSERT INTO roles (code, name_fa, is_system) "
                "VALUES (:code, :name_fa, :is_system) "
                "ON CONFLICT (code) DO UPDATE SET name_fa = EXCLUDED.name_fa"
            ),
            {"code": code, "name_fa": name_fa, "is_system": is_system},
        )

    # Permissions: the projection of the code-first catalogue.
    for code, description in PERMISSIONS:
        bind.execute(
            sa.text(
                "INSERT INTO permissions (code, description) "
                "VALUES (:code, :description) "
                "ON CONFLICT (code) DO UPDATE SET description = EXCLUDED.description"
            ),
            {"code": code, "description": description},
        )

    # Grants: resolved through codes at write time, so no surrogate id ever
    # appears in this migration.
    for role_code, permission_code in GRANTS:
        bind.execute(
            sa.text(
                "INSERT INTO role_permissions (role_id, permission_id) "
                "SELECT r.id, p.id FROM roles r, permissions p "
                "WHERE r.code = :role_code AND p.code = :permission_code "
                "ON CONFLICT DO NOTHING"
            ),
            {"role_code": role_code, "permission_code": permission_code},
        )


def downgrade() -> None:
    bind = op.get_bind()

    for role_code, permission_code in GRANTS:
        bind.execute(
            sa.text(
                "DELETE FROM role_permissions rp "
                "USING roles r, permissions p "
                "WHERE rp.role_id = r.id AND rp.permission_id = p.id "
                "AND r.code = :role_code AND p.code = :permission_code"
            ),
            {"role_code": role_code, "permission_code": permission_code},
        )

    for code, _description in PERMISSIONS:
        bind.execute(
            sa.text("DELETE FROM permissions WHERE code = :code"), {"code": code}
        )

    for code, _name_fa, _is_system in ROLES:
        bind.execute(
            sa.text("DELETE FROM roles WHERE code = :code AND is_system"), {"code": code}
        )
