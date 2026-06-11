"""baseline schema — full current model set

Creates every table currently defined on the application's ``Base.metadata``.
This is the adoption baseline for an ORM-first project: ``create_all`` is
idempotent (checkfirst), so on a brand-new database it builds the full schema,
and an already-provisioned database should instead be marked with
``alembic stamp head`` (no DDL run). Future schema changes get their own
incremental revisions on top of this one.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-06-11
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _metadata():
    # Import every model module so all tables are registered before create/drop.
    from app.database.models import Base

    for mod in (
        "app.accounting.models",
        "app.live_beta.models",
        "app.order_failures.models",
        "app.reconciliation.models",
    ):
        try:
            __import__(mod)
        except Exception:  # noqa: BLE001 — optional module must not break the migration
            pass
    return Base.metadata


def upgrade() -> None:
    _metadata().create_all(bind=op.get_bind())


def downgrade() -> None:
    _metadata().drop_all(bind=op.get_bind())
