# Docker Infrastructure

Production-grade orchestration for the FastAPI modular monolith.
**No application code is modified by this setup — infrastructure only.**

## Components

| Service | Image | Profile | Purpose |
|---|---|---|---|
| `postgres` | postgres:16-alpine | always | Primary datastore (SCRAM auth, persistent volume) |
| `redis` | redis:7-alpine | always | Cache / OTP store / rate limiting (AOF persistence, LRU) |
| `migrate` | built image | always | One-shot `alembic upgrade head`, then exits 0 |
| `api` | built image | **prod** | Multi-worker Uvicorn, immutable image |
| `api-dev` | built image | **dev** | Hot-reload Uvicorn, source bind-mounted |
| `relay` | built image | prod + dev | **Outbox relay worker** — drains `outbox_messages`, dispatches to consumers |
| `inventory-expiry` | built image | prod + dev | **Reservation TTL sweeper** — expires held stock, emits `inventory.InventoryExpired` |
| `pgadmin` | dpage/pgadmin4:8 | **dev** | DB admin UI |

### Background workers are not optional

The API process only *writes* events to the outbox. Two separate processes make
the system actually function, and running the API without them fails silently —
requests return 200 while nothing downstream ever happens.

**`relay`** — without it no event is ever delivered: BuyBox projections never
update and the checkout saga never advances past `OrderPlaced`. Events simply
accumulate unpublished.

**`inventory-expiry`** — without it reservations never expire. Held stock is
never returned, and an order whose hold has lapsed can still reach payment,
which is the ADR-0009 D1 oversell path. Shipping the expiry *code* without this
service leaves that hole open.

Both are plain `python -m` entry points with no HTTP surface, so they have no
health check. Monitor them by liveness plus these signals:

| Signal | Query | Alarm when |
|---|---|---|
| Relay backlog | `count(*) from outbox_messages where not published_flag and available_at <= now()` | grows steadily |
| Dead letters | `count(*) from dead_letter_messages` | any increase |
| Expiry backlog | `count(*) from inventory_reservation where status='pending' and expires_at <= now()` | stays > 0 across passes |

The expiry backlog alarm is the one that catches a stuck sweeper: a reservation
the domain refuses to expire is skipped and left `pending` by design, so a
persistent non-zero count means reconciliation is needed, not that the worker died.

**Scaling.** `inventory-expiry` is safe to replicate (`FOR UPDATE SKIP LOCKED`
claims). Keep `relay` at **one replica** for now — its claims are also skip-locked,
but `OWN_UNIT_OF_WORK` consumers commit outside the relay transaction
(`outbox/dispatcher.py`) and that path has not been load-tested concurrently.

---

## 4. Environment strategy

Three-tier, secrets never committed:

```
.env.example   committed template (placeholders only)
.env           local/dev secrets — auto-read by `docker compose`, gitignored
.env.prod      production secrets — passed explicitly via --env-file, gitignored
```

- **Compose-level vars** (`.env` at repo root) feed *variable substitution* in
  `docker-compose.yml` — DB/Redis/pgAdmin credentials, ports, image tag.
- **App-level vars** are injected into containers via the `environment:` block
  (the `x-app-env` anchor), so the app's Pydantic `Settings` reads them from the
  process environment. Container env **overrides** any baked `.env`.
- **DATABASE_URL is composed per service**: the `migrate` job gets the **sync**
  `postgresql+psycopg://…` URL; the API gets the **async** `postgresql+asyncpg://…`
  URL — same database, correct driver for each.
- Hostnames are **service names** on the private `backend` network
  (`postgres`, `redis`), never `localhost`.

```bash
cp .env.example .env          # fill in strong secrets (openssl rand -hex 32)
# production:
docker compose --env-file .env.prod --profile prod up -d --build
```

---

## 5. Startup sequence

Ordering is enforced by **health-gated `depends_on`**, not by sleeps:

```
        ┌─────────────┐        ┌─────────┐
        │  postgres   │        │  redis  │
        │ (healthy?)  │        │(healthy?)│
        └──────┬──────┘        └────┬────┘
               │ service_healthy     │ service_healthy
               ▼                     │
        ┌─────────────┐             │
        │   migrate   │  alembic upgrade head
        │ (runs once) │  exits 0
        └──────┬──────┘
               │ service_completed_successfully
               ▼  (+ postgres & redis healthy)
        ┌──────────────────────────────┐
        │   api  /  api-dev             │
        │   starts only after the DB is │
        │   migrated and caches are up  │
        └──────────────────────────────┘
```

1. `postgres` and `redis` boot; their health checks must pass.
2. `migrate` starts only once Postgres is **healthy**, applies all migrations,
   and exits. It does **not** stay running.
3. The API starts only once `migrate` **completed successfully** *and* both
   datastores are healthy. No race conditions, no "DB not ready" crashes.

---

## 6. Health-check strategy

Every long-running service has a check; orchestration depends on them.

| Service | Probe | Why |
|---|---|---|
| postgres | `pg_isready -U $USER -d $DB` | Accepts connections *and* the target DB exists |
| redis | `redis-cli ping == PONG` (auth via `REDISCLI_AUTH`) | Server responsive; no password in process list |
| api / api-dev | stdlib GET `/health` (200) | Real app readiness, not just "port open" — no curl needed in image |
| pgadmin | stdlib GET `/misc/ping` | UI actually serving |
| migrate | *(none)* — it's a job | Gated by `service_completed_successfully` instead |

- API checks use the **bundled Python interpreter** (`urllib`) so the runtime
  image stays lean (no `curl`/`wget` installed).
- `start_period` gives slow first-boot (e.g. app import) grace before failures count.
- The same probe is defined both in the `Dockerfile` `HEALTHCHECK` (for `docker run`)
  and in Compose (authoritative for orchestration).

---

## Quick commands

```bash
# Development (reload + pgAdmin)
docker compose --profile dev up --build
#   API     -> http://localhost:8000/docs
#   pgAdmin -> http://localhost:5050

# Production (detached, 4 workers, no pgAdmin)
docker compose --profile prod up -d --build

# Run migrations on demand
docker compose run --rm migrate

# Tail logs / status
docker compose --profile dev logs -f api-dev
docker compose ps

# Background workers (start with either profile; verify BOTH are up)
docker compose --profile prod logs -f relay
docker compose --profile prod logs -f inventory-expiry

# Run one expiry sweep by hand (debugging a backlog)
docker compose run --rm inventory-expiry \
  python -c "import asyncio; from src.modules.inventory.infrastructure.expiry_worker \
import sweep_once; print(asyncio.run(sweep_once()))"

# Tear down (keep data) / wipe volumes
docker compose --profile dev down
docker compose --profile dev down -v
```

## Security notes
- Postgres and Redis ports are bound to `127.0.0.1` only — not the public NIC.
- Redis requires a password; AOF persistence enabled; memory-capped with LRU eviction.
- API runs as a **non-root** user inside the image; build toolchain excluded from runtime.
- Production image is immutable (no source mount); only `api-dev` mounts code.
- Log rotation (`10m × 3`) prevents unbounded disk growth.
