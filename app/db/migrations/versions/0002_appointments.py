"""Create appointments.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BIGINT_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "appointments",
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
        sa.Column("patient_name", sa.String(100), nullable=False),
        sa.Column("phone", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(300)),
        sa.Column("slot_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("booked_via_call", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_appointments_phone", "appointments", ["phone"])
    op.create_index("ix_appointments_slot_start", "appointments", ["slot_start"])
    op.create_index(
        "uq_appointments_booked_slot",
        "appointments",
        ["slot_start"],
        unique=True,
        postgresql_where=sa.text("status = 'booked'"),
        sqlite_where=sa.text("status = 'booked'"),
    )


def downgrade() -> None:
    op.drop_table("appointments")
