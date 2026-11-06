"""aging snapshots (Analyze page)

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-30 09:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "aging_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("rows", sa.Integer(), nullable=False),
        sa.Column("data_json", sa.Text(), nullable=False),
        sa.Column("source_name", sa.String(length=255), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("aging_snapshots", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_aging_snapshots_level"), ["level"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_aging_snapshots_snapshot_date"), ["snapshot_date"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("aging_snapshots", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_aging_snapshots_snapshot_date"))
        batch_op.drop_index(batch_op.f("ix_aging_snapshots_level"))
    op.drop_table("aging_snapshots")
