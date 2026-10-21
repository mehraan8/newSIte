# ADR-0009 — Inventory as the first hard-consistency bounded context

- **Status:** Accepted — implemented 2026-07-26. D1–D6 resolved; three follow-ups remain (§13)
- **Date:** 2026-07-26
- **Supersedes:** the unused variant-anchored stock tables from migration `0006`
- **Related:** ADR-0007 (Unit of Work owns the transaction), ADR-0008 (event envelope)

> **Numbering assumption.** ADR-0007 and ADR-0008 are referenced from source
> (`shared_kernel/application/unit_of_work.py`, `outbox/serialization.py`,
> `migrations/0010_outbox_envelope.py`) but no ADR files are committed to this
> repo. `0009` is the next free number on that evidence; renumber if the ADR log
> lives elsewhere.

> **Scope note.** The brief framed this as greenfield with "no Ordering
> implementation yet." Both contexts already exist and are wired into the relay:
> `src/modules/inventory/**`, `src/modules/ordering/**`, migrations `0014`/`0015`,
> and six registrations in `outbox/runner.py`. This ADR therefore *ratifies and
> documents the design as built*, records the decisions that were made
> implicitly, and separates them from the defects the review found. Writing a
> greenfield design that ignored the running code would be fiction. §10 lists
> what is actually broken; everything above it describes intended design.

---

## 1. Context

Every context shipped so far avoids write contention:

| Context | Write shape | Contention |
|---|---|---|
| Identity | one aggregate per user | none |
| Catalog | one aggregate per product | none |
| Selling | one aggregate per offer | none |
| BuyBox | derived read model, idempotent projector | none (recomputed) |

Inventory is different in kind. It owns a **contended mutable counter**: many
concurrent checkouts race for the same `(offer_id, location)` row, and the
business consequence of losing that race is *overselling* — accepting money for
stock that does not exist. Eventual consistency is not available here. The
decision to reserve must be atomic with the state that justifies it.

This is also the first context that must expose a *reply* channel: the checkout
saga cannot proceed without knowing whether stock was secured.

**Forces:**

- Correctness beats throughput. An oversell is a refund, a support ticket, and a
  marketplace-trust cost; a rejected checkout is a retry.
- The existing UoW/Outbox/Relay/Inbox machinery must be reused unchanged. A new
  transactional pattern here would fork the architecture.
- The domain layer must stay pure — no SQLAlchemy, no session, no `IntegrityError`
  above the infrastructure boundary.
- Inventory must be extractable into its own service later, so it may reference
  Selling only through a narrow read-only ACL and must never share a transaction
  with another context.

---

## 2. Domain model

### 2.1 `InventoryItem` — aggregate root

Identity: surrogate `id`, natural key `(offer_id, location)`.

| Field | Meaning |
|---|---|
| `offer_id` | the *only* external reference; never a catalog variant/product id |
| `location` | stock location code (see §2.4) |
| `on_hand` | physical units present |
| `reserved` | units promised to open reservations |
| `version` | optimistic lock token |

`available` is a **computed property** (`on_hand - reserved`), never stored. A
stored `available` would be a third number that can disagree with the other two.

Behaviour: `receive(qty)`, `reserve(qty, reservation_id)`, `commit(qty, reservation_id)`,
`release(qty, reservation_id, reason)`, `fail_reservation(reservation_id, qty, reason)`.
Every mutator ends in `_check_invariants()`. All events originate here — this is
the only event-recording object in the context.

`InventoryItem` is the **consistency boundary for the oversell invariant.** That
is its entire reason to exist as an aggregate.

### 2.2 `Reservation` — separate aggregate

Identity: `reservation_id`, **supplied by the caller**. This is the design's
central idempotency lever: the caller (Ordering, or an API client) names the
reservation, so a retry of the same logical request is recognisable by primary
key rather than by inference.

| Field | Meaning |
|---|---|
| `id` | caller-supplied idempotency key |
| `offer_id`, `location` | which stock bucket it draws from |
| `quantity` | units held |
| `status` | lifecycle (§2.3) |
| `expires_at` | TTL horizon for the sweeper |

`Reservation` records **no events** and holds no version column. It is a receipt
with a state machine, not a source of facts.

### 2.3 `ReservationStatus` and lifecycle

