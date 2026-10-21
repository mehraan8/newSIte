"""OTP delivery — provider selection, configuration, the Kavenegar adapter and
RequestOtp's behaviour when delivery fails (DB-free, network-free).

No real Kavenegar account is involved: the adapter is driven through
`httpx.MockTransport`. These tests prove the request we WOULD send and how we
react to the replies Kavenegar documents. They do not prove that a real message
reaches a phone — that needs real credentials and a manual check.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest
from fastapi import FastAPI

from src.api.middleware.error_handlers import register_error_handlers
from src.config.settings import Settings
from src.modules.identity.application.ports import OtpChallenge, OtpDeliveryError
from src.modules.identity.application.use_cases import RequestOtp, VerifyOtp, _hash_code
from src.modules.identity.infrastructure.sms_sender import (
    ConsoleOtpSender,
    KavenegarOtpSender,
    build_otp_sender,
)
from src.shared_kernel.application.errors import UnauthorizedError
from src.shared_kernel.domain.value_objects import Mobile

API_KEY = "SECRET-API-KEY-0123456789"
TEMPLATE = "otp-login"
NUMBER = "+989121234567"
CODE = "424242"


def run(coro):
    return asyncio.run(coro)


def dev(**overrides) -> Settings:
    return Settings(**{"environment": "development", **overrides})


def production(**overrides) -> Settings:
    base = dict(
        environment="production", debug=False,
        jwt_secret="s" * 48, otp_pepper="p" * 24,
        sms_provider="kavenegar", kavenegar_api_key=API_KEY,
        kavenegar_otp_template=TEMPLATE,
    )
    return Settings(**{**base, **overrides})


def kavenegar(handler) -> KavenegarOtpSender:
    return KavenegarOtpSender(
        api_key=API_KEY, template=TEMPLATE, transport=httpx.MockTransport(handler),
    )


def ok_reply(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"return": {"status": 200, "message": "تایید شد"},
                                     "entries": [{"messageid": 1}]})


# ===========================================================================
# Configuration
# ===========================================================================
@pytest.mark.parametrize("provider", ["smsir", "twilio", "consol", ""])
def test_an_unknown_provider_refuses_to_boot(provider):
    """It used to fall back to console silently — in production, that meant no
    code was ever delivered and nothing said so."""
    with pytest.raises(ValueError) as exc:
        dev(sms_provider=provider)
    assert "SMS_PROVIDER" in str(exc.value)


@pytest.mark.parametrize(
    "overrides, missing",
    [
        ({}, "KAVENEGAR_API_KEY"),
        ({"kavenegar_api_key": API_KEY}, "KAVENEGAR_OTP_TEMPLATE"),
        ({"kavenegar_otp_template": TEMPLATE}, "KAVENEGAR_API_KEY"),
        ({"kavenegar_api_key": "  ", "kavenegar_otp_template": TEMPLATE}, "KAVENEGAR_API_KEY"),
    ],
)
def test_kavenegar_without_credentials_refuses_to_boot_even_in_development(overrides, missing):
    with pytest.raises(ValueError) as exc:
        dev(sms_provider="kavenegar", **overrides)
    assert missing in str(exc.value)


@pytest.mark.parametrize("environment", ["production", "staging", "PRODUCTION"])
def test_console_delivery_is_refused_outside_development(environment):
    """THE requirement: production cannot silently fall back to console."""
    with pytest.raises(ValueError) as exc:
        production(environment=environment, sms_provider="console")
    assert "SMS_PROVIDER" in str(exc.value)


@pytest.mark.parametrize("environment", ["development", "test", "local"])
def test_console_delivery_is_still_allowed_for_development_and_tests(environment):
    cfg = dev(environment=environment, sms_provider="console")
    assert cfg.sms_provider == "console"


def test_production_with_kavenegar_boots():
    """The counterweight."""
    assert production().sms_provider == "kavenegar"


def test_the_provider_name_is_case_insensitive():
    cfg = production(sms_provider=" Kavenegar ")
    assert cfg.sms_provider == "kavenegar"


def test_the_api_key_never_appears_in_a_settings_dump():
    cfg = production()
    assert API_KEY not in repr(cfg)
    assert API_KEY not in str(cfg.model_dump())
    assert cfg.kavenegar_api_key.get_secret_value() == API_KEY


def test_a_refused_boot_does_not_echo_secrets():
    with pytest.raises(ValueError) as exc:
        production(sms_provider="console")
    assert API_KEY not in str(exc.value)
    assert "s" * 48 not in str(exc.value)


def test_a_non_positive_timeout_is_refused():
    with pytest.raises(ValueError):
        dev(sms_timeout_seconds=0)


# ===========================================================================
# Provider selection
# ===========================================================================
def test_console_is_selected_for_console():
    assert isinstance(build_otp_sender(dev(sms_provider="console")), ConsoleOtpSender)


def test_kavenegar_is_selected_and_configured_from_settings():
    sender = build_otp_sender(production(sms_timeout_seconds=2.5))
    assert isinstance(sender, KavenegarOtpSender)
    assert sender._api_key == API_KEY
    assert sender._template == TEMPLATE
    assert sender._timeout == 2.5


def test_selection_refuses_rather_than_guesses_on_an_unvalidated_name():
    """Settings normally makes this unreachable; the factory must not have its
    own silent default behind it."""
    cfg = Settings.model_construct(sms_provider="smsir")
    with pytest.raises(ValueError):
        build_otp_sender(cfg)


def test_the_adapter_refuses_empty_credentials():
    with pytest.raises(ValueError):
        KavenegarOtpSender(api_key="", template=TEMPLATE)
    with pytest.raises(ValueError):
        KavenegarOtpSender(api_key=API_KEY, template="")


# ===========================================================================
# Kavenegar adapter — the request
# ===========================================================================
def test_the_request_matches_the_verify_lookup_contract():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok_reply(request)

    run(kavenegar(handler).send_code(Mobile(NUMBER), CODE))

    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == f"https://api.kavenegar.com/v1/{API_KEY}/verify/lookup.json"
    form = dict(httpx.QueryParams(request.content.decode()))
    assert form == {"receptor": "09121234567", "token": CODE, "template": TEMPLATE}


def test_the_code_travels_in_the_body_never_the_url():
    """URLs end up in proxy and access logs; bodies do not."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok_reply(request)

    run(kavenegar(handler).send_code(Mobile(NUMBER), CODE))
    assert CODE not in str(seen[0].url)


