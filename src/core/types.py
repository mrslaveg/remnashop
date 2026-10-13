from typing import TYPE_CHECKING, Annotated, Any, NewType, TypeAlias, Union

from aiogram.types import (
    ForceReply,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from pydantic import PlainValidator
from remnawave.models import UserResponseDto
from remnawave.models.webhook import UserDto as UserWebhookDto

from src.core.enums import Locale, SystemNotificationType, UserNotificationType

if TYPE_CHECKING:
    ListStr: TypeAlias = list[str]
    ListLocale: TypeAlias = list[Locale]
else:
    ListStr = NewType("ListStr", list[str])
    ListLocale = NewType("ListLocale", list[Locale])

AnyKeyboard: TypeAlias = Union[
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    ForceReply,
]


NotificationType: TypeAlias = Union[SystemNotificationType, UserNotificationType]


class RemnaUserView:
    """Normalize Remnawave v3 API and webhook users for Remnashop's domain layer."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def __getattr__(self, name: str) -> Any:
        if name == "uuid":
            return (
                getattr(self._payload, "vless_uuid", None)
                or getattr(self._payload, "uuid", None)
            )
        if name == "url":
            return getattr(self._payload, "subscription_url", None)
        if name == "first_connected_at":
            return getattr(
                getattr(self._payload, "user_traffic", self._payload),
                "first_connected_at",
                getattr(self._payload, "first_connected", None),
            )
        return getattr(self._payload, name)


RemnaUserDto: TypeAlias = Union[UserWebhookDto, UserResponseDto, RemnaUserView]

StringList: TypeAlias = Annotated[
    ListStr, PlainValidator(lambda x: [s.strip() for s in x.split(",")])
]
LocaleList: TypeAlias = Annotated[
    ListLocale,
    PlainValidator(
        func=lambda x: [Locale(loc.strip()) for loc in x.split(",")] if isinstance(x, str) else x
    ),
]