```
                   ┌── commit() ──▶ COMMITTED   (terminal)
   PENDING ────────┼── release() ─▶ RELEASED    (terminal)
                   └── expire() ──▶ EXPIRED     (terminal)
```

| From | Operation | Allowed | Effect on counters | Notes |
|---|---|---|---|---|
| PENDING | `commit` | yes | `on_hand -= q`, `reserved -= q` | stock leaves the building |
| PENDING | `release` | yes | `reserved -= q` | stock returns to available |
| PENDING | `expire` | yes | `reserved -= q` | TTL lapsed |
| COMMITTED | `commit` | no-op | none | idempotent |
| COMMITTED | `release` | **rejected** | none | money already taken; releasing would double-sell |
| RELEASED / EXPIRED | `release` | no-op | none | idempotent |
| RELEASED / EXPIRED | `commit` | **rejected** | none | the hold is gone (see §10, D1) |
| RELEASED / EXPIRED | `expire` | no-op | none | idempotent |

Every transition is either idempotent or an explicit rejection. There is no
transition whose second application changes state — this is what makes the
context safe under at-least-once relay delivery.

`COMMITTED` is deliberately a dead end. Reversing a commit is a *return*, a
different business process with its own event, not an inventory undo.

### 2.4 Warehouse / stock location model

**Decision: `location` stays an opaque `String(40)` code for v1. No warehouse
aggregate, no reference table.**

Rationale: the only property the invariant needs is that stock at location A is
not fungible with stock at location B, which a distinct string already gives.
Address, capacity, cut-off times, and carrier bindings are fulfilment concerns
with no bearing on the oversell invariant, and modelling them now would add a
table that nothing reads.

Cost, stated plainly: there is no referential integrity on location codes. A typo
in `ReceiveStockCommand.location` does not fail — `uq_inventory_offer_location`
happily creates a *new* stock bucket, and the units are stranded where no
reservation will find them. Mitigation: validate `location` against an
application-level allowlist at the interface boundary before it reaches the
domain. That is cheap and reversible; a table is not.

**`location` is not `region`.** BuyBox partitions by `region` (a *market* — which
catalogue and price a shopper sees); Inventory partitions by `location` (a
*place*). They are orthogonal axes: one region may draw from several locations,
and one location may serve several regions. `OrderReservationRequested` already
carries both, per-line. The mapping rule between them is **not yet defined**
(§11) — today Ordering simply passes a `location` through and Inventory trusts it.

---

## 3. Core invariants

### Hard invariants (never violable)

| # | Invariant | Enforced by |
|---|---|---|
| I1 | `on_hand >= 0` | domain `_check_invariants` + `ck_inv_onhand_nonneg` |
| I2 | `reserved >= 0` | domain `_check_invariants` + `ck_inv_reserved_nonneg` |
| I3 | `reserved <= on_hand` | domain `_check_invariants` + `ck_inv_reserved_le_onhand` |
| I4 | `reserve(q)` requires `available >= q` | domain `reserve` raises `InsufficientStock` |
| I5 | all quantities `> 0` | domain guards + `ck_resv_qty_pos` |
| I6 | a committed reservation is never released | domain `Reservation.release` |
| I7 | `status ∈ {pending, committed, released, expired}` | `ck_resv_status` |

I3 is the oversell invariant. I1+I3 together make negative stock unrepresentable:
`available = on_hand - reserved >= 0` follows from I3 alone.

### Derived invariant (auditable, not structurally enforced)

| # | Invariant |
|---|---|
| I8 | `SUM(quantity) WHERE status='pending' AND (offer_id, location) = X` **==** `inventory_stock.reserved` for X |

I8 is the seam created by splitting `Reservation` out of the `InventoryItem`
aggregate (§4). No aggregate boundary enforces it. It is held by two things:

1. **Every lifecycle operation writes both rows inside one UoW**, so a partial
   application cannot commit.
2. **A reconciliation check** must assert it periodically. This is the single
   most valuable operational alarm in the context: if I8 drifts, stock is either
   silently unsellable (`reserved` too high) or oversellable (`reserved` too low).

I8 has no supporting index today (§9).

### Defense in depth

Three layers, each catching what the one above cannot:

| Layer | Catches | Cannot catch |
|---|---|---|
| Domain `_check_invariants` | logic errors in the aggregate | stale reads |
| Version-guarded UPDATE | concurrent interleaving | a bug that computes a bad value from a fresh read |
| DB CHECK constraints | everything else, including direct SQL and migrations | nothing — last line |

