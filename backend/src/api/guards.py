"""Route-level permission guards (ADR-0011 §3, layer 3).

The four-layer model is: authentication → Principal assembly → **router coarse
check** → application policy. This module is layer 3, and only layer 3.

What belongs here: "may this caller invoke this KIND of operation at all?", which
is answerable from the assembled `Principal` alone, before any resource is
loaded. `catalog.products.create` is such a question — there is no object yet.

What does NOT belong here: "may this caller act on THIS object?". That needs the
object, which the router has not loaded, so it is decided in the use case by a
policy (ADR-0011 §3.2). A guard here is a floor, never a substitute.

Platform permissions only. `Principal.can()` is deliberately not satisfied by a
seller membership, so a guard can never be accidentally passed by seller-scoped
authority — the two universes stay separate (ADR-0011 §7).
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Depends

from src.api.dependencies import get_principal
from src.shared_kernel.application.errors import ForbiddenError
from src.shared_kernel.application.permissions import Permission
from src.shared_kernel.application.security import Principal


def require_permission(
    *permissions: Permission,
) -> Callable[..., Coroutine[Any, Any, Principal]]:
    """A dependency that admits only callers holding EVERY listed permission.

    Returns the `Principal` so a route can reuse it without resolving twice.

    Failing closed is the whole point: the guard names the permissions it needs
    as `Permission` members, so a typo is an AttributeError at import time rather
    than a string that silently matches nothing and admits everyone. That is why
    the enum exists (ADR-0011 §5).

    403, not 404: the caller authenticated successfully and the route's existence
    is not a secret — these are documented API operations. Object-level denials,
    where existence CAN be sensitive, are the policy layer's call.
    """
    if not permissions:  # pragma: no cover - programming error
        raise ValueError("require_permission() needs at least one permission")

    async def guard(principal: Principal = Depends(get_principal)) -> Principal:
        if not all(principal.can(permission) for permission in permissions):
            raise ForbiddenError("شما مجاز به انجام این عملیات نیستید.")
        return principal

    # Aids debugging and makes the requirement visible in a stack trace.
    guard.__name__ = "require_" + "_".join(p.name.lower() for p in permissions)
    return guard
