"""The User/Seller identity boundary (ADR-0011 §7, Phases pre-C … 1G).

`User != Seller`. A User is a person; a Seller is a business owned by Merchant; a
User reaches a Seller only through a SellerMember grant.

Phase 1G closed the last gap. Offer creation, which stood at 503 while there was
no honest source for `seller_id`, now takes the seller from the caller's
memberships — so the assertions that pinned the 503 have been replaced by ones
that pin the thing the 503 was protecting. What has NOT changed is the rule:
nothing anywhere may turn a user id into a seller id.

The source-level guard at the bottom is the one that matters most: it fails no
matter WHERE someone reintroduces the collapse, not just in the router it was
removed from. The behavioural half — that a caller cannot obtain seller authority
they do not hold — lives in tests/test_selling_offer_authorization.py and
tests/api/test_selling_authorization.py.

DB-free: everything here is either structural or refused before a session opens.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import pathlib
import re
import tokenize
import uuid

import httpx
import jwt as pyjwt
import pytest

from src.api.main import app
from src.config.settings import settings
from src.modules.selling.interface import router as selling_router
from src.modules.selling.interface.schemas import CreateOfferRequest
from src.shared_kernel.application.errors import AppError, CapabilityUnavailableError

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


def run(coro):
    return asyncio.run(coro)


def access_token(user_id: uuid.UUID) -> str:
    return pyjwt.encode(
        {
            "sub": str(user_id), "type": "access", "tenant_id": None, "roles": [],
            "iat": dt.datetime.now(dt.timezone.utc),
            "exp": dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1),
        },
        settings.jwt_secret, algorithm=settings.jwt_algorithm,
    )


def call(method: str, url: str, **kw) -> httpx.Response:
    async def _go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://testserver",
        ) as client:
            return await client.request(method, url, **kw)

    return run(_go())


OFFERS = "/api/v1/selling/offers"
BODY = {"variant_id": str(uuid.uuid4()), "condition": "new",
        "currency": "IRR", "amount": 1000, "scale": 0}


# --- A. seller_id is a SELECTOR, and only a selector -----------------------
def test_seller_id_is_a_request_field_again_but_selects_rather_than_grants():
    """Phase 1G reinstated the field. That is safe only because the value is
    proved against `Principal.memberships` before anything is created — see
    tests/test_selling_offer_authorization.py::test_case_b_*, which shows that
    naming a seller you are not a member of creates nothing."""
    assert "seller_id" in CreateOfferRequest.model_fields
    assert CreateOfferRequest.model_fields["seller_id"].annotation is uuid.UUID


def test_creation_still_refuses_every_other_unexpected_field():
    """extra="forbid" is still on: the schema names exactly what the server
    models, so a typo'd or smuggled field is a 422 rather than silent input."""
    with pytest.raises(Exception) as exc:
        CreateOfferRequest(seller_id=uuid.uuid4(), user_id=str(uuid.uuid4()), **BODY)
    assert "user_id" in str(exc.value)


# --- B. the user-id-as-seller path is structurally impossible --------------
def test_create_offer_does_not_receive_the_authenticated_user():
    """Structural: the handler has a Principal, never a CurrentUser, so there is
    no user id in scope to assign to a seller. The collapse cannot be written
    here without first adding a parameter."""
    params = inspect.signature(selling_router.create_offer).parameters
    assert "current" not in params
    assert set(params) == {"body", "principal", "uc"}


def test_every_selling_route_takes_the_principal_not_a_user():
    for name in ("create_offer", "activate_offer", "suspend_offer", "change_offer_price"):
        params = inspect.signature(getattr(selling_router, name)).parameters
        assert "principal" in params, name
        assert "current" not in params, name


def test_anonymous_offer_creation_fails_authentication_first():
    """Authorization must never be reachable without authentication: no
    credential is 401, not 403 — a missing credential is not a permission
    problem, and answering 403 would tell an anonymous prober that the seller
    they named exists."""
    resp = call("POST", OFFERS, json={**BODY, "seller_id": str(uuid.uuid4())})
    assert resp.status_code == 401, resp.text


def test_capability_unavailable_remains_a_mapped_application_error():
    """The 503 is no longer used by Selling, but the error type stays part of the
    taxonomy — the next capability to land dark needs it."""
    assert issubclass(CapabilityUnavailableError, AppError)
    assert CapabilityUnavailableError.http_status == 503
    assert CapabilityUnavailableError.code == "capability_unavailable"


