"""Selling context validation (DB-free) — first financial domain.

Real domain aggregate, real use cases, real UoW event-draining + PII guard.
Fakes: OfferRepository, CatalogGateway (ACL), SellingUnitOfWork.

Run: python tests/test_selling_flow.py
"""

from __future__ import annotations

import asyncio
import sys
import uuid

sys.path.insert(0, ".")
try:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")
except Exception:
    pass

from src.modules.selling.application.dtos import (
    ChangeOfferPriceCommand,
    CreateOfferCommand,
)
from src.modules.selling.application.unit_of_work import SellingUnitOfWork
from src.modules.selling.application.use_cases import (
    ActivateOffer,
    ChangeOfferPrice,
    CreateOffer,
)
from src.modules.selling.domain.entities import (
    OfferActivated,
    OfferCreated,
    OfferPriceChanged,
    OfferStatus,
)
from src.shared_kernel.application.errors import ConflictError, NotFoundError, ValidationError
from src.shared_kernel.infrastructure.outbox.serialization import event_payload


async def expect(exc, coro_fn):
    try:
        await coro_fn()
    except exc:
        return True
    raise AssertionError(f"expected {exc.__name__}")


# --- fakes ----------------------------------------------------------------
class FakeCatalog:
    def __init__(self, known: set[str]):
        self._known = known
    async def variant_exists(self, variant_id):
        return str(variant_id) in self._known


class FakeOfferRepo:
    def __init__(self, uow, store):
        self._uow = uow
        self._store = store
    async def get_by_id(self, offer_id):
        o = self._store.get(offer_id)
        if o:
            self._uow.track(o)
        return o
    async def add(self, offer):
        self._store[offer.id] = offer
        self._uow.track(offer)
    async def save(self, offer):
        self._store[offer.id] = offer
        self._uow.track(offer)
    async def find_active_for(self, seller_id, variant_id):
        for o in self._store.values():
            if o.seller_id == seller_id and o.variant_id == variant_id and o.status == OfferStatus.ACTIVE:
                self._uow.track(o); return o
        return None


class FakeSellingUoW(SellingUnitOfWork):
    def __init__(self, store):
        super().__init__()
        self.offers = FakeOfferRepo(self, store)
        self.outbox: list = []
    async def commit(self):
        for _agg, ev in self.collect_new_events():
            event_payload(ev)            # PII guard, as the real writer does
            self.outbox.append(ev)
        self._committed = True
    async def rollback(self):
        self._discard_tracked()


VARIANT = str(uuid.uuid4())
SELLER = str(uuid.uuid4())


async def main():
    store: dict = {}
    uow = FakeSellingUoW(store)
    factory = lambda: uow
    catalog = FakeCatalog(known={VARIANT})

    create = CreateOffer(uow_factory=factory, catalog=catalog)
    activate = ActivateOffer(uow_factory=factory, one_active_per_variant=True)
    change = ChangeOfferPrice(uow_factory=factory)

    # === ACL: unknown variant rejected, no offer created ===
    assert await expect(NotFoundError, lambda: create.execute(
        CreateOfferCommand(seller_id=SELLER, variant_id=str(uuid.uuid4()), amount=1000)))
    print("[acl]      unknown variant -> NotFound (catalog ACL)")

    # === create with initial price -> OfferCreated + OfferPriceChanged ===
    view = await create.execute(CreateOfferCommand(
        seller_id=SELLER, variant_id=VARIANT, condition="new", currency="IRR",
        amount=18_900_000, scale=0))
    assert view.status == "draft" and view.price is not None and view.price.amount == 18_900_000
    kinds = [type(e).__name__ for e in uow.outbox]
    assert kinds == ["OfferCreated", "OfferPriceChanged"], kinds
    assert uow.outbox[0].event_type == "selling.OfferCreated"
    print("[create]   events:", kinds, "| price:", view.price.amount, view.currency)
    offer_id = uuid.UUID(view.id)

    # === price mutation only via aggregate + event-driven ===
    n = len(uow.outbox)
    view2 = await change.execute(offer_id, ChangeOfferPriceCommand(amount=17_500_000, currency="IRR"))
    assert view2.price is not None and view2.price.amount == 17_500_000
    pc = [e for e in uow.outbox if isinstance(e, OfferPriceChanged)]
    assert pc[-1].old_amount == 18_900_000 and pc[-1].new_amount == 17_500_000
    assert len(uow.outbox) == n + 1
    print("[price]    OfferPriceChanged old->new:", pc[-1].old_amount, "->", pc[-1].new_amount)

    # === currency mismatch -> ValidationError (domain guard) ===
    assert await expect(ValidationError, lambda: change.execute(
        offer_id, ChangeOfferPriceCommand(amount=10, currency="USD", scale=2)))
    print("[currency] cross-currency price change rejected")

    # === activate -> OfferActivated with BuyBox-ready data ===
    view3 = await activate.execute(offer_id)
    assert view3.status == "active"
    act = [e for e in uow.outbox if isinstance(e, OfferActivated)][-1]
    bb = event_payload(act)
    assert act.event_type == "selling.OfferActivated"
    for key in ("offer_id", "seller_id", "variant_id", "condition", "currency",
                "price_amount", "price_scale"):
        assert key in bb, key
    assert bb["price_amount"] == 17_500_000 and bb["currency"] == "IRR"
    print("[activate] OfferActivated buybox payload keys ok; price:", bb["price_amount"])

    # === configurable invariant: 2nd active offer for same seller+variant ===
    v2 = await create.execute(CreateOfferCommand(
        seller_id=SELLER, variant_id=VARIANT, condition="refurbished", currency="IRR", amount=12_000_000))
    assert await expect(ConflictError, lambda: activate.execute(uuid.UUID(v2.id)))
    print("[invariant] 2nd active offer blocked when flag ON")

    # ...same scenario with the invariant DISABLED -> allowed
    store2: dict = {}
    uow2 = FakeSellingUoW(store2)
    f2 = lambda: uow2
    create2 = CreateOffer(uow_factory=f2, catalog=catalog)
    activate_off = ActivateOffer(uow_factory=f2, one_active_per_variant=False)
    a = await create2.execute(CreateOfferCommand(seller_id=SELLER, variant_id=VARIANT, currency="IRR", amount=1))
    b = await create2.execute(CreateOfferCommand(seller_id=SELLER, variant_id=VARIANT, condition="used", currency="IRR", amount=2))
    await activate_off.execute(uuid.UUID(a.id))
    await activate_off.execute(uuid.UUID(b.id))     # no ConflictError
    print("[invariant] multi-offer allowed when flag OFF (marketplace mode)")

    # === activate without price -> ValidationError ===
    uow3 = FakeSellingUoW({})
    cr3 = CreateOffer(uow_factory=lambda: uow3, catalog=catalog)
    ac3 = ActivateOffer(uow_factory=lambda: uow3, one_active_per_variant=True)
    nop = await cr3.execute(CreateOfferCommand(seller_id=SELLER, variant_id=VARIANT, currency="IRR"))
    assert nop.price is None
    assert await expect(ValidationError, lambda: ac3.execute(uuid.UUID(nop.id)))
    print("[priced]   activate without price rejected")

    print("\nSELLING CONTEXT VALIDATED (offer lifecycle + pricing + invariants + events)")


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main())