The CHECK constraints are not redundant. They are the only layer that survives a
bug in the other two, and the only one that binds an operator running manual SQL.

---

## 4. Concurrency strategy

### Chosen: optimistic offline lock + bounded application retry

Every write is a compare-and-set on the version column:

```
UPDATE inventory_stock
   SET on_hand = :new_on_hand, reserved = :new_reserved,
       version = version + 1, updated_at = now()
 WHERE id = :id AND version = :expected_version
```

`rowcount == 0` means someone else moved the row; the adapter translates that
(and any `IntegrityError`) into `ConcurrencyConflictError`, and the use case
retries the **whole UoW** with a fresh read — new session, new read, new decision.
Retrying the transaction rather than the statement is what keeps the invariant
check meaningful: the domain re-evaluates `available >= qty` against current state.

Why this shape:

- The decision stays in the aggregate. `reserve()` owns "you may not exceed
  available," and the database is a backstop, not the author of the rule.
- Locks are held for microseconds (one UPDATE), not for the duration of the
  business transaction including the outbox write.
- It composes with the existing UoW unchanged — no new transactional pattern.

### How overselling is actually prevented

Two concurrent reservations for the last unit, `on_hand=1, reserved=0, version=7`:

| | T1 | T2 |
|---|---|---|
| 1 | read v7, available=1 | read v7, available=1 |
| 2 | `reserve(1)` → reserved=1 ✓ | `reserve(1)` → reserved=1 ✓ |
| 3 | UPDATE … WHERE version=7 → **1 row**, v8 | |
| 4 | commit (state + outbox) | UPDATE … WHERE version=7 → **0 rows** |
| 5 | | `ConcurrencyConflictError` → retry |
| 6 | | read v8, available=**0** → `InsufficientStock` |

Both transactions computed a valid-looking result at step 2. Only one could
persist. The loser re-reads and is correctly rejected. Note that the domain check
at step 2 is *not* what prevents the oversell — the version guard at step 3 is.
The domain check prevents the *easy* case and keeps the rule where it belongs.

If both the domain check and the version guard were somehow bypassed,
`ck_inv_reserved_le_onhand` aborts the transaction. Three independent layers.

### Known weakness: false conflicts

The version guard locks the **whole row**, but two reservations are logically
*compatible* — both merely add to `reserved`, and if `available` covers both,
there is no real conflict. The CAS cannot express that, so it serialises requests
that did not need serialising. On a flash-sale offer this degenerates: N
concurrent reservations produce O(N²) retry work, and the fixed 4-attempt budget
starts rejecting requests that stock could have satisfied. The failure is *safe*
(a false rejection, never an oversell) but it is a real availability cost.

The retry loop also has **no backoff and no jitter** — 4 immediate attempts,
which is a thundering herd (§10, D4).

### Escape hatch (recommended evolution, not v1)

For a measured hot offer, a single conditional statement removes false conflicts
entirely:

```
UPDATE inventory_stock
   SET reserved = reserved + :q, version = version + 1
 WHERE id = :id AND on_hand - reserved >= :q
```

Zero retries, no lost updates, invariant enforced by the predicate. It is *not*
the default because it moves the business rule out of the aggregate and into SQL,
which is exactly the leakage this architecture spends effort avoiding. The
`InventoryRepository` port is already shaped to allow it as an additive method
(`try_reserve(...) -> bool`) without touching the domain — take that path only
when contention is measured, not anticipated.

### Reservation writes are not guarded

`save_reservation` is an unguarded blind `UPDATE … WHERE id = :id`. Today this is
*incidentally* safe because every lifecycle transition also writes the
version-guarded stock row, so concurrent commit-vs-release collide there first.
That safety is accidental, not structural: any future reservation-only transition
would be unprotected, and the expiry sweeper already has a path where the stock
row is absent and only the reservation is written. The WHERE clause should carry
the expected status (§10, D3).

---

## 5. Event model

All events are PII-free facts, named `inventory.<EventName>` by
`qualified_event_type` from the module path, written to the outbox inside the
committing transaction.

