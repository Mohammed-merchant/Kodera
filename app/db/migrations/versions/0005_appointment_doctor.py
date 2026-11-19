"""Appointments know their doctor: one booking per doctor per slot.

The clinic had one doctor, so a slot could be booked once. With the Smart Hospital backend there
are several doctors: two patients may book 5 PM with two different doctors. Existing rows get an
empty doctor, so for the one-doctor setup nothing changes.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BOOKED = sa.text("status = 'booked'")


def upgrade() -> None:
    with op.batch_alter_table("appointments") as batch:
        batch.add_column(sa.Column("doctor", sa.String(100), nullable=False, server_default=""))
    op.drop_index("uq_appointments_booked_slot", table_name="appointments")
    op.create_index(
        "uq_appointments_booked_doctor_slot", "appointments", ["doctor", "slot_start"],
        unique=True, postgresql_where=BOOKED, sqlite_where=BOOKED,
    )  # fmt: skip


def downgrade() -> None:
    op.drop_index("uq_appointments_booked_doctor_slot", table_name="appointments")
    op.create_index(
        "uq_appointments_booked_slot", "appointments", ["slot_start"],
        unique=True, postgresql_where=BOOKED, sqlite_where=BOOKED,
    )  # fmt: skip
    with op.batch_alter_table("appointments") as batch:
        batch.drop_column("doctor")
