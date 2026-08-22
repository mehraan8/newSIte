"""CI drift gate: seed migration literals vs the code baseline (Phase 1E).

Migrations must never import the live vocabulary — an old migration has to keep
reproducing the history it recorded even after the enum moves on (ADR-0011 §12).
The cost of frozen literals is that they can fall behind the code, so this is the
test that makes that impossible to ship:

    change Permission / PlatformRole / ROLE_PERMISSIONS
        without regenerating 0020
            -> this test fails

Regenerate with:

    python -m src.modules.identity.infrastructure.rbac_drift --emit
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

from src.modules.identity.application.rbac_baseline import expected_state
from src.modules.identity.infrastructure.rbac_drift import emit_seed_literals

MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "migrations" / "versions" / "0020_seed_platform_rbac.py"
)


@pytest.fixture(scope="module")
def seed():
    spec = importlib.util.spec_from_file_location("seed_0020", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_exists_and_is_chained_after_merchant():
    assert MIGRATION.exists()


def test_revision_chain(seed):
    assert seed.revision == "0020"
    assert seed.down_revision == "0019"


def test_seeded_roles_match_the_code_vocabulary(seed):
    assert tuple(sorted(seed.ROLES)) == expected_state().roles


def test_seeded_permissions_match_the_code_catalogue(seed):
    assert tuple(sorted(seed.PERMISSIONS)) == expected_state().permissions


def test_seeded_grants_match_the_baseline_mapping(seed):
    assert tuple(sorted(seed.GRANTS)) == expected_state().grants


def test_frozen_fingerprint_matches_the_current_baseline():
    """The single assertion that catches every kind of vocabulary drift."""
    text = MIGRATION.read_text(encoding="utf-8")
    assert expected_state().fingerprint() in text, (
        "The seed migration is stale. Regenerate it with:\n"
        "  python -m src.modules.identity.infrastructure.rbac_drift --emit"
    )


def test_migration_does_not_import_the_live_vocabulary(seed):
    """Importing the enum would make an old migration's behaviour change as the
    code evolves, destroying replay determinism."""
    text = MIGRATION.read_text(encoding="utf-8")
    for forbidden in (
        "from src.shared_kernel.application.permissions",
        "from src.modules.identity.application.roles",
        "from src.modules.identity.application.rbac_baseline",
        "import Permission",
        "import PlatformRole",
    ):
        assert forbidden not in text, f"migration imports live vocabulary: {forbidden}"


def test_migration_seeds_nothing_beyond_platform_rbac(seed):
    """No users, no sellers, no memberships, no test data."""
    text = MIGRATION.read_text(encoding="utf-8").lower()
    for table in ("into users", "into sellers", "into seller_members", "into user_roles"):
        assert table not in text, f"migration writes to {table}"


def test_generator_output_is_deterministic():
    assert len({emit_seed_literals() for _ in range(5)}) == 1


def test_generator_output_regenerates_the_committed_literals(seed):
    """Running the generator today must reproduce what is in the migration."""
    emitted = emit_seed_literals()
    for code, name_fa, _is_system in seed.ROLES:
        assert repr(code) in emitted and repr(name_fa) in emitted
    for code, _description in seed.PERMISSIONS:
        assert repr(code) in emitted
    for role_code, permission_code in seed.GRANTS:
        assert f"({role_code!r}, {permission_code!r})" in emitted


def test_no_surrogate_ids_appear_in_the_seed(seed):
    """Ids differ per environment; the seed must resolve through codes only."""
    text = MIGRATION.read_text(encoding="utf-8")
    assert "role_id\": 1" not in text and "permission_id\": 1" not in text
    assert "WHERE r.code = :role_code" in text
    assert "p.code = :permission_code" in text


def test_seed_writes_are_conflict_safe(seed):
    text = MIGRATION.read_text(encoding="utf-8").upper()
    assert text.count("ON CONFLICT") >= 3, "every seed write must be idempotent"
