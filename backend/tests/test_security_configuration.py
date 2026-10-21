"""Configuration and disclosure hardening — audit regression lock (DB-free).

Three findings, all confirmed against the running application:

  * `JWT_SECRET` and `OTP_PEPPER` fell back to published placeholders. A missing
    environment variable produced no error, and every token in the system became
    forgeable by anyone who had read this repository.
  * `ConsoleOtpSender` wrote the plaintext OTP and the unmasked mobile number to
    the log on every request, and it is the DEFAULT provider.
  * No security headers were sent at all.
"""

from __future__ import annotations

import logging

import pytest

from src.config.settings import Settings

SAFE_SECRET = "s" * 48
SAFE_PEPPER = "p" * 24


def production(**overrides) -> Settings:
    base = dict(
        environment="production", debug=False,
        jwt_secret=SAFE_SECRET, otp_pepper=SAFE_PEPPER,
        # Production refuses the console provider, so a well-formed production
        # config names a real one. Placeholder credentials: nothing is sent.
        sms_provider="kavenegar", kavenegar_api_key="test-key",
        kavenegar_otp_template="otp",
    )
    return Settings(**{**base, **overrides})


# ===========================================================================
# Insecure defaults must not reach production
# ===========================================================================
@pytest.mark.parametrize("environment", ["development", "test", "local"])
def test_development_still_boots_on_the_defaults(environment):
    """The guard must not make local development impossible — that is how guards
    get switched off.

    The defaults are passed explicitly: this repository's own .env supplies real
    secrets, so relying on the fallback would make the test pass for the wrong
    reason on one machine and fail on a fresh clone.
    """
    dev = Settings(
        environment=environment,
        jwt_secret=Settings.INSECURE_DEFAULTS["jwt_secret"],
        otp_pepper=Settings.INSECURE_DEFAULTS["otp_pepper"],
        debug=True,
    )
    assert dev.is_development
    assert dev.jwt_secret == Settings.INSECURE_DEFAULTS["jwt_secret"]


@pytest.mark.parametrize("environment", ["production", "staging", "prod", "PRODUCTION"])
@pytest.mark.parametrize("field", ["jwt_secret", "otp_pepper"])
def test_a_published_default_refuses_to_boot_outside_development(environment, field):
    """THE finding. Silent fallback turned a missing env var into total
    compromise; now it is a loud deployment failure instead."""
    with pytest.raises(ValueError) as exc:
        production(environment=environment, **{field: Settings.INSECURE_DEFAULTS[field]})
    assert field.upper() in str(exc.value)


def test_a_short_signing_key_is_refused():
    """HS256 with a short key is brute-forceable offline from one captured
    token; RFC 7518 §3.2 requires at least as many key bits as hash bits."""
    with pytest.raises(ValueError) as exc:
        production(jwt_secret="short-but-not-the-default")
    assert "JWT_SECRET" in str(exc.value)


def test_debug_is_refused_in_production():
    with pytest.raises(ValueError) as exc:
        production(debug=True)
    assert "DEBUG" in str(exc.value)


def test_otp_code_logging_is_refused_in_production():
    with pytest.raises(ValueError) as exc:
        production(otp_log_codes=True)
    assert "OTP_LOG_CODES" in str(exc.value)


def test_a_correctly_configured_production_boots():
    """The counterweight: a validator that rejects everything is not a validator."""
    cfg = production()
    assert not cfg.is_development
    assert cfg.jwt_secret == SAFE_SECRET


def test_every_error_is_reported_at_once():
    """A deployment fixing one variable per redeploy is a deployment that gives
    up and sets the environment to `development`."""
    with pytest.raises(ValueError) as exc:
        Settings(
            environment="production", debug=True,
            jwt_secret=Settings.INSECURE_DEFAULTS["jwt_secret"],
            otp_pepper=Settings.INSECURE_DEFAULTS["otp_pepper"],
        )
    message = str(exc.value)
    for expected in ("JWT_SECRET", "OTP_PEPPER", "DEBUG"):
        assert expected in message, message


# ===========================================================================
# OTP codes must not reach the log
# ===========================================================================
def test_the_console_sender_does_not_log_the_code_by_default(caplog, capsys):
    """THE finding. An OTP is a credential and the mobile is PII; both went to
    the log on every request, under the DEFAULT provider."""
    from src.modules.identity.infrastructure.sms_sender import ConsoleOtpSender
    from src.shared_kernel.domain.value_objects import Mobile

    import asyncio

    code = "424242"
    number = "+989121234567"
    with caplog.at_level(logging.DEBUG):
        asyncio.run(ConsoleOtpSender().send_code(Mobile(number), code))

    logged = caplog.text + capsys.readouterr().out
    assert code not in logged, "the OTP code reached the log"
    assert number not in logged, "the unmasked mobile reached the log"
    assert "dispatched" in logged, "delivery is no longer observable at all"


