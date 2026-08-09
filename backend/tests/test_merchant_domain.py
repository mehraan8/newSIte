"""Merchant domain invariants (ADR-0011 §7, Phase 1C).

The invariant under test above all others:

    USER != SELLER

DB-free: the Merchant domain is pure Python.
"""

from __future__ import annotations

import ast
import pathlib
import re
import tokenize
import uuid

import pytest

from src.modules.merchant.domain.entities import (
    MemberAdded,
    MemberRemoved,
    MemberRoleChanged,
    Seller,
    SellerActivated,
    SellerMember,
    SellerMemberRole,
    SellerMemberStatus,
    SellerRegistered,
    SellerStatus,
    SellerSuspended,
)
from src.modules.merchant.domain.exceptions import SellerIdentityError
from src.shared_kernel.application.security import SellerStatus as KernelSellerStatus

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
MERCHANT = SRC / "modules" / "merchant"


def a_seller(**kw) -> Seller:
    kw.setdefault("code", f"acme-{uuid.uuid4().hex[:8]}")
    kw.setdefault("name", "Acme Trading")
    return Seller.register(**kw)


# --- Seller identity is independent ----------------------------------------
def test_seller_id_is_independently_generated():
    a, b = a_seller(), a_seller()
    assert isinstance(a.id, uuid.UUID)
    assert a.id != b.id


def test_register_cannot_be_given_a_user_or_an_id():
    """The collapse is not expressible: there is no parameter through which a
    user id could become Seller.id."""
    import inspect

    params = set(inspect.signature(Seller.register).parameters)
    assert params == {"code", "name", "tenant_id"}
    for forbidden in ("id", "user", "user_id", "subject_id", "principal", "owner_id"):
        assert forbidden not in params


def test_a_seller_needs_no_user_to_exist():
    seller = a_seller()
    assert seller.status is SellerStatus.PENDING
    assert not hasattr(seller, "user_id")


def test_seller_and_user_ids_are_never_equal_in_a_membership():
    same = uuid.uuid4()
    with pytest.raises(SellerIdentityError):
        SellerMember.add(seller_id=same, user_id=same, role=SellerMemberRole.OWNER)


def test_seller_code_and_name_are_validated():
    with pytest.raises(SellerIdentityError):
        Seller.register(code="Not A Slug", name="x")
    with pytest.raises(SellerIdentityError):
        Seller.register(code="ok-code", name="   ")


# --- status model -----------------------------------------------------------
def test_new_seller_starts_pending_and_grants_nothing():
    seller = a_seller()
    assert seller.status is SellerStatus.PENDING
    assert not seller.grants_authority


def test_only_active_grants_authority():
    seller = a_seller()
    seller.activate()
    assert seller.status is SellerStatus.ACTIVE and seller.grants_authority
    seller.suspend(reason="fraud review")
    assert seller.status is SellerStatus.SUSPENDED and not seller.grants_authority


def test_suspended_seller_can_be_reinstated():
    seller = a_seller()
    seller.activate()
    seller.suspend()
    seller.activate()
    assert seller.grants_authority


def test_merchant_status_vocabulary_matches_the_shared_kernel_exactly():
    """The two enums are separate because the domain may not import the Shared
    Kernel's security module (that would drag Permission into the domain). This
    test is what stops them drifting."""
    assert {s.value for s in SellerStatus} == {s.value for s in KernelSellerStatus}
    for status in SellerStatus:
        assert KernelSellerStatus(status.value).value == status.value


def test_only_active_grants_authority_in_both_vocabularies():
    granting = {s.value for s in SellerStatus if Seller(
        id=uuid.uuid4(), code="c", name="n", status=s).grants_authority}
    kernel_granting = {s.value for s in KernelSellerStatus if s.grants_authority}
    assert granting == kernel_granting == {"active"}


# --- version / optimistic concurrency ---------------------------------------
def test_version_starts_at_one_and_increments_on_each_state_change():
    seller = a_seller()
    assert seller.version == 1
    seller.activate()
    assert seller.version == 2
    seller.suspend()
    assert seller.version == 3
    seller.rename("Acme Holdings")
    assert seller.version == 4


