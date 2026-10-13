import asyncio
from dataclasses import fields, is_dataclass
from datetime import timedelta
from typing import Any, Optional, Union
from uuid import UUID

from loguru import logger
from packaging.version import Version
from remnawave import RemnawaveSDK
from remnawave.enums import TrafficLimitStrategy, UserStatus
from remnawave.models import (
    CreateUserBodyDto,
    DeleteUserAllHwidDeviceRequestDto,
    DeleteUserHwidDeviceRequestDto,
    DropByUserUuids,
    DropConnectionsRequestDto,
    GetMetadataResponseDto,
    TargetAllNodes,
    UpdateUserBodyDto,
)
from remnawave.models.hwid import HwidDeviceDto

from src.application.common import Remnawave
from src.application.common.remnawave import T
from src.application.dto import (
    PlanSnapshotDto,
    RemnaSubscriptionDto,
    SquadInfoDto,
    SubscriptionDto,
    UserDto,
)
from src.core.constants import REMNAWAVE_MIN_VERSION
from src.core.enums import SubscriptionStatus
from src.core.utils.converters import days_to_datetime, gb_to_bytes
from src.core.utils.time import datetime_now
from src.core.types import RemnaUserView as RemnawaveUser


class RemnawaveImpl(Remnawave):
    def __init__(self, sdk: RemnawaveSDK) -> None:
        self.sdk = sdk
        self._user_ids: dict[UUID, int] = {}

    @staticmethod
    def _not_found(error: Exception) -> bool:
        response = getattr(error, "response", None)
        status_code = getattr(error, "status_code", None) or getattr(response, "status_code", None)
        return status_code == 404 or "notfound" in type(error).__name__.lower()

    @staticmethod
    def _conflict(error: Exception) -> bool:
        response = getattr(error, "response", None)
        status_code = getattr(error, "status_code", None) or getattr(response, "status_code", None)
        return status_code == 409 or "conflict" in type(error).__name__.lower()

    def _remember(self, payload: Any) -> RemnawaveUser:
        user = RemnawaveUser(payload)
        try:
            key = UUID(str(user.uuid))
            numeric_id = int(user.id)
        except (AttributeError, TypeError, ValueError):
            return user
        self._user_ids[key] = numeric_id
        return user

    async def _resolve_user_id(self, user_uuid: UUID) -> int:
        key = UUID(str(user_uuid))
        cached = self._user_ids.get(key)
        if cached is not None:
            return cached

        # The v3 API does not provide lookup-by-vlessUuid. Resolve it through
        # the paginated user collection and cache the numeric ID for later calls.
        offset = 0
        page_size = 1000
        while True:
            response = await self.sdk.users.get_all_users(start=offset, size=page_size)
            users = response.users
            for payload in users:
                user = self._remember(payload)
                if user.uuid == key:
                    return int(user.id)
            if len(users) < page_size:
                break
            offset += len(users)

        raise LookupError(f"Remnawave user with VLESS UUID '{key}' was not found")

    async def try_connection(self) -> Version:
        for attempt in range(1, 4):
            try:
                metadata = await self.sdk.system.get_metadata()
                break
            except Exception as error:
                if attempt < 3 and "authentication" not in type(error).__name__.lower():
                    logger.warning(
                        f"Failed to connect to Remnawave panel (attempt {attempt}/3): "
                        f"'{error}', retrying in 5s..."
                    )
                    await asyncio.sleep(5)
                else:
                    logger.error(f"Failed to connect to Remnawave panel: '{error}'")
                    raise

        if not isinstance(metadata, GetMetadataResponseDto):
            logger.error(f"Invalid response from Remnawave panel: '{metadata}'")
            raise ValueError(f"Invalid response from Remnawave panel: {metadata}")

        panel_version = Version(str(metadata.version).removeprefix("v"))
        if panel_version < REMNAWAVE_MIN_VERSION:
            raise ValueError(
                f"Remnawave panel version '{panel_version}' is not compatible. "
                f"Minimum required version: '{REMNAWAVE_MIN_VERSION}'"
            )

        logger.info(f"Successfully connected to Remnawave panel (version: {panel_version})")
        return panel_version

    async def create_user(
        self,
        user: UserDto,
        plan: Optional[PlanSnapshotDto] = None,
        subscription: Optional[SubscriptionDto] = None,
    ) -> RemnawaveUser:
        request_dto = self._build_create_request(user, plan, subscription)
        try:
            payload = await self.sdk.users.create_user(request_dto)
        except Exception as error:
            if self._conflict(error):
                logger.warning(
                    f"Remnawave user '{request_dto.username}' already exists in the panel"
                )
            raise
        created = self._remember(payload)
        logger.info(
            f"Remnawave user '{created.username}' created (id={created.id}, uuid={created.uuid})"
        )
        return created

    async def update_user(
        self,
        user: UserDto,
        uuid: UUID,
        plan: Optional[PlanSnapshotDto] = None,
        subscription: Optional[SubscriptionDto] = None,
        reset_traffic: bool = False,
    ) -> RemnawaveUser:
        request_dto = await self._build_update_request(user, uuid, plan, subscription)
        try:
            payload = await self.sdk.users.update_user(request_dto)
        except Exception as error:
            if self._not_found(error):
                logger.warning(f"Remnawave user '{uuid}' was not found")
            raise

        updated = self._remember(payload)
        if reset_traffic:
            await self.reset_traffic(uuid)
        return updated

    async def enable_user(self, uuid: UUID) -> None:
        user_id = await self._resolve_user_id(uuid)
        await self.sdk.users.enable_user(user_id)
        logger.info(f"Remnawave user '{uuid}' enabled")

    async def disable_user(self, uuid: UUID) -> None:
        user_id = await self._resolve_user_id(uuid)
        await self.sdk.users.disable_user(user_id)
        logger.info(f"Remnawave user '{uuid}' disabled")

    async def delete_user(self, uuid: UUID) -> bool:
        try:
            user_id = await self._resolve_user_id(uuid)
            await self.sdk.users.delete_user(user_id)
        except Exception as error:
            if self._not_found(error) or isinstance(error, LookupError):
                logger.debug(f"Remnawave user '{uuid}' was not found")
                return False
            raise
        self._user_ids.pop(UUID(str(uuid)), None)
        logger.info(f"Remnawave user '{uuid}' deleted")
        return True

    async def get_user_by_uuid(self, uuid: UUID) -> Optional[RemnawaveUser]:
        try:
            user_id = await self._resolve_user_id(uuid)
            return self._remember(await self.sdk.users.get_user_by_id(user_id))
        except Exception as error:
            if self._not_found(error) or isinstance(error, LookupError):
                logger.debug(f"Remnawave user '{uuid}' was not found")
                return None
            raise

    async def get_users_by_telegram_id(self, telegram_id: int) -> list[RemnawaveUser]:
        matches = [
            user
            for user in await self._fetch_users()
            if user.telegram_id == telegram_id
        ]
        logger.debug(f"Fetched {len(matches)} Remnawave users for telegram_id '{telegram_id}'")
        return matches

    async def get_all_users(self, limit: int, offset: int) -> list[RemnawaveUser]:
        response = await self.sdk.users.get_all_users(start=offset, size=limit)
        users = [self._remember(payload) for payload in response.users]
        logger.debug(f"Fetched {len(users)} Remnawave users (limit={limit}, offset={offset})")
        return users

    async def get_users_by_email(self, email: str) -> list[RemnawaveUser]:
        matches = [user for user in await self._fetch_users() if user.email == email]
        logger.debug(f"Fetched {len(matches)} Remnawave users for email '{email}'")
        return matches

    async def _fetch_users(self) -> list[RemnawaveUser]:
        users: list[RemnawaveUser] = []
        offset = 0
        page_size = 1000
        while True:
            response = await self.sdk.users.get_all_users(start=offset, size=page_size)
            batch = [self._remember(payload) for payload in response.users]
            users.extend(batch)
            if len(batch) < page_size:
                return users
            offset += len(batch)

    async def get_devices(self, user_uuid: UUID) -> list[HwidDeviceDto]:
        user_id = await self._resolve_user_id(user_uuid)
        response = await self.sdk.hwid.get_hwid_user(user_id)
        return response.devices if response.total else []

    async def delete_device(self, user_uuid: UUID, hwid_uuid: str) -> Optional[int]:
        try:
            user_id = await self._resolve_user_id(user_uuid)
            response = await self.sdk.hwid.delete_hwid_to_user(
                DeleteUserHwidDeviceRequestDto(user_id=user_id, hwid=hwid_uuid)
            )
        except Exception as error:
            if self._not_found(error) or isinstance(error, LookupError):
                return None
            raise
        logger.info(
            f"Deleted HWID device '{hwid_uuid}' for user '{user_uuid}'. "
            f"Total devices now: {response.total}"
        )
        return int(response.total)

    async def delete_all_devices(self, user_uuid: UUID) -> None:
        try:
            user_id = await self._resolve_user_id(user_uuid)
            result = await self.sdk.hwid.delete_all_hwid_user(
                DeleteUserAllHwidDeviceRequestDto(user_id=user_id)
            )
        except Exception as error:
            if self._not_found(error) or isinstance(error, LookupError):
                return
            raise
        logger.info(f"Deleted all HWID devices ({result.total}) for user '{user_uuid}'")

    async def drop_connections(self, user_uuid: UUID) -> None:
        try:
            user_id = await self._resolve_user_id(user_uuid)
            await self.sdk.connections.drop_connections(
                DropConnectionsRequestDto(
                    drop_by=DropByUserUuids(user_ids=[user_id]),
                    target_nodes=TargetAllNodes(),
                )
            )
        except Exception as error:
            logger.warning(f"Failed to drop connections for Remnawave user '{user_uuid}': {error}")

    async def reset_traffic(self, uuid: UUID) -> Optional[RemnawaveUser]:
        try:
            user_id = await self._resolve_user_id(uuid)
            payload = await self.sdk.users.reset_user_traffic(user_id)
        except Exception as error:
            if self._not_found(error) or isinstance(error, LookupError):
                logger.debug(f"Remnawave user '{uuid}' was not found")
                return None
            raise
        return self._remember(payload)

    async def revoke_subscription(self, uuid: UUID) -> None:
        try:
            user_id = await self._resolve_user_id(uuid)
            await self.sdk.users.revoke_user_subscription(user_id)
        except Exception as error:
            if self._not_found(error) or isinstance(error, LookupError):
                logger.debug(f"Remnawave user '{uuid}' was not found")
                return
            raise

    async def get_squads_available(self) -> bool:
        result = await self.sdk.internal_squads.get_internal_squads()
        return bool(result.internal_squads)

    async def get_internal_squads(self) -> list[SquadInfoDto]:
        result = await self.sdk.internal_squads.get_internal_squads()
        return [SquadInfoDto(uuid=s.uuid, name=s.name) for s in result.internal_squads]

    async def get_external_squads(self) -> list[SquadInfoDto]:
        result = await self.sdk.external_squads.get_external_squads()
        return [SquadInfoDto(uuid=s.uuid, name=s.name) for s in result.external_squads]

    def apply_sync(self, target: T, source: Union[SubscriptionDto, RemnaSubscriptionDto]) -> T:
        if not is_dataclass(target) or not is_dataclass(source):
            raise TypeError("Both target and source must be dataclasses")

        target_fields = {f.name for f in fields(target)}
        source_fields = {f.name for f in fields(source)}
        field_map = {"user_remna_id": "uuid"}

        for target_field, source_field in field_map.items():
            if target_field in target_fields and source_field in source_fields:
                old_value = getattr(target, target_field)
                new_value = getattr(source, source_field)
                if old_value != new_value:
                    logger.debug(
                        f"Field '{target_field}' changed from '{old_value}' to '{new_value}'"
                    )
                    setattr(target, target_field, new_value)

        for field_name in target_fields & source_fields:
            old_value = getattr(target, field_name)
            new_value = getattr(source, field_name)
            if old_value != new_value:
                logger.debug(f"Field '{field_name}' changed from '{old_value}' to '{new_value}'")
                setattr(target, field_name, new_value)
        return target

    def _build_create_request(
        self,
        user: UserDto,
        plan: Optional[PlanSnapshotDto],
        subscription: Optional[SubscriptionDto],
    ) -> CreateUserBodyDto:
        common = {
            "username": user.remna_name,
            "telegram_id": user.telegram_id,
            "description": user.remna_description,
            "email": user.email,
        }
        if subscription:
            return CreateUserBodyDto(
                **common,
                vless_uuid=subscription.user_remna_id,
                expire_at=subscription.expire_at,
                traffic_limit_strategy=TrafficLimitStrategy(subscription.traffic_limit_strategy),
                traffic_limit_bytes=gb_to_bytes(subscription.traffic_limit),
                hwid_device_limit=subscription.device_limit,
                tag=subscription.tag,
                active_internal_squads=subscription.internal_squads,
                external_squad_uuid=subscription.external_squad,
            )
        if plan:
            return CreateUserBodyDto(
                **common,
                expire_at=days_to_datetime(plan.duration),
                traffic_limit_strategy=TrafficLimitStrategy(plan.traffic_limit_strategy),
                traffic_limit_bytes=gb_to_bytes(plan.traffic_limit),
                hwid_device_limit=plan.device_limit,
                tag=plan.tag,
                active_internal_squads=plan.internal_squads,
                external_squad_uuid=plan.external_squad,
            )
        return CreateUserBodyDto(
            **common,
            expire_at=datetime_now() + timedelta(days=3650),
        )

    async def _build_update_request(
        self,
        user: UserDto,
        uuid: UUID,
        plan: Optional[PlanSnapshotDto],
        subscription: Optional[SubscriptionDto],
    ) -> UpdateUserBodyDto:
        user_id = await self._resolve_user_id(uuid)
        common = {
            "id": user_id,
            "telegram_id": user.telegram_id,
            "description": user.remna_description,
            "email": user.email,
        }
        if subscription:
            return UpdateUserBodyDto(
                **common,
                expire_at=subscription.expire_at,
                status=(
                    UserStatus.DISABLED
                    if subscription.status == SubscriptionStatus.DISABLED
                    else UserStatus.ACTIVE
                ),
                traffic_limit_strategy=TrafficLimitStrategy(subscription.traffic_limit_strategy),
                traffic_limit_bytes=gb_to_bytes(subscription.traffic_limit),
                hwid_device_limit=subscription.device_limit,
                tag=subscription.tag,
                active_internal_squads=subscription.internal_squads,
                external_squad_uuid=subscription.external_squad,
            )
        if plan:
            return UpdateUserBodyDto(
                **common,
                expire_at=days_to_datetime(plan.duration),
                status=UserStatus.ACTIVE,
                traffic_limit_strategy=TrafficLimitStrategy(plan.traffic_limit_strategy),
                traffic_limit_bytes=gb_to_bytes(plan.traffic_limit),
                hwid_device_limit=plan.device_limit,
                tag=plan.tag,
                active_internal_squads=plan.internal_squads,
                external_squad_uuid=plan.external_squad,
            )
        raise ValueError("Either 'plan' or 'subscription' must be provided")
