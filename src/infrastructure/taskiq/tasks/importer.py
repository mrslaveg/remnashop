from uuid import UUID

from adaptix import Retort
from dishka.integrations.taskiq import FromDishka, inject
from loguru import logger
from redis.asyncio import Redis
from remnawave import RemnawaveSDK
from remnawave.enums import UserStatus
from remnawave.models import CreateUserBodyDto

from src.application.use_cases.importer.dto import ExportedUserDto
from src.application.use_cases.remnawave.commands.synchronization import (
    SyncAllUsersFromBot,
    SyncAllUsersFromPanel,
)
from src.infrastructure.redis.keys import SyncBotRunningKey, SyncPanelRunningKey
from src.infrastructure.taskiq.broker import broker


@broker.task
@inject(patch_module=True)
async def import_exported_users_task(
    imported_users: list[ExportedUserDto],
    active_internal_squads: list[UUID],
    remnawave_sdk: FromDishka[RemnawaveSDK],
) -> tuple[int, int]:
    logger.info(f"Starting import of '{len(imported_users)}' users")

    success_count = 0
    failed_count = 0

    for user in imported_users:
        try:
            created_user = CreateUserBodyDto(
                username=user.username,
                telegram_id=user.telegram_id,
                status=UserStatus(user.status.value),
                expire_at=user.expire_at,
                traffic_limit_bytes=user.traffic_limit_bytes,
                hwid_device_limit=user.hwid_device_limit,
                tag=user.tag,
                active_internal_squads=active_internal_squads,
            )
            await remnawave_sdk.users.create_user(created_user)
            success_count += 1
        except Exception as exception:
            response = getattr(exception, "response", None)
            status_code = getattr(exception, "status_code", None) or getattr(
                response, "status_code", None
            )
            if status_code in {400, 409} or "conflict" in type(exception).__name__.lower():
                logger.warning(
                    f"User '{user.username}' already exists or was rejected by the panel: "
                    f"{exception}"
                )
            else:
                logger.exception(
                    f"Failed to create user '{user.username}' exception: {exception}"
                )
            failed_count += 1

    logger.info(f"Import completed: '{success_count}' successful, '{failed_count}' failed")
    return success_count, failed_count


@broker.task
@inject(patch_module=True)
async def sync_all_users_from_panel_task(
    retort: FromDishka[Retort],
    redis: FromDishka[Redis],
    sync_all_users: FromDishka[SyncAllUsersFromPanel],
) -> dict[str, int]:
    key = retort.dump(SyncPanelRunningKey())
    try:
        return await sync_all_users.system()
    finally:
        await redis.delete(key)


@broker.task
@inject(patch_module=True)
async def sync_all_users_from_bot_task(
    retort: FromDishka[Retort],
    redis: FromDishka[Redis],
    sync_all_users: FromDishka[SyncAllUsersFromBot],
) -> dict[str, int]:
    key = retort.dump(SyncBotRunningKey())
    try:
        return await sync_all_users.system()
    finally:
        await redis.delete(key)
