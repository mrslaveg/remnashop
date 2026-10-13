from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

from src.core.types import RemnaUserView


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
