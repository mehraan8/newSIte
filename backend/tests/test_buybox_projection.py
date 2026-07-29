"""BuyBox projection tests (DB-free) — real ranking + real projector, fake repo.

Covers: lowest-price wins, active-only *including suspension*, created_at and
offer_id tie-breaks under either row order, region isolation, exact cross-scale
ranking, currency-mismatch exclusion, idempotency on a winner-changing event,
and replay-from-scratch equivalence.

Run: pytest tests/test_buybox_projection.py
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.modules.buybox.application.consumers import (
    BUYBOX_HANDLERS,
    CONSUMED_EVENT_TYPES,
    PROJECTOR_DISPATCH,
)
from src.modules.buybox.application.ports import VariantState
from src.modules.buybox.application.projector import BuyBoxProjector
from src.modules.buybox.application.ranking import OfferProjection
from src.shared_kernel.infrastructure.outbox.contracts import IncomingEvent

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
V = uuid.UUID("00000000-0000-0000-0000-0000000000ff")   # variant under test
S1 = uuid.UUID("00000000-0000-0000-0000-000000000001")  # sellers
S2 = uuid.UUID("00000000-0000-0000-0000-000000000002")
S3 = uuid.UUID("00000000-0000-0000-0000-000000000003")
# Fixed, ordered offer ids so offer_id tie-breaks are assertable, not random.
OFFER_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000000")
OFFER_B = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000000")
OFFER_C = uuid.UUID("cccccccc-0000-0000-0000-000000000000")
OFFER_D = uuid.UUID("dddddddd-0000-0000-0000-000000000000")


def run(coro):
    """Drive a coroutine from a sync pytest test — keeps the suite dependency-free."""
    return asyncio.run(coro)


# --- fake repository -------------------------------------------------------
class FakeRepo:
    """In-memory BuyBoxRepository double with SqlBuyBoxRepository's semantics.

    Reads return detached copies and writes store copies, so:
      * a handler that mutates a projection and forgets `upsert_offer` does NOT
        silently persist (the old fake handed out live objects, hiding that);
      * nothing a caller does to a returned object leaks back into the store.

    `row_order` models the fact that `list_for`'s SELECT has no ORDER BY — the
    ranking must not depend on it.
    """

    def __init__(self, *, row_order: str = "insertion") -> None:
        self._offers: dict[uuid.UUID, OfferProjection] = {}
        self._state: dict[tuple[uuid.UUID, str], VariantState] = {}
        self._row_order = row_order

    async def get_offer(self, offer_id):
        row = self._offers.get(offer_id)
        return replace(row) if row is not None else None

    async def upsert_offer(self, offer):
        self._offers[offer.offer_id] = replace(offer)

    async def list_for(self, variant_id, region):
        rows = [
            replace(o)
            for o in self._offers.values()
            if o.variant_id == variant_id and o.region == region
        ]
        if self._row_order == "reversed":
            rows.reverse()
        return rows

    async def set_variant_state(self, variant_id, region, winner, last_updated_at):
        self._state[(variant_id, region)] = VariantState(
            variant_id=variant_id, region=region,
            winning_offer_id=winner.offer_id if winner else None,
            seller_id=winner.seller_id if winner else None,
            amount=winner.amount if winner else None,
            currency=winner.currency if winner else None,
            scale=winner.scale if winner else None,
            last_updated_at=last_updated_at,
        )

    async def get_variant_state(self, variant_id, region):
        return self._state.get((variant_id, region))

    # -- test-only introspection --
    def offer_table(self) -> dict:
        return {oid: replace(o) for oid, o in self._offers.items()}

    def state_table(self) -> dict:
        return dict(self._state)


# --- event builders --------------------------------------------------------
def ev(etype, offer_id, payload, *, region="ir", t=T0):
    return IncomingEvent(
        event_id=uuid.uuid4(), idempotency_key=str(uuid.uuid4()),
        event_type=etype, event_version=1,
        aggregate_id=str(offer_id), aggregate_type="Offer",
        occurred_at=t, payload=payload, region=region,
    )


def created(oid, seller, t, region="ir", currency="IRR"):
    return ev("selling.OfferCreated", oid, {
        "offer_id": str(oid), "seller_id": str(seller), "variant_id": str(V),
        "condition": "new", "currency": currency, "status": "draft"}, region=region, t=t)


def activated(oid, seller, amount, t, region="ir", currency="IRR", scale=0):
    return ev("selling.OfferActivated", oid, {
        "offer_id": str(oid), "seller_id": str(seller), "variant_id": str(V),
        "condition": "new", "currency": currency,
        "price_amount": amount, "price_scale": scale,
        "compare_at_amount": None}, region=region, t=t)


def repriced(oid, seller, old, new, t, region="ir", currency="IRR", scale=0):
    return ev("selling.OfferPriceChanged", oid, {
        "offer_id": str(oid), "seller_id": str(seller), "variant_id": str(V),
        "currency": currency, "old_amount": old, "new_amount": new,
        "scale": scale}, region=region, t=t)


def suspended(oid, seller, t, region="ir"):
    return ev("selling.OfferSuspended", oid, {
        "offer_id": str(oid), "seller_id": str(seller),
        "variant_id": str(V)}, region=region, t=t)


async def apply(projector, event):
    """Dispatch exactly the way the relay and the rebuild do."""
    await getattr(projector, PROJECTOR_DISPATCH[event.event_type])(event)


async def apply_all(projector, events):
    for e in events:
        await apply(projector, e)


async def project(events, *, row_order="insertion"):
    repo = FakeRepo(row_order=row_order)
    await apply_all(BuyBoxProjector(repo), events)
    return repo


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


# --- wiring ----------------------------------------------------------------
def test_offer_suspended_is_a_consumed_event_type():
    """The original bug: the domain emitted it and nothing subscribed."""
    assert "selling.OfferSuspended" in CONSUMED_EVENT_TYPES


def test_every_consumed_event_type_is_dispatchable():
    assert set(PROJECTOR_DISPATCH) == set(CONSUMED_EVENT_TYPES)
    for event_type, method in PROJECTOR_DISPATCH.items():
        assert callable(getattr(BuyBoxProjector, method, None)), event_type


def test_every_buybox_handler_is_registered_with_the_relay():
    pytest.importorskip("sqlalchemy", reason="relay wiring pulls in the DB layer")
    from src.shared_kernel.infrastructure.outbox.runner import build_registry

    registry = build_registry()
    for handler in BUYBOX_HANDLERS:
        registered = [h.name for h in registry.handlers_for(handler.event_type)]
        assert handler.name in registered, handler.event_type


def test_rebuild_replays_through_the_same_dispatch_map():
    """Guards the wiring point that silently went stale when events were added."""
    pytest.importorskip("sqlalchemy", reason="rebuild pulls in the DB layer")
    from src.modules.buybox.infrastructure import rebuild

    assert rebuild.PROJECTOR_DISPATCH is PROJECTOR_DISPATCH


# --- ranking basics --------------------------------------------------------
def test_draft_offers_have_no_winner():
    repo = run(project([created(OFFER_A, S1, at(1)), created(OFFER_B, S2, at(2))]))
    state = run(repo.get_variant_state(V, "ir"))
    assert state.winning_offer_id is None
    assert state.amount is None and state.seller_id is None


def test_lowest_price_wins():
    repo = run(project([
        created(OFFER_A, S1, at(1)), created(OFFER_B, S2, at(2)),
        activated(OFFER_A, S1, 100, at(3)), activated(OFFER_B, S2, 90, at(4)),
    ]))
    state = run(repo.get_variant_state(V, "ir"))
    assert (state.winning_offer_id, state.amount) == (OFFER_B, 90)


def test_reprice_moves_the_winner():
    repo = run(project([
        created(OFFER_A, S1, at(1)), created(OFFER_B, S2, at(2)),
        activated(OFFER_A, S1, 100, at(3)), activated(OFFER_B, S2, 90, at(4)),
        repriced(OFFER_A, S1, 100, 80, at(5)),
    ]))
    state = run(repo.get_variant_state(V, "ir"))
    assert (state.winning_offer_id, state.amount) == (OFFER_A, 80)


def test_variant_state_carries_the_full_winner_identity():
    repo = run(project([
        created(OFFER_B, S2, at(1)),
        activated(OFFER_B, S2, 1234, at(2), scale=2),
    ]))
    state = run(repo.get_variant_state(V, "ir"))
    assert state == VariantState(
        variant_id=V, region="ir", winning_offer_id=OFFER_B, seller_id=S2,
        amount=1234, currency="IRR", scale=2, last_updated_at=at(2),
    )


# --- tie-breaks: must not depend on row order ------------------------------
@pytest.mark.parametrize("row_order", ["insertion", "reversed"])
def test_price_tie_is_broken_by_created_at_in_either_row_order(row_order):
    """B is created LATER but listed FIRST when row_order is reversed, so a
    missing created_at tie-break flips the winner instead of passing silently."""
    repo = run(project([
        created(OFFER_A, S1, at(1)),      # earlier -> must win the tie
        created(OFFER_B, S2, at(2)),
        activated(OFFER_A, S1, 80, at(3)),
        activated(OFFER_B, S2, 80, at(4)),
    ], row_order=row_order))
    state = run(repo.get_variant_state(V, "ir"))
    assert (state.winning_offer_id, state.amount) == (OFFER_A, 80)


@pytest.mark.parametrize("row_order", ["insertion", "reversed"])
def test_created_at_tie_is_broken_by_offer_id_in_either_row_order(row_order):
    """Identical price AND identical created_at -> lowest offer_id, so replay is
    deterministic even for simultaneous offers."""
    same = at(1)
    repo = run(project([
        created(OFFER_B, S2, same), created(OFFER_A, S1, same),
        activated(OFFER_B, S2, 80, same), activated(OFFER_A, S1, 80, same),
    ], row_order=row_order))
    state = run(repo.get_variant_state(V, "ir"))
    assert state.winning_offer_id == OFFER_A   # "aaaa..." < "bbbb..."


# --- cross-scale exactness -------------------------------------------------
def test_cross_scale_ranking_compares_real_value_not_raw_amount():
    """A@1.00 (100 minor, scale 2) must beat B@2 (2 minor, scale 0)."""
    repo = run(project([
        created(OFFER_A, S1, at(1)), created(OFFER_B, S2, at(2)),
        activated(OFFER_A, S1, 100, at(3), scale=2),
        activated(OFFER_B, S2, 2, at(4), scale=0),
    ]))
    state = run(repo.get_variant_state(V, "ir"))
    assert (state.winning_offer_id, state.amount, state.scale) == (OFFER_A, 100, 2)


def test_high_scale_ranking_is_exact_at_large_magnitudes():
    """Float normalization collapsed these two into one value and handed the win
    to whichever was created first. B is one minor unit cheaper and created
    LATER, so it can only win if the comparison is exact."""
    dearer, cheaper = 20_000_000_000_000_002, 20_000_000_000_000_001
    repo = run(project([
        created(OFFER_A, S1, at(1)), created(OFFER_B, S2, at(2)),
        activated(OFFER_A, S1, dearer, at(3), scale=9),
        activated(OFFER_B, S2, cheaper, at(4), scale=9),
    ]))
    state = run(repo.get_variant_state(V, "ir"))
    assert (state.winning_offer_id, state.amount) == (OFFER_B, cheaper)


def test_scale_beyond_the_old_normalization_ceiling_still_ranks():
    """scale > 8 used to produce a float sort key; 1 unit at scale 12 is cheaper
    than 1 unit at scale 11."""
    repo = run(project([
        created(OFFER_A, S1, at(1)), created(OFFER_B, S2, at(2)),
        activated(OFFER_A, S1, 1, at(3), scale=11),
        activated(OFFER_B, S2, 1, at(4), scale=12),
    ]))
    state = run(repo.get_variant_state(V, "ir"))
    assert state.winning_offer_id == OFFER_B


# --- currency mismatch -----------------------------------------------------
def test_offer_in_a_foreign_currency_cannot_win():
    """No implicit FX: a numerically tiny USD amount must not beat the IRR
    incumbent just because 5 < 1_000_000."""
    repo = run(project([
        created(OFFER_A, S1, at(1)), created(OFFER_B, S2, at(2), currency="USD"),
        activated(OFFER_A, S1, 1_000_000, at(3)),
        activated(OFFER_B, S2, 5, at(4), currency="USD", scale=2),
    ]))
    state = run(repo.get_variant_state(V, "ir"))
    assert (state.winning_offer_id, state.currency) == (OFFER_A, "IRR")


def test_reference_currency_is_the_incumbents_not_the_cheapests():
    """Incumbent is USD here, so the later IRR offer is the excluded one."""
    repo = run(project([
        created(OFFER_A, S1, at(1), currency="USD"), created(OFFER_B, S2, at(2)),
        activated(OFFER_A, S1, 999, at(3), currency="USD", scale=2),
        activated(OFFER_B, S2, 1, at(4)),
    ]))
    state = run(repo.get_variant_state(V, "ir"))
    assert (state.winning_offer_id, state.currency) == (OFFER_A, "USD")


def test_foreign_currency_offer_alone_still_wins_its_variant():
    """Exclusion is relative to the incumbent, not a hardcoded currency."""
    repo = run(project([
        created(OFFER_B, S2, at(1), currency="USD"),
        activated(OFFER_B, S2, 500, at(2), currency="USD", scale=2),
    ]))
    assert run(repo.get_variant_state(V, "ir")).winning_offer_id == OFFER_B


# --- active-only, including suspension ------------------------------------
def test_draft_offer_is_excluded_even_when_cheapest():
    repo = run(project([
        created(OFFER_A, S1, at(1)), activated(OFFER_A, S1, 80, at(2)),
        created(OFFER_D, S1, at(3)),
        repriced(OFFER_D, S1, 0, 1, at(4)),   # priced but never activated
    ]))
    state = run(repo.get_variant_state(V, "ir"))
    assert (state.winning_offer_id, state.amount) == (OFFER_A, 80)


def test_suspended_winner_loses_the_buybox_to_the_runner_up():
    repo = run(project([
        created(OFFER_A, S1, at(1)), created(OFFER_B, S2, at(2)),
        activated(OFFER_A, S1, 80, at(3)), activated(OFFER_B, S2, 90, at(4)),
        suspended(OFFER_A, S1, at(5)),
    ]))
    state = run(repo.get_variant_state(V, "ir"))
    assert (state.winning_offer_id, state.amount) == (OFFER_B, 90)
    assert state.last_updated_at == at(5)


def test_suspending_the_only_offer_clears_the_winner():
    repo = run(project([
        created(OFFER_A, S1, at(1)), activated(OFFER_A, S1, 80, at(2)),
        suspended(OFFER_A, S1, at(3)),
    ]))
    state = run(repo.get_variant_state(V, "ir"))
    assert state.winning_offer_id is None
    assert (state.amount, state.seller_id, state.currency) == (None, None, None)


def test_suspension_marks_the_offer_inactive_but_keeps_its_price():
    repo = run(project([
        created(OFFER_A, S1, at(1)), activated(OFFER_A, S1, 80, at(2)),
        suspended(OFFER_A, S1, at(3)),
    ]))
    offer = run(repo.get_offer(OFFER_A))
    assert offer.is_active is False
    assert (offer.amount, offer.currency) == (80, "IRR")


def test_reactivation_after_suspension_restores_candidacy():
    repo = run(project([
        created(OFFER_A, S1, at(1)), created(OFFER_B, S2, at(2)),
        activated(OFFER_A, S1, 80, at(3)), activated(OFFER_B, S2, 90, at(4)),
        suspended(OFFER_A, S1, at(5)),
        activated(OFFER_A, S1, 80, at(6)),
    ]))
    state = run(repo.get_variant_state(V, "ir"))
    assert (state.winning_offer_id, state.amount) == (OFFER_A, 80)


def test_suspending_an_unknown_offer_is_safe():
    """Replay fallback: a suspension may arrive with no prior offer row."""
    repo = run(project([suspended(OFFER_A, S1, at(1))]))
    state = run(repo.get_variant_state(V, "ir"))
    assert state.winning_offer_id is None
    assert run(repo.get_offer(OFFER_A)).is_active is False


# --- region isolation ------------------------------------------------------
def test_regions_are_isolated():
    repo = run(project([
        created(OFFER_A, S1, at(1)), activated(OFFER_A, S1, 80, at(2)),
        created(OFFER_C, S3, at(3), region="eu"),
        activated(OFFER_C, S3, 50, at(4), region="eu"),
    ]))
    eu = run(repo.get_variant_state(V, "eu"))
    ir = run(repo.get_variant_state(V, "ir"))
    assert (eu.winning_offer_id, eu.amount) == (OFFER_C, 50)
    assert (ir.winning_offer_id, ir.amount) == (OFFER_A, 80)


def test_suspension_in_one_region_leaves_the_other_untouched():
    repo = run(project([
        created(OFFER_A, S1, at(1)), activated(OFFER_A, S1, 80, at(2)),
        created(OFFER_C, S3, at(3), region="eu"),
        activated(OFFER_C, S3, 50, at(4), region="eu"),
        suspended(OFFER_C, S3, at(5), region="eu"),
    ]))
    assert run(repo.get_variant_state(V, "eu")).winning_offer_id is None
    assert run(repo.get_variant_state(V, "ir")).winning_offer_id == OFFER_A


# --- idempotency on winner-changing events --------------------------------
def _full_log() -> list[IncomingEvent]:
    return [
        created(OFFER_A, S1, at(1)),
        created(OFFER_B, S2, at(2)),
        activated(OFFER_A, S1, 100, at(3)),
        activated(OFFER_B, S2, 90, at(4)),
        repriced(OFFER_A, S1, 100, 80, at(5)),   # A takes the win
        repriced(OFFER_B, S2, 90, 80, at(6)),    # tie -> A keeps it
        created(OFFER_C, S3, at(7), region="eu"),
        activated(OFFER_C, S3, 50, at(8), region="eu"),
        created(OFFER_D, S1, at(9)),
        repriced(OFFER_D, S1, 0, 1, at(10)),     # draft, excluded
        suspended(OFFER_A, S1, at(11)),          # B (also 80) takes over
    ]


@pytest.mark.parametrize(
    "index,label",
    [(2, "activated"), (4, "repriced-to-winner"), (10, "suspended-the-winner")],
)
def test_reapplying_a_winner_changing_event_changes_nothing(index, label):
    """Each of these events *moves* the winner, so a non-idempotent handler
    shows up here. The old test re-applied a draft reprice that could not affect
    the winner under any implementation."""
    log = _full_log()
    repo = run(project(log[: index + 1]))
    before = repo.state_table(), repo.offer_table()

    run(apply(BuyBoxProjector(repo), log[index]))

    assert (repo.state_table(), repo.offer_table()) == before, label


def test_reapplying_the_whole_log_is_a_no_op():
    log = _full_log()
    repo = run(project(log))
    before = repo.state_table(), repo.offer_table()

    run(apply_all(BuyBoxProjector(repo), log))

    assert (repo.state_table(), repo.offer_table()) == before


# --- replay equivalence ----------------------------------------------------
def test_replay_from_scratch_reproduces_the_incremental_projection():
    """Asserted, not printed — the old version printed the comparison as a bool
    and passed even when replay picked the wrong winner."""
    log = _full_log()
    incremental = run(project(log))
    replayed = run(project(log))

    assert replayed.state_table() == incremental.state_table()
    assert replayed.offer_table() == incremental.offer_table()
    assert run(replayed.get_variant_state(V, "ir")).winning_offer_id == OFFER_B
    assert run(replayed.get_variant_state(V, "eu")).winning_offer_id == OFFER_C


@pytest.mark.parametrize("row_order", ["insertion", "reversed"])
def test_replay_is_independent_of_repository_row_order(row_order):
    log = _full_log()
    baseline = run(project(log))
    other = run(project(log, row_order=row_order))
    assert other.state_table() == baseline.state_table()


# --- fake-repo fidelity ----------------------------------------------------
def test_repo_reads_are_detached_copies():
    """Documents the property that makes the suite able to catch a missing
    upsert_offer: mutating a read does not reach the store."""
    repo = run(project([created(OFFER_A, S1, at(1)), activated(OFFER_A, S1, 80, at(2))]))

    borrowed = run(repo.get_offer(OFFER_A))
    borrowed.amount = 1
    borrowed.is_active = False

    assert run(repo.get_offer(OFFER_A)).amount == 80
    assert run(repo.get_offer(OFFER_A)).is_active is True
    assert run(repo.list_for(V, "ir"))[0].amount == 80


def test_mutating_an_upserted_object_does_not_reach_the_store():
    repo = FakeRepo()
    offer = OfferProjection(
        offer_id=OFFER_A, variant_id=V, seller_id=S1, region="ir",
        offer_created_at=at(1), is_active=True, currency="IRR", amount=80,
    )
    run(repo.upsert_offer(offer))
    offer.amount = 1
    assert run(repo.get_offer(OFFER_A)).amount == 80
