"""
Alembic migration scaffolding (A).

Validates the migration environment without a database: a single baseline head,
the baseline is reversible, and the target metadata covers every model domain so
`alembic upgrade head` would build the whole schema.
"""

from __future__ import annotations

import pathlib

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _cfg() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    return cfg


def test_single_head_is_baseline():
    script = ScriptDirectory.from_config(_cfg())
    assert script.get_heads() == ["0001_baseline"]


def test_baseline_is_reversible_root():
    script = ScriptDirectory.from_config(_cfg())
    rev = script.get_revision("0001_baseline")
    assert rev is not None
    assert rev.down_revision is None  # it is the root
    mod = rev.module
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)


def test_target_metadata_covers_all_model_domains():
    import app.database.models as m

    for mod in (
        "app.accounting.models",
        "app.live_beta.models",
        "app.order_failures.models",
        "app.reconciliation.models",
    ):
        __import__(mod)
    tables = set(m.Base.metadata.tables)
    # One representative table from each domain must be present.
    for t in (
        "signals",
        "auth_users",
        "exchange_accounts",
        "live_positions",
        "live_orders",
        "reconciliation_issues",
    ):
        assert t in tables, f"missing table {t}"
    assert len(tables) >= 30


def test_alembic_ini_points_at_alembic_dir():
    cfg = Config(str(ROOT / "alembic.ini"))
    assert cfg.get_main_option("script_location") == "alembic"
