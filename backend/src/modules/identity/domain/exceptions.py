"""Identity domain exceptions.

Pure-domain errors. They carry a Persian message but know nothing about HTTP or
the application layer — the application/interface layer maps them to transport
errors. This keeps the dependency rule intact: domain depends on nothing outward.
"""

from __future__ import annotations


class IdentityDomainError(Exception):
    """Base for Identity business-rule violations."""

    message_fa: str = "خطای دامنه‌ی هویت."

    def __init__(self, message_fa: str | None = None) -> None:
        if message_fa is not None:
            self.message_fa = message_fa
        super().__init__(self.message_fa)


class UserBlockedError(IdentityDomainError):
    """A blocked user attempted an action requiring an active account."""

    message_fa = "حساب کاربری شما مسدود شده است."
