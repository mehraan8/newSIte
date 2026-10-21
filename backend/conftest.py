"""Root conftest: makes `src.` importable and pins the environment.

Two jobs, both of which must happen before pytest imports a single test module.

1. Path — replaces the per-file `sys.path.insert(0, ".")` that only worked when
   pytest happened to be run from `backend/`.

2. Environment — loads the .env layers into os.environ and, when a scratch
   TEST_DATABASE_URL exists, redirects DATABASE_URL at it.

   (2) cannot live in tests/integration/db.py, which is where it used to be.
   `src.config.settings` builds a module-level singleton at import time and
   `src.shared_kernel.infrastructure.database` builds the global engine from it,
   also at import time. The DB-free suites under tests/ import those modules
   during collection — i.e. *before* tests/integration/db.py is ever imported —
   so by the time db.py poked os.environ the engine was already bound to the
   wrong database, and the relay-driven consumers (which have no seam to inject
   a session factory) would have run the real saga against it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_ROOT = str(Path(__file__).resolve().parent)
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

from src.config.env_bootstrap import load_env  # noqa: E402

load_env()

# The whole test session — unit and integration alike — targets the scratch
# database. Nothing under tests/ may ever touch the development database.
_test_dsn = os.environ.get("TEST_DATABASE_URL", "").strip()
if _test_dsn:
    os.environ["DATABASE_URL"] = _test_dsn
