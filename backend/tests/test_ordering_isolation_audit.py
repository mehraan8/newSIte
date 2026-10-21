"""Ordering owns its tables and mutates nothing else.

A saga orchestrator is the context most likely to reach into a participant's
data "just this once". This audit makes that structural rather than a convention:
Ordering may READ another context's read model through its ACL, but every write
must target a table Ordering owns.

Run: pytest tests/test_ordering_isolation_audit.py
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

pytest.importorskip("sqlalchemy", reason="ordering infrastructure pulls in the DB layer")

ORDERING_ROOT = (
    pathlib.Path(__file__).resolve().parents[1] / "src" / "modules" / "ordering"
)

# The only tables Ordering is allowed to write.
OWNED_TABLES = {"orders_saga", "order_saga_lines", "order_idempotency"}

# Tables Ordering may READ through its read-only ACL.
READ_ONLY_TABLES = {"buy_box_offer"}

WRITE_SQL = re.compile(r"\b(insert\s+into|update|delete\s+from)\s+([a-z_][a-z0-9_]*)", re.I)


def ordering_sources():
    for path in ORDERING_ROOT.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        yield path, path.read_text(encoding="utf-8")


def sql_literals(source: str) -> list[str]:
    """String literals excluding docstrings.

    Scanning raw file text matches prose — "Sourced from BuyBox" reads as a FROM
    clause, and `import select, update` + newline `from x` reads as an UPDATE.
    Only real string constants can contain real SQL.
    """
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_the_audit_actually_sees_the_ordering_package():
    assert ORDERING_ROOT.name == "ordering"
    assert sum(1 for _ in ordering_sources()) > 5


# === ORM ownership ========================================================
def test_ordering_orm_models_declare_only_owned_tables():
    from src.modules.ordering.infrastructure import models

    from src.shared_kernel.infrastructure.database import Base

    declared = {
        obj.__tablename__
        for obj in vars(models).values()
        if isinstance(obj, type) and issubclass(obj, Base) and hasattr(obj, "__tablename__")
    }
    assert declared == OWNED_TABLES, declared


def test_ordering_imports_no_other_contexts_orm_models():
    """Importing another context's model class is how a 'quick fix' becomes a
    cross-context write."""
    offenders = []
    for path, src in ordering_sources():
        for module in re.findall(r"from (src\.modules\.\w+)[\w.]*\s+import", src):
            if module == "src.modules.ordering":
                continue
            # Identity's auth dependency is the one sanctioned import, and it
            # exposes no ORM model.
            if module == "src.modules.identity" and path.name == "router.py":
                continue
            offenders.append(f"{path.name}: {module}")
    assert offenders == [], offenders


# === raw SQL ownership ====================================================
def test_no_raw_sql_in_ordering_writes_to_a_foreign_table():
    offenders = []
    for path, src in ordering_sources():
        for literal in sql_literals(src):
            for verb, table in WRITE_SQL.findall(literal):
                if table.lower() not in OWNED_TABLES:
                    offenders.append(f"{path.name}: {verb} {table}")
    assert offenders == [], offenders


def test_the_pricing_acl_is_read_only():
    """It is the single place Ordering touches another context's data."""
    acl = (ORDERING_ROOT / "infrastructure" / "buybox_acl.py").read_text(encoding="utf-8")
    statements = sql_literals(acl)

    assert any("SELECT" in s.upper() for s in statements)
    for statement in statements:
        assert WRITE_SQL.findall(statement) == [], statement
        for table in re.findall(r"FROM\s+([a-z_][a-z0-9_]*)", statement, re.I):
            assert table.lower() in READ_ONLY_TABLES, table


def test_the_acl_does_not_import_buybox():
    """Raw SQL, not a BuyBox import — so BuyBox stays independently deployable."""
    acl = (ORDERING_ROOT / "infrastructure" / "buybox_acl.py").read_text(encoding="utf-8")
    assert "src.modules.buybox" not in acl


# === participant interaction is by event, not by write ====================
def test_ordering_never_imports_an_inventory_use_case_or_repository():
    """Inventory is driven by command EVENTS. A direct call would be a
    synchronous distributed transaction in disguise."""
    for path, src in ordering_sources():
        assert "src.modules.inventory" not in src, path.name


def test_ordering_writes_to_inventory_only_by_emitting_commands():
    entities = (ORDERING_ROOT / "domain" / "entities.py").read_text(encoding="utf-8")

    # The saga's whole Inventory surface: three command events.
    for command in (
        "OrderReservationRequested",
        "InventoryCommitRequested",
        "InventoryReleaseRequested",
    ):
        assert f"class {command}" in entities, command
    assert WRITE_SQL.findall(entities) == []


def test_the_domain_layer_has_no_infrastructure_imports_at_all():
    for path, src in (
        (p, s) for p, s in ordering_sources() if p.parent.name == "domain"
    ):
        for banned in ("sqlalchemy", "fastapi", "redis", "infrastructure"):
            assert banned not in src, f"{path.name}: {banned}"


# === repositories operate on the caller's session, never commit ===========
def test_ordering_repositories_never_commit():
    """Committing inside a repository would break the UoW's transactional
    boundary and split state from its outbox rows."""
    repo = (ORDERING_ROOT / "infrastructure" / "repository.py").read_text(encoding="utf-8")
    assert ".commit(" not in repo


def test_ordering_never_opens_a_session_outside_the_uow_except_the_acl():
    """The ACL deliberately uses its own session (a cross-context read must not
    join the write transaction). Nothing else may."""
    offenders = []
    for path, src in ordering_sources():
        if "SessionFactory" in src and path.name != "buybox_acl.py":
            offenders.append(path.name)
    assert offenders == [], offenders
