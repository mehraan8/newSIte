# ADR-0010 — Ordering as the checkout saga / process manager

- **Status:** Proposed — design ratified, **two critical defects block go-live** (§11)
- **Date:** 2026-07-27
- **Related:** ADR-0007 (UoW owns the transaction), ADR-0008 (event envelope), ADR-0009 (Inventory hard consistency)

> **Scope note.** As with ADR-0009, the brief framed this as greenfield design.
> Ordering already exists and is wired: `src/modules/ordering/**`, migration
> `0015`, four consumers registered in `outbox/runner.py`. This ADR therefore
> ratifies the design as built, records the decisions that were made implicitly,
> and separates them from defects. §11 lists what is actually broken. Everything
> above it describes intended design, and where the code disagrees with the
> design this ADR names the code as wrong.

---

## 1. Context

Checkout is the first operation that must coordinate **three** contexts —
Inventory (hard-consistency stock), Payments (money, not yet built), and Ordering
itself — with no distributed transaction available and no shared database
transaction permitted across context boundaries.

The constraint that shapes everything: **stock is reserved before money is
taken, and money is taken before stock is committed.** Those are two different
databases' worth of state in the general case, and the window between them is
where every marketplace failure story lives.

Forces:

- No synchronous cross-context calls. Availability must not multiply: a checkout
  that requires Inventory *and* Payments *and* Ordering to be simultaneously up
  has worse uptime than any of them.
- Reuse UoW / Outbox / Relay / Inbox unchanged. A second coordination mechanism
  would fork the architecture.
- Payments does not exist. The design must have a seam, not a placeholder that
  leaks assumptions.
- Inventory holds stock on a TTL (ADR-0009). Ordering must treat that TTL as a
  hard deadline it does not control.

---

## 2. Order domain model

### 2.1 `Order` — aggregate root **and** process manager

The single most consequential decision: **the saga is not a separate object.**
`Order` holds both the business state and the coordination state machine, and it
advances by recording events — it never calls another context.

| Field | Meaning |
|---|---|
| `id` | saga correlation anchor |
| `customer_id` | Identity reference (opaque) |
| `region` | market of record |
| `currency` | ISO-4217, fixed at placement |
| `lines` | immutable snapshot lines (§2.2) |
| `status` | `OrderStatus` (§2.3) |

`total_amount` is **derived** (`sum(line.line_total)`), never stored on the
aggregate — the same reasoning as Inventory's `available`. A stored total is a
third number that can disagree with the lines. (It *is* persisted as a
denormalised column for querying; see §11 O8.)

Reaction methods, one per participant reply: `on_line_reserved`,
`on_line_reservation_failed`, `on_line_reservation_expired`,
`on_payment_succeeded`, `on_payment_failed`, plus `cancel()`.

### 2.2 `OrderLine` — immutable snapshot

A line captures what was true **at placement**, and never changes afterwards
except its `status`. Price is a snapshot precisely because Selling may reprice
and BuyBox may hand the win to another seller a second later; the customer is
owed the price they were shown.

| Field | Mutable? |
|---|---|
| `offer_id`, `location`, `quantity`, `unit_amount` | **never** |
| `reservation_id` | never (minted once, at construction) |
| `status` | yes — `LineStatus` only |

**Decision: the price snapshot is server-derived, not client-supplied.**
Ordering MUST resolve `unit_amount` from BuyBox/Selling through a read-only ACL
at placement time, and MUST reject or overwrite any price in the request. The
current implementation takes `unit_amount` straight from the HTTP body — see
§11 O1. This is the difference between a snapshot and a customer-declared price.

### 2.3 `OrderStatus` state machine

```
                        all lines reserved
   PENDING ─────────────────────────────────▶ RESERVED
      │                                          │
      │ any line failed/expired                  │ payment succeeded
      ▼                                          ▼
   FAILED ◀───── any line failed/expired ──── PAID  (terminal)
      ▲                                          
      └──────── cancel() ────── CANCELLED (terminal)
```

