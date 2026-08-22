# ADR-0011 — Authorization architecture (RBAC + object-level)

- **Status:** Approved — Revision 4. Implementation in progress.
- **Date:** 2026-08-08
- **Related:** ADR-0007 (UoW owns the transaction), ADR-0008 (event envelope), ADR-0009 (Inventory hard consistency), ADR-0010 (Ordering checkout saga)

> **Numbering note.** This ADR was drafted as "ADR-0010" before it was noticed
> that `0010-ordering-checkout-saga.md` already holds that number. It is
> numbered **0011** here; internal references have been updated. Earlier drafts
> circulated as "ADR-0010 Rev 1–4" refer to this document.

> **Scope note.** An earlier authorization implementation existed and was lost
> before it reached `main`. It is preserved on branch
> `preserve/pre-adr0011-authorization` for reference only. It conflicts with
> this ADR in five documented ways and **must not** be used as the architecture.

---

## 1. Context

An API audit found ten write endpoints (Catalog Admin, Selling, Inventory)
reachable with **no credential at all**, documented as sitting behind a gateway
RBAC that was never built. The RBAC *schema* (`roles`, `permissions`,
`role_permissions`, `user_roles`) has existed since migration `0001` and was
never populated or read; token issuance hardcoded `roles=[]`, so nothing could
fail closed in the gateway's absence.

This ADR freezes the authorization architecture for a system intended to grow
into a production marketplace with Clean Architecture, DDD boundaries, an
event-driven core with a transactional outbox, and eventual microservice
extraction.

---

## 2. Non-negotiable principles

1. **The Domain layer is security-free.** No Principal, User, Role, Permission,
   Authorization, JWT, or Identity import in any aggregate.
2. Authorization lives only in the **Router** (coarse) and the **Application
   layer** (ownership and policy). Never in the Domain.
3. **Event consumers are never authorized.** Relay, saga handlers, projectors
   and workers execute with no user principal. Events are already-authorized
   facts.
4. **Principal is an explicit parameter.** Never a ContextVar, never
   thread-local, never ambient.
5. **Each bounded context owns its authorization policy.** No central God
   authorization service.
6. **Identity owns authentication, users, tokens and platform roles only.**
   Identity must not know business ownership.

---

## 3. The four-layer model

| Layer | Question | Where | Failure |
|---|---|---|---|
| Authentication | Who is this? | Composition root (`api/security`) | `401` |
| Principal assembly | What is this actor's capability set? | Composition root, via two ports | `401` |
| Coarse authorization | May this actor perform this *kind* of action? | **Router**, declarative | `403` |
| Object-level authorization | May they perform it on *this* object? | **Application layer**, pure policy, inside the transaction | `403` / `404` |

### 3.1 Two entry classes

| Entry class | Principal? | Examples |
|---|---|---|
| **Command entry** (HTTP) | **Yes — explicit parameter** | `PlaceOrder`, `ActivateOffer`, `ReceiveStock`, `CommitReservation` |
| **Event reaction** (relay, saga, projector, worker) | **No — the parameter does not exist** | `ReserveForOrder`, `CommitForOrder`, `HandlePaymentSucceeded`, BuyBox projectors, `UserRegisteredHandler`, expiry sweeper |

The codebase already splits `CommitReservation` (HTTP) from `CommitForOrder`
(saga). That existing seam is exactly where the Principal boundary falls, so the
rule is structural rather than conventional: a consumer cannot be authorized
because there is nothing to authorize with.

### 3.2 Why not the Router for ownership
A router check must load the object, then the use case loads it again inside the
transaction — a TOCTOU window. Ownership is evaluated inside the transaction
that mutates.

### 3.3 Why not the Domain
Authorization states *who may invoke*, not *which states are valid*. A cancelled
order is equally valid whoever cancelled it. Putting a Principal in the domain
would couple every context to Identity, make aggregates untestable without a
security context, and break the event path outright.

---

## 4. Principal

Immutable value object in the Shared Kernel application layer. Contains only
what policies consult.

| Field | Type |
|---|---|
| `subject_id` | `UUID` |
| `tenant_id` | `UUID \| None` |
| `platform_permissions` | `frozenset[Permission]` |
| `memberships` | `Mapping[seller_id, SellerGrant]` |