def test_receptor_uses_the_local_form():
    assert KavenegarOtpSender.receptor(Mobile.parse("09351112233")) == "09351112233"


# ===========================================================================
# Kavenegar adapter — failures
# ===========================================================================
@pytest.mark.parametrize(
    "http_status, body",
    [
        (401, {"return": {"status": 401, "message": "حساب کاربری غیرفعال شده است"}}),
        (418, {"return": {"status": 418, "message": "اعتبار حساب شما کافی نیست"}}),
        (424, {"return": {"status": 424, "message": "الگوی مورد نظر پیدا نشد"}}),
        (200, {"return": {"status": 418, "message": "inconsistent envelope"}}),
        (500, {"unexpected": True}),
    ],
)
def test_a_provider_rejection_is_a_delivery_error(http_status, body):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(http_status, json=body)

    with pytest.raises(OtpDeliveryError) as exc:
        run(kavenegar(handler).send_code(Mobile(NUMBER), CODE))
    assert exc.value.http_status == 503
    assert "rejected" in exc.value.reason


def test_a_success_status_without_a_kavenegar_envelope_is_not_trusted():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>captive portal</html>")

    with pytest.raises(OtpDeliveryError):
        run(kavenegar(handler).send_code(Mobile(NUMBER), CODE))


def test_a_timeout_is_a_delivery_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(OtpDeliveryError) as exc:
        run(kavenegar(handler).send_code(Mobile(NUMBER), CODE))
    assert exc.value.reason == "timeout"


