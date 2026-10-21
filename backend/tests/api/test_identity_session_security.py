"""Account status and session revocation over real HTTP (Phase 1G.1).

Two controls that are easy to confuse, and are deliberately separate:

  * THE ACCOUNT GATE — a blocked user is refused on the next authenticated
    request, even holding a cryptographically valid, unexpired token. Enforced
    once, in `get_access_claims`, before Principal assembly.
  * LOGOUT — revokes the caller's refresh-token FAMILY, so the session cannot be
    renewed. It cannot un-issue an access token; nothing in a stateless-JWT
    architecture can.

Neither substitutes for the other, and this suite asserts that boundary rather
than blurring it: logout leaves the current access token working (proved below,
not glossed over), and the account gate is what stops a blocked user instantly.

Everything is real — the ASGI app, `JwtTokenService`, `refresh_tokens` rows,
PostgreSQL. Nothing about authentication is mocked.
"""

from __future__ import annotations

import uuid

import pytest

from tests.api.conftest import (
    auth,
    call,
    live_tokens,
    make_seller,
    make_user,
    refresh_session,
    seed_offer,
    seed_variant,
)
from tests.integration import db

pytestmark = pytest.mark.integration

ME = "/api/v1/auth/me"
REFRESH = "/api/v1/auth/token/refresh"
LOGOUT = "/api/v1/auth/logout"


def block(user_id: uuid.UUID, status: str = "blocked") -> None:
    db.execute("update users set status = :s where id = :i", s=status, i=user_id)


# ===========================================================================
# The account gate
# ===========================================================================
def test_a_an_active_user_is_admitted(client):
    """Baseline. Without this, every assertion below could pass because the
    endpoint was broken rather than because the gate worked."""
    user = make_user()

    resp = call(client, "GET", ME, headers=auth(user))

    assert resp.status_code == 200, resp.text
    assert resp.json()["user_id"] == str(user)


def test_b_blocking_takes_effect_on_the_very_next_request(client):
    """THE control. The token is unchanged, unexpired and validly signed — and
    it stops working the moment the account does.

    The same token is used before and after on purpose: if the fix had been a
    shorter TTL, or a claim baked into the token, this test could not pass.
    """
    user = make_user()
    headers = auth(user)
    assert call(client, "GET", ME, headers=headers).status_code == 200

    block(user)

    resp = call(client, "GET", ME, headers=headers)
    assert resp.status_code == 401, resp.text
    assert resp.json()["title"] == "unauthorized"


@pytest.mark.parametrize("status", ["blocked", "pending"])
def test_c_a_non_active_account_is_refused_everywhere(client, status):
    """The gate is one choke point, so it covers every authenticated route —
    including ones whose own authorization would otherwise answer 403.

    `pending` is included because the gate is an ALLOW-list: only `active`
    authenticates. A status the build does not recognise must confer nothing.
    """
    seller = make_seller()
    user = make_user()
    db.execute(
        "insert into seller_members (seller_id, user_id, member_role_code, status) "
        "values (:s, :u, 'seller_owner', 'active')",
        s=seller, u=user,
    )
    offer = seed_offer(seller, seed_variant(), status="draft", amount=1000)
    headers = auth(user)
    block(user, status)

    for method, url, body in (
        ("GET", ME, None),
        ("POST", f"/api/v1/selling/offers/{offer}/suspend", None),
        ("POST", "/api/v1/orders", {"lines": [{"offer_id": str(offer), "quantity": 1}]}),
    ):
        resp = call(client, method, url, headers=headers, json=body)
        assert resp.status_code == 401, (
            f"{method} {url} answered {resp.status_code} for a {status} account; "
            f"expected 401. Body: {resp.text[:160]}"
        )

    # 401, never 403: this is a failure to authenticate, not a missing
    # permission — and the caller here holds owner authority over that offer.
    assert db.scalar("select status from offers where id = :i", i=offer) == "draft"


