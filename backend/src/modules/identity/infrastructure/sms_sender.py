"""OTP delivery adapters behind the `OtpSender` port.

`build_otp_sender` is the only place a provider is chosen. `Settings` has
already refused an unknown provider, a Kavenegar setup without credentials, and
`console` outside development, so this function never has to guess.
"""

from __future__ import annotations

import logging
from typing import NoReturn

import httpx

from src.config.settings import Settings, settings
from src.modules.identity.application.ports import OtpDeliveryError, OtpSender
from src.shared_kernel.domain.value_objects import Mobile

logger = logging.getLogger("identity.sms")

# httpx logs every request URL at INFO. Kavenegar puts the API key in the URL
# path, so that line would write the key to the application log on every OTP.
logging.getLogger("httpx").setLevel(logging.WARNING)


class ConsoleOtpSender:
    """Stands in for an SMS provider in development and tests. Delivers nothing.

    It used to log the plaintext code AND the unmasked number on every request.
    An OTP is a credential and a mobile number is PII, so that put both into the
    application log — and in a container, straight into whatever aggregates
    stdout. Because `sms_provider` defaults to "console", a deployment that
    simply forgot to configure a provider logged every OTP it issued.

    Now the code is emitted only when `OTP_LOG_CODES` is explicitly set, which
    `Settings` refuses outside development. The default path records that a code
    was sent, to a MASKED number, and nothing else — enough to debug delivery,
    useless to an attacker reading logs. `Settings` also refuses this provider
    outside development, so production cannot fall back to it.
    """

    async def send_code(self, mobile: Mobile, code: str) -> None:
        if settings.otp_log_codes:
            # Development convenience, gated and never reachable in production.
            logger.warning("OTP for %s -> %s  (OTP_LOG_CODES is on)", mobile.masked, code)
            print(f"[OTP] {mobile.masked} : {code}")  # noqa: T201
            return
        logger.info("OTP dispatched to %s", mobile.masked)


class KavenegarOtpSender:
    """Kavenegar Verify Lookup: the code is sent as `token` into a template
    created in the Kavenegar panel, which holds the message text.

    Secrets stay out of every error and log line. Kavenegar authenticates by
    putting the API key in the URL PATH, so neither the URL, nor httpx's
    exception text (which can include it), nor the response body is ever
    logged or attached to the raised error. The code travels in the POST body,
    never in the URL.

    `transport` exists so tests can use `httpx.MockTransport`; production passes
    nothing and gets a real network transport.
    """

    def __init__(
        self,
        *,
        api_key: str,
        template: str,
        base_url: str = "https://api.kavenegar.com",
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key or not template:
            raise ValueError("Kavenegar needs an API key and an OTP template")
        self._api_key = api_key
        self._template = template
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport

    @staticmethod
    def receptor(mobile: Mobile) -> str:
        """Kavenegar's local form: +989121234567 -> 09121234567."""
        return "0" + str(mobile)[3:]

    async def send_code(self, mobile: Mobile, code: str) -> None:
        url = f"{self._base_url}/v1/{self._api_key}/verify/lookup.json"
        data = {"receptor": self.receptor(mobile), "token": code, "template": self._template}
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(url, data=data)
        except httpx.TimeoutException:
            self._fail(mobile, "timeout")
        except httpx.HTTPError as exc:
            # The exception TYPE only: its message may contain the URL, and with
            # it the API key. `from None` keeps it out of any chained traceback.
            self._fail(mobile, f"transport:{type(exc).__name__}")

        status = self._provider_status(response)
        if response.status_code != 200 or status != 200:
            self._fail(mobile, f"rejected:http={response.status_code},status={status}")
        logger.info("OTP dispatched to %s via kavenegar", mobile.masked)

    @staticmethod
    def _provider_status(response: httpx.Response) -> int | None:
        """`return.status` from Kavenegar's envelope; None if the body is not one."""
        try:
            status = response.json()["return"]["status"]
        except (ValueError, KeyError, TypeError):
            return None
        return status if isinstance(status, int) else None

    @staticmethod
    def _fail(mobile: Mobile, reason: str) -> NoReturn:
        logger.warning("OTP delivery to %s failed via kavenegar (%s)", mobile.masked, reason)
        raise OtpDeliveryError(reason) from None


def build_otp_sender(cfg: Settings = settings) -> OtpSender:
    """The composition-root choice of provider, driven only by configuration."""
    if cfg.sms_provider == "kavenegar":
        return KavenegarOtpSender(
            api_key=cfg.kavenegar_api_key.get_secret_value(),
            template=cfg.kavenegar_otp_template,
            base_url=cfg.kavenegar_base_url,
            timeout_seconds=cfg.sms_timeout_seconds,
        )
    if cfg.sms_provider == "console":
        return ConsoleOtpSender()
    # Unreachable while Settings validates the name; refuse rather than guess.
    raise ValueError(f"unsupported SMS_PROVIDER {cfg.sms_provider!r}")