| Event | Emitted when | Payload | Consumed by |
|---|---|---|---|
| `inventory.InventoryReserved` | units held | `reservation_id, offer_id, location, quantity` | Ordering (`on_line_reserved`) |
| `inventory.InventoryCommitted` | units shipped out of stock | same | — (available for fulfilment/analytics) |
| `inventory.InventoryReleased` | hold voluntarily returned | same + `reason` | **nobody** (§10, D1) |
| `inventory.InventoryReservationFailed` | shortage on a saga request | same + `reason` | Ordering (`on_line_reservation_failed`) |
| `inventory.InventoryExpired` | **TTL lapsed — to be added** | same | Ordering (must fail the order) |

### Decision: `InventoryExpired` is a distinct event, not a `reason` on `InventoryReleased`

Currently expiry reuses `InventoryReleased(reason="expired")`. That is wrong, and
it is the most consequential finding in this review (§10, D1).

Two arguments, one architectural and one about semantics:

**Consumers subscribe by `event_type`.** `HandlerRegistry` maps event type → handlers.
Discriminating on a payload field forces every interested consumer to subscribe
to *all* releases and filter in application code — the registry can no longer
express who cares about what, and the Published Language stops describing the
domain.

**The two facts demand opposite reactions.** "I released this hold because the
saga told me to compensate" is the *expected* end of a cancelled order. "The
clock took this hold away from you" is an unplanned loss of a promise the saga is
still relying on. Collapsing them into one type with a string discriminator hides
a state divergence behind a payload field.

Adding `InventoryExpired` requires no migration — `ck_resv_status` already
permits `'expired'`, and events are rows in the existing outbox.

### Event/aggregate coupling hazards

- `qualified_event_type` derives the wire name from the **module path**. Moving or
  renaming an event class silently renames the integration event and orphans
  every subscriber. Event classes are part of the published contract; treat
  module moves as breaking changes.
- `event_version` lives on the envelope. Payload evolution is additive-only;
  removing or retyping a field needs a version bump plus a transition window
  where consumers accept both.
- `InventoryReservationFailed` is emitted from a `carrier` `InventoryItem` that is
  constructed in memory and never persisted, so the outbox row's `aggregate_id`
  points at a row that does not exist (§10, D5).

---

## 6. Repository and Unit-of-Work boundaries

### Layer contract

```
domain/          entities, events, exceptions
                 imports: stdlib + shared_kernel.domain ONLY
                 ↑ no session, no ORM, no DTO, no settings

application/     ports (Protocol), use cases, DTOs, UoW interface
                 imports: domain + shared_kernel.application
                 ↑ no SQLAlchemy — this is the line that matters

infrastructure/  ORM models, repository adapter, concrete UoW, Selling ACL
                 imports: everything

interface/       FastAPI router, consumers
```

Verified: `domain/entities.py` imports only `uuid`, `dataclasses`, `datetime`,
its own exceptions, and `shared_kernel.domain.base`. Keep it that way.

### Repositories return domain objects only

`_item_to_domain` / `_res_to_domain` convert at the boundary. No ORM row, no
`Result`, no `Row` ever reaches the application layer. Consequence worth naming:
the returned aggregate is **detached** — mutating it does not write, and the use
case must call `save_item`/`save_reservation` explicitly. That is deliberate; it
is also exactly the property the BuyBox review found a test fake violating, so
Inventory test doubles must copy on read for the same reason.

### The leakage boundary is error translation

This is the load-bearing piece of §4. `IntegrityError` and `rowcount == 0` are
infrastructure facts. The adapter translates both into
`ConcurrencyConflictError` — an *application* error from `shared_kernel` — so:

- the use-case retry loop catches a domain-meaningful condition, not a driver
  exception;
- swapping SQLAlchemy for anything else changes no application code;
- optimistic locking stays expressible without the application knowing a
  database exists.

If `ConcurrencyConflictError` were replaced by a bare `IntegrityError` catch in
the use case, the whole layering argument collapses. It is one `try/except` in
the adapter and it is the reason this design is clean.

### UoW boundaries

Per ADR-0007 the UoW owns the transaction, and its commit is `flush → drain events
→ stage outbox → single commit`. Inventory reuses `SqlAlchemyUnitOfWork`
unchanged; `SqlAlchemyInventoryUnitOfWork` only binds its repository. **No new
pattern.**

Rules:

- **One UoW per business operation.** Each of `ReserveStock`, `CommitReservation`,
  `ReleaseReservation`, `ReserveForOrder` opens its own. The retry loop wraps the
  *whole* UoW, so a retry gets a genuinely fresh session and read.
