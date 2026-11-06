"""appointments: TMS load id, appointment number and time

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-29 12:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("appointments", schema=None) as batch_op:
        batch_op.add_column(sa.Column("load_id", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("appt_num", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("appt_time", sa.String(length=16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("appointments", schema=None) as batch_op:
        batch_op.drop_column("appt_time")
        batch_op.drop_column("appt_num")
        batch_op.drop_column("load_id")
