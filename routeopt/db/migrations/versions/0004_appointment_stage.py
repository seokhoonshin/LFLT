"""appointments: stage (original / reoptimized)

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-29 15:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("appointments", schema=None) as batch_op:
        batch_op.add_column(sa.Column("stage", sa.String(length=16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("appointments", schema=None) as batch_op:
        batch_op.drop_column("stage")
