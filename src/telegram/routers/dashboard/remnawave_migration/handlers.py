import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiogram.types import CallbackQuery
from aiogram_dialog import DialogManager
from aiogram_dialog.widgets.kbd import Button
from dishka import FromDishka
from dishka.integrations.aiogram_dialog import inject
from loguru import logger
from redis.asyncio import Redis

from src.application.common.policy import Permission, PermissionPolicy
from src.application.dto import TelegramUserDto
from src.core.constants import USER_KEY

CONFIRMATION = "REMNAWAVE_V3_LOCAL_DB_ONLY"
RUNNING_KEY = "remnashop:remnawave-v3-migration:running"
AUDIT_DIR = Path("logs/remnawave-v3-migration")


async def _run_migration(apply: bool) -> tuple[int, dict[str, Any], str]:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    run_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid4().hex[:8]
    )
    report_path = AUDIT_DIR / f"{run_id}.json"
    log_path = AUDIT_DIR / f"{run_id}.log"
    command = [
        sys.executable,
        "scripts/remnawave_v3_migrate.py",
        "--report-file",
        str(report_path),
    ]
    if apply:
        command.extend(["--apply", "--confirm", CONFIRMATION])

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=900)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("Миграция превысила лимит времени (15 минут).")

    log = (stderr or b"").decode("utf-8", errors="replace")
    log_path.write_text(log, encoding="utf-8")
    if not report_path.exists():
        raise RuntimeError(
            "Мигратор не создал JSON-отчёт. Последние сообщения:\n" + log[-1200:]
        )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    log_tail = log[-2500:]
    log_tail += f"\nАудит: {report_path}\nЛог: {log_path}"
    return int(process.returncode or 0), report, log_tail


def _format_issues(report: dict[str, Any]) -> list[str]:
    unresolved = [
        match for match in report.get("matches", [])
        if match.get("status") not in {"matched", "already_migrated", "manually_mapped", "skipped_by_admin"}
    ]
    formatted = []
    for match in unresolved[:5]:
        details = (match.get("details") or "Требуется ручная проверка.")[:180]
        candidates = match.get("candidate_panel_ids") or []
        candidate_text = f"; кандидаты: {candidates[:10]}" if candidates else ""
        formatted.append(
            f"Подписка #{match.get('subscription_id')}: "
            f"{match.get('status')} — {details}{candidate_text}"
        )
    return formatted


def _is_clean(report: dict[str, Any]) -> bool:
    summary = report.get("summary", {})
    unresolved = sum(
        count for status, count in summary.items()
        if status not in {"matched", "already_migrated", "skipped_by_admin"}
    )
    return (
        report.get("panel_api_line") in {"v2_legacy_uuid_lookup", "v3_users_stream"}
        and report.get("local_current_subscriptions", 0) > 0
        and unresolved == 0
        and report.get("panel_write_requests_allowed") is False
    )


def _is_authorized(user: TelegramUserDto) -> bool:
    return PermissionPolicy.has_permission(user, Permission.VIEW_IMPORTER)


@inject
async def on_migration_check(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    redis: FromDishka[Redis],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    if not _is_authorized(user):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    if not await redis.set(RUNNING_KEY, str(user.telegram_id), nx=True, ex=1800):
        await callback.answer("Миграция уже выполняется.", show_alert=True)
        return

    await callback.answer("Запускаю read-only проверку…")
    dialog_manager.dialog_data["migration_can_apply"] = False
    dialog_manager.dialog_data["migration_result"] = "Выполняется read-only проверка…"
    try:
        code, report, log = await _run_migration(apply=False)
        dialog_manager.dialog_data["migration_report"] = {
            key: value for key, value in report.items() if key != "matches"
        }
        dialog_manager.dialog_data["migration_log"] = log
        dialog_manager.dialog_data["migration_issues"] = _format_issues(report)
        dialog_manager.dialog_data["migration_full_matches"] = report.get("matches", [])
        dialog_manager.dialog_data["migration_issues_total"] = sum(
            count for status, count in report.get("summary", {}).items()
            if status not in {"matched", "already_migrated", "skipped_by_admin"}
        )
        clean = code == 0 and _is_clean(report)
        dialog_manager.dialog_data["migration_can_apply"] = clean
        dialog_manager.dialog_data["migration_result"] = (
            "Проверка пройдена. Можно перейти к подтверждению применения."
            if clean
            else f"Применение заблокировано. Код завершения: {code}. Разберите все конфликты."
        )
        logger.info(
            "{} Remnawave migration dry-run: panel={}, local={}, summary={}",
            user.log,
            report.get("panel_version"),
            report.get("local_current_subscriptions"),
            report.get("summary"),
        )
    except Exception as error:
        logger.exception("Remnawave v3 migration dry-run failed")
        dialog_manager.dialog_data["migration_result"] = f"Ошибка проверки: {error}"
        dialog_manager.dialog_data["migration_log"] = str(error)[-2500:]
    finally:
        await redis.delete(RUNNING_KEY)


@inject
async def on_migration_apply(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    redis: FromDishka[Redis],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    if not _is_authorized(user):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    if not dialog_manager.dialog_data.get("migration_can_apply"):
        await callback.answer("Сначала выполните успешную проверку.", show_alert=True)
        return

    if not await redis.set(RUNNING_KEY, str(user.telegram_id), nx=True, ex=1800):
        await callback.answer("Миграция уже выполняется.", show_alert=True)
        return

    await callback.answer("Запускаю применение только в локальной БД…")
    dialog_manager.dialog_data["migration_can_apply"] = False
    dialog_manager.dialog_data["migration_result"] = "Применение локальных ID…"
    try:
        code, report, log = await _run_migration(apply=True)
        dialog_manager.dialog_data["migration_report"] = {
            key: value for key, value in report.items() if key != "matches"
        }
        dialog_manager.dialog_data["migration_log"] = log
        dialog_manager.dialog_data["migration_issues"] = _format_issues(report)
        dialog_manager.dialog_data["migration_issues_total"] = sum(
            count for status, count in report.get("summary", {}).items()
            if status not in {"matched", "already_migrated"}
        )
        if code == 0:
            dialog_manager.dialog_data["migration_result"] = (
                "Локальная миграция завершена. Проверьте итоговый отчёт и резервную копию."
            )
        else:
            dialog_manager.dialog_data["migration_result"] = (
                f"Миграция не завершена (код {code}). Изучите лог; транзакция могла быть отменена."
            )
        logger.info(
            "{} Remnawave migration apply finished: code={}, summary={}",
            user.log,
            code,
            report.get("summary"),
        )
    except Exception as error:
        logger.exception("Remnawave v3 migration apply failed")
        dialog_manager.dialog_data["migration_result"] = f"Ошибка применения: {error}"
        dialog_manager.dialog_data["migration_log"] = str(error)[-2500:]
    finally:
        await redis.delete(RUNNING_KEY)
