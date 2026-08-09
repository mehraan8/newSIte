"""Identity context Unit of Work interface.

Exposes only Identity's repositories, so an Identity use case cannot reach into
another context's data — preserving module boundaries. Cross-context effects go
through events/outbox, never through a shared UoW.
"""

from __future__ import annotations

import abc

from src.modules.identity.application.ports import (
    PlatformRoleQuery,
    RefreshTokenStore,
    UserRepository,
)
from src.shared_kernel.application.unit_of_work import AbstractUnitOfWork


class IdentityUnitOfWork(AbstractUnitOfWork, abc.ABC):
    # Declared for typing; bound by the concrete implementation at enter time.
    users: UserRepository
    refresh_tokens: RefreshTokenStore
    # Read inside the login transaction so the roles stamped into the token are
    # consistent with the grants as they were when the user was authenticated.
    platform_roles: PlatformRoleQuery
