"""Merchant domain — Seller and SellerMember (ADR-0011 §7).

The invariant this module exists to protect:

    USER != SELLER

A User is a person, owned by Identity. A Seller is a business, owned here. The
ONLY connection between them is a SellerMember row. `Seller.id` is generated
independently and is never derived from a user id — `Seller.register()` does not
accept a user at all, so the collapse is not expressible.

Layering: this module imports only `shared_kernel.domain`, matching every other
context's domain layer. In particular it does NOT import
`shared_kernel.application.security`, which would pull `Permission` into the
domain and break ADR-0011 principle 1. `SellerStatus` is therefore declared here
with values identical to the Shared Kernel enum, and mapped at Merchant's port
boundary — which is exactly what the Shared Kernel's own SellerStatus docstring
prescribes ("Merchant owns the authoritative status and maps its domain value
onto this one at the port boundary"). `tests/test_merchant_domain.py` asserts the
two vocabularies stay value-identical, so they cannot drift.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from enum import Enum

from src.modules.merchant.domain.exceptions import (
    InvalidMembershipTransition,
    InvalidSellerTransition,
    SellerIdentityError,
)
from src.shared_kernel.domain.base import AggregateRoot, DomainEvent, new_uuid

# Lowercase ascii words joined by single hyphens, like Catalog's Slug.
_CODE_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SellerStatus(str, Enum):
    """A seller's lifecycle. Values mirror shared_kernel SellerStatus exactly."""

    PENDING = "pending"
    ACTIVE = "active"
    SUSPENDED = "suspended"


class SellerMemberRole(str, Enum):
    """Authority a user holds INSIDE one seller (ADR-0011 §7).

    Deliberately NOT a platform role. There is no platform role `seller`: selling
    authority is a membership, never a global grant. Permission mapping for these
    codes is Phase 1D/1E; this is only the vocabulary.
    """

    OWNER = "seller_owner"
    MANAGER = "seller_manager"
    STAFF = "seller_staff"
    VIEWER = "seller_viewer"


class SellerMemberStatus(str, Enum):
    """Whether a membership currently confers anything.

    Two values only. A revoked member keeps their row (audit trail, re-invite)
    with status SUSPENDED rather than being deleted. No INVITED state: an
    onboarding flow is not specified by ADR-0011 and inventing one here would be
    scope the phase does not have.
    """

    ACTIVE = "active"
    SUSPENDED = "suspended"


# --- events (PII-free) -----------------------------------------------------
# Carry `version` so the Phase 1F seller-state cache can discard out-of-order
# deliveries (ADR-0011 §10.2). No consumer is registered in this phase.
@dataclass(frozen=True)
class SellerRegistered(DomainEvent):
    seller_id: uuid.UUID | None = None
    code: str = ""
    status: str = SellerStatus.PENDING.value
    tenant_id: uuid.UUID | None = None
    version: int = 1


@dataclass(frozen=True)
class SellerActivated(DomainEvent):
    seller_id: uuid.UUID | None = None
    tenant_id: uuid.UUID | None = None
    version: int = 0


@dataclass(frozen=True)
class SellerSuspended(DomainEvent):
    seller_id: uuid.UUID | None = None
    tenant_id: uuid.UUID | None = None
    version: int = 0
    reason: str = ""


@dataclass(frozen=True)
class SellerUpdated(DomainEvent):
    seller_id: uuid.UUID | None = None
    tenant_id: uuid.UUID | None = None
    version: int = 0
    changed_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemberAdded(DomainEvent):
    seller_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    member_role_code: str = ""


@dataclass(frozen=True)
class MemberRemoved(DomainEvent):
    seller_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None


@dataclass(frozen=True)
class MemberRoleChanged(DomainEvent):
    seller_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    member_role_code: str = ""