def test_idempotent_transitions_do_not_bump_the_version():
    """A version that moved without a state change would make the Phase 1F cache
    discard good updates."""
    seller = a_seller()
    seller.activate()
    v = seller.version
    seller.activate()
    seller.rename(seller.name)
    assert seller.version == v


def test_events_carry_the_post_change_version():
    """ADR-0011 §10.2: out-of-order delivery is made safe by the version, so the
    event must carry the value the seller has AFTER the change."""
    seller = a_seller()
    seller.activate()
    seller.suspend()
    events = seller.pull_events()
    assert [type(e) for e in events] == [SellerRegistered, SellerActivated, SellerSuspended]
    assert [e.version for e in events] == [1, 2, 3]


# --- membership: the many-to-many that matters ------------------------------
def test_one_user_can_belong_to_multiple_sellers():
    user = uuid.uuid4()
    x, y = a_seller(), a_seller()
    mx = SellerMember.add(seller_id=x.id, user_id=user, role=SellerMemberRole.OWNER)
    my = SellerMember.add(seller_id=y.id, user_id=user, role=SellerMemberRole.STAFF)
    assert mx.seller_id != my.seller_id
    assert mx.user_id == my.user_id == user
    assert mx.role is SellerMemberRole.OWNER and my.role is SellerMemberRole.STAFF


def test_one_seller_can_have_multiple_users():
    seller = a_seller()
    members = [
        SellerMember.add(seller_id=seller.id, user_id=uuid.uuid4(),
                         role=SellerMemberRole.MANAGER)
        for _ in range(3)
    ]
    assert len({m.user_id for m in members}) == 3
    assert {m.seller_id for m in members} == {seller.id}


def test_membership_identity_is_the_pair():
    seller_id, user_id = uuid.uuid4(), uuid.uuid4()
    member = SellerMember.add(seller_id=seller_id, user_id=user_id,
                              role=SellerMemberRole.VIEWER)
    assert member.id == (seller_id, user_id)


def test_membership_copies_no_user_profile_data():
    """Identity remains authoritative for who a user is."""
    member = SellerMember.add(seller_id=uuid.uuid4(), user_id=uuid.uuid4(),
                              role=SellerMemberRole.OWNER)
    for forbidden in ("mobile", "phone", "email", "name", "full_name",
                      "permissions", "roles", "token", "claims"):
        assert not hasattr(member, forbidden)


def test_membership_carries_no_tenant_of_its_own():
    """Tenancy is inherited from the Seller, so a membership's tenant cannot
    disagree with its seller's (ADR-0011 §16)."""
    member = SellerMember.add(seller_id=uuid.uuid4(), user_id=uuid.uuid4(),
                              role=SellerMemberRole.OWNER)
    assert not hasattr(member, "tenant_id")


def test_membership_lifecycle_and_events():
    member = SellerMember.add(seller_id=uuid.uuid4(), user_id=uuid.uuid4(),
                              role=SellerMemberRole.STAFF)
    assert member.grants_authority
    member.change_role(SellerMemberRole.MANAGER)
    member.suspend()
    assert not member.grants_authority
    member.reinstate()
    assert member.grants_authority
    assert [type(e) for e in member.pull_events()] == [
        MemberAdded, MemberRoleChanged, MemberRemoved, MemberAdded
    ]


def test_member_roles_are_the_adr_vocabulary():
    assert {r.value for r in SellerMemberRole} == {
        "seller_owner", "seller_manager", "seller_staff", "seller_viewer"
    }


def test_member_status_vocabulary():
    assert {s.value for s in SellerMemberStatus} == {"active", "suspended"}


def test_seller_status_and_membership_status_are_independent_state():
    """The data model must keep both, so Phase 1F can assemble authorization
    state without inferring one from the other."""
    seller = a_seller()
    seller.suspend()
    member = SellerMember.add(seller_id=seller.id, user_id=uuid.uuid4(),
                              role=SellerMemberRole.OWNER)
    # An active membership in a suspended seller is representable — and the
    # authorization decision (Phase 1G) is what must deny it.
    assert member.grants_authority
    assert not seller.grants_authority