Two accessors: `can(permission)` and `can_for_seller(permission, seller_id)`.
`SellerGrant` carries the seller's resolved permission set, status and tenant, so
**a policy can decide seller-scoped authorization entirely from the Principal and
performs no I/O**.

Deliberately absent: mobile, email, name, token string, raw JWT, request
metadata, IP, locale. A Principal is a capability set, not a user profile.

---

## 5. Permissions — code-first

The `Permission` enum in the Shared Kernel is the **single source of truth** for
what permissions exist. The `permissions` table is a **synchronized projection**,
written only by generated migrations. `role_permissions` remains database-backed.

```
Permission enum (version-controlled)
        → generated delta migration (deterministic literals)
        → permissions table (projection)
        → role_permissions (DB-owned mappings)
```

Each member carries: dotted `code`, human `description`, owning `context`, and
`scope` (`platform` | `seller_scopable`). Naming convention:
`<context>.<resource>.<action>`, with `.own` / `.any` where scope matters.

**String boundary rule.** Permission strings may exist only in database rows and
in generated migration literals. Everywhere else — policies, routers, route
declarations, tests — references are `Permission` members.

**Drift policy.** A DB code with no enum member is **dropped and logged**
(fail-closed). An enum member with no DB row fails the drift check in non-production
and warns in production. Deprecation is two-phase: revoke grants, then delete the
row a release later.

---

## 6. Roles

Platform roles: `customer`, `catalog_admin`, `inventory_manager`,
`support_agent`, `readonly_operator`, `super_admin`, plus implicit `anonymous`.

**`seller` is not a platform role.** Selling capability derives from *membership*
in a Seller, not from a global role. There is no such thing as "a seller"
globally — only "a member of seller X".

---

## 7. Merchant context — Seller / SellerMember

`Seller` and `SellerMember` belong to a **Merchant** bounded context, not to
Selling (which is about offers and prices) and not to Identity (principle 6).

| Aggregate | Attributes |
|---|---|
| **Seller** | id, code/slug, display name, status (`pending`/`active`/`suspended`), tenant_id |
| **SellerMember** | seller_id, user_id, member_role_code, status |

Member roles: `seller_owner`, `seller_manager`, `seller_staff`, `seller_viewer`.

The ownership predicate — `offer.seller_id ∈ principal.memberships` — is
identical for a single vendor, merchant staff, multi-vendor marketplace, a user
acting for two merchants, and multiple stores per seller. **No ownership rewrite
is ever required.**

### 7.0 Member roles → seller-scoped permissions (approved Phase 1F)

Earlier revisions named the four member roles but never said what each may do,
which left the actual authority implicit. This subsection records the approved
matrix so it is no longer a decision anyone has to re-derive.

| Seller-scoped permission | owner | manager | staff | viewer |
|---|:--:|:--:|:--:|:--:|
| `merchant.sellers.read.own` | ● | ● | ● | ● |
| `merchant.members.read` | ● | ● | | ● |
| `merchant.sellers.update` | ● | | | |
| `merchant.members.invite` | ● | | | |
| `merchant.members.remove` | ● | | | |
| `selling.offers.read.own` | ● | ● | ● | ● |
| `selling.offers.create` | ● | ● | ● | |
| `selling.offers.activate` | ● | ● | | |
| `selling.offers.suspend` | ● | ● | | |
| `selling.offers.price.change` | ● | ● | | |
| `inventory.stock.read.own` | ● | ● | ● | ● |
| `inventory.stock.receive` | ● | ● | ● | |
| `inventory.reservations.read` | ● | ● | ● | ● |
| **total** | **13** | **10** | **6** | **5** |

**Semantics.** `seller_owner` holds the complete seller-scoped set, including
member management and seller profile updates. `seller_manager` holds commercial
and operator authority — the full offer lifecycle including price, plus stock —
but **not** member management and **not** seller profile updates.
`seller_staff` holds day-to-day operations: create offers, receive stock, read.
`seller_viewer` is read-only, with no mutation of any kind.

Three boundaries are deliberate and are protected by tests that provably fail if
removed:

1. **`seller_staff` may not change prices.** Repricing is a commercial decision,
   not a day-to-day operation.
2. **`seller_staff` may not activate or suspend offers.** Putting an offer on or
   off the market is a commercial lifecycle act.
3. **Member management is owner-only.** A manager cannot grant, revoke or
   otherwise administer business membership.