def test_the_masked_number_is_still_logged():
    """Debuggability must survive the fix, or the fix gets reverted."""
    from src.shared_kernel.domain.value_objects import Mobile

    masked = Mobile("+989121234567").masked
    assert "989121234567" not in masked
    assert masked  # non-empty


def test_code_logging_requires_an_explicit_opt_in():
    """Structural: the only path that prints a code is behind the setting that
    production refuses."""
    import inspect

    from src.modules.identity.infrastructure.sms_sender import ConsoleOtpSender

    source = inspect.getsource(ConsoleOtpSender.send_code)
    assert "settings.otp_log_codes" in source
    body_after_guard = source.split("settings.otp_log_codes", 1)[1]
    assert "code" in body_after_guard.split("return", 1)[0]


# ===========================================================================
# Transport / response hardening
# ===========================================================================
def get(url: str, **kw):
    """One request against the real app, on its own event loop.

    Deliberately not `fastapi.testclient.TestClient`: that starts its own loop
    and runs the lifespan, which in a full-suite run collides with the shared
    loop tests/integration owns and closes the shared Redis client out from
    under it. ASGITransport exercises the same middleware stack without either
    side effect.
    """
    import asyncio

    import httpx

    from src.api.main import app

    async def _go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://testserver",
        ) as client:
            return await client.get(url, **kw)

    return asyncio.run(_go())


def test_cors_never_combines_a_wildcard_origin_with_credentials():
    """`allow_origins=["*"]` plus `allow_credentials=True` would let any site
    read authenticated responses. Starlette ignores that combination silently,
    so the mistake would look like it worked."""
    from src.config.settings import settings

    assert "*" not in settings.cors_origins


def test_the_app_refuses_to_build_with_a_wildcard_cors_origin(monkeypatch):
    from src.api import main as main_module

    monkeypatch.setattr(main_module.settings, "cors_origins", ["*"], raising=False)
    with pytest.raises(RuntimeError, match="allow_origins"):
        main_module.create_app()


def test_security_headers_are_applied():
    resp = get("/health")

    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["cross-origin-resource-policy"] == "same-site"


def test_hsts_is_not_sent_in_development():
    """Pinning a developer's browser to HTTPS for localhost breaks local work and
    is not undone by removing the header later."""
    assert "strict-transport-security" not in get("/health").headers


def test_health_does_not_disclose_the_environment():
    """An unauthenticated endpoint naming the deployment tells an attacker which
    target they reached, and which defaults might still be in place."""
    body = get("/health").json()

    assert body["status"] == "ok"
    assert "env" not in body
    assert "environment" not in body


# ===========================================================================
# Public search input — availability, not injection
# ===========================================================================
def test_like_metacharacters_are_escaped():
    """`q` is a bound parameter, so this was never SQL injection — but `%` and
    `_` are wildcards to the DATABASE. A public, unauthenticated `q=%` matched
    every active product and scanned the table.

    Escaping also gives the endpoint the semantics a search box should have: a
    shopper typing "50%" wants those characters, not a wildcard.

    Raw strings throughout: these assertions are entirely about backslashes, and
    ordinary escapes make them unreadable and easy to get subtly wrong.
    """
    from src.modules.catalog.infrastructure.repository import _escape_like

    assert _escape_like("50%") == r"50\%"
    assert _escape_like("a_b") == r"a\_b"
    assert _escape_like("plain search") == "plain search"
    # The backslash must be escaped FIRST, or it would escape the escapes.
    assert _escape_like("c" + chr(92) + "d") == r"c\\d"
    assert _escape_like("%_" + chr(92)) == r"\%\_\\"


def test_the_search_query_is_length_bounded():
    """An unbounded `q` on a public endpoint is a free amplification primitive:
    cheap to send, expensive to match against every row."""
    import inspect

    from annotated_types import MaxLen

    from src.modules.catalog.interface.router import list_products

    def max_len(name: str) -> int | None:
        param = inspect.signature(list_products).parameters[name].default
        return next(
            (m.max_length for m in getattr(param, "metadata", []) if isinstance(m, MaxLen)),
            None,
        )

    assert max_len("q") == 100, "the search term is not bounded"
    for name in ("type", "category", "brand"):
        assert max_len(name) is not None, f"{name} is unbounded"


def test_a_wildcard_search_is_treated_as_a_literal():
    """End to end through the real query builder: `%` must reach the database
    escaped, not as a wildcard."""
    from src.modules.catalog.infrastructure.repository import CatalogReadRepository

    stmt = CatalogReadRepository(None)._base_filter(None, None, None, "%")
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))

    assert "ESCAPE" in compiled.upper(), "no LIKE escape clause was emitted"
    # The user's `%` survives as an escaped literal, not as a wildcard.
    assert r"\%" in compiled, compiled
