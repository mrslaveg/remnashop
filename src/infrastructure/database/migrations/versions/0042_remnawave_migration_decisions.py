"""Persist administrator decisions for Remnawave ID migration.

Revision ID: 0042
Revises: 0041
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0042"
down_revision: Union[str, None] = "0041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "remnawave_migration_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "subscription_id",
            sa.Integer(),
            sa.ForeignKey("subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("legacy_uuid", sa.String(length=36), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("remnawave_user_id", sa.BigInteger(), nullable=True),
        sa.Column("admin_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("timezone('UTC', now())"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("timezone('UTC', now())"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('manual_map', 'skip')",
            name="ck_remnawave_migration_decisions_action",
        ),
        sa.CheckConstraint(
            "(action = 'manual_map' AND remnawave_user_id IS NOT NULL) OR "
            "(action = 'skip' AND remnawave_user_id IS NULL)",
            name="ck_remnawave_migration_decisions_target",
        ),
        sa.UniqueConstraint("subscription_id"),
    )
    op.create_index(
        "ix_remnawave_migration_decisions_user_id",
        "remnawave_migration_decisions",
        ["user_id"],
    )
    op.create_index(
        "uq_remnawave_migration_decisions_panel_id",
        "remnawave_migration_decisions",
        ["remnawave_user_id"],
        unique=True,
        postgresql_where=sa.text("remnawave_user_id IS NOT NULL"),
    )

    op.create_table(
        "remnawave_migration_audit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscription_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("legacy_uuid", sa.String(length=36), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.Column("remnawave_user_id", sa.BigInteger(), nullable=True),
        sa.Column("admin_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("timezone('UTC', now())"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_remnawave_migration_audit_subscription_id",
        "remnawave_migration_audit",
        ["subscription_id"],
    )
    op.create_index(
        "ix_remnawave_migration_audit_user_id",
        "remnawave_migration_audit",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_remnawave_migration_audit_user_id",
        table_name="remnawave_migration_audit",
    )
    op.drop_index(
        "ix_remnawave_migration_audit_subscription_id",
        table_name="remnawave_migration_audit",
    )
    op.drop_table("remnawave_migration_audit")
    op.drop_index(
        "uq_remnawave_migration_decisions_panel_id",
        table_name="remnawave_migration_decisions",
    )
    op.drop_index(
        "ix_remnawave_migration_decisions_user_id",
        table_name="remnawave_migration_decisions",
    )
    op.drop_table("remnawave_migration_decisions")
