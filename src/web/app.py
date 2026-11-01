from aiogram import Dispatcher
from fastapi import FastAPI, Request, Response
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.cors import CORSMiddleware

from src.__version__ import __version__
from src.core.config import AppConfig
from src.core.constants import API_V1, REMNAWAVE_WEBHOOK_PATH
from src.lifespan import lifespan

from .endpoints import (
    TelegramWebhookEndpoint,
    health_router,
    payments_router,
    public_router,
    remnawave_router,
)


def resolve_cors(origins: list[str]) -> tuple[list[str], bool]:
    if "*" in origins:
        logger.warning("CORS origins set to '*'; disabling allow_credentials for safety")
        return origins, False
    return origins, True


def get_app(config: AppConfig, dispatcher: Dispatcher) -> FastAPI:
    app: FastAPI = FastAPI(
        lifespan=lifespan,
        title="Remnashop API",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    cors_origins, allow_credentials = resolve_cors(list(config.origins))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.remnawave_migration_only = False

    @app.middleware("http")
    async def migration_mode_http_guard(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if not getattr(request.app.state, "remnawave_migration_only", False):
            return await call_next(request)

        allowed_paths = {
            config.bot.webhook_path,
            f"{API_V1}{REMNAWAVE_WEBHOOK_PATH}",
            "/health",
            "/health/details",
        }
        if request.url.path not in allowed_paths:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": (
                        "Remnawave migration is in progress. "
                        "Public API and payment operations are temporarily disabled."
                    )
                },
                headers={"Retry-After": "60"},
            )
        return await call_next(request)

    app.include_router(health_router)
    app.include_router(payments_router)
    app.include_router(remnawave_router)
    if config.web_enabled:
        app.include_router(public_router)

    if config.swagger_enabled:

        @app.get("/docs", include_in_schema=False)
        async def swagger_ui() -> HTMLResponse:
            return get_swagger_ui_html(
                openapi_url="/openapi.json",
                title=f"{app.title} - Swagger UI",
                swagger_ui_parameters={"persistAuthorization": True},
            )

        @app.get("/redoc", include_in_schema=False)
        async def redoc_ui() -> HTMLResponse:
            return get_redoc_html(
                openapi_url="/openapi.json",
                title=f"{app.title} - ReDoc",
            )

        @app.get("/openapi.json", include_in_schema=False)
        async def openapi_schema() -> dict:
            api_routes = [r for r in app.routes if getattr(r, "include_in_schema", True)]
            return get_openapi(title=app.title, version=app.version, routes=api_routes)

    telegram_webhook_endpoint = TelegramWebhookEndpoint(
        dispatcher=dispatcher,
        secret_token=config.bot.secret_token.get_secret_value(),
    )

    telegram_webhook_endpoint.register(app=app, path=config.bot.webhook_path)

    app.state.telegram_webhook_endpoint = telegram_webhook_endpoint
    app.state.dispatcher = dispatcher

    logger.info("FastAPI application initialized'")
    return app