- **One UoW per saga message, never one per batch.** A consumer handling N lines
  runs N independent UoWs (`OnOrderReservationRequested` loops over lines calling
  `ReserveForOrder` per line). One line's shortage must not roll back another
  line's success — the saga reasons per line. The expiry sweeper violates this
  (§10, D2).
- **Only `InventoryItem` is tracked.** Reservations emit no events, so they are
  not registered with the `Tracker`. The failure path must therefore call
  `uow.track(carrier)` explicitly to get its event drained.
- **The Selling ACL deliberately opens its own session**, outside the UoW, because
  a cross-context read must never join the write transaction. Consequence: the
  offer could disappear between check and write. Accepted — Inventory only needs
  the offer to have existed, and stock outliving an offer is harmless.

---

## 7. Integration points

### Selling — read-only, one narrow port

`SellingGateway.offer_exists(offer_id) -> bool`. That is the entire surface.
Implemented as a raw existence `SELECT 1 FROM offers WHERE id = :id LIMIT 1` —
deliberately *not* an import of Selling's repository or ORM, so there is no
Python coupling to break when Selling changes, and the adapter becomes an HTTP
call on extraction with no change above infrastructure.

Direction is strictly one-way: Inventory reads Selling; Selling knows nothing of
Inventory.

### Offer-anchored, never catalog-internal

`inventory_stock.offer_id` is the only external identifier in the context. There
is no `variant_id`, no `product_id`, no FK. Migration `0014` exists precisely
because `0006` anchored stock to variants — wrong, because *stock belongs to a
seller's offer*, not to a catalogue entry. Two sellers offering the same variant
hold independent stock.

Consequence: no database-level guarantee an offer exists. Orphan stock rows are
tolerated by design; the ACL check is advisory.

### Ordering — command/reply over events, already implemented

The brief said "no Ordering implementation yet." It exists and is wired. The
actual contract:

```
Ordering (orchestrator)                 Inventory (participant)
  OrderReservationRequested   ──────▶   ReserveForOrder
                              ◀──────   InventoryReserved | InventoryReservationFailed
  InventoryCommitRequested    ──────▶   CommitReservation
                              ◀──────   InventoryCommitted
  InventoryReleaseRequested   ──────▶   ReleaseReservation
                              ◀──────   InventoryReleased
                              ◀──────   InventoryExpired          ← MISSING SUBSCRIPTION
```

Inventory has **no compile-time dependency on Ordering** — its consumers match on
event-type strings and read payload dicts. The dependency direction survives, and
Inventory remains independently deployable. Ordering never calls Inventory
synchronously; `Order` is a process manager that only records events.

`ReserveForOrder` deliberately **never raises on shortage** — it emits
`InventoryReservationFailed` instead, because a saga participant that throws
gives the orchestrator no reply and strands the order until the DLQ. Compare
`ReserveStock` (the HTTP path), which *does* raise `ConflictError` for a
synchronous 409. Same invariant, two failure protocols, chosen per caller.

### BuyBox — deliberately stock-blind

BuyBox ranks on price and activity only. An offer with zero available stock can
win the BuyBox. Recorded here so it is a known consequence rather than an
oversight: making the projection stock-aware means consuming inventory events
into it, which couples a read model to a hard-consistency counter and invites a
projection that is wrong the moment it lags. Out of scope; revisit with a
"low stock" signal rather than a hard filter.

---

## 8. Failure handling

### Duplicate reservation request

Two mechanisms, layered:

1. **Application check** — `get_reservation(reservation_id)` first; a known id
   returns the existing view as a no-op.
2. **Primary-key backstop** — if two duplicates race past the check, the second
   `add_reservation` violates the PK, becomes `ConcurrencyConflictError`, retries,
   and now *sees* the row at step 1.

The caller-supplied `reservation_id` is what makes this work. Inventory never has
to guess whether two requests are "the same"; the caller asserts it.

A third layer sits above: the relay's **inbox** dedupes on
`(handler_name, idempotency_key)`, so a redelivered event never re-enters the use
case at all. Inventory's own idempotency is what protects it from *application*
retries and rebuilds, where the inbox does not apply.

### Concurrent reservation conflict

Covered in §4: version CAS → `ConcurrencyConflictError` → whole-UoW retry with a
fresh read → up to 4 attempts → surfaced as HTTP 409. The retry is *not* a
loop over the failed statement; it re-runs the business decision.