**The two vocabularies stay separate.** No member role may hold a
platform-scoped permission, and no platform role may hold a seller-scoped one
(§6, §5). The two permission universes are disjoint, member roles are Merchant-
owned and are *not* rows in the platform `roles` table, and there is no
`roles.scope` discriminator. A permission reached through this matrix is
exercisable only via `Principal.can_for_seller`, never through
`Principal.platform_permissions`.

The matrix lives in `merchant/application/member_permissions.py`, is code-first
and DB-free, and its `validate()` guard fails the build on drift.

### 7.1 Seller identity is never derived from a User identity

**`User != Seller`, without exception.** A Seller ID must never be derived from a
User ID merely because that user created an offer. A User reaches a Seller only
through a `SellerMember` grant; the seller a caller acts for is chosen from their
memberships and verified with `can_for_seller`.

**CURRENT STATE (Phase 1G — resolved).** `POST /api/v1/selling/offers` once set
`seller_id` to the authenticated user's id, collapsing `User == Seller`. That
derivation was removed in Phase 1 pre-C and the endpoint stood at
`503 capability_unavailable` until Merchant (1C) and membership resolution (1F)
existed. Phase 1G restored it over real memberships: the caller **nominates** a
target seller and the nomination is honoured only if it appears in
`Principal.memberships` with `selling.offers.create` (§9.2).

The source-level guard in `tests/test_seller_identity_boundary.py` still fails if
the derivation is reintroduced anywhere in `src/`, and
`tests/test_selling_authorization_guards.py` adds the structural guards that make
the rule hold for code nobody has written yet.

### 7.2 Seller identity backfill — corrected

The id-preserving backfill described in §12 (M4) was **conditional on every
existing `offers.seller_id` being a legitimate seller identity**. A read-only data
audit disproved that condition:

- 12 offers held **5 distinct `seller_id` values**;
- only **2** resolved to a `users` row — the rest were arbitrary UUIDs accepted by
  the pre-D4 endpoint, which took `seller_id` from the request body;
- every row was created inside a single 87-minute automated audit session, and
  the accompanying product slugs still carried that session's test markers.

Consequences, now binding:

1. **Development and test databases are NOT valid input for a Seller identity
   backfill.** They contain disposable audit residue, not merchant data.
2. **User IDs must never become canonical Seller IDs**, and no `SellerMember` may
   be fabricated for an orphan UUID that has no owning user.
3. **The clean development path starts with fresh Merchant Seller identities.**
   The disposable residue was removed by a separate, explicit operational
   cleanup — deliberately *not* inside a schema migration, so that a data
   deletion is never disguised as a schema change.
4. **Real business data must be handled differently.** If seller-bearing
   production data exists when Merchant is introduced, a separate audited
   mapping/migration strategy must be designed for it — with the ordering
   constraint that seller ids appear in immutable outbox events, so any remap of
   projection data without a matching history strategy is silently reverted by a
   BuyBox rebuild.

---

## 8. JWT

| Claim | Present |
|---|---|
| `sub` | Yes |
| `type` | Yes (`access` / `refresh`) |
| `tenant_id` | Yes — must come from the token, never from the `X-Tenant-ID` header, which is caller-controlled |
| `roles` | Yes — platform role **codes** |
| **permissions** | **Never** |
| **seller_ids / memberships** | **Never** |
| PII | **Never** |

Platform-role revocation is therefore bounded by access-token TTL. Seller
membership revocation follows §10.5 and is not bounded by token TTL. This
asymmetry is deliberate: roles are coarse and slow-changing; seller authority is
fine-grained and security-critical.

**ACCOUNT status is a third case, and is NOT bounded by token TTL** (§8.1). A
valid signature proves the token was issued; it does not prove the account is
still permitted to act.

### 8.1 Account status — the authentication gate (Phase 1G.1)

`users.status` is authoritative and is read **on every authenticated request**,
in `src/modules/identity/interface/dependencies.py::get_access_claims`. It is
never a claim, and putting it in the token is forbidden — a claim is a snapshot
from before the account was blocked, so trusting one would silently downgrade the
guarantee to "blocked within one token lifetime".

```text
bearer header
  → decode_access        credential: signature, algorithm, type, expiry, shape
  → users.status         account:    may this subject act AT ALL?
  → AccessTokenClaims    (both passed)
  → Principal assembly   ← a blocked caller never gets this far
```

