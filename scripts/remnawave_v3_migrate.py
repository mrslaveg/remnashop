"""Safely backfill Remnawave v3 numeric IDs into Remnashop's local database.

The panel client is guarded at the HTTP layer: any non-GET request is rejected
before it can reach Remnawave. Default mode is dry-run. Applying changes
requires --apply and the exact confirmation string documented in --help.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from httpx import AsyncClient, Request, Timeout
from loguru import logger
from packaging.version import Version
from remnawave import RemnawaveSDK
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import AppConfig
from src.infrastructure.database.models.subscription import Subscription
from src.infrastructure.database.models.user import User

CONFIRMATION = "REMNAWAVE_V3_LOCAL_DB_ONLY"
PAGE_SIZE = 1000
READ_ONLY_ENDPOINTS = {"/api/system/metadata", "/api/users/stream"}


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
    """Allow only the two read endpoints required for the migration."""
    if request.method.upper() != "GET" or request.url.path not in READ_ONLY_ENDPOINTS:
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


async def _fetch_panel_users(sdk: RemnawaveSDK) -> tuple[list[Any], str]:
    metadata = await sdk.system.get_metadata()
    panel_version = Version(str(metadata.version).removeprefix("v"))
    if panel_version < Version("3.0.0"):
        raise RuntimeError(f"Remnawave {panel_version} is not supported; expected v3.0.0+")

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
    return all_users, str(panel_version)


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


def _build_report(
    local_subscriptions: list[LocalSubscription],
    panel_users: list[Any],
) -> list[Match]:
    indexes = _index_panel_users(panel_users)
    matches = [_match_subscription(local, indexes) for local in local_subscriptions]

    reverse: dict[int, list[Match]] = defaultdict(list)
    for match in matches:
        if match.status == "matched" and match.panel_user_id is not None:
            reverse[match.panel_user_id].append(match)

    duplicates = {
        panel_id: items for panel_id, items in reverse.items() if len(items) > 1
    }
    if duplicates:
        rebuilt: list[Match] = []
        for match in matches:
            if match.panel_user_id in duplicates:
                rebuilt.append(
                    Match(
                        match.subscription_id,
                        match.user_id,
                        None,
                        "duplicate_local_mapping",
                        match.evidence,
                        f"Panel user {match.panel_user_id} matched multiple "
                        "local current subscriptions",
                        (match.panel_user_id,) if match.panel_user_id is not None else (),
                    )
                )
            else:
                rebuilt.append(match)
        matches = rebuilt

    by_subscription = {local.subscription_id: local for local in local_subscriptions}
    final: list[Match] = []
    for match in matches:
        local = by_subscription[match.subscription_id]
        if local.existing_panel_id is not None:
            if match.status == "matched" and match.panel_user_id == local.existing_panel_id:
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
                continue
            final.append(
                Match(
                    match.subscription_id,
                    match.user_id,
                    None,
                    "existing_id_conflict",
                    match.evidence,
                    "Local numeric ID is already set and differs from the read-only panel match",
                    match.candidate_panel_ids,
                )
            )
            continue
        final.append(match)
    return final


async def _apply_local_mapping(
    session_factory: async_sessionmaker[AsyncSession],
    original: list[LocalSubscription],
    matches: list[Match],
) -> int:
    if any(match.status not in {"matched", "already_migrated"} for match in matches):
        raise RuntimeError("Refusing to apply: report contains unresolved matches")

    mappings = {
        match.subscription_id: match.panel_user_id
        for match in matches
        if match.status == "matched" and match.panel_user_id is not None
    }
    if not mappings:
        return 0

    expected = {item.subscription_id: item for item in original}
    stmt = (
        select(Subscription, User)
        .join(User, User.current_subscription_id == Subscription.id)
        .where(Subscription.id.in_(list(expected)))
        .with_for_update()
    )
    changed = 0
    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(stmt)
            locked = result.all()
            if len(locked) != len(expected):
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
        panel_users, panel_version = await _fetch_panel_users(sdk)
        local = await _load_local_subscriptions(session_factory)
        matches = _build_report(local, panel_users)
        summary = _summary(matches)

        panel_snapshot_verified: Optional[bool] = None
        if apply:
            second_panel_read, second_panel_version = await _fetch_panel_users(sdk)
            panel_snapshot_verified = (
                panel_version == second_panel_version
                and _panel_snapshot_fingerprint(panel_users)
                == _panel_snapshot_fingerprint(second_panel_read)
            )

        report = {
            "mode": "apply_local_database_only" if apply else "dry_run",
            "panel_version": panel_version,
            "panel_users_read": len(panel_users),
            "panel_snapshot_verified": panel_snapshot_verified,
            "local_current_subscriptions": len(local),
            "summary": summary,
            "matches": [asdict(match) for match in matches],
            "panel_write_requests_allowed": False,
        }
        report_file.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

        unresolved = [
            match for match in matches if match.status not in {"matched", "already_migrated"}
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
            changed = await _apply_local_mapping(session_factory, local, matches)
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
        return asyncio.run(run(args.apply, args.confirm, args.report_file))
    except Exception as error:
        logger.exception("Remnawave v3 ID migration aborted: {}", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
