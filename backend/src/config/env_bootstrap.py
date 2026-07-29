"""The one place where .env files enter the process environment.

Every entrypoint — FastAPI, Alembic, pytest, the relay and the expiry worker —
calls :func:`load_env` before anything reads a setting, so all of them resolve
credentials identically no matter what the current working directory is.

Precedence, highest wins:

  1. the real process environment  (compose ``environment:``, CI secrets, shell)
  2. ``backend/.env``              (host-side overrides: driver, ports, DEBUG)
  3. ``<repo root>/.env``          (the file docker compose reads — SOURCE OF TRUTH)

Nothing here overwrites a variable that is already set, so a container handed
``DATABASE_URL`` by compose is unaffected by any ``.env`` baked into the image.

Connection strings are *derived* from ``POSTGRES_*`` / ``REDIS_*`` rather than
written out by hand, which is what keeps the root ``.env`` the single source of
truth: change the password in one file and compose, the app, Alembic and the
test suite all follow.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv

# .../backend/src/config/env_bootstrap.py -> parents[2] == .../backend
BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent

BACKEND_ENV = BACKEND_ROOT / ".env"
ROOT_ENV = REPO_ROOT / ".env"

_loaded = False


def _dsn(driver: str, database: str) -> str | None:
    """Build a Postgres DSN from the POSTGRES_* parts, or None if incomplete."""
    user = os.getenv("POSTGRES_USER", "").strip()
    password = os.getenv("POSTGRES_PASSWORD", "").strip()
    if not user or not password or not database:
        return None
    # Defaults to 127.0.0.1, never "localhost": compose publishes on 127.0.0.1
    # only, and Windows tries ::1 first, costing ~2s on every single connect.
    # Inside compose the DSN arrives ready-made via `environment:`, so this
    # branch only ever runs on the host.
    host = os.getenv("POSTGRES_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = os.getenv("POSTGRES_PORT", "5432").strip() or "5432"
    user_q = quote(user, safe="")
    password_q = quote(password, safe="")
    return f"postgresql+{driver}://{user_q}:{password_q}@{host}:{port}/{database}"


def _setdefault(key: str, value: str | None) -> None:
    if value and not os.environ.get(key, "").strip():
        os.environ[key] = value


def load_env(*, force: bool = False) -> None:
    """Populate ``os.environ`` from the .env layers. Idempotent."""
    global _loaded
    if _loaded and not force:
        return

    # override=False on both: an already-exported variable always wins, and the
    # backend layer is loaded first so it takes precedence over the root layer.
    for env_file in (BACKEND_ENV, ROOT_ENV):
        if env_file.is_file():
            load_dotenv(env_file, override=False)

    database = os.getenv("POSTGRES_DB", "").strip()
    test_database = os.getenv("POSTGRES_TEST_DB", "").strip() or (
        f"{database}_test" if database else ""
    )

    # The app speaks asyncpg; Alembic speaks psycopg. Both are derived here so
    # neither has to carry a hardcoded fallback DSN.
    _setdefault("DATABASE_URL", _dsn("asyncpg", database))
    _setdefault("TEST_DATABASE_URL", _dsn("asyncpg", test_database))

    redis_password = os.getenv("REDIS_PASSWORD", "").strip()
    if redis_password:
        redis_host = os.getenv("REDIS_HOST", "127.0.0.1").strip() or "127.0.0.1"
        redis_port = os.getenv("REDIS_PORT", "6379").strip() or "6379"
        _setdefault(
            "REDIS_URL",
            f"redis://:{quote(redis_password, safe='')}@{redis_host}:{redis_port}/0",
        )

    _loaded = True


def sync_dsn(dsn: str) -> str:
    """asyncpg DSN -> psycopg DSN (Alembic runs synchronously)."""
    return dsn.replace("+asyncpg", "+psycopg")
