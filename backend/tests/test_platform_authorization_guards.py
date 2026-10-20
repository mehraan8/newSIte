"""Durable guards for platform back-office authorization (Phase 1H §B).

The behavioural suites prove today's code denies the right callers. These prove
the shape that keeps it true — each fails wherever the rule is broken, not only
where it was broken first.

The failure modes targeted here all pass every behavioural test that exists:
a use case added without a Principal; authorization written as a role-name
comparison; a policy that grows a query; a second authorization mechanism beside
the first. Every one of them is a silent regression.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import tokenize

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
CATALOG = SRC / "modules" / "catalog"
INVENTORY = SRC / "modules" / "inventory"
POLICY = SRC / "shared_kernel" / "application" / "platform_policy.py"

# Use cases that mutate on behalf of an authenticated caller. Saga participants
# are deliberately absent: they react to events the outbox delivers and have no
# caller to authorize (see ReleaseForOrder).
OPERATOR_USE_CASES = [
    (CATALOG / "application" / "write_use_cases.py", "CreateProduct"),
    (CATALOG / "application" / "write_use_cases.py", "UpdateProduct"),
    (INVENTORY / "application" / "use_cases.py", "ReserveStock"),
    (INVENTORY / "application" / "use_cases.py", "CommitReservation"),
    (INVENTORY / "application" / "use_cases.py", "ReleaseReservation"),
    (INVENTORY / "application" / "use_cases.py", "ReceiveStock"),
]

SAGA_USE_CASES = ["ReserveForOrder", "CommitForOrder", "ReleaseForOrder", "ExpireReservations"]


def code_lines(path: pathlib.Path) -> dict[int, str]:
    """Executable tokens only — comments and strings stripped, so a guard cannot
    be silenced by moving code into a string nor tripped by a docstring."""
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


def class_node(path: pathlib.Path, name: str) -> ast.ClassDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)


def execute_of(cls: ast.ClassDef):
    return next(
        n for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "execute"
    )


# --- Guard: every operator-facing mutation takes a Principal ----------------
@pytest.mark.parametrize(
    ("path", "name"), OPERATOR_USE_CASES, ids=[n for _p, n in OPERATOR_USE_CASES]
)
def test_every_operator_facing_use_case_requires_a_principal(path, name):
    """A use case that CAN be called without one WILL be. Making it a required
    positional argument turns a forgotten check into a signature change that
    shows up in review, not an invisible default."""
    execute = execute_of(class_node(path, name))
    args = [a.arg for a in execute.args.args] + [a.arg for a in execute.args.kwonlyargs]
    assert "principal" in args, f"{name}.execute takes no principal"


@pytest.mark.parametrize(
    ("path", "name"), OPERATOR_USE_CASES, ids=[n for _p, n in OPERATOR_USE_CASES]
)
def test_every_operator_facing_use_case_authorizes(path, name):
    """Not merely accepting a Principal — actually consulting it."""
    source = ast.unparse(class_node(path, name))
    assert (
        "ensure_can_platform" in source or "ensure_can_receive_stock" in source
    ), f"{name} accepts a principal and never checks it"


# --- Guard: authorization precedes the first read and the first write -------
@pytest.mark.parametrize(
    ("path", "name"),
    [(p, n) for p, n in OPERATOR_USE_CASES if n != "ReceiveStock"],
    ids=[n for _p, n in OPERATOR_USE_CASES if n != "ReceiveStock"],
)
def test_platform_authorization_precedes_any_repository_access(path, name):
    """Platform permissions do not depend on the resource, so the check belongs
    before the load — which closes the existence oracle as well as the
    write-before-authorize hole.

    `ReceiveStock` is excluded and different by necessity: its permission is
    SELLER-scoped, so it must resolve the offer's owner first. It has its own
    ordering guard in tests/test_inventory_authorization.py.
    """
    execute = execute_of(class_node(path, name))

    def first(pred) -> int | None:
        hits = [n.lineno for n in ast.walk(execute) if pred(n)]
        return min(hits) if hits else None

    guard = first(
        lambda n: isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "ensure_can_platform"
    )
    repository_touch = first(
        lambda n: isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"get_by_id", "get_by_slug", "get_reservation", "get_item",
                            "add", "save", "commit"}
    )

    assert guard is not None, f"{name}.execute never calls ensure_can_platform"
    if repository_touch is not None:
        assert guard < repository_touch, (
            f"{name}.execute touches the repository at line {repository_touch} "
            f"before authorizing at line {guard}"
        )


# --- Guard: no role-name authorization anywhere -----------------------------
def test_authorization_is_never_written_as_a_role_name_comparison():
    """`if principal.role == "catalog_admin"` bypasses the permission model
    entirely, and silently stops matching when a role is renamed or a second role
    gains the same capability.

    AST, not text: an earlier text version flagged the RBAC drift GENERATOR,
    which iterates `state.roles` to emit a seed file. `for c, n, s in
    state.roles` is not an authorization decision, and a guard that cries wolf
    on it gets deleted. What matters is a COMPARISON or a membership TEST whose
    operand is role-shaped.
    """
    role_codes = {"catalog_admin", "inventory_manager", "support_agent",
                  "readonly_operator", "super_admin", "customer", "seller"}
    # These own the role vocabulary itself; naming role codes is their job.
    VOCABULARY = {
        "modules/identity/application/roles.py",
        "modules/identity/application/rbac_baseline.py",
        "modules/identity/infrastructure/rbac_drift.py",
    }

    # The attribute must hang off something CALLER-shaped. `self.role` on a
    # domain entity is not authorization — SellerMember.change_role compares it
    # to short-circuit an idempotent rename — and flagging it would train the
    # reader to ignore this guard.
    CALLER_NAMES = {"principal", "current", "current_user", "claims", "user", "token"}

    def role_shaped(node) -> bool:
        if isinstance(node, ast.Constant) and node.value in role_codes:
            return True
        if (
            isinstance(node, ast.Attribute)
            and node.attr in ("role", "roles", "role_codes")
            and isinstance(node.value, ast.Name)
            and node.value.id in CALLER_NAMES
        ):
            return True
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return any(role_shaped(e) for e in node.elts)
        return False

    offenders = []
    for path in SRC.rglob("*.py"):
        rel = str(path.relative_to(SRC)).replace("\\", "/")
        if rel in VOCABULARY:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Compare):
                continue
            operands = [node.left, *node.comparators]
            # `x in roles`, `role == "admin"`, `"admin" in principal.roles`
            if any(role_shaped(o) for o in operands):
                offenders.append(f"{rel}:{node.lineno}: {ast.unparse(node)[:70]}")

    assert offenders == [], (
        "authorization must be permission-based, not role-name based:\n  "
        + "\n  ".join(offenders)
    )


def test_the_role_name_guard_detects_what_it_claims_to():
    """A guard that cannot fail proves nothing."""
    def offends(snippet: str) -> bool:
        role_codes = {"catalog_admin", "super_admin", "customer"}

        CALLER_NAMES = {"principal", "current", "current_user", "claims", "user", "token"}

        def role_shaped(n) -> bool:
            if isinstance(n, ast.Constant) and n.value in role_codes:
                return True
            if (
                isinstance(n, ast.Attribute)
                and n.attr in ("role", "roles", "role_codes")
                and isinstance(n.value, ast.Name)
                and n.value.id in CALLER_NAMES
            ):
                return True
            if isinstance(n, (ast.List, ast.Tuple, ast.Set)):
                return any(role_shaped(e) for e in n.elts)
            return False

        return any(
            isinstance(n, ast.Compare)
            and any(role_shaped(o) for o in [n.left, *n.comparators])
            for n in ast.walk(ast.parse(snippet))
        )

    assert offends('x = principal.role == "catalog_admin"')
    assert offends('x = "super_admin" in principal.roles')
    assert offends('x = claims.role_codes == ()')
    # The shapes that must NOT trip it:
    assert not offends("for c, n, s in state.roles:\n    pass")
    assert not offends("x = principal.can(Permission.CATALOG_PRODUCTS_CREATE)")


def test_no_super_admin_special_case():
    """`if super_admin: allow()` would make the seeded baseline advisory."""
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.name in ("roles.py", "rbac_baseline.py"):
            continue
        for lineno, line in code_lines(path).items():
            if "SUPER_ADMIN" in line.replace(" ", ""):
                offenders.append(f"{path.relative_to(SRC)}:{lineno}")
    assert offenders == [], offenders


# --- Guard: the policy stays pure and platform-only -------------------------
def test_the_platform_policy_performs_no_io():
    forbidden = ("sqlalchemy", "redis", "asyncpg", "jwt", "fastapi",
                 "src.shared_kernel.infrastructure")
    tree = ast.parse(POLICY.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        elif isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        offenders += [f"{node.lineno}: {n}" for n in names if n.startswith(forbidden)]
    assert offenders == [], offenders


def test_the_platform_policy_is_synchronous():
    tree = ast.parse(POLICY.read_text(encoding="utf-8"))
    offenders = [
        f"line {n.lineno}: {type(n).__name__}"
        for n in ast.walk(tree)
        if isinstance(n, (ast.AsyncFunctionDef, ast.Await, ast.AsyncFor, ast.AsyncWith))
    ]
    assert offenders == [], offenders


def test_the_platform_policy_never_consults_seller_state():
    """Platform authority and seller membership are separate universes. A policy
    that reached into `memberships` would be the bridge between them."""
    code = "".join(code_lines(POLICY).values())
    for forbidden in ("memberships", "can_for_seller", "SellerGrant", "seller_id"):
        assert forbidden not in code, f"platform policy references {forbidden!r}"


# --- Guard: one authorization mechanism, not several ------------------------
def test_platform_authorization_has_exactly_one_implementation():
    """Two mechanisms drift, and the weaker one becomes the way in. `Principal.can`
    may be called from the policy and the router guard, and nowhere else."""
    callers = set()
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "can"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "principal"
            ):
                callers.add(str(path.relative_to(SRC)).replace("\\", "/"))
    assert callers <= {
        "shared_kernel/application/platform_policy.py",   # the use-case policy
        "api/guards.py",                                  # the router floor
        "modules/inventory/application/policies.py",      # the documented override
    }, callers


def test_the_principal_is_never_ambient():
    offenders = []
    for path in SRC.rglob("*.py"):
        for lineno, line in code_lines(path).items():
            flat = line.replace(" ", "")
            if ("ContextVar" in flat and "rincipal" in flat) or "request.state.principal" in flat:
                offenders.append(f"{path.relative_to(SRC)}:{lineno}")
    assert offenders == [], offenders


# --- Guard: saga participants stay caller-free ------------------------------
@pytest.mark.parametrize("name", SAGA_USE_CASES)
def test_saga_participants_take_no_principal(name):
    """They run in the relay, reacting to events only the outbox delivers. Giving
    one a Principal parameter would force the invention of a "system principal" —
    a construct that, by existing, is a bypass primitive for anything that can
    build it."""
    path = INVENTORY / "application" / "use_cases.py"
    execute = execute_of(class_node(path, name))
    args = [a.arg for a in execute.args.args] + [a.arg for a in execute.args.kwonlyargs]
    assert "principal" not in args, f"{name} is a saga participant and must not need one"


def test_the_release_consumer_uses_the_saga_facing_class():
    """`ReleaseReservation` once served both HTTP and the saga. Requiring a
    Principal on it would have broken compensation; authorizing on the `reason`
    string would have been authorization by caller-supplied value."""
    consumers = (INVENTORY / "application" / "consumers.py").read_text(encoding="utf-8")
    assert "ReleaseForOrder" in consumers
    assert "import ReleaseReservation" not in consumers


def test_the_saga_release_path_shares_the_operator_mechanics():
    """Two copies of release would drift, and only one would get the next fix."""
    from src.modules.inventory.application.use_cases import ReleaseForOrder

    assert "release_unchecked" in inspect.getsource(ReleaseForOrder.execute)