| From | Trigger | To | Emits |
|---|---|---|---|
| — | `place()` | PENDING | `OrderPlaced`, `OrderReservationRequested` |
| PENDING | all lines reserved | RESERVED | `OrderReserved`, `PaymentRequested` |
| PENDING/RESERVED | line failed or expired | FAILED | `OrderFailed`, `InventoryReleaseRequested`\* |
| RESERVED | payment succeeded | PAID | `OrderPaid`, `InventoryCommitRequested` |
| RESERVED | payment failed | FAILED | `OrderFailed`, `InventoryReleaseRequested`\* |
| PENDING/RESERVED | `cancel()` | CANCELLED | `OrderCancelled`, `InventoryReleaseRequested`\* |
| PAID | `cancel()` | **rejected** | — |

\* only when reserved lines exist to release.

**`PAID` is terminal.** Reversing a paid order is a refund/return — a different
process with its own events and its own money movement. Modelling it as an
Ordering state transition would put refund authority in the wrong context.

### 2.4 `LineStatus`

```
PENDING ──▶ RESERVED ──▶ COMMITTED   (terminal)
   │            │
   │            └──────▶ RELEASED    (compensated)
   └───────────────────▶ FAILED      (shortage or expiry)
```

### 2.5 Value objects

Deliberately none beyond primitives. `unit_amount` is minor units + the order's
`currency`, mirroring `Money` without importing it, because a line's currency is
fixed by the order and a per-line `Money` would invite mixed-currency orders.
**Accepted debt:** this duplicates `Money`'s scale concept without its
validation. Revisit if multi-currency orders are ever allowed.

---

## 3. Ownership boundaries

### Ordering owns

- The `Order` and `OrderLine` rows, and their entire lifecycle.
- **`reservation_id` minting.** Ordering generates it at line construction, and
  it is simultaneously (a) the correlation key that maps an Inventory reply back
  to a line and (b) the idempotency key Inventory dedupes on. One identifier,
  two jobs, minted by the party that can guarantee uniqueness per line.
- The price snapshot and the order total, once server-derived.
- The saga's state machine and every compensation decision.

### Ordering references (by id, never joined)

| Reference | Owner | How obtained |
|---|---|---|
| `offer_id` | Selling | read-only ACL at placement |
| `unit_amount` | BuyBox/Selling | read-only ACL at placement, then frozen |
| `location` | Inventory | supplied by the caller, validated by Inventory |
| `customer_id` | Identity | **must** come from the authenticated principal (§11 O2) |

Never referenced: `variant_id`, `product_id`, or any Catalog internal. Ordering
reaches Catalog only transitively through the offer, and never directly.

### Ordering must never mutate

- `inventory_stock` / `inventory_reservation` — it *requests* via command events
  and waits for a reply. It has no Inventory repository, no Inventory import,
  and no write path.
- `offers` / `offer_prices` — a placed order does not change what is for sale.
- BuyBox projections — derived, owned by their projector.
- Payment records — Payments will own capture and refund authority.

**Verified:** `grep "from src.modules"` inside `src/modules/ordering/` returns
nothing outside Ordering itself. The context has no compile-time dependency on
any other module.

---

## 4. Saga design

Ordering is an **orchestrator**: it holds the state, decides the next step, and
issues commands. Participants (Inventory, Payments) react and reply. The
alternative — choreography, where each context listens for the others' facts —
was rejected (§12.2).

Command events are named imperatively (`…Requested`) and are addressed to one
participant. Reply events are facts named in the past tense, owned by the
participant that emitted them.

```
  Customer                Ordering                 Inventory            Payments
     │  POST /orders          │                        │                    │
     ├───────────────────────▶│                        │                    │
     │   201 PENDING          │ OrderPlaced            │                    │
     │◀───────────────────────┤ OrderReservationRequested ─────────────────▶│
     │                        │                        │ reserve per line   │
     │                        │◀── InventoryReserved ──┤ (idempotent)       │
     │                        │  (one per line)        │                    │
     │                        │                        │                    │
     │                    all lines reserved           │                    │
     │                        │ OrderReserved          │                    │
     │                        │ PaymentRequested ──────┼───────────────────▶│
     │                        │◀────────── PaymentSucceeded ────────────────┤
     │                        │ OrderPaid              │                    │
     │                        │ InventoryCommitRequested ──────────────────▶│
     │                        │                        │ commit → stock out │
```