def test_d_reactivating_restores_access_with_the_same_token(client):
    """The gate must be a gate, not a one-way door. A support agent unblocking an
    account has to work without forcing the user to log in again."""
    user = make_user()
    headers = auth(user)

    block(user)
    assert call(client, "GET", ME, headers=headers).status_code == 401

    block(user, "active")
    assert call(client, "GET", ME, headers=headers).status_code == 200


def test_e_a_blocked_user_cannot_mint_a_new_access_token(client):
    """The bypass that made account lockout cosmetic.

    Before this phase, refresh never consulted `users.status`: a blocked user
    could renew indefinitely, and each renewal produced a token that passed every
    check. Blocking `/auth/me` alone would have left the hole wide open.
    """
    user = make_user()
    _family, raw_refresh = refresh_session(user)
    assert call(client, "POST", REFRESH, json={"refresh_token": raw_refresh}).status_code == 200

    _family2, raw_refresh2 = refresh_session(user)
    block(user)

    resp = call(client, "POST", REFRESH, json={"refresh_token": raw_refresh2})
    assert resp.status_code == 401, resp.text
    assert "access_token" not in resp.text


def test_the_gate_runs_before_authorization_work(client):
    """A blocked caller must not reach membership resolution at all.

    Structural: `get_principal` depends on `get_access_claims`, so the account
    check is upstream of Principal assembly by construction, not by convention.
    """
    import inspect

    from src.api.dependencies import get_principal
    from src.modules.identity.interface.dependencies import get_access_claims

    depends_on = [
        param.default.dependency
        for param in inspect.signature(get_principal).parameters.values()
        if getattr(param.default, "dependency", None) is not None
    ]
    assert get_access_claims in depends_on


def test_a_token_for_a_deleted_user_stops_working(client):
    """Fail-closed on a missing row: no user, no status, no authority."""
    user = make_user()
    headers = auth(user)
    assert call(client, "GET", ME, headers=headers).status_code == 200

    db.execute("delete from users where id = :i", i=user)

    assert call(client, "GET", ME, headers=headers).status_code == 401


# ===========================================================================
# Logout
# ===========================================================================
def test_logout_revokes_the_family_and_refresh_then_fails(client):
    user = make_user()
    family, raw_refresh = refresh_session(user)
    assert live_tokens(family) == 1

    assert call(client, "POST", LOGOUT, json={"refresh_token": raw_refresh}).status_code == 204

    assert live_tokens(family) == 0
    assert call(client, "POST", REFRESH,
                json={"refresh_token": raw_refresh}).status_code == 401


def test_logout_is_idempotent(client):
    """A client retrying after a dropped response must not be told its own
    already-ended session is an error."""
    user = make_user()
    _family, raw_refresh = refresh_session(user)

    first = call(client, "POST", LOGOUT, json={"refresh_token": raw_refresh})
    second = call(client, "POST", LOGOUT, json={"refresh_token": raw_refresh})

    assert first.status_code == 204, first.text
    assert second.status_code == 204, second.text


def test_logout_revokes_only_the_presented_family(client):
    """Cross-user isolation. The family comes from the token's SIGNED claims, so
    there is no field a caller could point at someone else's session."""
    victim, attacker = make_user(), make_user()
    victim_family, _victim_refresh = refresh_session(victim)
    _attacker_family, attacker_refresh = refresh_session(attacker)

    assert call(client, "POST", LOGOUT,
                json={"refresh_token": attacker_refresh}).status_code == 204

    assert live_tokens(victim_family) == 1, "another user's family was revoked"


def test_logout_does_not_revoke_a_second_session_of_the_same_user(client):
    """Two devices, two families. Logging out of one must not sign the other out."""
    user = make_user()
    phone_family, phone_refresh = refresh_session(user)
    laptop_family, _laptop_refresh = refresh_session(user)

    call(client, "POST", LOGOUT, json={"refresh_token": phone_refresh})

    assert live_tokens(phone_family) == 0
    assert live_tokens(laptop_family) == 1


