"""Catalog write-side validation (DB-free).

Uses the REAL domain aggregate, REAL use cases, REAL UoW event-draining, REAL
serialization/PII guard. Only the repository is an in-memory fake.

Run: python tests/test_catalog_write_flow.py
"""

from __future__ import annotations

import asyncio
import sys
import uuid

sys.path.insert(0, ".")
try:
    sys.stdout.reconfigure(encoding="utf-8")  # allow Persian output on Windows
except Exception:
    pass

from src.modules.catalog.application.unit_of_work import CatalogUnitOfWork
from src.modules.catalog.application.write_dtos import (
    CreateProductCommand,
    UpdateProductCommand,
    VariantInput,
)
from src.modules.catalog.application.write_use_cases import CreateProduct, UpdateProduct
from src.modules.catalog.domain.entities import Product, ProductCreated, ProductUpdated
from src.shared_kernel.application.errors import ConflictError, NotFoundError
from src.shared_kernel.infrastructure.outbox.serialization import event_payload


async def expect(exc, coro_fn):
    try:
        await coro_fn()
    except exc:
        return True
    raise AssertionError(f"expected {exc.__name__}")


# --- in-memory fake repo + UoW (the only fakes) ---------------------------
class FakeProductRepo:
    def __init__(self, uow, store):
        self._uow = uow
        self._store = store               # shared dict id->Product

    async def get_by_slug(self, slug):
        for p in self._store.values():
            if p.slug == slug:
                self._uow.track(p); return p
        return None

    async def get_by_id(self, pid):
        p = self._store.get(pid)
        if p:
            self._uow.track(p)
        return p

    async def add(self, product):
        self._store[product.id] = product
        self._uow.track(product)

    async def save(self, product):
        self._store[product.id] = product
        self._uow.track(product)


class FakeCatalogUoW(CatalogUnitOfWork):
    def __init__(self, store):
        super().__init__()
        self.products = FakeProductRepo(self, store)
        self.outbox: list = []

    async def commit(self):
        for _agg, ev in self.collect_new_events():
            event_payload(ev)             # runs the PII guard, as the real writer does
            self.outbox.append(ev)
        self._committed = True

    async def rollback(self):
        self._discard_tracked()


async def main():
    store: dict = {}
    uow = FakeCatalogUoW(store)
    factory = lambda: uow                  # same uow reused; __aenter__ resets per-op

    create = CreateProduct(uow_factory=factory)
    update = UpdateProduct(uow_factory=factory)

    # === create -> ProductCreated emitted to outbox ===
    view = await create.execute(
        CreateProductCommand(
            product_type_id=1, category_id=102, slug="dermacol-make-up-cover",
            name_fa="کرم‌پودر پوشاننده درماکول",
            variants=(VariantInput(sku="DRM-MUC-209", variant_axis_values={"color": "بژ"}),),
        )
    )
    assert view.status == "draft" and view.slug == "dermacol-make-up-cover"
    assert len(view.variants) == 1 and view.variants[0].sku == "DRM-MUC-209"
    assert len(uow.outbox) == 1 and isinstance(uow.outbox[0], ProductCreated)
    created_payload = event_payload(uow.outbox[0])
    assert uow.outbox[0].event_type == "catalog.ProductCreated"
    assert set(created_payload) == {"product_id", "slug", "name_fa", "product_type_id", "status"}
    assert "name" not in created_payload                      # PII-safe (name_fa only)
    print("[create]  ProductCreated:", created_payload)

    pid = uuid.UUID(view.id)

    # === slug conflict -> ConflictError, no event ===
    before = len(uow.outbox)
    assert await expect(ConflictError, lambda: create.execute(
        CreateProductCommand(product_type_id=1, category_id=102,
                             slug="dermacol-make-up-cover", name_fa="dup")))
    assert len(uow.outbox) == before
    print("[conflict] duplicate slug rejected, no event emitted")

    # === update -> granular ProductUpdated facts (one per kind of change) ===
    view2 = await update.execute(pid, UpdateProductCommand(name_fa="کرم‌پودر جدید", status="active"))
    assert view2.status == "active" and view2.name_fa == "کرم‌پودر جدید"
    updated = [e for e in uow.outbox if isinstance(e, ProductUpdated)]
    assert len(updated) == 2, updated                     # name_fa change + status change
    changed = set().union(*(set(e.changed_fields) for e in updated))
    assert changed == {"name_fa", "status"}
    assert all(e.event_type == "catalog.ProductUpdated" for e in updated)
    assert any(e.changed_fields == ("status",) and e.status == "active" for e in updated)
    print("[update]  ProductUpdated facts:", [e.changed_fields for e in updated])

    # === update no-op (same values) -> no new event ===
    n = len(uow.outbox)
    await update.execute(pid, UpdateProductCommand(name_fa="کرم‌پودر جدید"))
    assert len(uow.outbox) == n
    print("[no-op]   unchanged update emits no event")

    # === update missing product -> NotFoundError ===
    assert await expect(NotFoundError, lambda: update.execute(uuid.uuid4(), UpdateProductCommand(name_fa="x")))
    print("[404]     update of unknown product rejected")

    # === pure domain: aggregate records its own events ===
    p = Product.create(product_type_id=2, category_id=201, slug="samsung-galaxy-a55", name_fa="گلکسی A55")
    evs = p.pull_events()
    assert len(evs) == 1 and isinstance(evs[0], ProductCreated)
    assert evs[0].aggregate_id == p.id and evs[0].aggregate_type == "Product"
    print("[domain]  aggregate stamped event:", evs[0].event_type, "| agg_type", evs[0].aggregate_type)

    print("\nCATALOG WRITE SIDE VALIDATED (domain + use cases + UoW events + PII guard)")


if __name__ == "__main__":
    main_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(main_loop)
    main_loop.run_until_complete(main())
