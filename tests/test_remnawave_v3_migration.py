from types import SimpleNamespace

import pytest
from httpx import Request
from uuid import uuid4

from scripts.remnawave_v3_migrate import (
    LocalSubscription,
    _allow_get_only,
    _build_report,
    _index_panel_users,
    _match_subscription,
    _short_uuid_from_url,
)


def local(url: str, telegram_id: int | None = 123, legacy_uuid: str | None = None):
    return LocalSubscription(
        subscription_id=10,
        user_id=2,
        telegram_id=telegram_id,
        username="alice",
        legacy_uuid=legacy_uuid or str(uuid4()),
        url=url,
        current_subscription_id=10,
        existing_panel_id=None,
    )


def panel(
    panel_id: int,
    *,
    url: str | None = None,
    short_uuid: str | None = None,
    telegram_id: int | None = None,
    username: str | None = None,
    vless_uuid: str | None = None,
):
    return SimpleNamespace(
        id=panel_id,
        subscription_url=url,
        short_uuid=short_uuid,
        telegram_id=telegram_id,
        username=username,
        vless_uuid=vless_uuid,
    )


def test_extracts_short_uuid_from_subscription_path():
    assert _short_uuid_from_url("https://sub.example/api/sub/abc123") == "abc123"


def test_strong_subscription_token_resolves_user_even_without_telegram_id():
    source = local("https://sub.example/api/sub/token-1", telegram_id=None)
    match = _match_subscription(
        source,
        _index_panel_users([panel(321, short_uuid="token-1")]),
    )
    assert match.status == "matched"
    assert match.panel_user_id == 321


def test_telegram_and_username_must_agree_without_subscription_token():
    source = local("https://sub.example/", telegram_id=123)
    match = _match_subscription(
        source,
        _index_panel_users(
            [panel(321, telegram_id=123, username="rs_123")]
        ),
    )
    assert match.status == "matched"
    assert match.panel_user_id == 321


def test_legacy_vless_uuid_alone_is_never_enough():
    source = local("https://sub.example/", telegram_id=None)
    match = _match_subscription(
        source,
        _index_panel_users([panel(321, vless_uuid=source.legacy_uuid)]),
    )
    assert match.status == "unmatched"


def test_conflicting_telegram_and_username_are_rejected():
    source = local("https://sub.example/", telegram_id=123)
    match = _match_subscription(
        source,
        _index_panel_users(
            [
                panel(321, telegram_id=123),
                panel(654, username="rs_123"),
            ]
        ),
    )
    assert match.status == "conflict"


def test_one_panel_user_cannot_be_assigned_to_two_local_subscriptions():
    first = local("https://sub.example/api/sub/same-token", telegram_id=None)
    second = LocalSubscription(
        subscription_id=11,
        user_id=3,
        telegram_id=None,
        username="bob",
        legacy_uuid=str(uuid4()),
        url="https://sub.example/api/sub/same-token",
        current_subscription_id=11,
        existing_panel_id=None,
    )
    report = _build_report(
        [first, second],
        [panel(321, short_uuid="same-token")],
    )
    assert {item.status for item in report} == {"duplicate_local_mapping"}



@pytest.mark.asyncio
async def test_panel_http_guard_allows_get_and_rejects_mutations():
    await _allow_get_only(Request("GET", "https://panel.example/api/users"))

    with pytest.raises(PermissionError, match="blocked non-GET"):
        await _allow_get_only(Request("POST", "https://panel.example/api/users"))