Two properties make this the whole of the control:

* **One gate.** `get_current_user` and Principal assembly both consume
  `get_access_claims`, so no route, and no bounded context, holds its own copy of
  the rule. A guard asserts `decode_access` is called from exactly one place.
* **Allow-list.** `AUTHENTICABLE_STATUSES == {"active"}`
  (`identity/domain/entities.py`). `blocked`, `pending`, an unrecognised value,
  and a missing user row all confer nothing. `User.is_active` delegates to the
  same predicate, so the aggregate and the gate cannot disagree.

**Refresh is gated too.** `RefreshAccessToken` checks account status before
minting — after reuse detection, so a blocked user replaying a stolen token still
burns the family (§8.2). Without this, blocking was cosmetic: the user simply
refreshed and carried on indefinitely.

**Status code: `401`.** A blocked account is a failure to *authenticate*, not a
missing permission. `403` would assert the caller is authenticated and merely
under-privileged, which is false, and would make an account lockout
indistinguishable from an ordinary authorization denial.

**Cost and residual window.** One primary-key read per authenticated request,
which is now the only per-request authentication I/O (rate limiting adds one
Redis round trip on the unauthenticated auth routes only).
Between that read and the end of the request there is a single-request TOCTOU
window; the next request is refused. Closing it further would require holding a
lock across the request, which buys nothing real.

### 8.2 Logout — refresh-family revocation (Phase 1G.1)

`POST /api/v1/auth/logout` revokes the presented refresh token's entire family.

* **Credential: the refresh token**, not the access token. Access tokens are
  stateless, so the family is the only thing there is to revoke; and possession
  of a leaked refresh token should be enough to burn it, without also needing a
  working session.
* **Scope comes from a signed claim.** `family_id` is read from the
  cryptographically verified payload, never from a request field, so a caller can
  only ever end their own session. `revoke_family` filters on that one id.
* **Idempotent.** Revocation touches only rows with `revoked_at IS NULL`; a
  repeat logout updates nothing and still returns `204`.
* **D1 unchanged.** Logout does not alter reuse detection: replaying a consumed
  token still revokes the family and returns `401`.

**What logout does NOT do: it does not invalidate already-issued access tokens.**
Nothing in this architecture can — they are stateless and unrevocable until
expiry. Logout ends the ability to *renew*. Immediate account-level enforcement
is §8.1's job. The two are separate controls and the ADR does not claim
otherwise; a test asserts the outstanding access token still works after logout,
so the limitation is recorded in executable form rather than in prose alone.

---

## 9. Ownership strategy

Expressed **only** through pure Policy objects — not the Domain, not Routers, not
ORM models.

| Context | Predicate | Override |
|---|---|---|
| Ordering | `order.customer_id == principal.subject_id` | `ordering.orders.read.any` / `cancel.any` |
| Selling | `principal.can_for_seller(perm, offer.seller_id)` — implemented, §9.2 | `selling.offers.manage.any` (platform moderation only; **not** consulted by the seller-member policy) |
| Inventory | `principal.can_for_seller(perm, stock.seller_id)` — denormalized | `inventory.manage.any` |
| Merchant | `principal.can_for_seller(perm, seller.id)` | `merchant.sellers.manage.any` |
| Catalog / BuyBox | none — role-scoped only | — |

**Inventory ownership will be denormalized** onto `inventory_stock.seller_id`,
safe because offer ownership is immutable after creation. Inventory therefore
never calls Selling at runtime, before or after extraction.

> **CURRENT STATE:** the `inventory_stock.seller_id` column **does not exist
> yet** — Inventory is anchored on `offer_id` alone and holds no seller identity.
> **TARGET STATE:** the column is added by M6 and populated from the offer.
> Until then, Inventory has no seller-scoped ownership to enforce.

**Overrides never bypass domain invariants.** `cancel.any` permits a support
agent to *attempt* cancellation; it does not permit cancelling a PAID order.

### 9.1 Status codes

- `401` — unauthenticated
- `403` — authenticated but lacking the required permission, **including
  seller-scoped ownership denial**
- `404` — the resource genuinely does not exist; also **ownership denial for
  customer-scoped resources** (orders), where the resource is private and
  concealing its existence is worth something

Coarse-authorization failure and ownership failure remain distinct outcomes.

