"""Concrete Identity Unit of Work — wires Identity repositories to the session.

The repositories register loaded/added aggregates with this UoW (passed as the
`tracker`), so their domain events are collected at commit.
"""

from __future__ import annotations

from src.modules.identity.application.unit_of_work import IdentityUnitOfWork
from src.modules.identity.infrastructure.repository import (
    SqlRefreshTokenStore,
    SqlUserRepository,
)
from src.modules.identity.infrastructure.role_repository import SqlPlatformRoleQuery
from src.shared_kernel.infrastructure.unit_of_work import SqlAlchemyUnitOfWork


class SqlAlchemyIdentityUnitOfWork(SqlAlchemyUnitOfWork, IdentityUnitOfWork):
    def _build_repositories(self) -> None:
        # `self` is the Tracker; the User aggregate is event-bearing so it is
        # tracked. Refresh tokens are child entities (no events) — not tracked.
        self.users = SqlUserRepository(self.session, tracker=self)
        self.refresh_tokens = SqlRefreshTokenStore(self.session)
        # Role grants are read-only reference data — no aggregate, nothing to track.
        self.platform_roles = SqlPlatformRoleQuery(self.session)
