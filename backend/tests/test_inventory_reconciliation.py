"""I8 reconciliation classification (DB-free).

The SQL itself is exercised in tests/integration/test_i8_reconciliation.py
against real Postgres — FULL OUTER JOIN semantics cannot be validated here.

Run: pytest tests/test_inventory_reconciliation.py
"""

from __future__ import annotations

import logging

import pytest

pytest.importorskip("sqlalchemy", reason="reconciliation module imports the DB layer")

from src.modules.inventory.infrastructure.reconciliation import (  # noqa: E402
    Drift,
    report,
)

OFFER = "0ffe0000-0000-0000-0000-000000000001"


def drift(reserved: int, pending: int) -> Drift:
    return Drift(
        offer_id=OFFER, location="default",
        reserved_counter=reserved, pending_total=pending,
    )


def test_counter_below_live_holds_is_the_oversellable_direction():
    """reserved < pending means `available` is overstated, so the same units can
    be promised twice. This is the direction that must page someone."""
    d = drift(reserved=2, pending=5)
    assert d.delta == -3
    assert d.oversellable is True
    assert "OVERSELLABLE" in d.describe()


def test_counter_above_live_holds_is_stranded_stock_not_an_oversell():
    d = drift(reserved=5, pending=2)
    assert d.delta == 3
    assert d.oversellable is False
    assert "stranded stock" in d.describe()


def test_agreement_is_not_drift():
    assert drift(reserved=4, pending=4).delta == 0


def test_report_is_silent_and_succeeds_when_there_is_no_drift():
    assert report([]) == 0


def test_report_fails_the_run_when_drift_exists():
    """Exit code 1 so any scheduler treats a drifted audit as a failed run."""
    assert report([drift(2, 5)]) == 1


def test_report_names_every_drifted_bucket(caplog):
    rows = [drift(2, 5), drift(5, 2)]
    with caplog.at_level(logging.ERROR):
        assert report(rows) == 1
    logged = caplog.text
    assert "1 oversellable" in logged
    assert logged.count(OFFER) == 2


def test_reconciliation_never_exposes_a_repair_path():
    """Repair means choosing an authoritative side; choosing wrong in the
    reserved<pending direction double-sells. Kept a human decision on purpose."""
    from src.modules.inventory.infrastructure import reconciliation

    exported = [n for n in dir(reconciliation) if not n.startswith("_")]
    for banned in ("repair", "fix", "heal", "resync", "correct"):
        assert not any(banned in n.lower() for n in exported), banned