> **AMENDED, Phase 1G.** This section previously prescribed `404` for ownership
> denial on **offers and stock** as well. That is now `403`, for two reasons.
> First, concealment buys nothing there: an offer is public — the storefront and
> `GET /api/v1/buybox/variants/{variant_id}` already expose it — so answering
> `404` to a competitor hides a fact the same competitor can read anonymously,
> while costing the owner a misleading error. Second, `403` is the convention the
> API already established for cross-seller access, and §20 of the Phase 1G brief
> directs that the established convention be followed rather than a new one
> introduced. Orders keep `404`, where the resource really is private.
>
> What does **not** change is that every denial looks the same: one status, one
> message, no disclosure of the owning seller.
>
> **CONFIRMED, Phase 1G.1.** Offer ownership denial is `403`. The reasoning is
> resource privacy, not convenience: an Offer is already public through the
> storefront and BuyBox, so `404` conceals nothing an anonymous caller cannot
> read anyway. Ownership failures are **not** globally normalised to either code.
>
> **CORRECTION, security audit.** Earlier revisions of this section stated that
> Orders answer `404` on ownership denial. **They do not.**
> `GET /api/v1/orders/{order_id}` raises `ForbiddenError` (403) for another
> customer's order — see `ordering/interface/router.py`, asserted by
> `tests/test_ordering_authorization.py`. The ADR described an intention that was
> never implemented, and this note corrects the record rather than quietly
> changing either side.
>
> The residual exposure is that `403` vs `404` reveals whether an order id
> exists. Order ids are random UUIDv4, so an attacker must already possess the id
> for that distinction to tell them anything, and the endpoint denies them either
> way. Assessed as LOW and left as-is: changing a working, tested API contract is
> a product decision, not a security necessity. If order-id existence is later
> judged sensitive, the change is one line plus its test.

### 9.2 Seller-aware offer authorization (implemented, Phase 1G)

Selling's ownership predicate is implemented by one pure function,
`src/modules/selling/application/policies.py::ensure_can_act_for_seller`:

```text
target seller -> Principal.memberships[seller] -> SellerGrant.allows(permission)
```

Everything security-critical is in **where the target seller comes from**, and
there are exactly two sources — never the caller:

| Operation | Target seller | Required permission |
|---|---|---|
| Create offer | the seller **nominated in the request**, honoured only if present in `Principal.memberships` | `selling.offers.create` |
| Activate offer | `offer.seller_id`, read after loading the offer | `selling.offers.activate` |
| Suspend offer | `offer.seller_id` | `selling.offers.suspend` |
| Change price | `offer.seller_id` | `selling.offers.price.change` |
| Read seller-owned offer | `offer.seller_id` | `selling.offers.read.own` |

**Target-seller selection on create.** `CreateOfferRequest.seller_id` was
previously banned outright, back when the access token was the only source of
seller identity. It no longer is: a User is not a Seller, and one User may be
staff at one seller and manager at another, so there is nothing to infer from and
the caller has to say which seller the offer is for. The field is a **resource
selector, never a claim of authority** — naming a stranger's seller, an unknown
UUID, or one's own user id all resolve to no membership and are denied
identically. `extra="forbid"` still rejects every other unmodelled field.

**Resource-owner resolution on existing offers.** The offer is loaded first, and
`offer.seller_id` is the authoritative owner. The caller contributes nothing to
the answer, so knowing an offer's UUID confers nothing (§9.3).

**Ordering.** Authorization happens after the load and **before any mutation**,
and on create before the catalog is consulted — so a denial leaves the aggregate
untouched, emits no event, writes no outbox row, and does not reveal whether a
variant exists. Enforced structurally by
`tests/test_selling_authorization_guards.py`.

**One decision per mutation.** The check lives in the use case, not the router.
The router authenticates, parses, and hands over the `Principal`; there is no
second, divergent authorization point (ADR principle 5).

**No I/O in the policy.** The function is synchronous and imports no session, no
repository and no cache. Every fact it needs was resolved once during Principal
assembly (§10). A guard asserts the module contains no `async`/`await` and no
infrastructure import, because a policy that *can* query is a policy that
eventually *will*.

**Platform authority is not seller authority.** `selling.offers.manage.any` is
deliberately **not** consulted here. It is platform moderation, a separate
universe; honouring it would let a platform role act *as* a seller member.

### 9.3 IDOR protection

