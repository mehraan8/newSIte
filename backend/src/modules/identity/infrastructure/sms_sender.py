"""OTP delivery adapters. The console sender is for local dev; production
swaps in a provider adapter (Kavenegar / SMS.ir) behind the same port.
"""

from __future__ import annotations

import logging

from src.shared_kernel.domain.value_objects import Mobile

logger = logging.getLogger("identity.sms")


class ConsoleOtpSender:
    """Logs the code instead of sending an SMS — dev/test only."""

    async def send_code(self, mobile: Mobile, code: str) -> None:
        logger.info("OTP for %s -> %s", mobile.masked, code)
        print(f"[OTP] {mobile} : {code}")  # noqa: T201 - intentional dev output


class KavenegarOtpSender:
    """Skeleton for the real provider. Inject an HTTP client + template id.

    The actual HTTP call is intentionally left as a TODO so no live credentials
    or network calls hide in the codebase until configured.
    """

    def __init__(self, api_key: str, template: str) -> None:
        self._api_key = api_key
        self._template = template

    async def send_code(self, mobile: Mobile, code: str) -> None:  # pragma: no cover
        raise NotImplementedError("Wire Kavenegar verify/lookup API here.")