# --- C. Seller exists in exactly one place: Merchant ------------------------
# Until Phase 1C these two asserted Merchant did not exist yet. Phase 1C landed
# it, so they now assert the DURABLE rule instead of the temporal one: Seller is
# Merchant's and nobody else may define or own one.
def test_seller_entities_are_defined_only_by_merchant():
    """No context may grow its own Seller — that would be a second source of
    truth for merchant identity (ADR-0011 §7)."""
    definers = {
        p.relative_to(SRC).parts[1] if p.relative_to(SRC).parts[0] == "modules" else "shared_kernel"
        for p in SRC.rglob("*.py")
        if re.search(r"^\s*class\s+(Seller|SellerMember)\b",
                     p.read_text(encoding="utf-8"), re.M)
    }
    assert definers == {"merchant"}, f"Seller is defined outside Merchant: {definers}"


def test_seller_tables_are_created_by_exactly_one_migration():
    """One migration owns the Merchant schema, and no other migration may create
    or FK to it."""
    migrations = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions"
    creators = {
        path.name for path in migrations.glob("*.py")
        if 'create_table(\n        "sellers"' in path.read_text(encoding="utf-8")
        or '"seller_members"' in path.read_text(encoding="utf-8")
    }
    assert creators == {"0019_merchant_seller_identity.py"}, creators


# --- D/E. Phase 1A/1B artifacts unchanged in meaning ------------------------
def test_jwt_still_carries_no_seller_information():
    """Seller identity must never be solved by putting it in the token."""
    from src.modules.identity.infrastructure.jwt_service import ACCESS_TOKEN_CLAIMS

    assert ACCESS_TOKEN_CLAIMS == frozenset(
        {"sub", "type", "tenant_id", "roles", "iat", "exp"}
    )
    for forbidden in ("seller_id", "seller_ids", "memberships",
                      "seller_memberships", "seller_permissions"):
        assert forbidden not in ACCESS_TOKEN_CLAIMS


def test_principal_still_models_seller_membership_separately_from_subject():
    """The Phase 1A shape already expresses User != Seller and must not have been
    collapsed to make this phase easier."""
    from src.shared_kernel.application.security import Principal, SellerGrant

    subject = uuid.uuid4()
    seller = uuid.uuid4()
    principal = Principal(subject_id=subject)
    # A user is NOT implicitly a member of a seller bearing their own id.
    assert not principal.is_member_of(subject)
    assert not principal.is_member_of(seller)
    assert {f for f in SellerGrant.__dataclass_fields__} == {
        "seller_id", "permissions", "status", "tenant_id"
    }


# --- the source-level regression lock --------------------------------------
# Matches `seller_id = <principal-ish>.id` in any spacing/keyword form.
COLLAPSE = re.compile(
    r"seller_id\s*=\s*(?:str\(\s*)?"
    r"(?:current|current_user|principal|user|claims)\s*\.\s*"
    r"(?:id|subject_id|user_id)",
)


def _code_lines(path: pathlib.Path) -> dict[int, str]:
    """Source with comments and string literals removed, keyed by line number.

    Scanning raw text would flag the router docstring that *explains* the removed
    pattern, and would equally miss nothing real — so the guard reads executable
    tokens only. It therefore cannot be silenced by moving the collapse into a
    string, and cannot be tripped by documenting it.
    """
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


def test_no_source_file_derives_seller_identity_from_a_user():
    """Fails wherever the collapse is reintroduced — not only in the router it
    was removed from. This is the durable guard for `User != Seller`."""
    offenders = []
    for path in SRC.rglob("*.py"):
        for lineno, code in _code_lines(path).items():
            if COLLAPSE.search(code):
                offenders.append(f"{path.relative_to(SRC)}:{lineno}: {code.strip()}")
    assert offenders == [], (
        "seller identity is being derived from a user identity:\n  "
        + "\n  ".join(sorted(offenders))
    )


def test_the_guard_actually_detects_the_pattern_it_claims_to():
    """A guard that cannot fail proves nothing."""
    for sample in (
        "        seller_id=str(current.id),",
        "seller_id = current.id",
        "seller_id=principal.subject_id",
        "seller_id = current_user.id",
    ):
        assert COLLAPSE.search(sample), sample
    assert not COLLAPSE.search("seller_id=str(offer.seller_id)")
    assert not COLLAPSE.search("customer_id=str(current.id)")  # legitimate: a customer IS a user