The threat: an authenticated caller mutating another seller's offer by knowing
its UUID. Resolved by the ordering above, and locked by tests at three levels —
unit (`tests/test_selling_offer_authorization.py`), real PostgreSQL
(`tests/integration/test_selling_offer_ownership.py`) and real HTTP
(`tests/api/test_selling_authorization.py`) — each asserting both the refusal and
that the row, the price and the status are unchanged.

Denials are **indistinguishable**: no membership, an inactive membership, a
non-active seller, and a role that lacks the permission all produce one identical
`403`, and the response never names the owning seller. Distinguishable messages
would turn a denial into an enumeration oracle.


### 9.4 Back-office authorization (security audit)

Layer 3 of the four-layer model — the router coarse check — existed in the design
from §3 but had never been wired. Catalog admin and Inventory therefore shipped
with **authentication only**, which a live probe confirmed was exploitable by any
logged-in customer: creating and editing catalogue products, injecting stock
against another seller's offer, and committing or releasing any reservation by
id. Authentication is not authorization, and a route named `/admin/` is not a
guard.

`src/api/guards.py::require_permission` implements layer 3. It takes `Permission`
members, not strings, so a typo fails at import rather than matching nothing and
admitting everyone.

| Operation | Control | Required |
|---|---|---|
| `POST /catalog/admin/products` | router guard | `catalog.products.create` |
| `PATCH /catalog/admin/products/{id}` | router guard | `catalog.products.update` |
| `POST /inventory/stock` | **object policy** | `inventory.stock.receive` for the OFFER's seller, or platform `inventory.manage.any` |
| `POST /inventory/reservations` | router guard | `inventory.reservations.create` |
| `POST /inventory/reservations/{id}/commit` | router guard | `inventory.reservations.commit` |
| `POST /inventory/reservations/{id}/release` | router guard | `inventory.reservations.release` |

**Why stock is not a router guard.** Stock belongs to the offer's seller, and the
router has not loaded the offer — the same reason Selling decides ownership in
its use case (§3.2). `inventory/application/policies.py` holds the decision;
`SellingGateway.seller_of` supplies the owner through the existing ACL, so
Inventory still never imports Selling. It replaces the previous `offer_exists`
call in that path rather than adding one: a `None` return answers both "does it
exist" and "who owns it" in a single round trip.

**Why reservations are not seller-scoped.** A reservation is not a merchant-owned
resource. In normal operation the Ordering saga creates and commits them
in-process; the HTTP routes exist for back-office correction, so they carry
platform permissions and no seller reaches them.

**The one deliberate override.** `inventory.manage.any` lets an operator correct
stock they do not own. It is written at the policy's call site, not hidden inside
`can_for_seller`, so the widening is visible where it is granted — and it is the
*only* platform override in the seller-scoped surface. Selling grants none.

**A guard that locks out the right people is also a defect.** The first draft
placed a router-wide `catalog.products.read` requirement on the catalog admin
router; that permission belongs to `super_admin` alone in the Phase 1E baseline,
so it would have barred `catalog_admin` from its own endpoints. Caught by the
"legitimate role still works" counterweight test, which is why every guard here
has one.

---

## 10. Membership resolution — Redis cache with outbox invalidation

Evaluated: (A) per-request DB lookup, (B) memberships embedded in the JWT,
(C) Redis cache with event-driven invalidation. **Option C adopted.**

A was rejected: it places a mandatory query on the least scalable resource, in
contention with checkout across a 30-connection pool, and becomes a synchronous
cross-service hot-path dependency after extraction. B was rejected: it cannot
revoke seller authority before token expiry, and every credible mitigation
reintroduces the per-request lookup it was chosen to avoid.

> **Governing principle.** Authorization state must never be embedded in a bearer
> credential it cannot be recalled from, and must never be resolved from the
> primary datastore on the hot path.

### 10.1 Membership cache
- Key `authz:memberships:{user_id}` — flat, **no epoch**, no version component
- Value: `seller_id → member_role_code`, plus a materialization timestamp
- TTL 5 minutes, ±10% jitter
- **Negative caching mandatory** — a user with no memberships caches an explicit
  empty set, or every customer request becomes a cache miss and Option C silently
  degrades into Option A
- Invalidated by exact per-user `DELETE` on `MemberAdded` / `MemberRemoved` /
  `MemberRoleChanged`

