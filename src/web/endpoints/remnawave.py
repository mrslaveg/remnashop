from typing import cast

from dishka import FromDishka
from dishka.integrations.fastapi import inject
from fastapi import APIRouter, HTTPException, Request, Response, status
from loguru import logger
from remnawave.controllers import WebhookUtility
from remnawave.models.webhook import (
    NodeDto,
    TorrentBlockerReportDto,
    UserDto,
    UserHwidDeviceEventDto,
)

from src.application.common import EventPublisher
from src.application.events import ErrorEvent
from src.application.services import RemnaWebhookService
from src.core.config import AppConfig
from src.core.constants import API_V1, REMNAWAVE_WEBHOOK_PATH
from src.core.types import RemnaUserView

router = APIRouter(prefix=API_V1, include_in_schema=False)


async def _process_remnawave_webhook(
    request: Request,
    config: AppConfig,
    remna_webhook_service: RemnaWebhookService,
    event_publisher: EventPublisher,
) -> Response:
    try:
        raw_body = await request.body()
        logger.debug(f"Received Remnawave webhook raw body: '{raw_body.decode('utf-8')[:500]}'")
        payload = WebhookUtility.parse_webhook(
            body=raw_body.decode("utf-8"),
            headers=dict(request.headers),
            webhook_secret=config.remnawave.webhook_secret.get_secret_value(),
            validate=True,
        )
    except Exception as e:
        logger.exception(f"Webhook validation failed with error '{e}'")
        raise HTTPException(status_code=401)

    if not payload:
        logger.warning("Payload is empty after validation")
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        if WebhookUtility.is_user_event(payload.event):
            user = cast(UserDto, WebhookUtility.get_typed_data(payload))
            await remna_webhook_service.handle_user_event(payload.event, RemnaUserView(user))

        elif WebhookUtility.is_user_hwid_devices_event(payload.event):
            event = cast(UserHwidDeviceEventDto, WebhookUtility.get_typed_data(payload))
            await remna_webhook_service.handle_device_event(
                payload.event,
                RemnaUserView(event.user),
                event.hwid_user_device,
            )

        elif WebhookUtility.is_node_event(payload.event):
            node = cast(NodeDto, WebhookUtility.get_typed_data(payload))
            await remna_webhook_service.handle_node_event(payload.event, node)

        elif payload.event.startswith("torrent_blocker."):
            # The v3 SDK's webhook schema no longer matches the legacy report DTO.
            # Do not feed an incompatible payload into the legacy notification handler.
            logger.warning(
                "Ignoring unsupported Remnawave v3 torrent-blocker payload; "
                "its report schema needs a dedicated v3 adapter"
            )

        else:
            logger.warning(f"Unhandled Remnawave event type '{payload.event}'")

    except Exception as e:
        logger.exception(f"Failed to process Remnawave webhook due to '{e}'")
        error_event = ErrorEvent(**config.build.data, exception=e)
        await event_publisher.publish(error_event)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    return Response(status_code=status.HTTP_200_OK)


@router.post(REMNAWAVE_WEBHOOK_PATH)
@inject
async def remnawave_webhook(
    request: Request,
    config: FromDishka[AppConfig],
    remna_webhook_service: FromDishka[RemnaWebhookService],
    event_publisher: FromDishka[EventPublisher],
) -> Response:
    return await _process_remnawave_webhook(
        request=request,
        config=config,
        remna_webhook_service=remna_webhook_service,
        event_publisher=event_publisher,
    )
