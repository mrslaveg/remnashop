#!/bin/sh
set -eu

ASSETS_CONTAINER_PATH="/opt/remnashop/assets"
ASSETS_DEFAULT_PATH="/opt/remnashop/assets.default"
UVICORN_RELOAD_ARGS=""

echo "Starting asset initialization"

USER_BANNERS="${ASSETS_CONTAINER_PATH}/banners"
if [ ! -d "$USER_BANNERS" ] || [ -z "$(ls -A "$USER_BANNERS" 2>/dev/null)" ]; then
    echo "No user banners found — copying defaults"
    cp -a "${ASSETS_DEFAULT_PATH}/banners/." "$USER_BANNERS/"
fi

USER_TRANSLATIONS="${ASSETS_CONTAINER_PATH}/translations"
if [ -d "$ASSETS_DEFAULT_PATH" ] && [ -d "$USER_TRANSLATIONS" ]; then
    for locale_dir in "$USER_TRANSLATIONS"/*/; do
        [ -d "$locale_dir" ] || continue
        for ftl in "$locale_dir"*.ftl; do
            [ -f "$ftl" ] || continue
            case "$(basename "$ftl")" in
                custom.ftl) continue ;;
            esac
            mkdir -p "${locale_dir}.legacy"
            mv "$ftl" "${locale_dir}.legacy/"
            echo "Moved legacy translation $(basename "$ftl")"
        done
    done
fi

if [ -d "${ASSETS_DEFAULT_PATH}/translations" ]; then
    for locale_dir in "${ASSETS_DEFAULT_PATH}/translations"/*/; do
        locale=$(basename "$locale_dir")
        USER_LOCALE_DIR="${ASSETS_CONTAINER_PATH}/translations/${locale}"
        CUSTOM_FTL="${USER_LOCALE_DIR}/custom.ftl"
        TEMPLATE_FTL="${ASSETS_DEFAULT_PATH}/translations/${locale}/custom.ftl"
        mkdir -p "$USER_LOCALE_DIR"
        if [ ! -f "$CUSTOM_FTL" ]; then
            if [ -f "$TEMPLATE_FTL" ]; then
                cp "$TEMPLATE_FTL" "$CUSTOM_FTL"
            else
                touch "$CUSTOM_FTL"
            fi
        elif [ -f "$TEMPLATE_FTL" ]; then
            cp "$TEMPLATE_FTL" "${CUSTOM_FTL}.example"
        fi
    done
fi

echo "Asset initialization complete"
echo "Applying database schema migrations"
alembic -c src/infrastructure/database/alembic.ini upgrade head

echo "Running Remnawave ID migration and verification"
python scripts/remnawave_v3_migrate.py --apply --confirm REMNAWAVE_V3_LOCAL_DB_ONLY --report-file /tmp/remnawave-v3-migration-report.json

if [ "${UVICORN_RELOAD_ENABLED:-false}" = "true" ]; then
    UVICORN_RELOAD_ARGS="--reload --reload-dir /opt/remnashop/src --reload-dir /opt/remnashop/assets --reload-include *.ftl"
    echo "Uvicorn will run with reload enabled"
else
    echo "Uvicorn will run without reload"
fi

exec uvicorn src.__main__:application --host "${APP_HOST:-0.0.0.0}" --port "${APP_PORT:-5000}" --factory --use-colors ${UVICORN_RELOAD_ARGS}