def test_logout_mints_nothing_and_changes_no_authorization_state(client):
    """Revocation only. Logout must not touch status, roles or memberships."""
    seller = make_seller()
    user = make_user()
    db.execute(
        "insert into seller_members (seller_id, user_id, member_role_code, status) "
        "values (:s, :u, 'seller_owner', 'active')",
        s=seller, u=user,
    )
    _family, raw_refresh = refresh_session(user)

    resp = call(client, "POST", LOGOUT, json={"refresh_token": raw_refresh})

    assert resp.status_code == 204
    assert resp.text == ""
    assert db.scalar("select status from users where id = :i", i=user) == "active"
    assert db.scalar("select count(*) from seller_members where user_id = :u", u=user) == 1
    assert db.scalar("select count(*) from user_roles where user_id = :u", u=user) == 0


def test_logout_does_not_invalidate_an_already_issued_access_token(client):
    """Stated honestly rather than claimed away.

    Access tokens are stateless and unrevocable in this architecture. Logout ends
    the ability to RENEW; the outstanding access token remains valid until it
    expires. Immediate account-level enforcement is the account gate's job, which
    is exactly why the two controls are separate.
    """
    user = make_user()
    headers = auth(user)
    _family, raw_refresh = refresh_session(user)

    call(client, "POST", LOGOUT, json={"refresh_token": raw_refresh})

    assert call(client, "GET", ME, headers=headers).status_code == 200
    assert call(client, "POST", REFRESH,
                json={"refresh_token": raw_refresh}).status_code == 401

    # ...and blocking the account DOES stop it at once. The pairing is the point.
    block(user)
    assert call(client, "GET", ME, headers=headers).status_code == 401


@pytest.mark.parametrize("bad", ["not-a-token", "", "x.y.z"])
def test_logout_refuses_a_credential_it_cannot_verify(client, bad):
    resp = call(client, "POST", LOGOUT, json={"refresh_token": bad})
    assert resp.status_code == 401, resp.text


def test_logout_refuses_an_access_token(client):
    """Type confusion: an access token is signed by the same key and would decode
    cleanly. Only `type == "refresh"` may revoke a family."""
    from tests.api.conftest import access_token

    user = make_user()
    resp = call(client, "POST", LOGOUT, json={"refresh_token": access_token(user)})
    assert resp.status_code == 401, resp.text


def test_a_blocked_user_can_still_end_their_own_session(client):
    """Logout is not an authenticated action, and burning a session is never
    something to refuse — least of all for an account already in trouble."""
    user = make_user()
    family, raw_refresh = refresh_session(user)
    block(user)

    assert call(client, "POST", LOGOUT,
                json={"refresh_token": raw_refresh}).status_code == 204
    assert live_tokens(family) == 0


# ===========================================================================
# Refresh rotation and D1 reuse detection — must not regress
# ===========================================================================
def test_rotation_consumes_the_presented_token(client):
    user = make_user()
    family, first = refresh_session(user)

    rotated = call(client, "POST", REFRESH, json={"refresh_token": first})
    assert rotated.status_code == 200, rotated.text
    second = rotated.json()["refresh_token"]

    assert call(client, "POST", REFRESH, json={"refresh_token": second}).status_code == 200
    assert live_tokens(family) >= 1


def test_d1_replaying_a_consumed_token_burns_the_whole_family(client):
    """D1 regression lock. A presented-but-unknown token means the family may be
    compromised, so the whole family dies — and the revocation must PERSIST past
    the 401, which is the bug that made this a Critical."""
    user = make_user()
    family, first = refresh_session(user)

    rotated = call(client, "POST", REFRESH, json={"refresh_token": first})
    assert rotated.status_code == 200
    live_sibling = rotated.json()["refresh_token"]
    assert live_tokens(family) == 1

    replay = call(client, "POST", REFRESH, json={"refresh_token": first})

    assert replay.status_code == 401, replay.text
    assert live_tokens(family) == 0, "family revocation did not persist past the 401"
    assert call(client, "POST", REFRESH,
                json={"refresh_token": live_sibling}).status_code == 401


