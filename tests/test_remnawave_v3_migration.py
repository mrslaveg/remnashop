from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import AsyncClient, MockTransport, Request, Response

from scripts.remnawave_v3_migrate import (
    LocalSubscription,
    _allow_get_only,
    _build_report,
    _build_v2_report,
    _fetch_panel_users_v2,
    _fetch_panel_version,
     _index_panel_users,
    _match_subscription,
    _panel_api_line,
    _panel_snapshot_fingerprint,
    _short_uuid_from_url,
)


def local(
    url: str,
    telegram_id: int | None = 123,
    legacy_uuid: str | None = None,
    existing_panel_id: int | None = None,
):
    return LocalSubscription(
        subscription_id=10,
        user_id=2,
        telegram_id=telegram_id,
        username="alice",
        legacy_uuid=legacy_uuid or str(uuid4()),
        url=url,
        current_subscription_id=10,
        existing_panel_id=existing_panel_id,
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


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("2.7.4", "v2_legacy_uuid_lookup"),
        ("2.8.0", "v2_legacy_uuid_lookup"),
        ("2.8.1", "v2_legacy_uuid_lookup"),
        ("3.0.0", "v3_users_stream"),
    ],
)
def test_panel_version_selects_correct_user_identity_contract(version, expected):
    from packaging.version import Version

    assert _panel_api_line(Version(version)) == expected


def test_panel_version_between_api_generations_is_rejected():
    from packaging.version import Version

    with pytest.raises(RuntimeError, match="not supported"):
        _panel_api_line(Version("2.9.0"))


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


def test_v3_verifies_previously_migrated_mapping_by_numeric_id():
    source = local("https://sub.example/", existing_panel_id=321)
    remote = panel(321, telegram_id=123)

    match = _build_report([source], [remote])[0]

    assert match.status == "already_migrated"
    assert match.panel_user_id == 321
    assert "numeric_panel_id" in match.evidence
    assert "telegram_id" in match.evidence


def test_v3_rejects_migrated_id_with_conflicting_telegram_identity():
    source = local("https://sub.example/", existing_panel_id=321)
    remote = panel(321, telegram_id=999)

    match = _build_report([source], [remote])[0]

    assert match.status == "existing_id_conflict"
    assert match.panel_user_id is None
    assert match.candidate_panel_ids == (321,)


def test_v3_rejects_migrated_id_missing_from_panel_stream():
    source = local("https://sub.example/", existing_panel_id=321)

    match = _build_report([source], [panel(654, telegram_id=123)])[0]

    assert match.status == "existing_id_conflict"
    assert match.panel_user_id is None
    assert match.candidate_panel_ids == (321,)


def test_v3_duplicate_existing_numeric_mapping_is_blocked():
    first = local("https://sub.example/a", existing_panel_id=321)
    second = LocalSubscription(
        subscription_id=11,
        user_id=3,
        telegram_id=456,
        username="bob",
        legacy_uuid=str(uuid4()),
        url="https://sub.example/b",
        current_subscription_id=11,
        existing_panel_id=321,
    )
    remote = panel(321, telegram_id=123)

    matches = _build_report([first, second], [remote])

    assert {item.status for item in matches} == {"duplicate_local_mapping"}


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


def test_strong_subscription_token_with_conflicting_telegram_identity_is_rejected():
    source = local("https://sub.example/api/sub/token-1", telegram_id=123)
    match = _match_subscription(
        source,
        _index_panel_users(
            [
                panel(321, short_uuid="token-1", telegram_id=123, username="rs_123"),
                panel(654, telegram_id=123, username="rs_other"),
            ]
        ),
    )
    assert match.status == "conflict"
    assert match.candidate_panel_ids == (321, 654)


def test_duplicate_numeric_panel_ids_abort_indexing():
    with pytest.raises(ValueError, match="duplicate numeric ID"):
        _index_panel_users([panel(321), panel(321)])


def test_panel_snapshot_fingerprint_detects_identity_changes():
    first = [panel(321, short_uuid="token-1", telegram_id=123, username="rs_123")]
    changed = [panel(321, short_uuid="token-2", telegram_id=123, username="rs_123")]
    assert _panel_snapshot_fingerprint(first) != _panel_snapshot_fingerprint(changed)


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



