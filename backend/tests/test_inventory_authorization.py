"""Inventory object-level authorization (DB-free) — audit regression lock.

`POST /inventory/stock` required only a valid token. Any logged-in customer
could add stock to any seller's offer, which flows into BuyBox availability and
lets an attacker make a shop appear to stock goods it does not have — every order
against that phantom stock then fails at fulfilment.

Stock has no seller column of its own (ADR-0011 §9 records it as TARGET STATE),
so ownership comes from the OFFER via the Selling ACL. These tests pin both
halves: that the owner is read from the offer, and that the caller's authority is
proved from the Principal.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from src.modules.inventory.application.policies import ensure_can_receive_stock
from src.modules.merchant.application.member_permissions import MEMBER_ROLE_PERMISSIONS
from src.modules.merchant.domain.entities import SellerMemberRole
from src.shared_kernel.application.errors import ForbiddenError
from src.shared_kernel.application.permissions import Permission
from src.shared_kernel.application.security import Principal, SellerGrant, SellerStatus


def member(seller_id, role=SellerMemberRole.OWNER, status=SellerStatus.ACTIVE) -> Principal:
    """Built from the APPROVED matrix, not a hand-picked permission set, so the
    suite cannot keep passing after the matrix changes."""
    return Principal(
        subject_id=uuid.uuid4(),
        memberships={
            seller_id: SellerGrant(
                seller_id=seller_id,
                permissions=MEMBER_ROLE_PERMISSIONS[role],
                status=status,
            )
        },
    )


def operator(*permissions: Permission) -> Principal:
    return Principal(subject_id=uuid.uuid4(), platform_permissions=frozenset(permissions))


def stranger() -> Principal:
    return Principal(subject_id=uuid.uuid4())


# --- who may add stock ------------------------------------------------------
@pytest.mark.parametrize(
    "role", [SellerMemberRole.OWNER, SellerMemberRole.MANAGER, SellerMemberRole.STAFF]
)
def test_a_member_holding_stock_receive_is_allowed(role):
    seller = uuid.uuid4()
    ensure_can_receive_stock(member(seller, role), seller)


def test_a_viewer_is_denied():
    """Read-only membership is read-only; `inventory.stock.receive` is not in the
    viewer's approved set."""
    seller = uuid.uuid4()
    with pytest.raises(ForbiddenError):
        ensure_can_receive_stock(member(seller, SellerMemberRole.VIEWER), seller)


def test_a_caller_with_no_membership_is_denied():
    """THE finding, at the policy level."""
    with pytest.raises(ForbiddenError):
        ensure_can_receive_stock(stranger(), uuid.uuid4())


def test_a_member_of_a_different_seller_is_denied():
    """Cross-seller isolation: full authority over your own shop is not authority
    over a rival's."""
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(ForbiddenError):
        ensure_can_receive_stock(member(mine, SellerMemberRole.OWNER), theirs)


@pytest.mark.parametrize("status", [SellerStatus.SUSPENDED, SellerStatus.PENDING])
def test_a_non_active_seller_confers_nothing(status):
    seller = uuid.uuid4()
    with pytest.raises(ForbiddenError):
        ensure_can_receive_stock(member(seller, SellerMemberRole.OWNER, status), seller)


# --- the one deliberate platform override -----------------------------------
def test_the_platform_override_is_honoured():
    """`inventory.manage.any` exists so back office can correct stock across
    sellers. It is written out at the call site, not hidden inside
    `can_for_seller`, so the widening is visible where it is granted."""
    ensure_can_receive_stock(operator(Permission.INVENTORY_MANAGE_ANY), uuid.uuid4())


def test_a_seller_scoped_permission_held_at_platform_level_does_not_count():
    """`Principal.can_for_seller` is strictly membership-scoped. A platform grant
    of a seller-scopable permission must not satisfy a seller-scoped check —
    otherwise a role could be quietly turned into a membership."""
    seller = uuid.uuid4()
    with pytest.raises(ForbiddenError):
        ensure_can_receive_stock(operator(Permission.INVENTORY_STOCK_RECEIVE), seller)


def test_an_unrelated_platform_permission_does_not_count():
    with pytest.raises(ForbiddenError):
        ensure_can_receive_stock(
            operator(Permission.CATALOG_PRODUCTS_CREATE), uuid.uuid4()
        )


# --- shape ------------------------------------------------------------------
def test_the_policy_is_pure_and_synchronous():
    """An async policy is free to query, and a query inside a policy is how
    per-request authorization I/O creeps back in."""
    assert not asyncio.iscoroutinefunction(ensure_can_receive_stock)


def test_every_denial_reads_the_same():
    """No membership, wrong seller, wrong role and a suspended seller must be
    indistinguishable, or the response becomes an enumeration oracle."""
    seller = uuid.uuid4()
    messages = set()
    for principal in (
        stranger(),
        member(seller, SellerMemberRole.VIEWER),
        member(seller, SellerMemberRole.OWNER, SellerStatus.SUSPENDED),
        member(uuid.uuid4(), SellerMemberRole.OWNER),
    ):
        with pytest.raises(ForbiddenError) as exc:
            ensure_can_receive_stock(principal, seller)
        messages.add(str(exc.value))
    assert len(messages) == 1, messages


def test_receive_stock_resolves_the_owner_from_the_offer_not_the_caller():
    """Structural. Taking the seller from the caller would mean asking "may you
    act for yourself", which is always yes."""
    import ast
    import inspect

    from src.modules.inventory.application.use_cases import ReceiveStock

    tree = ast.parse(inspect.getsource(ReceiveStock).lstrip())
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ensure_can_receive_stock"
    ]
    assert len(calls) == 1, "expected exactly one authorization site"
    assert ast.unparse(calls[0].args[1]) == "seller_id"

    source = inspect.getsource(ReceiveStock)
    assert "self._selling.seller_of(" in source, "the owner is not read from the offer"
    assert source.index("ensure_can_receive_stock") < source.index("uow.commit"), (
        "the check does not precede the write"
    )


def test_receive_stock_requires_a_principal():
    """A use case that can be called without one is a use case that will be."""
    import inspect

    from src.modules.inventory.application.use_cases import ReceiveStock
    from src.shared_kernel.application.security import Principal as P

    params = inspect.signature(ReceiveStock.execute).parameters
    assert "principal" in params
    assert params["principal"].annotation in (P, "Principal")
