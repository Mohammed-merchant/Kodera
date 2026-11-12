"""Appointments can live in Cal.diy: add where they came from and the Cal.diy booking id.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("appointments") as batch:
        batch.add_column(sa.Column("source", sa.String(20), nullable=False, server_default="local"))
        batch.add_column(sa.Column("external_id", sa.String(64)))
        batch.create_unique_constraint("uq_appointments_external_id", ["external_id"])


def downgrade() -> None:
    with op.batch_alter_table("appointments") as batch:
        batch.drop_constraint("uq_appointments_external_id", type_="unique")
        batch.drop_column("external_id")
        batch.drop_column("source")