### Rollback

`AbstractUnitOfWork.__aexit__` rolls back anything not explicitly committed. Two
properties matter for Inventory:

- **State and events roll back together.** Outbox rows are staged on the same
  session before the single commit, so a rolled-back reservation cannot leave an
  `InventoryReserved` event behind. This is the whole point of the outbox pattern
  and it is what makes the counter and the saga agree.
- **`_discard_tracked` drains and drops events on rollback**, so a retried
  operation reusing an aggregate instance cannot emit the failed attempt's events
  on the next pass. Without this, a 4-attempt retry could publish 4
  `InventoryReserved` events for one reservation.

### Replay

- **Relay redelivery (at-least-once):** inbox dedupe first; if it is bypassed,
  every lifecycle transition is idempotent (§2.3), so the second application is a
  no-op. Counters are never incremented twice for one `reservation_id`.
- **Poison messages:** the relay retries with backoff to `max_retries=10`, then
  dead-letters. A permanently-failing Inventory handler therefore does *not* block
  the relay — but it *does* silently abandon the business operation after 10
  attempts, which is exactly how the §10 D1 defect turns into a paid order with
  no stock. Dead-letter depth on inventory handlers needs an alarm.
- **Rebuild:** Inventory is *not* replayable from events. It is a write model with
  authoritative state, unlike BuyBox. Recovering `inventory_stock` from the event
  log is not a supported operation; backup/PITR is. Stated explicitly because the
  BuyBox rebuild routine sets a precedent that does not transfer.

---

## 9. Migration and schema strategy

### Tables (migration `0014`, linear on `0013`)

**`inventory_stock`** — `id` PK, `offer_id`, `location`, `on_hand`, `reserved`,
`version`, `updated_at`.

**`inventory_reservation`** — `id` PK (caller-supplied), `offer_id`, `location`,
`quantity`, `status`, `expires_at`, `created_at`, `updated_at`.

### Indexes

| Index | Purpose |
|---|---|
| `inventory_stock` PK | — |
| `uq_inventory_offer_location` | uniqueness **and** the hot lookup path (`get_item`) — one index, two jobs |
| `inventory_reservation` PK | idempotency lookup by `reservation_id` |
| `ix_resv_pending_expiry` on `(expires_at) WHERE status='pending'` | sweeper scan; partial so it stays small as committed reservations accumulate |

**Missing:** `(offer_id, location, status)` on `inventory_reservation`. The I8
reconciliation query has no supporting index and will table-scan as reservation
history grows.

### Constraints

`ck_inv_onhand_nonneg`, `ck_inv_reserved_nonneg`, `ck_inv_reserved_le_onhand`,
`ck_resv_qty_pos`, `ck_resv_status`.

**Status as a CHECK, not a native enum, not a lookup FK.** Tradeoff accepted:
adding a status requires an `ALTER … DROP CONSTRAINT` / `ADD CONSTRAINT`
migration — real friction, and easy to forget when the Python `ReservationStatus`
class is edited. A Postgres `ENUM` type has the same migration cost with worse
ergonomics (`ALTER TYPE` cannot run in some transaction contexts); a lookup table
adds a join and a FK to a table with four rows. CHECK is the least-bad option,
but the Python constant and the constraint must move together — a test asserting
`ReservationStatus.ALL` equals the constraint's value set would close that gap.

**No FK from `inventory_reservation` to `inventory_stock`.** Worth being precise:
the project's no-FK rule is about *cross-context* references (offer_id, variant_id),
and it does not apply here — both tables are Inventory's own. A composite FK on
`(offer_id, location)` is available (the unique constraint it needs exists) and
would make orphan reservations unrepresentable, which is a genuine strengthening
of I8. Cost: it couples the two tables' lifecycles and constrains any future
partitioning or archival of reservation history. **Recommended**, as a follow-up
with a data check for existing orphans.

### Versioning

- `version INTEGER`, application-incremented, monotonic per row. No wraparound
  concern at INTEGER range for this workload.
- Migrations are linear (`0014` revises `0013`); no branching.
- **`0006` legacy:** the variant-anchored stock tables are superseded but never
  dropped, so the schema currently carries two inventory models — one live, one
  dead. An explicit drop migration should follow, after confirming no reads.
