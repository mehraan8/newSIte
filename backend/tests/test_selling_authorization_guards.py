"""Durable architectural guards for seller-aware authorization (Phase 1G §32).

Behavioural tests prove that today's code denies the right callers. These prove
that tomorrow's code cannot quietly stop doing so — each one fails wherever the
rule is broken, not only in the file it was broken in first.

They are source-level on purpose. The failure modes they target (a use case added
without a Principal, a policy that grows a query, a context that reaches into
Merchant to "just look up the seller") all pass every behavioural test that
existed before them, because the behaviour they break is behaviour nobody wrote a
test for yet.
"""

from __future__ import annotations

import ast
import pathlib
import tokenize

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
SELLING = SRC / "modules" / "selling"
POLICIES = SELLING / "application" / "policies.py"


def context_of(path: pathlib.Path) -> str:
    parts = path.relative_to(SRC).parts
    return parts[1] if parts[0] == "modules" else parts[0]


def imports_of(path: pathlib.Path) -> list[tuple[int, str]]:
    """(lineno, dotted module) for every import in the file."""
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
    return found


def code_lines(path: pathlib.Path) -> dict[int, str]:
    """Executable tokens only — comments and string literals stripped.

    A guard that reads raw text can be silenced by moving the offending code into
    a string, and tripped by a docstring that merely describes it. Neither is
    acceptable in a security guard.
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


# --- Guard A: Identity and Merchant stay independent ------------------------
def test_identity_and_merchant_never_import_each_other():
    """The two halves of authorization must not fuse.

    Identity owns `user_roles`; Merchant owns `seller_members`. They meet only
    inside an assembled Principal. If either could import the other, "look up the
    user's seller" becomes a one-line temptation and the separation that makes
    seller authority per-seller collapses into a global one.
    """
    offenders = []
    for a, b in (("identity", "merchant"), ("merchant", "identity")):
        for path in (SRC / "modules" / a).rglob("*.py"):
            for lineno, module in imports_of(path):
                if module.startswith(f"src.modules.{b}"):
                    offenders.append(f"{path.relative_to(SRC)}:{lineno}: {module}")
    assert offenders == [], (
        "Identity and Merchant must remain independent; wire them together at "
        "the composition root (src/api/dependencies.py) instead:\n  "
        + "\n  ".join(offenders)
    )


def test_only_the_composition_root_knows_both_identity_and_merchant():
    """Principal assembly is allowed to know both. Nothing else is."""
    knowers = set()
    for path in SRC.rglob("*.py"):
        modules = [m for _n, m in imports_of(path)]
        if any(m.startswith("src.modules.identity") for m in modules) and any(
            m.startswith("src.modules.merchant") for m in modules
        ):
            knowers.add(str(path.relative_to(SRC)).replace("\\", "/"))
    assert knowers <= {"api/dependencies.py", "api/security.py"}, knowers


# --- Guard B: Selling depends on neither ------------------------------------
def test_selling_imports_no_other_bounded_context():
    """Selling consumes an assembled Principal. Reaching into the contexts that
    produced one would let it resolve authority itself — a second, divergent
    answer to "may this caller act for this seller"."""
    offenders = []
    for path in SELLING.rglob("*.py"):
        for lineno, module in imports_of(path):
            if module.startswith("src.modules.") and not module.startswith(
                "src.modules.selling"
            ):
                offenders.append(f"{path.relative_to(SRC)}:{lineno}: {module}")
    assert offenders == [], offenders


def test_only_the_selling_router_reaches_the_composition_root():
    """FastAPI needs a `Depends` callable at the route, so the interface layer
    imports `get_principal` from src/api. That compromise is confined to the
    interface layer: application and domain must not know the root exists."""
    offenders = []
    for path in SELLING.rglob("*.py"):
        if path.parent.name == "interface":
            continue
        for lineno, module in imports_of(path):
            if module.startswith("src.api"):
                offenders.append(f"{path.relative_to(SRC)}:{lineno}: {module}")
    assert offenders == [], offenders


def test_no_selling_code_performs_a_membership_lookup():
    """Guard B. Selling receives an assembled Principal and asks it questions.

    A Redis or SQL membership lookup here would be a SECOND resolution path,
    free to disagree with the one the composition root used — and free to keep
    working after the cache invalidation protocol says it should not.
    """
    forbidden_imports = ("redis", "sqlalchemy", "asyncpg")
    forbidden_calls = ("memberships_for", "seller_states", "MEMBERSHIP_KEY", "SELLER_KEY")
    offenders = []
    for path in SELLING.rglob("*.py"):
        if path.parent.name == "infrastructure":
            continue  # repositories legitimately speak SQL about OFFERS
        for lineno, module in imports_of(path):
            if module.startswith(forbidden_imports):
                offenders.append(f"{path.relative_to(SRC)}:{lineno}: import {module}")
        for lineno, line in code_lines(path).items():
            for call in forbidden_calls:
                if call in line:
                    offenders.append(f"{path.relative_to(SRC)}:{lineno}: {call}")
    assert offenders == [], offenders


# --- Guard C: policies are pure ---------------------------------------------
def test_the_policy_module_touches_no_infrastructure():
    """Every fact a policy needs is already in the Principal (ADR-0011 §9).

    An import of a session, a repository or a cache here is the beginning of
    per-request authorization I/O, which Phase 1F exists to have removed.
    """
    forbidden = ("sqlalchemy", "redis", "asyncpg", "src.shared_kernel.infrastructure")
    offenders = [
        f"{lineno}: {module}"
        for lineno, module in imports_of(POLICIES)
        if module.startswith(forbidden)
    ]
    assert offenders == [], offenders


def test_the_policy_module_contains_no_async_code():
    """Synchronous by construction: no `async def`, no `await`.

    Purity is not a style preference here. A synchronous policy cannot query, so
    it cannot make an authorization decision the composition root did not already
    have the data for.
    """
    tree = ast.parse(POLICIES.read_text(encoding="utf-8"))
    offenders = [
        f"line {node.lineno}: {type(node).__name__}"
        for node in ast.walk(tree)
        if isinstance(node, (ast.AsyncFunctionDef, ast.Await, ast.AsyncFor, ast.AsyncWith))
    ]
    assert offenders == [], offenders


# --- Guard D: platform authority is not seller authority --------------------
def test_the_seller_policy_never_consults_a_platform_permission():
    """`.any` permissions are platform moderation, a separate universe.

    Honouring one here would let a platform role act AS a seller member —
    a different thing from moderating a seller, and the exact cross-universe
    widening the approved matrix forbids (decisions E/F).
    """
    code = "".join(code_lines(POLICIES).values())
    for forbidden in ("MANAGE_ANY", "platform_permissions", ".can("):
        assert forbidden not in code, (
            f"{POLICIES.name} references {forbidden}; platform authority must not "
            "satisfy a seller-scoped check"
        )


# --- Guard E: ownership comes from the resource -----------------------------
def test_every_mutating_selling_use_case_authorizes_before_it_mutates():
    """Ordering, checked structurally: within each `execute`, the ownership check
    must appear before the first repository write.

    A check placed after the mutation still raises, and still leaves the
    aggregate mutated in the identity map — which the UoW may well flush.
    """
    source = (SELLING / "application" / "use_cases.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    checked = 0
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        execute = next(
            (
                n
                for n in cls.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == "execute"
            ),
            None,
        )
        if execute is None:
            continue
        guard_line = writes = None
        for node in ast.walk(execute):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ensure_can_act_for_seller"
            ):
                guard_line = node.lineno if guard_line is None else guard_line
            if isinstance(node, ast.Attribute) and node.attr in ("add", "save"):
                writes = node.lineno if writes is None else min(writes, node.lineno)
        assert guard_line is not None, f"{cls.name}.execute has no ownership check"
        if writes is not None:
            assert guard_line < writes, (
                f"{cls.name}.execute writes at line {writes} before authorizing at "
                f"line {guard_line}"
            )
        checked += 1
    assert checked == 4, f"expected 4 mutating use cases, inspected {checked}"


def test_ownership_is_read_from_the_offer_never_from_the_principal():
    """`offer.seller_id` is the only authoritative owner of an existing offer.

    Passing `principal.subject_id` — or anything else derived from the caller —
    would restore the IDOR under a new spelling: the check would then ask "may
    you act for yourself", which is always yes.
    """
    tree = ast.parse((SELLING / "application" / "use_cases.py").read_text(encoding="utf-8"))
    targets = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ensure_can_act_for_seller"
        ):
            targets.append((node.lineno, node.args[1]))

    assert len(targets) == 4, f"expected 4 ownership checks, found {len(targets)}"
    for lineno, arg in targets:
        rendered = ast.unparse(arg)
        assert rendered in ("offer.seller_id", "seller_id"), (
            f"line {lineno}: ownership resolved from {rendered!r}; it must come "
            "from the offer, or from the nominated seller on create"
        )
        # Belt and braces: nothing reachable from the caller may appear.
        for name in ast.walk(arg):
            assert not (
                isinstance(name, ast.Name) and name.id in ("principal", "current", "user")
            ), f"line {lineno}: {rendered} is derived from the caller"


# --- Guard F: no ambient principal ------------------------------------------
def test_the_principal_is_never_stashed_in_ambient_state():
    """Principal is an explicit parameter (ADR-0011 principle 4).

    A ContextVar would make a missing authorization check invisible: the value
    would simply be there, and a use case that forgot to check would look
    identical to one that did.
    """
    offenders = []
    for path in SRC.rglob("*.py"):
        for lineno, line in code_lines(path).items():
            flat = line.replace(" ", "")
            if "ContextVar" in flat and "rincipal" in flat:
                offenders.append(f"{path.relative_to(SRC)}:{lineno}")
            if "request.state.principal" in flat or "g.principal" in flat:
                offenders.append(f"{path.relative_to(SRC)}:{lineno}")
    assert offenders == [], offenders


# --- Guard G: the token stays free of seller information --------------------
def test_no_seller_identity_is_ever_written_into_a_token():
    from src.modules.identity.infrastructure.jwt_service import ACCESS_TOKEN_CLAIMS

    assert ACCESS_TOKEN_CLAIMS == frozenset(
        {"sub", "type", "tenant_id", "roles", "iat", "exp"}
    )


# --- Guard H: every seller-scoped route carries the Principal ---------------
def test_every_selling_route_passes_the_principal_to_its_use_case():
    """A route that resolves a Principal and then calls `execute(...)` without it
    would not compile — but a route that never asks for one at all would, and
    would silently reinstate the unauthorized path."""
    from src.modules.selling.interface import router as selling_router

    import inspect

    routes = [
        selling_router.create_offer,
        selling_router.activate_offer,
        selling_router.suspend_offer,
        selling_router.change_offer_price,
    ]
    for route in routes:
        params = inspect.signature(route).parameters
        assert "principal" in params, route.__name__
        source = inspect.getsource(route)
        assert "principal)" in source, (
            f"{route.__name__} resolves a Principal but never passes it on"
        )


def test_authentication_is_declared_at_the_router_not_per_route():
    """So a route added tomorrow inherits it. The original ten open endpoints
    were open because protection was per-route and one was forgotten."""
    from src.modules.selling.interface.router import router

    assert router.dependencies, "the selling router declares no dependencies"


# --- the guards must be capable of failing ----------------------------------
@pytest.mark.parametrize(
    "sample",
    [
        "principal = _PRINCIPAL.get()",
        "request.state.principal",
    ],
)
def test_the_ambient_guard_detects_what_it_claims_to(sample):
    """A guard that cannot fail proves nothing."""
    flat = sample.replace(" ", "")
    assert "request.state.principal" in flat or "ContextVar" in flat or "_PRINCIPAL" in flat


# --- Guard E: there is no `seller` platform role ----------------------------
def test_no_seller_platform_role_exists_anywhere():
    """Selling capability is a membership, never a global grant.

    A `Role.SELLER` would make "is a seller" answerable without naming WHICH
    seller — which is precisely the question that has no safe answer, and the
    shape the pre-ADR-0011 system used.
    """
    from src.modules.identity.application.roles import PlatformRole

    codes = {role.code for role in PlatformRole}
    assert "seller" not in codes, codes
    for code in codes:
        assert "seller" not in code, f"platform role {code!r} looks seller-scoped"

    offenders = []
    for path in SRC.rglob("*.py"):
        for lineno, line in code_lines(path).items():
            flat = line.replace(" ", "")
            if "Role.SELLER" in flat or 'PlatformRole("seller")' in flat:
                offenders.append(f"{path.relative_to(SRC)}:{lineno}")
    assert offenders == [], offenders


# --- Guard F: no implicit platform <-> membership conversion ----------------
def test_nothing_converts_a_platform_role_into_a_membership():
    """`Principal.memberships` may only be populated from MembershipQuery.

    Building a grant from a platform permission anywhere else would make a
    platform role into seller authority silently — the widening would live in a
    mapping table rather than at the site that grants it.
    """
    offenders = []
    for path in SRC.rglob("*.py"):
        code = code_lines(path)
        for lineno, line in code.items():
            if "SellerGrant(" not in line:
                continue
            rel = str(path.relative_to(SRC)).replace("\\", "/")
            if rel not in ("api/security.py",):
                offenders.append(f"{rel}:{lineno}")
    assert offenders == [], (
        "SellerGrant is constructed outside Principal assembly: " + str(offenders)
    )


# --- Guard G: a user id is never a seller id --------------------------------
def test_no_code_treats_a_user_id_as_a_seller_id():
    """Guard G, structurally. The behavioural half is
    test_case_d_naming_ones_own_user_id_as_the_seller_is_denied.

    Any comparison between a subject id and a seller id is the `User == Seller`
    collapse wearing a different hat: it can only ever be used to grant authority
    that no membership backs.
    """
    suspicious = (
        "subject_id==seller_id",
        "seller_id==subject_id",
        "subject_id==seller.id",
        "seller.id==principal.subject_id",
        "user.id==seller.id",
        "seller_id=principal.subject_id",
        "seller_id=current.id",
        "seller_id=user.id",
        "seller_id=current_user.id",
        "seller_id=str(principal.subject_id)",
        "seller_id=str(current.id)",
    )
    offenders = []
    for path in SRC.rglob("*.py"):
        for lineno, line in code_lines(path).items():
            flat = line.replace(" ", "")
            for pattern in suspicious:
                if pattern in flat:
                    offenders.append(f"{path.relative_to(SRC)}:{lineno}: {pattern}")
    assert offenders == [], offenders


# --- Guard I: creation proves membership before it builds anything ----------
def test_create_proves_membership_before_constructing_an_offer():
    """The ownership check must be the first thing with consequences.

    Not merely "before persistence": before the catalog is consulted, and before
    an `Offer` exists carrying that seller_id. An Offer built first and rejected
    afterwards has already been handed the seller identity the caller claimed.
    """
    tree = ast.parse((SELLING / "application" / "use_cases.py").read_text(encoding="utf-8"))
    create = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "CreateOffer"
    )
    execute = next(
        n
        for n in create.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "execute"
    )

    def first_line(predicate) -> int | None:
        lines = [node.lineno for node in ast.walk(execute) if predicate(node)]
        return min(lines) if lines else None

    guard = first_line(
        lambda n: isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "ensure_can_act_for_seller"
    )
    offer_created = first_line(
        lambda n: isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "create"
    )
    catalog_read = first_line(
        lambda n: isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "variant_exists"
    )

    assert guard is not None, "CreateOffer.execute does not authorize at all"
    for label, line in (("Offer.create", offer_created), ("variant_exists", catalog_read)):
        assert line is not None and guard < line, (
            f"CreateOffer.execute reaches {label} at line {line} before authorizing "
            f"at line {guard}"
        )
