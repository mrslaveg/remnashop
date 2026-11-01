from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from src.core.types import RemnaUserView
from src.infrastructure.services.remnawave import RemnawaveImpl


def test_v3_user_view_uses_vless_uuid_as_subscription_identifier() -> None:
    vless_uuid = UUID("9d24b8c4-5d3c-4a1b-8c32-64d4e50b92d1")
    payload = SimpleNamespace(
        id=123,
        uuid=None,
        vless_uuid=vless_uuid,
        subscription_url="https://sub.example.test/short",
        user_traffic=SimpleNamespace(first_connected_at=None),
    )

    user = RemnaUserView(payload)

    assert user.id == 123
    assert user.uuid == vless_uuid
    assert user.url == "https://sub.example.test/short"


def test_v3_webhook_view_reads_nested_traffic_timestamp() -> None:
    first_connected_at = datetime(2026, 10, 13, tzinfo=timezone.utc)
    payload = SimpleNamespace(
        uuid=None,
        vless_uuid=UUID("9d24b8c4-5d3c-4a1b-8c32-64d4e50b92d1"),
        user_traffic=SimpleNamespace(first_connected_at=first_connected_at),
    )

    user = RemnaUserView(payload)

    assert user.first_connected_at == first_connected_at



class _FakeUsers:
    def __init__(self, users: list[SimpleNamespace]) -> None:
        self.users = users

    async def get_users_stream(self, *, size: int, cursor: int | None):
        return SimpleNamespace(users=self.users, next_cursor=None)


class _FakeSDK:
    def __init__(self, users: list[SimpleNamespace]) -> None:
        self.users = _FakeUsers(users)


@pytest.mark.asyncio
async def test_numeric_panel_id_does_not_require_uuid_lookup() -> None:
    adapter = RemnawaveImpl(_FakeSDK([]))  # type: ignore[arg-type]

    assert await adapter._resolve_user_id(321, username="rs_123") == 321


@pytest.mark.asyncio
async def test_exact_vless_uuid_resolves_from_panel_stream() -> None:
    vless_uuid = uuid4()
    panel_user = SimpleNamespace(id=321, vless_uuid=vless_uuid, username="rs_123")
    adapter = RemnawaveImpl(_FakeSDK([panel_user]))  # type: ignore[arg-type]

    assert await adapter._resolve_user_id(vless_uuid) == 321


@pytest.mark.asyncio
async def test_legacy_panel_uuid_is_never_resolved_by_username() -> None:
    legacy_uuid = uuid4()
    panel_user = SimpleNamespace(id=321, vless_uuid=uuid4(), username="rs_123")
    adapter = RemnawaveImpl(_FakeSDK([panel_user]))  # type: ignore[arg-type]

    with pytest.raises(LookupError, match="numeric remnawave_user_id is required"):
        await adapter._resolve_user_id(legacy_uuid, username="rs_123")
