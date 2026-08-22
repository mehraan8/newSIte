"""Durable guards for the account gate and session revocation (Phase 1G.1).

The behavioural suite proves today's code refuses blocked users and revokes the
right family. These prove the shape that makes it hold: one gate, upstream of
authorization, reading the database rather than the token — and one revocation
scope, derived from a signed claim rather than a request field.

The failure modes targeted here all pass every existing behavioural test:
a second authentication path added beside `get_access_claims`; the status check
quietly moved into a token claim; `revoke_family` called with something the
caller supplied. Each would be a silent security regression.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import tokenize

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
IDENTITY = SRC / "modules" / "identity"


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


# --- the gate exists, and is the ONLY one -----------------------------------
def test_the_account_gate_consults_the_database_not_the_token():
    """Status must be read at request time.

    If this check ever moved into `decode_access`, it would be reading a claim —
    i.e. a snapshot from before the account was blocked — and the guarantee would
    quietly become "blocked within one token lifetime".
    """
    from src.modules.identity.interface import dependencies

    source = inspect.getsource(dependencies.get_access_claims)
    assert "SqlUserStatusQuery" in source
    assert "may_authenticate" in source

    params = inspect.signature(dependencies.get_access_claims).parameters
    assert "session" in params, "the gate performs no database read"


def test_the_token_still_carries_no_account_status():
    """The fix must not have been "put status in the JWT" (Hard Stop 2)."""
    from src.modules.identity.infrastructure.jwt_service import ACCESS_TOKEN_CLAIMS

    assert ACCESS_TOKEN_CLAIMS == frozenset(
        {"sub", "type", "tenant_id", "roles", "iat", "exp"}
    )
    for forbidden in ("status", "is_active", "blocked", "account_status"):
        assert forbidden not in ACCESS_TOKEN_CLAIMS


def test_decode_access_remains_a_pure_token_validator():
    """Separation of the two questions: is the CREDENTIAL good (here), and may
    the ACCOUNT act (the gate). Mixing them would put I/O inside token parsing
    and make `decode_access` untestable without a database."""
    from src.modules.identity.infrastructure.jwt_service import JwtTokenService

    source = inspect.getsource(JwtTokenService.decode_access)
    for forbidden in ("session", "select", "await", "status_of"):
        assert forbidden not in source, f"decode_access references {forbidden!r}"


def test_principal_assembly_sits_downstream_of_the_gate():
    """A blocked caller must never reach membership resolution or the Redis
    cache. Structural, not a matter of route discipline."""
    from src.api.dependencies import get_principal
    from src.modules.identity.interface.dependencies import get_access_claims

    deps = [
        p.default.dependency
        for p in inspect.signature(get_principal).parameters.values()
        if getattr(p.default, "dependency", None) is not None
    ]
    assert get_access_claims in deps


def test_there_is_exactly_one_place_that_decodes_an_access_token():
    """A second authentication path is how a gate gets bypassed: the new route
    calls the other decoder, and nobody notices the missing status check."""
    # AST, not text: `ports.py` DECLARES `decode_access` on the TokenService
    # Protocol, which is a contract, not a second authentication path.
    callers = set()
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "decode_access"
            ):
                callers.add(str(path.relative_to(SRC)).replace("\\", "/"))
    assert callers == {"modules/identity/interface/dependencies.py"}, callers


def test_the_authenticable_status_set_is_an_allow_list():
    """Fail-closed. A deny-list would admit any status added later — and any
    status a corrupted row happens to hold."""
    from src.modules.identity.domain.entities import (
        AUTHENTICABLE_STATUSES,
        may_authenticate,
    )

    assert AUTHENTICABLE_STATUSES == frozenset({"active"})
    for denied in ("blocked", "pending", "deleted", "", "ACTIVE", None, "unknown"):
        assert not may_authenticate(denied), denied
    assert may_authenticate("active")


def test_the_aggregate_and_the_gate_share_one_predicate():
    """Two implementations of "is this account usable" would eventually
    disagree, and the one that drifts open is a lockout bypass."""
    from src.modules.identity.domain.entities import User, may_authenticate
    from src.shared_kernel.domain.value_objects import Mobile

    source = inspect.getsource(User.is_active.fget)
    assert "may_authenticate" in source

    for status in ("active", "blocked", "pending"):
        user = User(mobile=Mobile("+989121234567"), status=status)
        assert user.is_active is may_authenticate(status)


def test_refresh_cannot_mint_for_a_non_active_account():
    """The renewal path needs its own check: it issues tokens without ever
    passing through the request-time gate."""
    from src.modules.identity.application.use_cases import RefreshAccessToken

    source = inspect.getsource(RefreshAccessToken)
    assert "may_authenticate" in source
    assert "user_status" in inspect.signature(RefreshAccessToken.__init__).parameters


def test_reuse_detection_still_runs_before_the_account_check():
    """Ordering. A blocked user replaying a stolen token must still burn the
    family (D1); checking status first would turn them away with the compromised
    family left usable."""
    tree = ast.parse(
        (IDENTITY / "application" / "use_cases.py").read_text(encoding="utf-8")
    )
    cls = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "RefreshAccessToken"
    )
    execute = next(
        n for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "execute"
    )

    def first(pred) -> int | None:
        hits = [n.lineno for n in ast.walk(execute) if pred(n)]
        return min(hits) if hits else None

    revoke = first(
        lambda n: isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "revoke_family"
    )
    status_check = first(
        lambda n: isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "may_authenticate"
    )
    mint = first(
        lambda n: isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "create_access_token"
    )

    assert revoke is not None and status_check is not None and mint is not None
    assert revoke < status_check, "the account check pre-empts D1 reuse detection"
    assert status_check < mint, "a token is minted before the account is checked"


# --- revocation scope --------------------------------------------------------
def test_logout_derives_the_family_from_a_verified_token():
    """The family must come from a signed claim. Reading it from a request body
    would let any caller name any family — including someone else's."""
    from src.modules.identity.application.use_cases import Logout

    source = inspect.getsource(Logout.execute)
    assert "self._tokens.decode(" in source, "the token is not cryptographically verified"
    assert 'payload.get("family_id")' in source
    assert 'payload.get("type") != "refresh"' in source


