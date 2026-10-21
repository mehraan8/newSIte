"""The platform role vocabulary (ADR-0011 §6) — Identity-owned.

Platform roles answer "what may this user do at the PLATFORM level?". They are
not, and must never become, seller authority: that question is answered by a
`SellerMember` grant in the Merchant context and resolved through
`Principal.can_for_seller`. The two are separate systems that meet only inside an
assembled Principal.

    User --user_roles--> PlatformRole --role_permissions--> Permission   (here)
    User --SellerMember--> Seller                                        (Merchant)

**There is no `seller` platform role**, by architectural rule. Selling capability
is a membership, never a global grant, so a user cannot become "a seller" by
being handed a role code.

LAYER. This lives in the application layer, not the domain: ADR-0011 principle 1
keeps the domain free of Role and Permission. Identity owns it because Identity
owns the grant (`user_roles`), per ADR-0011 §5.1.

SCOPE. This is the vocabulary only. Seeding `roles` rows from it belongs to Phase
1E alongside the permission seeder, so that role and permission seeding stay one
reviewed, generated artefact rather than two mechanisms that can disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class PlatformRoleSpec:
    """What a role row needs. `name_fa` is carried here because `roles.name_fa`
    is NOT NULL, so the seeder must get it from the vocabulary rather than
    inventing one."""

    code: str
    name_fa: str
    description: str


class PlatformRole(Enum):
    """Every grantable platform role. Codes are the stable identifier — nothing
    in authorization logic may key off the surrogate `roles.id` (ADR-0011 §12)."""

    CUSTOMER = PlatformRoleSpec(
        "customer", "مشتری",
        "Default role granted on registration. Buys; sees only their own orders.",
    )
    CATALOG_ADMIN = PlatformRoleSpec(
        "catalog_admin", "مدیر کاتالوگ",
        "Owns the product graph. Deliberately holds no pricing or stock authority.",
    )
    INVENTORY_MANAGER = PlatformRoleSpec(
        "inventory_manager", "مدیر موجودی",
        "First-party stock operations. Cannot create products or set prices.",
    )
    SUPPORT_AGENT = PlatformRoleSpec(
        "support_agent", "کارشناس پشتیبانی",
        "Reads any order and cancels on a customer's behalf. Mutates nothing else.",
    )
    READONLY_OPERATOR = PlatformRoleSpec(
        "readonly_operator", "ناظر فقط‌خواندنی",
        "Observability and incident response with zero write capability.",
    )
    SUPER_ADMIN = PlatformRoleSpec(
        "super_admin", "مدیر ارشد",
        "Break-glass. Holds every permission, including role granting.",
    )

    # `anonymous` from ADR-0011 §6 is deliberately NOT a member: it is the
    # ABSENCE of an authenticated principal, not a grant. There is no row to
    # assign and nothing to hold it, so modelling it as a grantable role would
    # invite someone to assign it.

    @property
    def code(self) -> str:
        return self.value.code

    @property
    def name_fa(self) -> str:
        return self.value.name_fa

    @property
    def description(self) -> str:
        return self.value.description

    def __str__(self) -> str:
        return self.value.code


_BY_CODE: dict[str, PlatformRole] = {r.code: r for r in PlatformRole}


def from_code(code: str) -> PlatformRole | None:
    """Parse a database role code, or None if this build does not know it.

    Returns None rather than raising, mirroring `permissions.from_code`: a
    database holding a role the deployed code has never heard of must confer
    nothing, not crash the request.
    """
    return _BY_CODE.get(code)


def all_codes() -> frozenset[str]:
    """Every grantable role code. Used by the Phase 1E seeder."""
    return frozenset(_BY_CODE)