# --- context isolation ------------------------------------------------------
def test_merchant_does_not_import_other_bounded_contexts():
    forbidden = ("selling", "inventory", "buybox", "ordering", "catalog", "notification")
    offenders = []
    for path in MERCHANT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for other in forbidden:
            if re.search(rf"^\s*(from|import)\s+src\.modules\.{other}\b", text, re.M):
                offenders.append(f"{path.relative_to(SRC)} -> {other}")
    assert offenders == [], f"Merchant must not depend on: {offenders}"


def test_merchant_domain_does_not_import_the_application_security_module():
    """Domains import only shared_kernel.domain; importing the security module
    would pull Permission into the domain layer (ADR-0011 principle 1).

    Checks executable imports, not raw text — the module is named in prose in
    entities.py precisely to explain why it is NOT imported.
    """
    offenders = []
    for path in (MERCHANT / "domain").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("src.shared_kernel.application"):
                    offenders.append(f"{path.relative_to(SRC)} -> {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("src.shared_kernel.application"):
                        offenders.append(f"{path.relative_to(SRC)} -> {alias.name}")
    assert offenders == [], f"Merchant domain imports the application layer: {offenders}"


def test_identity_does_not_learn_about_sellers():
    """Merchant owns membership; Identity must not become its owner."""
    identity = SRC / "modules" / "identity"
    offenders = [
        p.relative_to(SRC) for p in identity.rglob("*.py")
        if re.search(r"^\s*(from|import)\s+src\.modules\.merchant\b",
                     p.read_text(encoding="utf-8"), re.M)
    ]
    assert offenders == [], f"Identity imports Merchant: {offenders}"


# --- architectural guard ----------------------------------------------------
COLLAPSE = re.compile(
    r"(?:seller\s*\.\s*id|seller_id)\s*=\s*(?:str\(\s*)?"
    r"(?:current|current_user|principal|user|claims)\s*\.\s*"
    r"(?:id|subject_id|user_id)",
)


def _code_lines(path: pathlib.Path) -> dict[int, str]:
    """Executable tokens only — comments and strings removed."""
    lines: dict[int, list[str]] = {}
    with tokenize.open(path) as handle:
        try:
            for tok in tokenize.generate_tokens(handle.readline):
                if tok.type in (tokenize.COMMENT, tokenize.STRING):
                    continue
                lines.setdefault(tok.start[0], []).append(tok.string)
        except (tokenize.TokenError, IndentationError):  # pragma: no cover
            return {}
    return {no: "".join(parts) for no, parts in lines.items()}


def test_no_source_derives_a_seller_identity_from_a_user():
    offenders = []
    for path in SRC.rglob("*.py"):
        for lineno, code in _code_lines(path).items():
            if COLLAPSE.search(code):
                offenders.append(f"{path.relative_to(SRC)}:{lineno}: {code.strip()}")
    assert offenders == [], (
        "Seller identity derived from a user identity:\n  " + "\n  ".join(sorted(offenders))
    )


def test_the_guard_detects_what_it_claims_to():
    for sample in ("seller_id=str(current.id)", "seller.id = user.id",
                   "seller_id = principal.subject_id", "seller.id=current.id"):
        assert COLLAPSE.search(sample), sample
    assert not COLLAPSE.search("seller_id=str(offer.seller_id)")
    assert not COLLAPSE.search("customer_id=str(current.id)")


def test_no_platform_seller_role_exists():
    """Selling authority is a membership, never a platform role (ADR-0011 §6)."""
    from src.shared_kernel.application.permissions import Permission

    for permission in Permission:
        assert permission.code != "seller"
    offenders = [
        p.relative_to(SRC) for p in SRC.rglob("*.py")
        if re.search(r"Role\s*\.\s*SELLER\b", p.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"a platform SELLER role was reintroduced: {offenders}"