def test_a_transport_error_does_not_leak_the_url_or_the_code():
    """httpx exception text can contain the URL — and Kavenegar's URL contains
    the API key. Neither the raised error nor its chain may carry it."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url} token={CODE}", request=request)

    with pytest.raises(OtpDeliveryError) as exc:
        run(kavenegar(handler).send_code(Mobile(NUMBER), CODE))

    err = exc.value
    for text in (str(err), err.reason, err.message_fa, repr(err)):
        assert API_KEY not in text
        assert CODE not in text
    assert err.__cause__ is None
    assert err.__suppress_context__ is True


@pytest.mark.parametrize("fails", [False, True])
def test_logs_carry_no_key_no_code_and_no_full_number(caplog, fails):
    def handler(request: httpx.Request) -> httpx.Response:
        if fails:
            raise httpx.ConnectError(f"cannot reach {request.url}", request=request)
        return ok_reply(request)

    with caplog.at_level(logging.DEBUG):
        try:
            run(kavenegar(handler).send_code(Mobile(NUMBER), CODE))
        except OtpDeliveryError:
            pass

    assert caplog.records, "delivery is no longer observable at all"
    for text in (caplog.text, *(str(r.args) for r in caplog.records)):
        assert API_KEY not in text
        assert CODE not in text
        assert NUMBER not in text


def test_httpx_request_logging_is_silenced():
    """httpx logs each URL at INFO, and the URL holds the API key."""
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


# ===========================================================================
# RequestOtp / VerifyOtp when delivery fails
# ===========================================================================
class FakeOtpStore:
    def __init__(self) -> None:
        self.challenges: dict[str, OtpChallenge] = {}
        self.cooldowns: dict[str, int] = {}

    async def save(self, mobile, code_hash, ttl):
        self.challenges[str(mobile)] = OtpChallenge(code_hash, 0, ttl)

    async def get(self, mobile):
        return self.challenges.get(str(mobile))

    async def increment_attempts(self, mobile):
        ch = self.challenges[str(mobile)]
        ch.attempts += 1
        return ch.attempts

    async def delete(self, mobile):
        self.challenges.pop(str(mobile), None)

    async def seconds_until_resend(self, mobile):
        return self.cooldowns.get(str(mobile), 0)

    async def mark_sent(self, mobile, cooldown):
        self.cooldowns[str(mobile)] = cooldown


class RecordingSender:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[str, str]] = []

    async def send_code(self, mobile, code):
        self.sent.append((str(mobile), code))
        if self.fail:
            raise OtpDeliveryError("rejected:http=418,status=418")


def no_uow():
    raise AssertionError("verification must not reach the database")


def test_a_delivered_code_is_stored_hashed_and_verifiable():
    store, sender = FakeOtpStore(), RecordingSender()
    result = run(RequestOtp(store, sender).execute(NUMBER))

    (number, code), = sender.sent
    assert number == NUMBER
    assert store.challenges[NUMBER].code_hash == _hash_code(code)
    assert code not in store.challenges[NUMBER].code_hash
    assert code not in str(result.__dict__), "the code reached the API response"
    assert result.masked_mobile == Mobile(NUMBER).masked


def test_a_failed_delivery_leaves_no_verifiable_code():
    """The user never received it; it must not stay guessable."""
    store, sender = FakeOtpStore(), RecordingSender(fail=True)

    with pytest.raises(OtpDeliveryError):
        run(RequestOtp(store, sender).execute(NUMBER))
    assert NUMBER not in store.challenges


def test_a_failed_delivery_keeps_the_resend_cooldown():
    """Deliberate: without it a caller could hammer a failing provider."""
    store, sender = FakeOtpStore(), RecordingSender(fail=True)

    with pytest.raises(OtpDeliveryError):
        run(RequestOtp(store, sender).execute(NUMBER))
    assert store.cooldowns[NUMBER] > 0


def test_verifying_after_a_failed_delivery_is_unauthorized():
    store, sender = FakeOtpStore(), RecordingSender(fail=True)
    with pytest.raises(OtpDeliveryError):
        run(RequestOtp(store, sender).execute(NUMBER))
    (_, code), = sender.sent

    verify = VerifyOtp(otp_store=store, tokens=None, uow_factory=no_uow)
    with pytest.raises(UnauthorizedError):
        run(verify.execute(NUMBER, code))


def test_a_wrong_code_is_unauthorized_and_counts_an_attempt():
    store, sender = FakeOtpStore(), RecordingSender()
    run(RequestOtp(store, sender).execute(NUMBER))
    (_, code), = sender.sent
    wrong = "000000" if code != "000000" else "111111"

    verify = VerifyOtp(otp_store=store, tokens=None, uow_factory=no_uow)
    with pytest.raises(UnauthorizedError):
        run(verify.execute(NUMBER, wrong))
    assert store.challenges[NUMBER].attempts == 1


# ===========================================================================
# HTTP mapping
# ===========================================================================
def test_a_delivery_failure_is_a_503_with_a_generic_message():
    app = FastAPI()
    register_error_handlers(app)

    @app.post("/boom")
    async def boom():
        raise OtpDeliveryError(f"transport:ConnectError url=/v1/{API_KEY}/ token={CODE}")

    async def call() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.post("/boom")

    response = run(call())
    assert response.status_code == 503
    body = response.json()
    assert body["title"] == "otp_delivery_failed"
    assert body["detail"] is None
    assert body["message_fa"] == OtpDeliveryError.message_fa
    assert API_KEY not in response.text
    assert CODE not in response.text
    assert "transport" not in response.text, "the internal reason reached the client"