**Step 1 — reserve.** `Order.place()` records `OrderPlaced` *and*
`OrderReservationRequested` carrying one entry per line
(`offer_id`, `location`, `quantity`, `reservation_id`). Inventory reserves each
line independently and replies per line. Partial success is normal and expected.

**Step 2 — confirm reservation.** `on_line_reserved` flips one line to RESERVED.
Only when **all** lines are RESERVED does the order transition and emit
`OrderReserved` + `PaymentRequested`. The all-or-nothing gate lives in the
aggregate, not in a handler.

**Step 3 — payment.** `PaymentRequested` carries `order_id`, `amount`,
`currency`. Today a stub consumer inside Ordering auto-approves it; that is the
seam Payments replaces (§8).

**Step 4 — commit.** `on_payment_succeeded` sets PAID, marks reserved lines
COMMITTED, and emits `InventoryCommitRequested` with the reservation ids.
Inventory converts holds into stock-out.

**Compensation** is always the same shape: `_compensate_reserved()` collects
lines currently RESERVED, flips them to RELEASED, and emits a single
`InventoryReleaseRequested`. Lines that never reserved need no compensation;
lines already COMMITTED are past the point of no return.

---

## 5. Event flow

### Consumed by Ordering

| Event | Handler | Reaction |
|---|---|---|
| `inventory.InventoryReserved` | `ordering.on_inventory_reserved` | line → RESERVED; if all reserved → RESERVED + request payment |
| `inventory.InventoryReservationFailed` | `ordering.on_inventory_reservation_failed` | line → FAILED; order → FAILED; compensate siblings |
| `inventory.InventoryExpired` | `ordering.on_inventory_expired` | same as failure, reason `reservation_expired` |
| `ordering.PaymentRequested` | `ordering.payment_stub_on_payment_requested` | **stub** — auto-approves |
| `payments.PaymentSucceeded` | *(future)* | → PAID, request commit |
| `payments.PaymentFailed` | *(future)* | → FAILED, compensate |

### Deliberately NOT consumed

- **`inventory.InventoryReleased`** — this is the echo of Ordering's own
  compensation command. Consuming it would make the saga react to its own
  action, risking a loop. The line is already RELEASED when the command is sent.
- **`inventory.InventoryCommitted`** — the order is already PAID; the fact adds
  no decision. Fulfilment will consume it.

### Emitted by Ordering

| Event | Kind | Addressed to |
|---|---|---|
| `ordering.OrderPlaced` | fact | anyone (analytics, notification) |
| `ordering.OrderReservationRequested` | **command** | Inventory |
| `ordering.OrderReserved` | fact | — |
| `ordering.PaymentRequested` | **command** | Payments |
| `ordering.OrderPaid` | fact | fulfilment, notification |
| `ordering.InventoryCommitRequested` | **command** | Inventory |
| `ordering.InventoryReleaseRequested` | **compensation command** | Inventory |
| `ordering.OrderFailed` | fact | notification |
| `ordering.OrderCancelled` | fact | notification |

All are PII-free: ids, amounts, currency, region only.

### Late payment after expiry — the unresolved case

The sequence ADR-0009 §13 flagged and this ADR must now specify:

1. Order RESERVED, `PaymentRequested` emitted.
2. Inventory TTL lapses → `InventoryExpired` → order FAILED, siblings released.
3. Payment settles **successfully**, late.
4. `on_payment_succeeded` requires status RESERVED; the order is FAILED, so it
   **returns silently**.

Net: money captured, order failed, stock correctly released, and **no record
that a refund is owed.** Not an oversell — Inventory's invariants all hold — but
a money-side inconsistency that vanishes without trace.

**Decision:** `on_payment_succeeded` on a FAILED/CANCELLED order must record a
`ordering.PaymentRefundRequired` fact (`order_id`, `amount`, `currency`,
`reason`) instead of returning. Payments consumes it when it exists; until then
it feeds an alert and a manual reconciliation queue. **This is specified here and
deliberately not implemented** — see §10 and §11 O5.