- Adding `InventoryExpired` needs **no migration**: `ck_resv_status` already
  permits `'expired'` and events are outbox rows.

---

## 10. Defects found during this review

These are bugs in the implementation, not open design questions. D1 is the one
that defeats the context's entire purpose.

| # | Severity | Defect | Status |
|---|---|---|---|
| **D1** | **Critical** | **TTL expiry is invisible to the saga → paid order, no stock.** | fixed |
| D2 | High | Expiry sweeper: one UoW for the whole batch, no retry, no `SKIP LOCKED` | fixed |
| D3 | Medium | `save_reservation` is an unguarded blind UPDATE | fixed |
| D4 | Medium | Retry loop has no backoff/jitter (4 immediate attempts) | fixed |
| D5 | Low | `InventoryReservationFailed` carries an `aggregate_id` for a non-existent row | fixed |
| D6 | Low | `CommitReservation` / `ReleaseReservation` dereference `get_item(...)` without a `None` check | fixed |

### D1 in detail

Nothing subscribes to `inventory.InventoryReleased`, and there is no expiry event
at all. Sequence:

1. Order placed → all lines reserved → order `RESERVED`, `PaymentRequested` emitted.
2. Reservation TTL (default 900s) lapses before payment settles.
3. Sweeper releases the stock; reservation → `EXPIRED`. **The order is not told.**
   The units are now available and can be reserved by another shopper.
4. Payment succeeds → `on_payment_succeeded` → order `PAID` → `InventoryCommitRequested`.
5. `CommitReservation` finds status `EXPIRED` → neither `COMMITTED` (idempotent
   return) nor `PENDING` (valid) → raises `ConflictError`.
6. The relay retries with backoff 10 times, then dead-letters.

Result: **an order marked PAID whose stock was never committed and may already
belong to someone else.** That is an oversell reached through the expiry path
rather than the concurrency path — the exact outcome this context exists to
prevent, and no test covers it.

Fix, three parts:
1. Emit a distinct `inventory.InventoryExpired` from the sweeper (§5).
2. Subscribe Ordering to it and fail/compensate the order.
3. Make `CommitReservation` treat `EXPIRED` as a terminal *business* outcome that
   emits a fact, not an exception thrown into the relay — a dead-lettered event
   is an invisible failure, and this one must be loud.

Also set the TTL to exceed the worst-case payment window, so expiry-during-payment
is rare rather than routine. That is mitigation, not a fix: the race remains.

---

## 11. Rejected alternatives

**1. Pessimistic locking (`SELECT … FOR UPDATE`).** Correct, and simpler to reason
about than a CAS. Rejected because the lock would be held across the entire
business transaction — including event drainage and the outbox write — turning
contention into DB-side queueing with connection-pool exhaustion as the failure
mode. A multi-line order touching several offers also invites lock-ordering
deadlocks. Reconsider only for a genuinely serial operation such as a stock
recount.

**2. Conditional atomic UPDATE as the primary mechanism.** Strictly better under
contention: no false conflicts, no retries (§4). Rejected as the *default* because
it relocates the oversell rule from the aggregate into a SQL predicate, leaving
the domain model decorative — a high price for a contention problem not yet
measured. Retained as a documented escape hatch behind the repository port.

**3. `Reservation` as a child collection inside `InventoryItem`.** The DDD-orthodox
choice, and it would make I8 structural rather than auditable. Rejected on load
amplification: reserving one unit would hydrate every reservation ever made
against that offer/location, and the aggregate would grow without bound. Splitting
them trades a structural guarantee for a reconciliation job — an explicit,
monitored cost.

**4. Event-sourced `InventoryItem`.** Attractive here: an append-only log has no
lost-update problem and gives a perfect audit trail. Rejected because the entire
monolith is state-based with an outbox (ADR-0007), and one event-sourced aggregate
would need its own store, snapshotting, and rebuild path — a second persistence
architecture for one context.

**5. `SERIALIZABLE` isolation for inventory transactions.** Would make the
oversell impossible without any version column. Rejected: it pushes conflict
detection to commit time as serialization failures needing the *same* retry loop,
costs performance across every statement in the transaction, and buries the
business rule in an isolation-level setting where no reader will find it.