### 10.2 Seller authorization-state cache
- Key `authz:seller:{seller_id}`
- Value: `seller_id`, `status`, `tenant_id`, `version` — security-relevant state
  only, no PII
- TTL 15 minutes, ±10% jitter
- Populated by a projector on `SellerActivated` / `SellerSuspended` /
  `SellerUpdated`, **version-guarded** (an older version is discarded), which is
  what makes out-of-order delivery safe
- **Seller suspension is O(1)**: it updates one key and requires no membership
  fan-out and no reverse index

### 10.3 Three states
| Layer | What | Owner | Consistency |
|---|---|---|---|
| Authoritative | `sellers`, `seller_members` | Merchant | Transactional |
| Authorization read | the two Redis keys | Authorization read model | Eventually consistent, bounded by §10.5 |
| Invalidation | sync delete + outbox projector + TTL | Merchant emits, projector applies | Durable via outbox |

### 10.4 Invalidation protocol
1. Merchant commits the mutation **and** the outbox row in one transaction.
2. **After** commit succeeds, best-effort `DELETE` of the user's key. Failure is
   logged and counted, never raised.
3. The outbox relay independently performs the same `DELETE`. Idempotent.

Deleting before commit would allow a concurrent request to repopulate from
pre-commit state.

### 10.5 Revocation guarantees — stated precisely
| Tier | Condition | Guarantee |
|---|---|---|
| **G1 Normal** | Synchronous invalidation succeeds | Visible to the **next request**; no staleness |
| **G2 Degraded** | Sync fails, relay healthy | Bounded by relay delivery lag (sub-second typical) |
| **G3 Worst case** | Both fail | Bounded by TTL: **≤5 min memberships, ≤15 min seller status** |
| **G4 Impossible** | Arbitrary infrastructure failure | **Staleness is never provably zero.** The cache is not transactional with Postgres. |

**Revocation is not mathematically immediate under arbitrary failure. G3 is the
security planning figure.** A total Redis outage makes revocation immediate,
because fallback reads authoritative state; the dangerous window is Redis being
*up* while an invalidation was lost.

### 10.6 Route-scoped resolution
Membership and seller-state resolution occurs **only for routes declaring at
least one seller-scopable permission**. Anonymous, customer and platform-admin
routes perform **zero** membership I/O. Deterministic per-route, not adaptive.

A CI guard must verify that any policy consulting `memberships` is reachable only
from such routes.

### 10.7 Bounded fallback
> **Invariant: a Redis failure must never convert authorization fallback into
> uncontrolled Postgres load.**

| Control | Behaviour |
|---|---|
| Redis op timeout | 50 ms → treat as miss |
| Breaker opens | >50% errors/timeouts over a 10 s rolling window, min 20 samples |
| Half-open | after 5 s; 3 consecutive successes close; one failure reopens |
| Fallback concurrency | hard semaphore of **5** process-wide (of a 30-connection pool) |
| Fallback rate limit | token bucket, well below pool budget |
| Fallback query timeout | 250 ms |
| Single-flight | concurrent misses for one key collapse to one query |
| Budget exhausted | **deny (403)** for seller-scoped routes; never allow unchecked |
| Recovery | lazy repopulation with jittered TTL; no warming job, no mass invalidation |

### 10.8 Failure semantics
Never fail open. In every case the outcome is *authorized against fresh
authoritative data* or *denied*.

| Scenario | Result | Source | DB? |
|---|---|---|---|
| Cache hit | normal | Redis | No |
| Cache miss | normal | Postgres, single-flight | Yes, once per TTL |
| Redis brief outage | normal | Postgres, capped | Yes |
| Breaker open | seller routes denied beyond budget; others unaffected | Postgres within budget | Rate-limited |
| Sync invalidation failed | stale until G2/G3 | Redis | No |
| Outbox delayed / duplicated / out-of-order | correct | — | No |
| Seller suspended, membership cached | **denied** | seller-state key | No |
| Concurrent populate | correct | Postgres | Bounded by instances |
| Redis recovers | correct | lazy repopulation | On miss |

---

## 11. Microservice compatibility

- Each service verifies tokens independently; a gateway may pre-validate but is
  never trusted.
- Principal assembly sits behind a factory: in-process ports become network calls
  or token claims with **no policy changes**.
