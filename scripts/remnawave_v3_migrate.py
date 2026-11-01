"""Safely backfill Remnawave v3 numeric IDs into Remnashop's local database.

The panel client is guarded at the HTTP layer: any non-GET request is rejected
before it can reach Remnawave. Default mode is dry-run. Applying changes
requires --apply and the exact confirmation string documented in --help.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import UUID

from httpx import AsyncClient, Request, Timeout
from loguru import logger
from packaging.version import Version
from remnawave import RemnawaveSDK
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import AppConfig
from src.infrastructure.database.models.remnawave_migration import (
    RemnawaveMigrationAudit,
    RemnawaveMigrationDecision,
)
from src.infrastructure.database.models.subscription import Subscription
from src.infrastructure.database.models.user import User

CONFIRMATION = "REMNAWAVE_V3_LOCAL_DB_ONLY"
PAGE_SIZE = 1000
READ_ONLY_ENDPOINTS = {"/api/system/metadata", "/api/users/stream"}
V2_USER_ENDPOINT = re.compile(
    r"^/api/users/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


@dataclass(frozen=True)
class LocalSubscription:
    subscription_id: int
    user_id: int
    telegram_id: Optional[int]
    username: Optional[str]
    legacy_uuid: str
    url: str
    current_subscription_id: Optional[int]
    existing_panel_id: Optional[int]


@dataclass(frozen=True)
class Match:
    subscription_id: int
    user_id: int
    panel_user_id: Optional[int]
    status: str
    evidence: tuple[str, ...]
    details: str = ""
    candidate_panel_ids: tuple[int, ...] = ()


async def _allow_get_only(request: Request) -> None:
    """Allow metadata, v3 user stream, and exact legacy-UUID lookups on v2."""
    path_allowed = (
        request.url.path in READ_ONLY_ENDPOINTS
        or V2_USER_ENDPOINT.fullmatch(request.url.path) is not None
    )
    if request.method.upper() != "GET" or not path_allowed:
        raise PermissionError(
            "Read-only migration blocked Remnawave request: "
            f"{request.method} {request.url.path}"
        )


def _normalize_url(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return value.strip().rstrip("/")


def _short_uuid_from_url(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    parsed = urlsplit(value)
    query = parse_qs(parsed.query)
    for key in ("shortUuid", "short_uuid", "shortuuid", "token"):
        values = query.get(key)
        if values and values[0]:
            return unquote(values[0]).strip()
    parts = [unquote(part).strip() for part in parsed.path.split("/") if part]
    if not parts:
        return None
    candidate = parts[-1]
    if candidate.lower() in {"sub", "subscription", "subscribe", "index", "index.html"}:
        return None
    return candidate


def _as_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _index_panel_users(panel_users: list[Any]) -> dict[str, dict[Any, set[int]]]:
    indexes: dict[str, dict[Any, set[int]]] = {
        name: defaultdict(set)
        for name in ("url", "short_uuid", "telegram_id", "username")
    }
    seen_panel_ids: set[int] = set()
    for panel_user in panel_users:
        panel_id = _as_int(getattr(panel_user, "id", None))
        if panel_id is None or panel_id <= 0:
            raise ValueError("Panel returned a user without a valid positive numeric id")
        if panel_id in seen_panel_ids:
            raise ValueError(f"Panel users stream returned duplicate numeric ID {panel_id}")
        seen_panel_ids.add(panel_id)

        url = _normalize_url(getattr(panel_user, "subscription_url", None))
        short_uuid = getattr(panel_user, "short_uuid", None)
        telegram_id = _as_int(getattr(panel_user, "telegram_id", None))
        username = getattr(panel_user, "username", None)

        if url:
            indexes["url"][url].add(panel_id)
        if short_uuid:
            indexes["short_uuid"][str(short_uuid).strip()].add(panel_id)
        if telegram_id is not None:
            indexes["telegram_id"][telegram_id].add(panel_id)
        if username:
            indexes["username"][str(username).strip().casefold()].add(panel_id)
    return indexes


def _collect_identity_evidence(
    local: LocalSubscription,
    indexes: dict[str, dict[Any, set[int]]],
) -> dict[str, set[int]]:
    evidence: dict[str, set[int]] = {}

    local_url = _normalize_url(local.url)
    if local_url:
        found = indexes["url"].get(local_url, set())
        if found:
            evidence["exact_url"] = set(found)

    short_uuid = _short_uuid_from_url(local.url)
    if short_uuid:
        found = indexes["short_uuid"].get(short_uuid, set())
        if found:
            evidence["short_uuid"] = set(found)

    if local.telegram_id is not None:
        found = indexes["telegram_id"].get(local.telegram_id, set())
        if found:
            evidence["telegram_id"] = set(found)

    expected_username = (
        f"rs_{local.telegram_id}" if local.telegram_id is not None else f"rs_web_{local.user_id}"
    )
    found = indexes["username"].get(expected_username.casefold(), set())
    if found:
        evidence["username"] = set(found)

    return evidence


def _match_from_evidence(
    local: LocalSubscription,
    evidence: dict[str, set[int]],
    candidate: Optional[int],
    status: str,
    details: str,
) -> Match:
    candidate_ids = (
        tuple(sorted(set().union(*evidence.values())))
        if evidence
        else ()
    )
    return Match(
        subscription_id=local.subscription_id,
        user_id=local.user_id,
        panel_user_id=candidate,
        status=status,
        evidence=tuple(sorted(evidence)),
        details=details,
        candidate_panel_ids=candidate_ids,
    )


def _match_strong_identifiers(
    local: LocalSubscription,
    evidence: dict[str, set[int]],
) -> Optional[Match]:
    strong = [evidence[name] for name in ("exact_url", "short_uuid") if name in evidence]
    if not strong:
        return None

    candidates = set.intersection(*strong)
    if len(candidates) != 1:
        status = "ambiguous" if candidates else "conflict"
        return _match_from_evidence(
            local,
            evidence,
            None,
            status,
            "Strong URL identifiers do not resolve to exactly one panel user",
        )

    candidate = next(iter(candidates))
    for name, ids in evidence.items():
        if ids != {candidate}:
            return _match_from_evidence(
                local,
                evidence,
                None,
                "conflict",
                f"URL identifies panel user {candidate}, but {name} is not a unique match",
            )

    return _match_from_evidence(local, evidence, candidate, "matched", "")


def _match_secondary_identifiers(
    local: LocalSubscription,
    evidence: dict[str, set[int]],
) -> Match:
    required = ("telegram_id", "username")
    if any(name not in evidence for name in required):
        return _match_from_evidence(
            local,
            evidence,
            None,
            "unmatched",
            "No unique subscription URL token; Telegram ID and username must both match",
        )

    candidates = set.intersection(*(evidence[name] for name in required))
    if len(candidates) != 1 or any(
        evidence[name] != candidates for name in required
    ):
        status = "ambiguous" if candidates else "conflict"
        return _match_from_evidence(
            local,
            evidence,
            None,
            status,
            "Telegram ID and username do not resolve to exactly one panel user",
        )

    return _match_from_evidence(local, evidence, next(iter(candidates)), "matched", "")


def _match_subscription(
    local: LocalSubscription,
    indexes: dict[str, dict[Any, set[int]]],
) -> Match:
    evidence = _collect_identity_evidence(local, indexes)
    strong_match = _match_strong_identifiers(local, evidence)
    if strong_match is not None:
        return strong_match
    return _match_secondary_identifiers(local, evidence)


def _panel_api_line(panel_version: Version) -> str:
    """Select the user identity contract exposed by a supported panel version."""
    if Version("2.7.0") <= panel_version < Version("2.9.0"):
        return "v2_legacy_uuid_lookup"
    if panel_version >= Version("3.0.0"):
        return "v3_users_stream"
    raise RuntimeError(
        f"Remnawave {panel_version} is not supported; expected 2.7.x/2.8.x or 3.0.0+"
    )


async def _fetch_panel_version(client: AsyncClient) -> Version:
    """Read the version using the shared metadata route available on both API lines."""
    base_url = str(client.base_url).rstrip("/")
    response = await client.get(f"{base_url}/system/metadata")
    response.raise_for_status()
    payload = response.json().get("response")
    if not isinstance(payload, dict) or not payload.get("version"):
        raise RuntimeError("Remnawave metadata response does not contain a version")
    return Version(str(payload["version"]).removeprefix("v"))


async def _fetch_panel_users_v3(sdk: RemnawaveSDK) -> list[Any]:
    """Read the complete v3 user stream. No write-capable SDK method is called."""
    all_users: list[Any] = []
    cursor: Optional[int] = None
    seen_cursors: set[int] = set()
    while True:
        response = await sdk.users.get_users_stream(size=PAGE_SIZE, cursor=cursor)
        batch = list(response.users)
        all_users.extend(batch)
        next_cursor = getattr(response, "next_cursor", None)
        if next_cursor is None or not batch:
            break
        next_cursor = int(next_cursor)
        if next_cursor in seen_cursors or next_cursor == cursor:
            raise RuntimeError(f"Panel pagination returned a repeated cursor: {next_cursor}")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return all_users


async def _fetch_panel_users_v2(
    client: AsyncClient,
    local_subscriptions: list[LocalSubscription],
) -> list[Any]:
    """Resolve each legacy UUID through the v2 GET /users/{uuid} endpoint.

    The response UUID must equal the requested UUID. A Telegram-ID mismatch is
    retained for the report as a conflict; no username fallback is attempted.
    """
    base_url = str(client.base_url).rstrip("/")
    users: list[Any] = []
    seen_uuids: set[str] = set()

    for local in local_subscriptions:
        try:
            requested_uuid = str(UUID(local.legacy_uuid))
        except ValueError as error:
            raise RuntimeError(
                f"Local subscription {local.subscription_id} has an invalid legacy UUID"
            ) from error

        if requested_uuid in seen_uuids:
            continue
        seen_uuids.add(requested_uuid)

        response = await client.get(f"{base_url}/users/{requested_uuid}")
        if response.status_code == 404:
            continue
        response.raise_for_status()

        envelope = response.json()
        payload = envelope.get("response") if isinstance(envelope, dict) else None
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"Panel v2 returned an invalid user payload for legacy UUID {requested_uuid}"
            )

        returned_uuid = payload.get("uuid") or payload.get("vlessUuid") or payload.get("vless_uuid")
        try:
            canonical_returned_uuid = str(UUID(str(returned_uuid)))
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                f"Panel v2 did not return a valid UUID for legacy UUID {requested_uuid}"
            ) from error
        if canonical_returned_uuid != requested_uuid:
            raise RuntimeError(
                f"Panel v2 returned UUID {canonical_returned_uuid} for requested UUID "
                f"{requested_uuid}; refusing to infer an identity"
            )

        panel_id = _as_int(payload.get("id"))
        if panel_id is None or panel_id <= 0:
            raise RuntimeError(
                f"Panel v2 returned an invalid numeric ID for legacy UUID {requested_uuid}"
            )

        telegram_id = _as_int(payload.get("telegramId", payload.get("telegram_id")))
        users.append(
            SimpleNamespace(
                id=panel_id,
                uuid=canonical_returned_uuid,
                short_uuid=payload.get("shortUuid", payload.get("short_uuid")),
                subscription_url=payload.get(
                    "subscriptionUrl", payload.get("subscription_url")
                ),
                telegram_id=telegram_id,
                username=payload.get("username"),
                updated_at=payload.get("updatedAt", payload.get("updated_at")),
            )
        )

    return users


async def _load_local_subscriptions(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[LocalSubscription]:
    stmt = (
        select(Subscription, User)
        .join(User, User.current_subscription_id == Subscription.id)
        .order_by(Subscription.id)
    )
    async with session_factory() as session:
        result = await session.execute(stmt)
        rows = result.all()

    locals_: list[LocalSubscription] = []
    for subscription, user in rows:
        locals_.append(
            LocalSubscription(
                subscription_id=subscription.id,
                user_id=user.id,
                telegram_id=user.telegram_id,
                username=user.username,
                legacy_uuid=str(subscription.user_remna_id),
                url=subscription.url,
                current_subscription_id=user.current_subscription_id,
                existing_panel_id=subscription.remnawave_user_id,
            )
        )
    return locals_


def _source_fingerprint(local: LocalSubscription) -> str:
    """Fingerprint identity-bearing local fields; decisions expire when any change."""
    payload = {
        "subscription_id": local.subscription_id,
        "user_id": local.user_id,
        "legacy_uuid": local.legacy_uuid.lower(),
        "telegram_id": local.telegram_id,
        "username": (local.username or "").casefold(),
        "url": _normalize_url(local.url),
        "current_subscription_id": local.current_subscription_id,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def _load_migration_decisions(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[int, RemnawaveMigrationDecision]:
    async with session_factory() as session:
        result = await session.execute(select(RemnawaveMigrationDecision))
        return {item.subscription_id: item for item in result.scalars().all()}


def _apply_admin_decisions(  # noqa: C901
    local_subscriptions: list[LocalSubscription],
    matches: list[Match],
    decisions: dict[int, RemnawaveMigrationDecision],
) -> list[Match]:
    """Overlay only current, fingerprint-matching administrator decisions."""
    locals_by_id = {item.subscription_id: item for item in local_subscriptions}
    result = {item.subscription_id: item for item in matches}
    for subscription_id, decision in decisions.items():
        local = locals_by_id.get(subscription_id)
        if local is None:
            continue
        match = result[subscription_id]
        if decision.source_fingerprint != _source_fingerprint(local) or (
            decision.user_id != local.user_id
            or decision.legacy_uuid.lower() != local.legacy_uuid.lower()
        ):
            result[subscription_id] = Match(
                subscription_id, local.user_id, None, "decision_stale",
                match.evidence,
                "Administrator decision no longer matches current local identity; review again",
            )
            continue
        if decision.action == "skip":
            result[subscription_id] = Match(
                subscription_id, local.user_id, None, "skipped_by_admin",
                ("admin_decision",), "Explicitly skipped by administrator",
            )
        elif decision.action == "manual_map" and decision.remnawave_user_id:
            result[subscription_id] = Match(
                subscription_id, local.user_id, decision.remnawave_user_id,
                "manually_mapped", ("admin_verified_numeric_id",),
                "Numeric Remnawave ID assigned manually by administrator",
            )

    # A panel ID may belong to only one current local subscription.
    reverse: dict[int, list[Match]] = defaultdict(list)
    for match in result.values():
        if match.status in {"matched", "already_migrated", "manually_mapped"}:
            if match.panel_user_id is not None:
                reverse[match.panel_user_id].append(match)
    duplicates = {panel_id for panel_id, rows in reverse.items() if len(rows) > 1}
    for panel_id in duplicates:
        for match in reverse[panel_id]:
            result[match.subscription_id] = Match(
                match.subscription_id, match.user_id, None, "duplicate_local_mapping",
                match.evidence,
                f"Panel user {panel_id} is assigned to multiple local current subscriptions",
                (panel_id,),
            )
    return [result[item.subscription_id] for item in matches]


def _match_existing_numeric_id(
    local: LocalSubscription,
    panel_user: Optional[Any],
) -> Match:
    """Verify a v2-migrated mapping by its already persisted numeric panel ID."""
    panel_id = local.existing_panel_id
    assert panel_id is not None

    if panel_user is None:
        return Match(
            local.subscription_id,
            local.user_id,
            None,
            "existing_id_conflict",
            ("numeric_panel_id",),
            f"Stored Remnawave user ID {panel_id} is absent from the v3 users stream",
            (panel_id,),
        )

    local_telegram_id = local.telegram_id
    panel_telegram_id = _as_int(getattr(panel_user, "telegram_id", None))
    if (
        local_telegram_id is not None
        and panel_telegram_id is not None
        and local_telegram_id != panel_telegram_id
    ):
        return Match(
            local.subscription_id,
            local.user_id,
            None,
            "existing_id_conflict",
            ("numeric_panel_id", "telegram_id"),
            (
                f"Stored numeric ID {panel_id} exists, but its Telegram ID "
                f"{panel_telegram_id} differs from local Telegram ID {local_telegram_id}"
            ),
            (panel_id,),
        )

    evidence = ["numeric_panel_id"]
    if local_telegram_id is not None and panel_telegram_id == local_telegram_id:
        evidence.append("telegram_id")
    return Match(
        local.subscription_id,
        local.user_id,
        panel_id,
        "already_migrated",
        tuple(evidence),
        "Stored numeric Remnawave user ID exists in the v3 users stream",
        (panel_id,),
    )


def _build_report(
    local_subscriptions: list[LocalSubscription],
    panel_users: list[Any],
) -> list[Match]:
    indexes = _index_panel_users(panel_users)
    users_by_id = {
        _as_int(getattr(panel_user, "id", None)): panel_user
        for panel_user in panel_users
    }
    matches: list[Match] = []
    for local in local_subscriptions:
        if local.existing_panel_id is not None:
            matches.append(
                _match_existing_numeric_id(
                    local,
                    users_by_id.get(local.existing_panel_id),
                )
            )
        else:
            matches.append(_match_subscription(local, indexes))

    locals_by_subscription = {
        item.subscription_id: item for item in local_subscriptions
    }
    reverse: dict[int, list[Match]] = defaultdict(list)
    for match in matches:
        local = locals_by_subscription[match.subscription_id]
        assigned_id = local.existing_panel_id
        if assigned_id is None and match.status == "matched":
            assigned_id = match.panel_user_id
        if assigned_id is not None:
            reverse[assigned_id].append(match)

    duplicates = {
        panel_id: items for panel_id, items in reverse.items() if len(items) > 1
    }
    if duplicates:
        rebuilt: list[Match] = []
        for match in matches:
            local = locals_by_subscription[match.subscription_id]
            assigned_id = (
                local.existing_panel_id
                if local.existing_panel_id is not None
                else match.panel_user_id
            )
            if assigned_id in duplicates:
                rebuilt.append(
                    Match(
                        match.subscription_id,
                        match.user_id,
                        None,
                        "duplicate_local_mapping",
                        match.evidence,
                        (
                            f"Panel user {assigned_id} is mapped to multiple "
                            "local current subscriptions"
                        ),
                        (assigned_id,),
                    )
                )
            else:
                rebuilt.append(match)
        matches = rebuilt

    return matches


def _match_v2_subscription(
    local: LocalSubscription,
    panel_user: Optional[Any],
) -> Match:
    if panel_user is None:
        return Match(
            local.subscription_id,
            local.user_id,
            None,
            "unmatched",
            (),
            "Remnawave v2 did not return a user for the exact stored legacy UUID",
        )

    panel_id = _as_int(getattr(panel_user, "id", None))
    remote_telegram_id = _as_int(getattr(panel_user, "telegram_id", None))
    if panel_id is None or panel_id <= 0:
        raise ValueError("Panel v2 returned a user without a valid positive numeric id")

    if (
        local.telegram_id is not None
        and remote_telegram_id is not None
        and remote_telegram_id != local.telegram_id
    ):
        return Match(
            local.subscription_id,
            local.user_id,
            None,
            "conflict",
            ("legacy_uuid", "telegram_id"),
            "The exact legacy UUID resolved, but the panel Telegram ID differs "
            "from the local account",
            (panel_id,),
        )

    evidence = ("legacy_uuid",)
    if local.telegram_id is not None and remote_telegram_id == local.telegram_id:
        evidence = ("legacy_uuid", "telegram_id")
    return Match(
        local.subscription_id,
        local.user_id,
        panel_id,
        "matched",
        evidence,
    )


def _mark_v2_duplicates_and_existing_ids(
    local_subscriptions: list[LocalSubscription],
    matches: list[Match],
) -> list[Match]:
    reverse: dict[int, list[Match]] = defaultdict(list)
    for match in matches:
        if match.status in {"matched", "manually_mapped"} and match.panel_user_id is not None:
            reverse[match.panel_user_id].append(match)
    duplicates = {
        panel_id for panel_id, items in reverse.items() if len(items) > 1
    }

    by_subscription = {local.subscription_id: local for local in local_subscriptions}
    final: list[Match] = []
    for match in matches:
        local = by_subscription[match.subscription_id]
        if match.panel_user_id in duplicates:
            final.append(
                Match(
                    match.subscription_id,
                    match.user_id,
                    None,
                    "duplicate_local_mapping",
                    match.evidence,
                    (
                        f"Panel user {match.panel_user_id} matched multiple "
                        "local current subscriptions"
                    ),
                    (match.panel_user_id,) if match.panel_user_id is not None else (),
                )
            )
        elif local.existing_panel_id is not None:
            if (
                match.status == "matched"
                and match.panel_user_id == local.existing_panel_id
            ):
                final.append(
                    Match(
                        match.subscription_id,
                        match.user_id,
                        match.panel_user_id,
                        "already_migrated",
                        match.evidence,
                        candidate_panel_ids=(match.panel_user_id,),
                    )
                )
            else:
                final.append(
                    Match(
                        match.subscription_id,
                        match.user_id,
                        None,
                        "existing_id_conflict",
                        match.evidence,
                        "Local numeric ID differs from the exact v2 UUID lookup",
                        match.candidate_panel_ids,
                    )
                )
        else:
            final.append(match)

    return final


def _build_v2_report(
    local_subscriptions: list[LocalSubscription],
    panel_users: list[Any],
) -> list[Match]:
    """Match v2 users only by the exact legacy UUID requested from the panel."""
    by_uuid = {str(getattr(user, "uuid", "")).lower(): user for user in panel_users}
    matches = [
        _match_v2_subscription(local, by_uuid.get(local.legacy_uuid.lower()))
        for local in local_subscriptions
    ]
    return _mark_v2_duplicates_and_existing_ids(local_subscriptions, matches)


async def _apply_local_mapping(  # noqa: C901
    session_factory: async_sessionmaker[AsyncSession],
    original: list[LocalSubscription],
    matches: list[Match],
    expected_decisions: dict[int, RemnawaveMigrationDecision],
) -> int:
    resolved_statuses = {"matched", "already_migrated", "manually_mapped", "skipped_by_admin"}
    if any(match.status not in resolved_statuses for match in matches):
        raise RuntimeError("Refusing to apply: report contains unresolved matches")

    mappings = {
        match.subscription_id: match.panel_user_id
        for match in matches
        if match.status in {"matched", "manually_mapped"} and match.panel_user_id is not None
    }
    expected = {item.subscription_id: item for item in original}
    stmt = (
        select(Subscription, User)
        .join(User, User.current_subscription_id == Subscription.id)
        .order_by(Subscription.id)
        .with_for_update()
    )
    changed = 0
    async with session_factory() as session:
        async with session.begin():
            # Prevent concurrent subscription creation/current-subscription switches
            # while the local ID mapping transaction is being committed.
            await session.execute(text("LOCK TABLE subscriptions IN SHARE ROW EXCLUSIVE MODE"))
            await session.execute(text("LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE"))
            await session.execute(
                text("LOCK TABLE remnawave_migration_decisions IN SHARE ROW EXCLUSIVE MODE")
            )
            current_decisions_result = await session.execute(select(RemnawaveMigrationDecision))
            current_decisions = {
                item.subscription_id: item
                for item in current_decisions_result.scalars().all()
            }
            if set(current_decisions) != set(expected_decisions):
                raise RuntimeError(
                    "Administrator decisions changed during migration; retry dry-run"
                )
            for subscription_id, expected_decision in expected_decisions.items():
                current_decision = current_decisions[subscription_id]
                if (
                    current_decision.source_fingerprint != expected_decision.source_fingerprint
                    or current_decision.action != expected_decision.action
                    or current_decision.remnawave_user_id != expected_decision.remnawave_user_id
                    or current_decision.admin_telegram_id != expected_decision.admin_telegram_id
                ):
                    raise RuntimeError(
                        "Administrator decisions changed during migration; retry dry-run"
                    )
            result = await session.execute(stmt)
            locked = result.all()
            if len(locked) != len(expected) or {
                subscription.id for subscription, _ in locked
            } != set(expected):
                raise RuntimeError(
                    "Local current subscriptions changed during migration; retry dry-run"
                )

            for subscription, user in locked:
                snapshot = expected[subscription.id]
                if (
                    subscription.user_id != snapshot.user_id
                    or str(subscription.user_remna_id) != snapshot.legacy_uuid
                    or subscription.url != snapshot.url
                    or user.current_subscription_id != snapshot.current_subscription_id
                    or user.telegram_id != snapshot.telegram_id
                    or user.username != snapshot.username
                    or subscription.remnawave_user_id != snapshot.existing_panel_id
                ):
                    raise RuntimeError(
                        f"Local subscription {subscription.id} changed during migration; "
                        "no changes committed"
                    )

                target_id = mappings.get(subscription.id)
                if target_id is None:
                    continue
                if subscription.remnawave_user_id not in (None, target_id):
                    raise RuntimeError(
                        f"Subscription {subscription.id} already has a different numeric panel ID"
                    )
                if subscription.remnawave_user_id is None:
                    result = await session.execute(
                        update(Subscription)
                        .where(
                            Subscription.id == subscription.id,
                            Subscription.remnawave_user_id.is_(None),
                        )
                        .values(remnawave_user_id=target_id)
                    )
                    if result.rowcount != 1:
                        raise RuntimeError(
                            f"Concurrent update detected for local subscription {subscription.id}"
                        )
                    changed += 1
    return changed


def _panel_snapshot_fingerprint(panel_users: list[Any]) -> tuple[tuple[Any, ...], ...]:
    records = []
    for panel_user in panel_users:
        records.append(
            (
                _as_int(getattr(panel_user, "id", None)),
                str(getattr(panel_user, "uuid", "") or "").lower(),
                _normalize_url(getattr(panel_user, "subscription_url", None)),
                str(getattr(panel_user, "short_uuid", "") or ""),
                _as_int(getattr(panel_user, "telegram_id", None)),
                str(getattr(panel_user, "username", "") or "").casefold(),
                str(getattr(panel_user, "updated_at", "") or ""),
            )
        )
    return tuple(sorted(records, key=lambda item: item[0] or -1))


def _summary(matches: list[Match]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for match in matches:
        counts[match.status] += 1
    return dict(sorted(counts.items()))


async def record_admin_decision(
    subscription_id: int,
    action: str,
    admin_telegram_id: int,
    panel_user_id: Optional[int] = None,
) -> None:
    """Persist an explicitly reviewed mapping/skip and append an audit event."""
    if action not in {"manual_map", "skip"}:
        raise ValueError("Decision action must be manual_map or skip")
    if action == "manual_map" and (panel_user_id is None or panel_user_id <= 0):
        raise ValueError("Manual mapping requires a positive numeric Remnawave ID")
    if action == "skip" and panel_user_id is not None:
        raise ValueError("Skip decision must not contain a panel ID")

    config = AppConfig.get()
    engine = create_async_engine(config.database.dsn, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            async with session.begin():
                stmt = (
                    select(Subscription, User)
                    .join(User, User.current_subscription_id == Subscription.id)
                    .where(Subscription.id == subscription_id)
                    .with_for_update()
                )
                row = (await session.execute(stmt)).first()
                if row is None:
                    raise ValueError("Subscription is not a current local subscription")
                subscription, user = row
                local = LocalSubscription(
                    subscription_id=subscription.id,
                    user_id=user.id,
                    telegram_id=user.telegram_id,
                    username=user.username,
                    legacy_uuid=str(subscription.user_remna_id),
                    url=subscription.url,
                    current_subscription_id=user.current_subscription_id,
                    existing_panel_id=subscription.remnawave_user_id,
                )
                fingerprint = _source_fingerprint(local)

                if action == "manual_map":
                    existing_subscription = await session.scalar(
                        select(Subscription.id).where(
                            Subscription.remnawave_user_id == panel_user_id,
                            Subscription.id != subscription_id,
                        ).limit(1)
                    )
                    existing_decision = await session.scalar(
                        select(RemnawaveMigrationDecision.subscription_id).where(
                            RemnawaveMigrationDecision.remnawave_user_id == panel_user_id,
                            RemnawaveMigrationDecision.subscription_id != subscription_id,
                        ).limit(1)
                    )
                    if existing_subscription is not None or existing_decision is not None:
                        raise ValueError(
                            f"Remnawave ID {panel_user_id} is already assigned "
                            "to another local user"
                        )

                decision = await session.scalar(
                    select(RemnawaveMigrationDecision)
                    .where(RemnawaveMigrationDecision.subscription_id == subscription_id)
                    .with_for_update()
                )
                if decision is None:
                    decision = RemnawaveMigrationDecision(
                        subscription_id=subscription.id,
                        user_id=user.id,
                        legacy_uuid=local.legacy_uuid,
                        source_fingerprint=fingerprint,
                        action=action,
                        remnawave_user_id=panel_user_id,
                        admin_telegram_id=admin_telegram_id,
                    )
                    session.add(decision)
                else:
                    decision.user_id = user.id
                    decision.legacy_uuid = local.legacy_uuid
                    decision.source_fingerprint = fingerprint
                    decision.action = action
                    decision.remnawave_user_id = panel_user_id
                    decision.admin_telegram_id = admin_telegram_id
                    decision.note = None

                session.add(
                    RemnawaveMigrationAudit(
                        subscription_id=subscription.id,
                        user_id=user.id,
                        legacy_uuid=local.legacy_uuid,
                        source_fingerprint=fingerprint,
                        action=action,
                        remnawave_user_id=panel_user_id,
                        admin_telegram_id=admin_telegram_id,
                        details={"source": "admin_dialog"},
                    )
                )
    finally:
        await engine.dispose()


async def run(apply: bool, confirmation: Optional[str], report_file: Path) -> int:
    if apply and confirmation != CONFIRMATION:
        raise RuntimeError(
            f"Applying requires --confirm {CONFIRMATION}. Dry-run has no write access to the panel."
        )

    config = AppConfig.get()
    engine = create_async_engine(config.database.dsn, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    headers = {
        "Authorization": f"Bearer {config.remnawave.token.get_secret_value()}",
        "X-Api-Key": config.remnawave.caddy_token.get_secret_value(),
        "CF-Access-Client-Id": config.remnawave.cf_client_id.get_secret_value(),
        "CF-Access-Client-Secret": config.remnawave.cf_client_secret.get_secret_value(),
    }
    if not config.remnawave.is_external:
        headers["x-forwarded-proto"] = "https"
        headers["x-forwarded-for"] = "127.0.0.1"

    client = AsyncClient(
        base_url=f"{config.remnawave.url.get_secret_value()}/api",
        headers=headers,
        cookies=config.remnawave.cookies,
        verify=True,
        timeout=Timeout(connect=15.0, read=60.0, write=10.0, pool=5.0),
        event_hooks={"request": [_allow_get_only]},
    )
    try:
        sdk = RemnawaveSDK(client)
        local = await _load_local_subscriptions(session_factory)
        panel_version_value = await _fetch_panel_version(client)

        panel_api_line = _panel_api_line(panel_version_value)
        if panel_api_line == "v2_legacy_uuid_lookup":
            panel_users = await _fetch_panel_users_v2(client, local)
            matches = _build_v2_report(local, panel_users)
        else:
            panel_users = await _fetch_panel_users_v3(sdk)
            matches = _build_report(local, panel_users)

        decisions = await _load_migration_decisions(session_factory)
        matches = _apply_admin_decisions(local, matches, decisions)
        panel_version = str(panel_version_value)
        summary = _summary(matches)

        panel_snapshot_verified: Optional[bool] = None
        if apply:
            second_panel_version = await _fetch_panel_version(client)
            if panel_api_line == "v2_legacy_uuid_lookup":
                second_panel_read = await _fetch_panel_users_v2(client, local)
            else:
                second_panel_read = await _fetch_panel_users_v3(sdk)
            panel_snapshot_verified = (
                panel_version_value == second_panel_version
                and _panel_snapshot_fingerprint(panel_users)
                == _panel_snapshot_fingerprint(second_panel_read)
            )

        report = {
            "mode": "apply_local_database_only" if apply else "dry_run",
            "panel_version": panel_version,
            "panel_api_line": panel_api_line,
            "panel_users_read": len(panel_users),
            "panel_snapshot_verified": panel_snapshot_verified,
            "local_current_subscriptions": len(local),
            "summary": summary,
            "matches": [asdict(match) for match in matches],
            "local_identities": {
                str(item.subscription_id): {
                    "telegram_id": item.telegram_id,
                    "username": item.username,
                    "user_id": item.user_id,
                    "legacy_uuid": item.legacy_uuid,
                }
                for item in local
            },
            "panel_write_requests_allowed": False,
            "admin_decisions_loaded": len(decisions),
        }
        report_file.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

        unresolved = [
            match for match in matches
            if match.status not in {
                "matched", "already_migrated", "manually_mapped", "skipped_by_admin"
            }
        ]
        if unresolved:
            logger.error(
                "Migration blocked: {} unresolved local subscription(s). "
                "No local DB data was changed.",
                len(unresolved),
            )
            return 2

        if apply and not panel_snapshot_verified:
            logger.error(
                "Panel snapshot changed between read-only scans; refusing local DB writes."
            )
            return 3

        if apply:
            changed = await _apply_local_mapping(session_factory, local, matches, decisions)
            logger.success(
                "Local migration committed: {} subscription(s) updated. Remnawave was read-only.",
                changed,
            )
        else:
            logger.info(
                "Dry-run successful. No data was changed. Re-run with --apply "
                "and confirmation to write local DB IDs."
            )
        return 0
    finally:
        await client.aclose()
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill Remnawave v3 numeric IDs into Remnashop's DB. "
            "The panel is strictly read-only; default operation is dry-run."
        )
    )
    parser.add_argument(
        "--decision-subscription",
        type=int,
        help="persist an administrator decision for a current subscription",
    )
    parser.add_argument("--decision-action", choices=("manual_map", "skip"))
    parser.add_argument("--decision-panel-id", type=int)
    parser.add_argument("--admin-telegram-id", type=int)
    parser.add_argument("--apply", action="store_true", help="write mappings to Remnashop DB only")
    parser.add_argument("--confirm", help="required literal confirmation when --apply is used")
    parser.add_argument(
        "--report-file",
        type=Path,
        default=Path("remnawave-v3-migration-report.json"),
        help="JSON report path (default: %(default)s)",
    )
    args = parser.parse_args()
    try:
        if args.decision_subscription is not None:
            if args.decision_action is None or args.admin_telegram_id is None:
                parser.error(
                    "--decision-subscription requires --decision-action "
                    "and --admin-telegram-id"
                )
            asyncio.run(record_admin_decision(
                args.decision_subscription,
                args.decision_action,
                args.admin_telegram_id,
                args.decision_panel_id,
            ))
            return 0
        return asyncio.run(run(args.apply, args.confirm, args.report_file))
    except Exception as error:
        logger.exception("Remnawave v3 ID migration aborted: {}", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
