# Backend — Persian RTL E-Commerce API

FastAPI modular monolith with Clean Architecture + DDD. Each bounded context is
a vertical slice: `domain → application → infrastructure → interface`.

## Layout

```
src/
├── config/                  # Pydantic settings (12-factor, env-driven)
├── shared_kernel/           # Money, Mobile, base Entity/AggregateRoot, errors,
│   ├── domain/              #   DB engine/session, Redis client — no business logic
│   ├── application/
│   └── infrastructure/
├── modules/
│   ├── identity/            # ✅ OTP auth: request/verify/refresh/me (fully implemented)
│   │   ├── domain/         #   User aggregate, domain events
│   │   ├── application/    #   use cases + ports (UserRepository, OtpStore, TokenService…)
│   │   ├── infrastructure/ #   SQLAlchemy repos, Redis OTP store, JWT, SMS adapters
│   │   └── interface/      #   routers, schemas, DI, auth guard
│   └── catalog/             # ✅ read-side: vertical-agnostic product list + detail
└── api/                     # composition root: main.py, middleware, /api/v1 router
```

Not yet implemented (folders/contracts designed in the blueprint): cart,
ordering, payment, shipping, content/seo, admin/rbac, notification. Each is added
by creating a module and registering its router in `src/api/v1/router.py`.

## Run

```powershell
cd backend
python -m pip install -r requirements.txt
Copy-Item .env.example .env     # then edit secrets

# 1) migrate (Alembic uses the sync psycopg URL)
$env:DATABASE_URL = "postgresql+psycopg://app:app@localhost:5432/ecommerce"
alembic upgrade head

# 2) run the API (app uses the async asyncpg URL from .env)
uvicorn src.api.main:app --reload
```

- Swagger UI: http://localhost:8000/docs
- Health: http://localhost:8000/health
- In dev (`SMS_PROVIDER=console`) the OTP code is printed to the server log.

## What's verified
- App imports; 7 routes register; OpenAPI 3.1 generates (`docs_openapi.json`).
- Full OTP flow tested with in-memory fakes: request → reject-wrong-code →
  verify+register → JWT issue → refresh rotation → reuse detection →
  returning-user detection → mobile normalization.

## Architecture rules
- Domain/application layers never import FastAPI, SQLAlchemy, or Redis.
- Contexts talk only through published interfaces (sync) or domain events (async).
- Errors raised as `AppError` subclasses; mapped to Problem-Details HTTP in one place.
- App = async (asyncpg); Alembic = sync (psycopg). Two drivers, one database.
```
