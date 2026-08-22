"""OTP delivery adapters. The console sender is for local dev; production
swaps in a provider adapter (Kavenegar / SMS.ir) behind the same port.
"""

from __future__ import annotations

import logging

from src.config.settings import settings
from src.shared_kernel.domain.value_objects import Mobile

logger = logging.getLogger("identity.sms")


class ConsoleOtpSender:
    """Stands in for an SMS provider in development.

    It used to log the plaintext code AND the unmasked number on every request.
    An OTP is a credential and a mobile number is PII, so that put both into the
    application log — and in a container, straight into whatever aggregates
    stdout. Because `sms_provider` defaults to "console", a deployment that
    simply forgot to configure a provider logged every OTP it issued.

    Now the code is emitted only when `OTP_LOG_CODES` is explicitly set, which
    `Settings` refuses outside development. The default path records that a code
    was sent, to a MASKED number, and nothing else — enough to debug delivery,
    useless to an attacker reading logs.
    """

    async def send_code(self, mobile: Mobile, code: str) -> None:
        if settings.otp_log_codes:
            # Development convenience, gated and never reachable in production.
            logger.warning("OTP for %s -> %s  (OTP_LOG_CODES is on)", mobile.masked, code)
            print(f"[OTP] {mobile.masked} : {code}")  # noqa: T201
            return
        logger.info("OTP dispatched to %s", mobile.masked)


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
