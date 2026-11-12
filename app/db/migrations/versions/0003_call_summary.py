"""Add call outcome/summary columns and per-turn timings.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("calls", sa.Column("outcome", sa.String(20)))
    op.add_column("calls", sa.Column("turn_count", sa.Integer()))
    op.add_column("calls", sa.Column("avg_response_ms", sa.Integer()))
    for column in ("response_ms", "stt_ms", "llm_ms", "tts_ms"):
        op.add_column("transcript_turns", sa.Column(column, sa.Integer()))


def downgrade() -> None:
    for column in ("tts_ms", "llm_ms", "stt_ms", "response_ms"):
        op.drop_column("transcript_turns", column)
    for column in ("avg_response_ms", "turn_count", "outcome"):
        op.drop_column("calls", column)
