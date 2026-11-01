from typing import Any, Awaitable, Callable

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram_dialog.utils import remove_intent_id
from dishka import AsyncContainer
from loguru import logger

from src.application.common.dao import SubscriptionDao
from src.application.common.policy import Permission, PermissionPolicy
from src.application.dto import TelegramUserDto
from src.core.constants import CONTAINER_KEY, USER_KEY
from src.core.enums import MiddlewareEventType

from .base import EventTypedMiddleware


class MigrationGuardMiddleware(EventTypedMiddleware):
    """Restrict Telegram updates to the migration UI while v3 IDs are incomplete."""

    __event_types__ = [MiddlewareEventType.MESSAGE, MiddlewareEventType.CALLBACK_QUERY]

    async def middleware_logic(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        container: AsyncContainer = data[CONTAINER_KEY]
        subscription_dao = await container.get(SubscriptionDao)
        missing_ids = await subscription_dao.get_current_missing_remnawave_ids()
        if not missing_ids:
            return await handler(event, data)

        user: TelegramUserDto | None = data.get(USER_KEY)
        if user is None or not PermissionPolicy.has_permission(user, Permission.VIEW_IMPORTER):
            await self._deny(event)
            return None

        state: FSMContext | None = data.get("state")
        current_state = await state.get_state() if state else None

        if isinstance(event, Message):
            command = (event.text or "").split(maxsplit=1)[0].split("@", 1)[0]
            if command in {"/start", "/remnawave_migration"}:
                return await handler(event, data)

        if isinstance(event, CallbackQuery):
            payload = remove_intent_id(event.data or "")
            action = payload[-1] if payload else ""
            if current_state == "MainMenu:MAIN" and action == "dashboard":
                return await handler(event, data)
            if current_state == "Dashboard:MAIN" and action == "remnawave_migration":
                return await handler(event, data)
            if (
                current_state
                and current_state.startswith("DashboardRemnawaveMigration:")
                and action in {"migration_check", "migration_apply", "migration_confirm", "back"}
            ):
                return await handler(event, data)

        logger.warning(
            "{} blocked by Remnawave migration guard; {} current subscription(s) "
            "still have no numeric panel ID",
            user.log,
            len(missing_ids),
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