def test_the_account_gate_does_not_disturb_reuse_detection(client):
    """Ordering: reuse detection runs BEFORE the status check, so a blocked user
    replaying a stolen token still burns the family rather than being turned away
    with the family left usable."""
    user = make_user()
    family, first = refresh_session(user)
    rotated = call(client, "POST", REFRESH, json={"refresh_token": first})
    assert rotated.status_code == 200

    block(user)
    replay = call(client, "POST", REFRESH, json={"refresh_token": first})

    assert replay.status_code == 401
    assert live_tokens(family) == 0


# ===========================================================================
# Concurrent rotation — a single-use token must be spendable exactly once
# ===========================================================================
def race_refresh(raw_refresh: str, n: int) -> list[int]:
    """Fire `n` refreshes of the SAME token simultaneously; return the statuses.

    Uses its own client on the suite's shared loop so the requests genuinely
    overlap — a sequential loop would pass even with the bug, because each call
    would see the previous one's commit.
    """
    import asyncio

    import httpx

    from src.api.main import app

    async def _go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://testserver",
        ) as c:
            responses = await asyncio.gather(*[
                c.post(REFRESH, json={"refresh_token": raw_refresh}) for _ in range(n)
            ])
        return [r.status_code for r in responses]

    return db.run(_go())


@pytest.mark.parametrize("concurrency", [2, 5, 10])
def test_a_refresh_token_can_only_be_spent_once_under_concurrency(client, concurrency):
    """THE race. Ten parallel refreshes of one token used to return ten valid
    tokens, because rotation read the row, then wrote it.

    The damage is not the duplication. Reuse detection fires when a token that is
    no longer in the table is presented again — under the race a thief and the
    victim each receive a DIFFERENT valid token, so neither ever replays a spent
    one and the theft is never detected. The race disabled the alarm.
    """
    user = make_user()
    family, raw_refresh = refresh_session(user)

    statuses = race_refresh(raw_refresh, concurrency)

    assert statuses.count(200) == 1, (
        f"a single-use token was spent {statuses.count(200)} times: {statuses}"
    )
    assert set(statuses) == {200, 401} if concurrency > 1 else {200}


def test_losing_the_rotation_race_is_treated_as_reuse(client):
    """A token consumed twice is indistinguishable from a replayed one, so it
    gets the same response: burn the family.

    Two honest clients racing a refresh are logged out, which is the safe
    direction. The alternative is the behaviour above, where a stolen session
    survives silently.
    """
    user = make_user()
    family, raw_refresh = refresh_session(user)

    statuses = race_refresh(raw_refresh, 5)

    assert 401 in statuses
    assert live_tokens(family) == 0, (
        "the family survived a double-spend; reuse response did not fire"
    )


def test_sequential_rotation_is_unaffected(client):
    """The counterweight: normal, one-at-a-time refresh must still work. A fix
    that made every rotation fail would also pass the race tests."""
    user = make_user()
    _family, token = refresh_session(user)

    for _ in range(3):
        resp = call(client, "POST", REFRESH, json={"refresh_token": token})
        assert resp.status_code == 200, resp.text
        token = resp.json()["refresh_token"]


def test_rotation_claims_the_token_before_minting_an_access_token():
    """Ordering, structurally. Minting first and rotating afterwards would hand
    back a working access token on a request that then failed to claim."""
    import inspect

    from src.modules.identity.application.use_cases import RefreshAccessToken

    source = inspect.getsource(RefreshAccessToken.execute)
    assert source.index("self._refresh.rotate(") < source.index("create_access_token"), (
        "an access token is minted before the refresh token is claimed"
    )
