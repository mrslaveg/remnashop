from datetime import datetime
from typing import Any, Optional

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import BaseSql
from .timestamp import NOW_FUNC, TimestampMixin


class RemnawaveMigrationDecision(BaseSql, TimestampMixin):
    """Persistent administrator decision for one local current subscription."""

    __tablename__ = "remnawave_migration_decisions"
    __table_args__ = (
        CheckConstraint(
            "action IN ('manual_map', 'skip')",
            name="ck_remnawave_migration_decisions_action",
        ),
        CheckConstraint(
            "(action = 'manual_map' AND remnawave_user_id IS NOT NULL) OR "
            "(action = 'skip' AND remnawave_user_id IS NULL)",
            name="ck_remnawave_migration_decisions_target",
        ),
        Index(
            "uq_remnawave_migration_decisions_panel_id",
            "remnawave_user_id",
            unique=True,
            postgresql_where="remnawave_user_id IS NOT NULL",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    legacy_uuid: Mapped[str] = mapped_column(String(36))
    source_fingerprint: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(16))
    remnawave_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    admin_telegram_id: Mapped[int] = mapped_column(BigInteger)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class RemnawaveMigrationAudit(BaseSql):
    """Append-only audit trail; intentionally has no cascading source FK."""

    __tablename__ = "remnawave_migration_audit"

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    legacy_uuid: Mapped[str] = mapped_column(String(36))
    source_fingerprint: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(24))
    remnawave_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    admin_telegram_id: Mapped[int] = mapped_column(BigInteger)
    details: Mapped[Optional[dict[str, Any]]]
    created_at: Mapped[datetime] = mapped_column(
        server_default=NOW_FUNC,
        nullable=False,
    )