def test_nothing_revokes_a_family_from_a_caller_supplied_value():
    """Every `revoke_family` argument must trace to a decoded token payload."""
    offenders = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "revoke_family"
            ):
                continue
            rendered = ast.unparse(node.args[0]) if node.args else "<none>"
            if not any(
                token in rendered for token in ("family", "payload")
            ) or "body" in rendered or "request" in rendered:
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}: {rendered}")
    assert offenders == [], offenders


def test_family_revocation_is_scoped_by_family_id():
    """An unfiltered UPDATE would log every user out at once — the kind of bug
    that only shows up in production."""
    from src.modules.identity.infrastructure.repository import SqlRefreshTokenStore

    source = inspect.getsource(SqlRefreshTokenStore.revoke_family)
    assert "RefreshTokenModel.family_id == family_id" in source
    assert "revoked_at.is_(None)" in source, "revocation is not idempotent"


def test_logout_writes_nothing_but_the_revocation():
    """Logout must not mint, must not touch status, roles or memberships."""
    from src.modules.identity.application.use_cases import Logout

    source = inspect.getsource(Logout)
    for forbidden in (
        "create_access_token", "create_refresh_token", "rotate", "save(",
        "status", "role", "membership", "seller",
    ):
        assert forbidden not in source, f"Logout references {forbidden!r}"


# --- the guards must be able to fail ----------------------------------------
@pytest.mark.parametrize(
    ("sample", "should_flag"),
    [
        ("body.family_id", True),
        ("request.json()['family_id']", True),
        ("_uuid.UUID(family)", False),
    ],
)
def test_the_revocation_guard_detects_what_it_claims_to(sample, should_flag):
    """A guard that cannot fail proves nothing."""
    flagged = (
        not any(t in sample for t in ("family", "payload"))
        or "body" in sample
        or "request" in sample
    )
    assert flagged is should_flag, sample
