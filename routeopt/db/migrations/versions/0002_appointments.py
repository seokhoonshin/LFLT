"""appointments

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-29 07:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "appointments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("truck", sa.String(length=64), nullable=False),
        sa.Column("load_seq", sa.Integer(), nullable=False),
        sa.Column("stop_seq", sa.Integer(), nullable=False),
        sa.Column("stop_id", sa.String(length=32), nullable=False),
        sa.Column("shipment_id", sa.String(length=128), nullable=False),
        sa.Column("customer", sa.Text(), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("bill_to_code", sa.String(length=64), nullable=True),
        sa.Column("delivery_date", sa.Date(), nullable=False),
        sa.Column("eta", sa.String(length=16), nullable=False),
        sa.Column("window", sa.String(length=40), nullable=False),
        sa.Column("volume_cuft", sa.Float(), nullable=False),
        sa.Column("miles_from_prev", sa.Float(), nullable=False),
        sa.Column("stop_transit_min", sa.Float(), nullable=False),
        sa.Column("poc", sa.Text(), nullable=False),
        sa.Column("dc", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("selected_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("appointments", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_appointments_run_id"), ["run_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("appointments", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_appointments_run_id"))
    op.drop_table("appointments")