def test_v2_exact_legacy_uuid_maps_numeric_id():
    legacy_uuid = str(uuid4())
    source = local("https://sub.example/sub/token-1", legacy_uuid=legacy_uuid)
    remote = SimpleNamespace(uuid=legacy_uuid, id=987, telegram_id=123)
    match = _build_v2_report([source], [remote])[0]

    assert match.status == "matched"
    assert match.panel_user_id == 987
    assert set(match.evidence) == {"legacy_uuid", "telegram_id"}


def test_v2_legacy_uuid_telegram_conflict_is_rejected():
    legacy_uuid = str(uuid4())
    source = local("https://sub.example/sub/token-1", legacy_uuid=legacy_uuid)
    remote = SimpleNamespace(uuid=legacy_uuid, id=987, telegram_id=999)
    match = _build_v2_report([source], [remote])[0]

    assert match.status == "conflict"
    assert match.panel_user_id is None
    assert match.candidate_panel_ids == (987,)


def test_v2_does_not_fallback_to_username_or_subscription_url():
    source = local("https://sub.example/sub/token-1", legacy_uuid=str(uuid4()))
    remote = SimpleNamespace(
        uuid=str(uuid4()),
        id=987,
        telegram_id=123,
        username="rs_123",
        subscription_url=source.url,
        short_uuid="token-1",
    )
    match = _build_v2_report([source], [remote])[0]

    assert match.status == "unmatched"


def test_v2_duplicate_legacy_uuid_mapping_is_blocked():
    legacy_uuid = str(uuid4())
    first = local("https://sub.example/sub/a", legacy_uuid=legacy_uuid)
    second = LocalSubscription(
        subscription_id=11,
        user_id=3,
        telegram_id=123,
        username="bob",
        legacy_uuid=legacy_uuid,
        url="https://sub.example/sub/b",
        current_subscription_id=11,
        existing_panel_id=None,
    )
    remote = SimpleNamespace(uuid=legacy_uuid, id=987, telegram_id=123)
    matches = _build_v2_report([first, second], [remote])

    assert {match.status for match in matches} == {"duplicate_local_mapping"}




@pytest.mark.asyncio
async def test_remnawave_2_8_0_metadata_is_read_through_v2_compatible_route():
    requests: list[Request] = []

    async def handler(request: Request) -> Response:
        requests.append(request)
        return Response(
            200,
            json={"response": {"version": "2.8.0"}},
            request=request,
        )

    async with AsyncClient(
        base_url="https://panel.example/api",
        transport=MockTransport(handler),
        event_hooks={"request": [_allow_get_only]},
    ) as client:
        version = await _fetch_panel_version(client)

    assert str(version) == "2.8.0"
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/api/system/metadata"


@pytest.mark.asyncio
async def test_v2_fetch_queries_only_the_exact_stored_uuid():
    legacy_uuid = str(uuid4())
    source = local("https://sub.example/sub/token-1", legacy_uuid=legacy_uuid)
    requests: list[Request] = []

    async def handler(request: Request) -> Response:
        requests.append(request)
        return Response(
            200,
            json={
                "response": {
                    "uuid": legacy_uuid,
                    "id": 987,
                    "telegramId": 123,
                    "shortUuid": "token-1",
                    "subscriptionUrl": source.url,
                    "username": "rs_123",
                }
            },
            request=request,
        )

    async with AsyncClient(
        base_url="https://panel.example/api",
        transport=MockTransport(handler),
        event_hooks={"request": [_allow_get_only]},
    ) as client:
        users = await _fetch_panel_users_v2(client, [source])

    assert len(users) == 1
    assert users[0].id == 987
    assert users[0].uuid == legacy_uuid
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == f"/api/users/{legacy_uuid}"


@pytest.mark.asyncio
async def test_panel_http_guard_allows_only_metadata_stream_and_uuid_lookup():
    await _allow_get_only(Request("GET", "https://panel.example/api/system/metadata"))
    await _allow_get_only(Request("GET", "https://panel.example/api/users/stream"))
    await _allow_get_only(
        Request("GET", f"https://panel.example/api/users/{uuid4()}")
    )

    with pytest.raises(PermissionError, match="blocked"):
        await _allow_get_only(Request("GET", "https://panel.example/api/users"))

    with pytest.raises(PermissionError, match="blocked"):
        await _allow_get_only(
            Request("GET", "https://panel.example/api/users/not-a-uuid")
        )

    with pytest.raises(PermissionError, match="blocked"):
        await _allow_get_only(
            Request("POST", f"https://panel.example/api/users/{uuid4()}")
        )

    with pytest.raises(PermissionError, match="blocked"):
        await _allow_get_only(Request("POST", "https://panel.example/api/users/stream"))
