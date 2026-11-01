import html
from typing import Any

from aiogram_dialog import DialogManager


async def migration_getter(
    dialog_manager: DialogManager,
    **kwargs: Any,
) -> dict[str, Any]:
    report = dialog_manager.dialog_data.get("migration_report") or {}
    summary = report.get("summary", {})
    return {
        "panel_version": report.get("panel_version", "—"),
        "panel_api_line": report.get("panel_api_line", "—"),
        "panel_users_read": report.get("panel_users_read", 0),
        "local_current_subscriptions": report.get("local_current_subscriptions", 0),
        "matched": summary.get("matched", 0),
        "already_migrated": summary.get("already_migrated", 0),
        "unmatched": summary.get("unmatched", 0),
        "conflict": summary.get("conflict", 0),
        "duplicate_local_mapping": summary.get("duplicate_local_mapping", 0),
        "existing_id_conflict": summary.get("existing_id_conflict", 0),
        "other_issues": sum(
            count for status, count in summary.items()
            if status not in {
                "matched", "already_migrated", "unmatched", "conflict",
                "duplicate_local_mapping", "existing_id_conflict",
            }
        ),
        "can_apply": bool(dialog_manager.dialog_data.get("migration_can_apply")),
        "log": html.escape(
            dialog_manager.dialog_data.get("migration_log", "Проверка ещё не запускалась.")
        ),
        "result": html.escape(dialog_manager.dialog_data.get("migration_result", "")),
        "issues_total": dialog_manager.dialog_data.get("migration_issues_total", 0),
        "issues": html.escape(
            "\n".join(dialog_manager.dialog_data.get("migration_issues", []))
            or "Проблемных записей нет."
        ),
    }
