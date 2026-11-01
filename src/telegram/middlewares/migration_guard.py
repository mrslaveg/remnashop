from typing import Any, Awaitable, Callable

from aiogram.types import CallbackQuery, Message, PreCheckoutQuery, TelegramObject
from aiogram_dialog import DialogManager
from aiogram_dialog.utils import remove_intent_id
from dishka import AsyncContainer
from loguru import logger
from redis.asyncio import Redis

from src.application.common.policy import Permission, PermissionPolicy
from src.application.dto import TelegramUserDto
from src.core.constants import (
    CONTAINER_KEY,
    REMNAWAVE_MIGRATION_GUARD_KEY,
    USER_KEY,
)
from src.core.enums import MiddlewareEventType

from .base import EventTypedMiddleware


class MigrationGuardMiddleware(EventTypedMiddleware):
    """Restrict Telegram updates to the migration UI while migration is required."""

    __event_types__ = [
        MiddlewareEventType.MESSAGE,
        MiddlewareEventType.CALLBACK_QUERY,
        MiddlewareEventType.PRE_CHECKOUT_QUERY,
    ]

    async def middleware_logic(  # noqa: C901
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        container: AsyncContainer = data[CONTAINER_KEY]
        redis = await container.get(Redis)
        if not await redis.get(REMNAWAVE_MIGRATION_GUARD_KEY):
            return await handler(event, data)

        user: TelegramUserDto | None = data.get(USER_KEY)
        if user is None or not PermissionPolicy.has_permission(user, Permission.VIEW_IMPORTER):
            await self._deny(event)
            return None

        # aiogram-dialog stores its active state in DialogManager's context;
        # FSMContext.get_state() does not reliably expose it at this middleware stage.
        dialog_manager: DialogManager | None = data.get("dialog_manager")
        dialog_state = None
        if dialog_manager is not None and dialog_manager.has_context():
            dialog_state = dialog_manager.current_context().state.state

        if isinstance(event, Message):
            command = (event.text or "").split(maxsplit=1)[0].split("@", 1)[0]
            if command in {"/start", "/remnawave_migration"}:
                return await handler(event, data)
            if dialog_state == "DashboardRemnawaveMigration:REVIEW":
                return await handler(event, data)

        if isinstance(event, CallbackQuery):
            payload = remove_intent_id(event.data or "")
            action = payload[-1] if payload else ""
            if dialog_state == "MainMenu:MAIN" and action == "dashboard":
                return await handler(event, data)
            if dialog_state == "Dashboard:MAIN" and action == "remnawave_migration":
                return await handler(event, data)
            if (
                dialog_state
                and dialog_state.startswith("DashboardRemnawaveMigration:")
                and action in {
                    "migration_check", "migration_review", "migration_apply",
                    "migration_confirm", "back",
                }
            ):
                return await handler(event, data)

        logger.warning(
            "{} blocked by Remnawave migration guard; panel is still in migration-only mode",
            user.log,
        )
        await self._deny(event)
        return None

    @staticmethod
    async def _deny(event: TelegramObject) -> None:
        message = (
            "Выполняется техническая миграция Remnawave. "
            "Обычные действия временно отключены. Откройте /remnawave_migration."
        )
        if isinstance(event, Message):
            await event.answer(message)
        elif isinstance(event, CallbackQuery):
            await event.answer(message, show_alert=True)
        elif isinstance(event, PreCheckoutQuery):
            await event.answer(ok=False, error_message=message)
