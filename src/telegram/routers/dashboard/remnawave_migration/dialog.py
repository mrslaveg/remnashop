from aiogram_dialog import Dialog, StartMode, Window

from src.core.enums import BannerName
from src.telegram.states import Dashboard, DashboardRemnawaveMigration
from src.telegram.widgets import Banner, I18nFormat, IgnoreUpdate
from aiogram_dialog.widgets.input import MessageInput

from src.telegram.widgets.kbd import Button, Row, Start, SwitchTo

from .getters import migration_getter
from .handlers import on_migration_apply, on_migration_check, on_migration_decision_message

main = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-remnawave-migration-main"),
    Row(
        Button(
            text=I18nFormat("btn-remnawave-migration.check"),
            id="migration_check",
            on_click=on_migration_check,
        ),
    ),
    Row(
        SwitchTo(
            text=I18nFormat("btn-remnawave-migration.review"),
            id="migration_review",
            state=DashboardRemnawaveMigration.REVIEW,
            when=lambda data, widget, manager: bool(
                manager.dialog_data.get("migration_issues_total")
            ),
        ),
    ),
    Row(
        SwitchTo(
            text=I18nFormat("btn-remnawave-migration.apply"),
            id="migration_apply",
            state=DashboardRemnawaveMigration.CONFIRM,
            when=lambda data, widget, manager: bool(
                manager.dialog_data.get("migration_can_apply")
            ),
        ),
    ),
    Row(
        Start(
            text=I18nFormat("btn-back.general"),
            id="back",
            state=Dashboard.MAIN,
            mode=StartMode.RESET_STACK,
        ),
    ),
    IgnoreUpdate(),
    state=DashboardRemnawaveMigration.MAIN,
    getter=migration_getter,
)

confirm = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-remnawave-migration-confirm"),
    Row(
        Button(
            text=I18nFormat("btn-remnawave-migration.confirm"),
            id="migration_confirm",
            on_click=on_migration_apply,
        ),
    ),
    Row(
        SwitchTo(
            text=I18nFormat("btn-back.general"),
            id="back",
            state=DashboardRemnawaveMigration.MAIN,
        ),
    ),
    IgnoreUpdate(),
    state=DashboardRemnawaveMigration.CONFIRM,
    getter=migration_getter,
)


review = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-remnawave-migration-review"),
    MessageInput(func=on_migration_decision_message),
    Row(
        SwitchTo(
            text=I18nFormat("btn-back.general"),
            id="back",
            state=DashboardRemnawaveMigration.MAIN,
        ),
    ),
    IgnoreUpdate(),
    state=DashboardRemnawaveMigration.REVIEW,
    getter=migration_getter,
)

router = Dialog(main, review, confirm)
