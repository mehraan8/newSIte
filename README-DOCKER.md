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
| `pgadmin` | dpage/pgadmin4:8 | **dev** | DB admin UI |

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