**6. Redis counter with asynchronous DB reconciliation.** The standard flash-sale
answer, and much faster. Rejected outright for a *hard-consistency* context: it
makes the authoritative number non-transactional with the order, so any Redis
failure or reconciliation lag oversells. Speed is not the constraint we are
optimising.

**7. Variant-anchored stock** (what `0006` built). Wrong domain model: stock
belongs to a seller's offer, not a catalogue entry, and two sellers offering the
same variant hold independent stock. Superseded by `0014`.

**8. Reusing `InventoryReleased(reason="expired")` for expiry.** The current
implementation. Rejected on the reasoning in §5: consumers subscribe by event
type, and the two facts require opposite saga reactions.

**9. Synchronous call from Ordering into Inventory.** Simplest possible checkout —
call, get an answer, proceed. Rejected: it couples the contexts at compile time,
puts Inventory's availability on the checkout critical path, and makes the two
writes non-atomic with no compensation story. The saga exists to avoid exactly this.

**10. Postgres advisory locks keyed by `offer_id`.** Would give per-offer mutual
exclusion without a version column. Rejected: locks live outside the data model,
are invisible to anyone reading the schema, leak on connection loss, and would be
a bespoke concurrency primitive in a codebase that already has one.

---

## 12. Open questions

1. **`region` → `location` mapping.** Undefined. Who decides which location serves
   an order line, and what happens when a region has several?
2. **Multi-location allocation.** One line currently draws from exactly one
   location. Splitting a line across locations, or falling back when the primary
   is short, is unmodelled.
3. **Should BuyBox become stock-aware?** (§7 — deliberately deferred.)
4. **Backorders / permitted oversell.** I3 forbids `reserved > on_hand` absolutely.
   A seller-configurable oversell allowance would need it relaxed to
   `reserved <= on_hand + allowance`, which changes a CHECK constraint and should
   be its own ADR.
5. **Reconciliation cadence and alarm thresholds for I8** (§3).
6. **Return/refund flow.** `COMMITTED` is terminal by design; returns need a
   separate process and event.

---

## 13. Consequences

**Positive**

- Overselling is prevented by three independent layers, with the business rule
  living in the aggregate rather than in SQL.
- No new architectural pattern: UoW, Outbox, Relay, and Inbox are reused as-is.
- Caller-supplied `reservation_id` gives idempotency by primary key rather than by
  inference, which is what makes the saga safe under at-least-once delivery.
- The domain layer is genuinely pure; error translation at the adapter boundary is
  what buys that, in one `try/except`.
- Offer-anchored with a single-method read-only ACL, so Inventory stays extractable.

**Negative**

- I8 (`SUM(pending) == reserved`) is auditable, not structural — it needs a
  reconciliation job that does not exist yet.
- Row-level version CAS produces false conflicts; the design degrades under
  flash-sale contention (safely, as false rejections).
- `location` has no referential integrity; a typo strands stock.
- Inventory is not rebuildable from events, unlike every context before it. The
  BuyBox rebuild precedent does not transfer.
- The schema carries `0006`'s dead variant-anchored tables until they are dropped.

**Follow-ups**

Done (2026-07-26):

1. ~~**D1**~~ — `InventoryExpired` added, Ordering subscribed via
   `ordering.on_inventory_expired`, and `CommitForOrder` replies with a fact
   instead of throwing `EXPIRED` into the relay.
2. ~~**D2**~~ — sweeper rewritten to one UoW + one `FOR UPDATE SKIP LOCKED` claim
   per reservation, with retry.
3. ~~**D3/D4**~~ — `save_reservation` is a status CAS; retry uses exponential
   backoff with full jitter.
4. ~~`(offer_id, location, status)` index~~ — migration `0016`.
5. ~~Test asserting `ReservationStatus.ALL` matches `ck_resv_status`~~.
6. ~~**D5/D6**~~ — failure replies emit from a persisted carrier; `None` guards
   on `get_item`.

Remaining:

7. **Reconciliation job for I8** — the index exists, the periodic check does not.
8. **Composite FK `inventory_reservation → inventory_stock`** — needs an orphan
   data check against a live database first.
9. **Drop the `0006` legacy variant-anchored tables**, after confirming no reads.
10. **Payment-vs-expiry race.** With expiry now failing the order, a payment that
    settles *after* the sweeper fires leaves a FAILED order with money taken —
    a refund case, not an oversell. Closing it properly needs the real Payments
    context; the stub auto-approves, so the window is reachable in tests today.