- Policies live in each context's application layer and extract with it.
- Ownership data is always local — no cross-service call on the authorization path.
- The `Permission` enum ships in the Shared Kernel library, so services cannot
  disagree about what a permission means.
- Invalidation rides the event bus, which exists regardless.

---

## 12. Migrations

All additive, INSERT-only, behaviour-neutral. Enforcement is activated by
configuration, never by a migration.

| # | Content |
|---|---|
| M1 | `sellers`, `seller_members`; `roles.scope` discriminator |
| M2 | Seed `permissions` from the enum (generated literals) + roles |
| M3 | Seed `role_permissions`, joined on `code` |
| M4 | Backfill `Seller` + `SellerMember` — **see §7.2: id-preserving only where existing values are verified legitimate seller identities. NOT valid against development/test residue, where M4 is a no-op and Seller identities start fresh.** |
| M5 | Grant `customer` to existing users |
| M6 | Denormalize `seller_id` onto `inventory_stock` |

**Two hard rules:** migrations join on `code`, never on surrogate `id` (ids differ
per environment); and only the seeder writes to `permissions`.

Migrations must **never import the live enum** — an old migration must remain
historically deterministic even as the enum evolves.

---

## 13. Risks

| # | Risk | Mitigation |
|---|---|---|
| R15 | Lost invalidation → stale-permissive | Three layers; TTL ceiling; invalidation-failure metric |
| R16 | Negative caching omitted → C degrades to A | Design requirement + cache-hit-rate alert |
| R17 | Redis outage → uncontrolled fallback | §10.7 |
| R18 | Policy consulting unpopulated memberships | §10.6 CI guard |
| R19 | Seller-state projector lag | Version guard, TTL, lag metric |
| R21 | G1 misread as the security guarantee | §10.5 states G3 as the planning figure |

---

## 14. Observability

Required before enforcement is enabled. Thresholds are **design** values, to be
re-baselined against production.

Membership cache hit/miss rate · **negative-cache hit rate** · seller-state hit
rate · Redis latency p50/p95/p99 · Redis errors · DB fallback count/rate/
concurrency · circuit-breaker state · invalidation success/failure ·
invalidation event lag · invalidation retries · stale-cache detections ·
authorization decision latency p50/p95/p99 · fallback denials.

Two are load-bearing: **negative-cache hit rate** is the only signal
distinguishing a healthy Option C from an accidental Option A, and
**invalidation failure count** is the only signal that G1 has degraded to G3.

---

## 15. Alternatives rejected

| Alternative | Why rejected |
|---|---|
| Permissions in the JWT | Changes wait for token expiry; token bloat; catalogue duplicated into every credential |
| Seller = User | Cannot express staff or multi-store; unwinding later means rewriting live `offers.seller_id` |
| Central Authorization Service | God object in the monolith, distributed-monolith seam after extraction |
| Domain-layer authorization | Couples domain to Identity, breaks the event path |
| Ambient / ContextVar Principal | Makes a missing check invisible — the bug class this ADR exists to prevent |
| External policy engine (OPA/Casbin) | Ops surface and a second language for dynamic policy not yet needed |
| Zanzibar ReBAC | Ownership here is shallow and stable; tuple store buys nothing |
| Postgres RLS | Invisible to the application, untestable as truth tables, does not survive extraction |
| Service principal for consumers | Invites authorizing a consumer, which would silently stop compensation |
| Seller inside Selling | Makes Selling the hub for every context needing merchant identity |
| Global epoch in the cache key | Requires an epoch lookup per request, defeating its own purpose |
| Seller status read from Postgres in policy | Reintroduces per-request DB load and a runtime cross-context call |
| Denormalizing seller status into membership entries | Turns an O(1) suspension into O(members) fan-out |
| Unbounded DB fallback | Turns a cache outage into a checkout outage via pool exhaustion |

---

## 16. Consequences

**Positive.** Deny-by-default with startup enforcement; ownership expressible
identically from single-vendor to marketplace; no authorization state in bearer
credentials; no synchronous cross-service authorization dependency; the Domain
stays pure; the outbox gains a second, well-understood use.

**Negative / accepted.** Cache invalidation is real complexity with a
stale-permissive failure mode bounded only by TTL; platform-role revocation is
bounded by token TTL; Redis joins the authorization path (mitigated by bounded
fallback); and the permission catalogue requires a generator plus a CI drift gate
to stay honest.
