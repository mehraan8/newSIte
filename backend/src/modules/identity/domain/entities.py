"""Identity domain model. Pure Python."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import cast

from src.modules.identity.domain.exceptions import UserBlockedError
from src.shared_kernel.domain.base import AggregateRoot, DomainEvent, new_uuid, utcnow
from src.shared_kernel.domain.value_objects import Mobile


# --- Domain events ---------------------------------------------------------
@dataclass(frozen=True)
class UserRegistered(DomainEvent):
    user_id: uuid.UUID | None = None
    mobile: str = ""


@dataclass(frozen=True)
class UserLoggedIn(DomainEvent):
    user_id: uuid.UUID | None = None


# --- Aggregate -------------------------------------------------------------
class User(AggregateRoot):
    def __init__(
        self,
        mobile: Mobile,
        *,
        id: uuid.UUID | None = None,
        full_name: str | None = None,
        email: str | None = None,
        status: str = "active",
        is_phone_verified: bool = False,
    ) -> None:
        super().__init__()
        self.id = id or new_uuid()
        self.mobile = mobile
        self.full_name = full_name
        self.email = email
        self.status = status
        self.is_phone_verified = is_phone_verified

    @classmethod
    def register(cls, mobile: Mobile) -> "User":
        user = cls(mobile=mobile, is_phone_verified=True)
        # self.id may be typed loosely by base classes; cast to UUID for the event
        user.record_event(UserRegistered(user_id=cast(uuid.UUID, user.id), mobile=str(mobile)))
        return user

    def mark_logged_in(self) -> None:
        if self.status == "blocked":
            raise UserBlockedError()
        self.is_phone_verified = True
        # self.id may be typed loosely by base classes; cast to UUID for the event
        self.record_event(UserLoggedIn(user_id=cast(uuid.UUID, self.id)))

    @property
    def is_active(self) -> bool:
        return self.status == "active"
