import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from adaptix import Retort
from aiogram import Dispatcher
from aiogram.types import WebhookInfo
from dishka import AsyncContainer, Scope
from fastapi import FastAPI
from loguru import logger
from packaging.version import Version
from redis.asyncio import Redis

from src.application.common import BotService, Remnawave
from src.application.common.dao import SettingsDao, SubscriptionDao
from src.application.events import (
    BotInlineModeDisabledEvent,
    BotShutdownEvent,
    BotStartupEvent,
    RemnawaveErrorEvent,
    WebhookErrorEvent,
)
from src.application.events.system import RemnashopWelcomeEvent
from src.application.use_cases.gateways.commands.payment import CreateDefaultPaymentGateway
from src.application.use_cases.settings.commands.defaults import CreateDefaultSettings
from src.core.config import AppConfig
from src.core.constants import REMNAWAVE_MIGRATION_GUARD_KEY
from src.core.exceptions import RemnawaveCompatibilityError
from src.core.utils.i18n_helpers import i18n_format_seconds
from src.core.utils.time import get_uptime
from src.infrastructure.redis.keys import WelcomedVersionKey
from src.infrastructure.services import (
    CommandService,
    EventBusImpl,
    NotificationWorker,
    WebhookService,
)
from src.web.endpoints import TelegramWebhookEndpoint


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    dispatcher: Dispatcher = app.state.dispatcher
    telegram_webhook_endpoint: TelegramWebhookEndpoint = app.state.telegram_webhook_endpoint
    container: AsyncContainer = app.state.dishka_container

    event_bus = await container.get(EventBusImpl)
    event_bus.set_container_factory(lambda: container)
    event_bus.autodiscover()

    notification_worker = await container.get(NotificationWorker)
    notification_worker.set_container_factory(lambda: container)

    async with container(scope=Scope.REQUEST) as startup_container:
        config = await startup_container.get(AppConfig)
        bot_service = await startup_container.get(BotService)
        settings_dao = await startup_container.get(SettingsDao)
        webhook_service = await startup_container.get(WebhookService)
        command_service = await startup_container.get(CommandService)
        remnawave_service = await startup_container.get(Remnawave)
        subscription_dao = await startup_container.get(SubscriptionDao)

        migration_only_mode: Optional[bool] = None
        try:
            panel_version = await remnawave_service.try_connection()
        except RemnawaveCompatibilityError:
            raise
        except Exception as e:
            remnawave_error_event = RemnawaveErrorEvent(
                **config.build.data,
                exception=e,
            )
            await event_bus.publish(remnawave_error_event)
        else:
            missing_panel_ids = await subscription_dao.get_current_missing_remnawave_ids()
            migration_only_mode = panel_version < Version("3.0.0") or bool(missing_panel_ids)
            if migration_only_mode:
                sample_ids = ", ".join(str(value) for value in missing_panel_ids[:10]) or "none"
                logger.critical(
                    "Remnawave v{} connected; migration-only mode enabled "
                    "(missing numeric IDs: {}). Only authorized administrators can use "
                    "the migration screen. Open /remnawave_migration. Sample subscription IDs: {}",
                    panel_version,
                    len(missing_panel_ids),
                    sample_ids,
                )
            else:
                logger.info(
                    f"Remnawave v{panel_version} connected; all current subscriptions "
                    "have numeric panel IDs"
                )
        create_default_payment_gateway = await startup_container.get(CreateDefaultPaymentGateway)
        create_default_settings = await startup_container.get(CreateDefaultSettings)
        redis = await startup_container.get(Redis)
        if migration_only_mode is True:
            await redis.set(REMNAWAVE_MIGRATION_GUARD_KEY, "1")
        elif migration_only_mode is False:
            await redis.delete(REMNAWAVE_MIGRATION_GUARD_KEY)
        app.state.remnawave_migration_only = bool(
            await redis.get(REMNAWAVE_MIGRATION_GUARD_KEY)
        )
        retort = await startup_container.get(Retort)

        if not await bot_service.is_inline_enabled():
            logger.warning(
                "Bot is not enabled for inline mode. "
                "Please enable Inline Mode in BotFather for correct work of some features"
            )
            await event_bus.publish(BotInlineModeDisabledEvent())

        states = await bot_service.get_bot_states()
        await create_default_payment_gateway.system()
        # Seed the default settings row (committed, cache-invalidating) before any
        # request can read it, so `get()` never has to lazily create — and cache —
        # an uncommitted row whose id would later fail to match on update().
        await create_default_settings.system()
        settings = await settings_dao.get()
        allowed_updates = dispatcher.resolve_used_update_types()
        webhook_info: WebhookInfo = await webhook_service.setup_webhook(allowed_updates)

        if webhook_service.has_error(webhook_info):
            logger.critical(
                f"Webhook has a last error message: '{webhook_info.last_error_message}'"
            )
            webhook_error_event = WebhookErrorEvent(
                error_message=webhook_info.last_error_message,
            )
            await event_bus.publish(webhook_error_event)

        await command_service.setup_commands()

    await telegram_webhook_endpoint.startup()

    logger.opt(colors=True).info(
        rf"""
    <cyan> _____                                _                 </>
    <cyan>|  __ \                              | |                </>
    <cyan>| |__) |___ _ __ ___  _ __   __ _ ___| |__   ___  _ __  </>
    <cyan>|  _  // _ \ '_ ` _ \| '_ \ / _` / __| '_ \ / _ \| '_ \ </>
    <cyan>| | \ \  __/ | | | | | | | | (_| \__ \ | | | (_) | |_) |</>
    <cyan>|_|  \_\___|_| |_| |_|_| |_|\__,_|___/_| |_|\___/| .__/ </>
    <cyan>                                                 | |    </>
    <cyan>                                                 |_|    </>

        <green>Build Time: {config.build.time}</>
        <green>Branch: {config.build.branch} ({config.build.tag})</>
        <green>Commit: {config.build.commit}</>
        <cyan>------------------------</>
        Groups Mode  - {states["groups_mode"]}"
        Privacy Mode - {states["privacy_mode"]}"
        Inline Mode  - {states["inline_mode"]}"
        <cyan>------------------------</>
        <yellow>Bot in access mode: '{settings.access.mode}'</>
        <yellow>Payments allowed: '{settings.access.payments_allowed}'</>
        <yellow>Registration allowed: '{settings.access.registration_allowed}'</>
        """  # noqa: W605
    )

    current_version = config.build.tag or "dev"
    welcomed_key = retort.dump(WelcomedVersionKey(version="*"))
    last_welcomed = await redis.get(welcomed_key)

    if last_welcomed != current_version:
        await redis.set(welcomed_key, current_version)
        await event_bus.publish(RemnashopWelcomeEvent())

    bot_startup_event = BotStartupEvent(
        **config.build.data,
        access_mode=settings.access.mode,
        payments_allowed=settings.access.payments_allowed,
        registration_allowed=settings.access.registration_allowed,
    )
    await event_bus.publish(bot_startup_event)

    yield

    bot_shutdown_event = BotShutdownEvent(
        **config.build.data,
        uptime=i18n_format_seconds(get_uptime()),
    )
    await event_bus.publish(bot_shutdown_event)

    await asyncio.sleep(2)

    await event_bus.shutdown()
    await notification_worker.shutdown()
    await telegram_webhook_endpoint.shutdown()
    await command_service.delete_commands()
    await webhook_service.delete_webhook()
    await container.close()
