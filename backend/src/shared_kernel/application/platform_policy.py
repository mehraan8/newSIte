"""Platform-scoped authorization for use cases (ADR-0011 §3, layer 4).

The counterpart to `src/api/guards.py`. That module is layer 3 — a router
dependency, a floor. This one is layer 4: the check the *use case* makes, so the
business boundary holds no matter who calls it.

Both are needed, and they are not redundant. A router guard protects the HTTP
route; it protects nothing when a use case is invoked from a consumer, a CLI, a
test, or a future gRPC surface. The router says "this request may proceed"; the
use case says "this operation may happen".

Deliberately in the Shared Kernel, not in a bounded context: Catalog and
Inventory both need it and must not import each other. It depends only on
`Principal` and `Permission`, which is why it can live here — the Shared Kernel
imports no context (ADR-0011 §4).

PLATFORM ONLY. `Principal.can()` reads `platform_permissions`, which is populated
from `user_roles` alone. A seller membership can never satisfy it, so this
function cannot be used — accidentally or otherwise — to let seller authority act
as platform authority. Seller-scoped decisions belong in the owning context's
policy (`selling/application/policies.py`,
`inventory/application/policies.py`), where the resource's owner is known.

PURE. Synchronous, deterministic, no I/O of any kind. Every fact it needs was
resolved once, during Principal assembly. A policy that *can* query is a policy
that eventually *will*, and per-request authorization I/O is what Phase 1F
exists to have removed.
"""

from __future__ import annotations

from src.shared_kernel.application.errors import ForbiddenError
from src.shared_kernel.application.permissions import Permission
from src.shared_kernel.application.security import Principal


def ensure_can_platform(principal: Principal, *permissions: Permission) -> None:
    """Raise unless the principal holds EVERY listed platform permission.

    Variadic because some operations legitimately need two — editing a product's
    attributes *and* publishing it in one request needs both
    `catalog.products.update` and `catalog.products.publish`. Requiring all of
    them, rather than any, is the conservative reading and the one that keeps the
    permissions genuinely independent.

    The denial is identical for every failing permission and names none of them.
    Reporting *which* permission was missing would hand an attacker a map of the
    authorization model, one request at a time.
    """
    if not permissions:  # pragma: no cover - programming error
        raise ValueError("ensure_can_platform() needs at least one permission")

    if not all(principal.can(permission) for permission in permissions):
        raise ForbiddenError("شما مجاز به انجام این عملیات نیستید.")