# --- Seller aggregate ------------------------------------------------------
class Seller(AggregateRoot):
    """A business identity. Owns its own lifecycle and knows nothing about users.

    `version` is owned by the aggregate rather than stamped by the repository
    (which is how Ordering does it). The difference is deliberate: a seller's
    version is PUBLISHED in its events so the authorization cache can order
    them, so the value must be known at the moment the event is recorded.
    """

    def __init__(
        self,
        *,
        id: uuid.UUID,
        code: str,
        name: str,
        status: SellerStatus = SellerStatus.PENDING,
        tenant_id: uuid.UUID | None = None,
        version: int = 1,
    ) -> None:
        super().__init__()
        if not _CODE_RE.match(code):
            raise SellerIdentityError(
                "کد فروشنده باید با حروف کوچک انگلیسی، ارقام و خط تیره باشد."
            )
        if not name or not name.strip():
            raise SellerIdentityError("نام فروشنده الزامی است.")
        self.id = id
        self.code = code
        self.name = name.strip()
        self.status = status
        self.tenant_id = tenant_id
        self.version = version

    # -- factory --
    @classmethod
    def register(
        cls, *, code: str, name: str, tenant_id: uuid.UUID | None = None
    ) -> "Seller":
        """Create a seller with an INDEPENDENTLY generated id.

        Takes no user and no caller-supplied id: `Seller.id` cannot be set from
        a `user.id`, a `principal.subject_id`, or a JWT `sub`, because there is
        no parameter through which such a value could arrive (ADR-0011 §7.1).
        A user becomes connected to this seller only by adding a SellerMember.
        """
        seller = cls(
            id=new_uuid(), code=code, name=name,
            status=SellerStatus.PENDING, tenant_id=tenant_id, version=1,
        )
        seller.record_event(
            SellerRegistered(
                seller_id=seller.id, code=seller.code,
                status=seller.status.value, tenant_id=tenant_id, version=1,
            )
        )
        return seller

    # -- behaviour --
    @property
    def grants_authority(self) -> bool:
        """Only an ACTIVE seller confers anything. Mirrors the Shared Kernel rule;
        the authorization DECISION stays in policies (Phase 1G+)."""
        return self.status is SellerStatus.ACTIVE

    def activate(self) -> None:
        if self.status is SellerStatus.ACTIVE:
            return  # idempotent, no event
        if self.status is SellerStatus.SUSPENDED:
            # Reinstatement is legitimate and deliberately allowed.
            pass
        self.status = SellerStatus.ACTIVE
        self.version += 1
        self.record_event(
            SellerActivated(
                seller_id=self.id, tenant_id=self.tenant_id, version=self.version
            )
        )

    def suspend(self, *, reason: str = "") -> None:
        if self.status is SellerStatus.SUSPENDED:
            return  # idempotent
        self.status = SellerStatus.SUSPENDED
        self.version += 1
        self.record_event(
            SellerSuspended(
                seller_id=self.id, tenant_id=self.tenant_id,
                version=self.version, reason=reason,
            )
        )

    def rename(self, name: str) -> None:
        if not name or not name.strip():
            raise InvalidSellerTransition("نام فروشنده الزامی است.")
        if name.strip() == self.name:
            return
        self.name = name.strip()
        self.version += 1
        self.record_event(
            SellerUpdated(
                seller_id=self.id, tenant_id=self.tenant_id,
                version=self.version, changed_fields=("name",),
            )
        )


# --- SellerMember aggregate ------------------------------------------------
class SellerMember(AggregateRoot):
    """A User's membership in a Seller — the ONLY link between the two.

    Identified by the pair `(seller_id, user_id)`; the database enforces that as
    a composite primary key, so a user cannot hold two memberships in the same
    seller. Carries NO user profile data: mobile, email and name stay in
    Identity, which remains the authoritative source for who a user is.

    No `tenant_id`: a membership inherits tenancy from its Seller. Storing it
    twice would create a way for a membership's tenant to disagree with its
    seller's, and the cheapest enforcement of that invariant is to make it
    inexpressible (ADR-0011 §16).
    """

    def __init__(
        self,
        *,
        seller_id: uuid.UUID,
        user_id: uuid.UUID,
        role: SellerMemberRole,
        status: SellerMemberStatus = SellerMemberStatus.ACTIVE,
    ) -> None:
        super().__init__()
        if seller_id == user_id:
            # Not a real-world collision risk for v4 UUIDs; it is a tripwire for
            # code that passes a user id where a seller id belongs.
            raise SellerIdentityError(
                "شناسهٔ فروشنده و کاربر نمی‌تواند یکسان باشد."
            )
        # AggregateRoot identity is the pair, not a surrogate.
        self.id = (seller_id, user_id)
        self.seller_id = seller_id
        self.user_id = user_id
        self.role = role
        self.status = status

    @classmethod
    def add(
        cls, *, seller_id: uuid.UUID, user_id: uuid.UUID, role: SellerMemberRole
    ) -> "SellerMember":
        member = cls(seller_id=seller_id, user_id=user_id, role=role)
        member.record_event(
            MemberAdded(
                aggregate_id=seller_id, aggregate_type="SellerMember",
                seller_id=seller_id, user_id=user_id, member_role_code=role.value,
            )
        )
        return member

    @property
    def grants_authority(self) -> bool:
        return self.status is SellerMemberStatus.ACTIVE

    def change_role(self, role: SellerMemberRole) -> None:
        if role is self.role:
            return
        self.role = role
        self.record_event(
            MemberRoleChanged(
                aggregate_id=self.seller_id, aggregate_type="SellerMember",
                seller_id=self.seller_id, user_id=self.user_id,
                member_role_code=role.value,
            )
        )

    def suspend(self) -> None:
        if self.status is SellerMemberStatus.SUSPENDED:
            return
        self.status = SellerMemberStatus.SUSPENDED
        self.record_event(
            MemberRemoved(
                aggregate_id=self.seller_id, aggregate_type="SellerMember",
                seller_id=self.seller_id, user_id=self.user_id,
            )
        )

    def reinstate(self) -> None:
        if self.status is SellerMemberStatus.ACTIVE:
            return
        if self.status is not SellerMemberStatus.SUSPENDED:  # pragma: no cover
            raise InvalidMembershipTransition()
        self.status = SellerMemberStatus.ACTIVE
        self.record_event(
            MemberAdded(
                aggregate_id=self.seller_id, aggregate_type="SellerMember",
                seller_id=self.seller_id, user_id=self.user_id,
                member_role_code=self.role.value,
            )
        )