**Operational mitigation available now:** reservation TTL must exceed the payment
authorization window with margin, and relay lag beyond TTL/3 must alarm. That
turns a silent race into an alarm that fires before the race can.

---

## 6. Compensation matrix

| # | Failure point | Order → | Compensated | Immutable | Retried | Escalated |
|---|---|---|---|---|---|---|
| 1 | One line short at reserve | FAILED | sibling RESERVED lines released | price snapshot, line qty | Inventory reserve (relay retry) | after 10 attempts → DLQ |
| 2 | All lines short | FAILED | nothing to release | snapshot | reserve | DLQ |
| 3 | Hold expires pre-payment | FAILED | sibling RESERVED lines released | snapshot | — (TTL is terminal) | — |
| 4 | Payment fails | FAILED | all RESERVED lines released | snapshot, order id | payment (Payments' policy) | DLQ |
| 5 | Payment succeeds late, order FAILED | stays FAILED | stock already released | **money — needs refund** | — | **`PaymentRefundRequired` → manual queue** |
| 6 | Commit fails (hold gone) | stays PAID | none possible — money taken | order, lines | Inventory replies a fact, no raise | `InventoryReservationFailed` → refund path |
| 7 | Customer cancels pre-payment | CANCELLED | RESERVED lines released | snapshot | — | — |
| 8 | Customer cancels post-payment | **rejected** | — | everything | — | returns process (out of scope) |
| 9 | Crash mid-saga | unchanged | none needed | all | relay redelivers | — |
| 10 | Late reserve on a FAILED order | stays FAILED | **nothing — stock leaks until TTL** | snapshot | — | §11 O10 |

**What is never compensated:** the price snapshot, the quantity, the
`reservation_id`, and any COMMITTED line. Those are the record of what was
agreed.

---

## 7. Idempotency strategy

Three independent layers, each covering what the one above cannot:

| Layer | Mechanism | Covers |
|---|---|---|
| 1. Inbox | `(handler_name, idempotency_key)` | relay redelivery of the same event |
| 2. State-machine guards | every reaction checks line/order status first | replays that bypass the inbox, out-of-order arrival, rebuilds |
| 3. `uq_order_line_reservation` | unique index on `reservation_id` | two lines ever claiming one reservation |

**Duplicate commands.** `POST /orders` is *not* idempotent today: a retried
checkout creates a second order with fresh `reservation_id`s. Ordering owns this
gap. **Decision:** accept a client-supplied `Idempotency-Key` header, stored
unique per customer, returning the original `OrderView` on replay. Deferred, but
required before public API exposure (§10).

**Duplicate events.** Every reaction is guard-first:
`on_line_reserved` returns unless the line is PENDING; `on_payment_succeeded`
returns if already PAID (idempotent) or not RESERVED (not applicable). No
reaction increments anything — all are assignments — so second application is a
no-op by construction.

**Replay safety.** Ordering is a write model with authoritative state; it is
**not** rebuildable from its event log, unlike BuyBox. Recovery is backup/PITR.
Stated explicitly because the BuyBox rebuild precedent does not transfer.

---

## 8. Repository, UoW and integration boundaries

**Layering** is identical to Inventory and verified clean: `domain/` imports only
stdlib + `shared_kernel.domain`; no SQLAlchemy above `infrastructure/`;
`SqlOrderRepository` returns `Order`/`OrderLine` domain objects only, with
`selectinload` for lines so no lazy-load escapes the session.

**UoW.** `SqlAlchemyOrderingUnitOfWork` reuses `SqlAlchemyUnitOfWork` unchanged —
`flush → drain events → stage outbox → single commit`. Saga state and the
commands it emits are therefore atomic. No new pattern.

**Consumers** declare `TransactionMode.OWN_UNIT_OF_WORK` (they emit events, which
requires a UoW to drain the outbox). Per the dispatcher contract that means
delivery is at-least-once and safety comes from layer 2 above, not the inbox.

**Selling / BuyBox.** Read-only, at placement time only, through a narrow ACL
returning `(offer_exists, is_active, unit_amount, currency)` — mirroring
Inventory's `SellingGateway`: a narrow SQL read behind a Protocol, replaceable
with an HTTP call on extraction. **This ACL does not currently exist** (§11 O1/O3).

**Inventory.** Events only, both directions. No port, no gateway, no import.

**Payments.** A stub consumer inside Ordering's interface layer auto-approves
`PaymentRequested`. When Payments lands: delete the stub, register its
`PaymentSucceeded`/`PaymentFailed` handlers. Ordering's domain does not change —
which is the test of whether the seam was drawn correctly.

---

## 9. Failure scenarios

**Reservation failure.** Inventory replies `InventoryReservationFailed` rather
than raising (ADR-0009), so the saga always gets a reply. Order → FAILED,
siblings released. No customer-visible hang.

**Payment failure.** `on_payment_failed` → FAILED + compensate. Currently
unreachable: nothing emits `PaymentFailed` (§11 O6).

**Expiry before payment.** Covered in §5. The saga hears about it because
Inventory emits a *distinct* `InventoryExpired` (ADR-0009 D1) rather than
overloading `InventoryReleased`.

**Duplicate replay.** Three layers, §7.

**Partial saga progress.** By design. Each line reserves independently; the
dispatcher's per-handler savepoints mean one handler's failure does not roll back
its siblings. The order sits in PENDING until every line reports, and TTL bounds
how long that can last.

**Crash between steps.** The outbox guarantees a command is durable iff the state
change that produced it committed. A crash after commit but before relay
delivery is just an undelivered row — redelivered later. A crash mid-handler
rolls back that handler's UoW; the event is retried. The invariant: **there is no
state in which Ordering believes it has issued a command that was never
durably recorded.**

---

## 10. Readiness criteria

**Must be true before Ordering is exposed to real customers:**

1. **O1 fixed** — price resolved server-side through the Selling/BuyBox ACL.
2. **O2 fixed** — `customer_id` taken from the authenticated principal.
3. **O3 fixed** — offer existence/active checked at placement.
4. **O5 specified path implemented** — `PaymentRefundRequired` recorded.
5. **O10 fixed** — late reserve on a terminal order releases immediately.
6. Idempotency-Key on `POST /orders`.
7. The live-Postgres saga suite actually executed (ADR-0009's caveat applies:
   it has never run in this environment).
8. Relay and expiry workers deployed and alarmed; I8 reconciliation scheduled.

**Intentionally deferred to Payments / Ledger:**

- Capture, authorization windows, retries, provider adapters, 3DS.
- Refund execution — Ordering only records that one is owed.
- Double-entry ledger, settlement, seller payouts.
- Returns/RMA — the process that can undo a PAID order.
- Partial fulfilment and split shipments.
- Multi-currency orders and FX.

---

## 11. Defects in the current implementation

| # | Severity | Defect |
|---|---|---|
| **O1** | **Critical** | `unit_amount` is taken from the HTTP body — a client can buy anything at any price |
| **O2** | **Critical** | `customer_id` is taken from the HTTP body — orders can be placed as any user |
| O3 | High | No offer validity check at placement; Inventory reports `insufficient_stock` for a non-existent offer |
| O4 | High | `Order` has no version column; saga correctness depends on the relay running a single instance |
| O5 | High | Late payment on a FAILED order is silently discarded (§5) |
| O6 | Medium | `HandlePaymentFailed` is unreachable — nothing emits `PaymentFailed` |
| O7 | Medium | `Order.cancel()` has no endpoint |
| O8 | Medium | `total_amount` persisted but never recomputed on `save()` |
| O9 | Low | `order.region` and the envelope's `ctx.region` can diverge |
| O10 | High | Late `InventoryReserved` on a FAILED order marks the line RESERVED and releases nothing — stock leaks until TTL |

**O1 and O2 together** are a complete authorization bypass: place an order as any
customer, for any offer, at any price. Neither is exotic; both are the first
thing a marketplace review looks for. They block go-live absolutely.

**O4** deserves emphasis: `orders_saga` has no optimistic lock, so two concurrent
reactions to different lines of the same order can lost-update each other's line
status. Today the relay is pinned to `replicas: 1` in compose, which serialises
all handlers. That pin is load-bearing correctness, not a performance choice, and
it is a hard scaling ceiling.

**O10** is self-healing but wasteful: the leaked hold is reclaimed by Inventory's
TTL sweeper. That the system survives it is a good sign the TTL safety net works;
that it happens at all is still a defect.

---

## 12. Rejected alternatives

**12.1 Two-phase commit / synchronous distributed transaction.** Explicitly
forbidden, and rightly: it multiplies availability, holds locks across network
calls, and has no story when the coordinator dies. The saga trades atomicity for
eventual consistency plus explicit compensation — the compensation matrix in §6
is the price, paid visibly.

**12.2 Choreography instead of orchestration.** Each context listens for the
others' facts and decides for itself. Rejected: the "all lines reserved → charge"
decision has to live *somewhere*, and spreading it across participants makes it
unauditable. With orchestration the whole checkout policy is readable in one
aggregate, and there is exactly one place to look when an order is stuck.

**12.3 Separate `SagaState` table beside `Order`.** The textbook process-manager
layout. Rejected: it splits one lifecycle across two rows that must be kept
consistent, recreating Inventory's I8 problem for no benefit. The order *is* the
process; there is no order that is not in some saga state.

**12.4 Synchronous call into Inventory at placement.** Tempting — the customer
gets an immediate yes/no instead of a PENDING order. Rejected: it couples
checkout availability to Inventory's, makes the two writes non-atomic with no
compensation story, and re-creates the distributed transaction through the back
door. The cost accepted: the customer sees PENDING and must poll or be notified.

**12.5 Reserving stock synchronously but paying asynchronously.** A halfway house.
Rejected for the same reason plus a worse one: it makes the *fast* path
synchronous and the *slow* path async, which is exactly backwards.

**12.6 Inventory-owned reservation ids.** Let Inventory mint them and return them.
Rejected: the reply would have no key to correlate against, so Ordering would
need a second correlation mechanism. Caller-minted ids make the request naturally
idempotent — Inventory dedupes on a key the caller can reproduce on retry.

**12.7 Compensating by deleting the order.** Rejected outright. A failed order is
a business record — customers ask why, sellers dispute, fraud review needs it.
Failure is a status, never a delete.

**12.8 Holding the customer's HTTP connection until the saga completes.** Rejected:
couples request lifetime to the slowest participant and gives no better guarantee
than polling, because the connection can drop at any point anyway.

---

## 13. Why this survives a 10-year marketplace review

**Every cross-context interaction is a durable, replayable, inspectable row.**
Ten years in, the question is never "what does the code do" but "what happened to
order X on that Tuesday". The outbox answers it.

**The saga policy is one readable aggregate.** New engineers ask "when do we
charge?" — the answer is a single method, not a distributed argument between four
consumers.

**TTL is the universal safety net.** Every hold self-releases. That means the
worst outcome of almost any bug in this design — including O10 — is *temporarily
stranded stock*, not oversold stock and not lost money. Designs without that net
degrade toward permanent inconsistency; this one degrades toward a delay.

**Money and stock are never assumed, always confirmed.** Ordering does not
believe stock is held until Inventory says so, and does not commit stock until
Payments says so. Each belief is grounded in a fact event from the context that
owns it.

**The Payments seam is a real boundary, not a placeholder.** Replacing the stub
requires deleting one consumer and registering two. If that turns out to be
false, the seam was wrong — and it is cheap to test now, before money is real.

**Compensation is enumerable.** §6 is a finite table. A design whose failure modes
cannot be tabulated cannot be reviewed, and an unreviewable checkout is how
marketplaces lose money quietly for years.

**Accepted long-term costs, stated plainly:** the customer sees PENDING rather
than an instant answer; the relay is a single point of serialisation until O4 is
fixed; compensation is eventual, so a shopper can briefly see stock they cannot
buy; and the price snapshot means a repriced offer still honours the shown price,
which is a deliberate cost of trust.
