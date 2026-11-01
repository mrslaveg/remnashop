import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from aiogram.types import CallbackQuery
from aiogram_dialog import DialogManager
from aiogram_dialog.widgets.kbd import Button
from dishka import FromDishka
from dishka.integrations.aiogram_dialog import inject
from loguru import logger
from redis.asyncio import Redis

from src.application.dto import TelegramUserDto
from src.core.constants import USER_KEY

CONFIRMATION = "REMNAWAVE_V3_LOCAL_DB_ONLY"
RUNNING_KEY = "remnashop:remnawave-v3-migration:running"


async def _run_migration(apply: bool) -> tuple[int, dict[str, Any], str]:
    with tempfile.TemporaryDirectory(prefix="remnawave-v3-") as temp_dir:
        report_path = str(Path(temp_dir) / "report.json")
        command = [
            sys.executable,
            "scripts/remnawave_v3_migrate.py",
            "--report-file",
            report_path,
        ]
        if apply:
            command.extend(["--apply", "--confirm", CONFIRMATION])

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=900)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise RuntimeError("Миграция превысила лимит времени (15 минут).")

        report_file = Path(report_path)
        if not report_file.exists():
            details = output.decode("utf-8", errors="replace")[-3000:]
            raise RuntimeError(
                "Мигратор не создал JSON-отчёт. Подробности:\n" + details
            )

        report = json.loads(report_file.read_text(encoding="utf-8"))
        log = output.decode("utf-8", errors="replace")[-3500:]
        return int(process.returncode or 0), report, log


def _is_clean(report: dict[str, Any]) -> bool:
    summary = report.get("summary", {})
    unresolved = sum(
        count for status, count in summary.items()
        if status not in {"matched", "already_migrated"}
    )
    return (
        report.get("panel_api_line") in {"v2_legacy_uuid_lookup", "v3_numeric_id_stream"}
        and report.get("local_current_subscriptions", 0) > 0
        and unresolved == 0
        and report.get("panel_write_requests_allowed") is False
    )


@inject
async def on_migration_check(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    redis: FromDishka[Redis],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    if not await redis.set(RUNNING_KEY, str(user.telegram_id), nx=True, ex=1800):
        await callback.answer("Миграция уже выполняется.", show_alert=True)
        return

    dialog_manager.dialog_data["migration_can_apply"] = False
    dialog_manager.dialog_data["migration_result"] = "Выполняется read-only проверка…"
    try:
        code, report, log = await _run_migration(apply=False)
        dialog_manager.dialog_data["migration_report"] = {
            key: value for key, value in report.items() if key != "matches"
        }
        dialog_manager.dialog_data["migration_log"] = log
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
        dialog_manager.dialog_data["migration_log"] = str(error)[-3500:]
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
    if not dialog_manager.dialog_data.get("migration_can_apply"):
        await callback.answer("Сначала выполните успешную проверку.", show_alert=True)
        return

    if not await redis.set(RUNNING_KEY, str(user.telegram_id), nx=True, ex=1800):
        await callback.answer("Миграция уже выполняется.", show_alert=True)
        return

    dialog_manager.dialog_data["migration_can_apply"] = False
    dialog_manager.dialog_data["migration_result"] = "Применение локальных ID…"
    try:
        code, report, log = await _run_migration(apply=True)
        dialog_manager.dialog_data["migration_report"] = {
            key: value for key, value in report.items() if key != "matches"
        }
        dialog_manager.dialog_data["migration_log"] = log
        if code == 0:
            dialog_manager.dialog_data["migration_result"] = (
                "Локальная миграция завершена. Проверьте итоговый отчёт и резервную копию."
            )
        else:
            dialog_manager.dialog_data["migration_result"] = (
                f"Миграция не завершена (код {code}). Изменения не применены либо "
                "транзакция была отменена; изучите лог."
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
        dialog_manager.dialog_data["migration_log"] = str(error)[-3500:]
    finally:
        await redis.delete(RUNNING_KEY)
