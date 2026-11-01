"""Add numeric Remnawave v3 user ID to local subscriptions.

The legacy UUID column is intentionally retained and never rewritten.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0041"
down_revision: Union[str, None] = "0040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column("remnawave_user_id", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_subscriptions_remnawave_user_id",
        "subscriptions",
        ["remnawave_user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_subscriptions_remnawave_user_id", table_name="subscriptions")
    op.drop_column("subscriptions", "remnawave_user_id")
