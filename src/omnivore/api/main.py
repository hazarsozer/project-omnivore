from contextlib import asynccontextmanager

import structlog
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from omnivore.api.routes import admin, auth_route, documents, handlers_route, health, search, tenant
from omnivore.api.schemas import APIResponse, ErrorDetail
from omnivore.auth.errors import AuthError
from omnivore.config import get_settings
from omnivore.logging_config import configure_logging
from omnivore.pipeline.registry import registry

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings)

    registry.discover()
    logger.info("startup", environment=settings.ENVIRONMENT, handlers=len(registry.all_handlers()))

    app.state.arq_pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))

    yield

    await app.state.arq_pool.close()
    logger.info("shutdown")


app = FastAPI(
    title="Omnivore API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix="/v1")
app.include_router(auth_route.router, prefix="/v1")
app.include_router(admin.router, prefix="/v1")
app.include_router(tenant.router, prefix="/v1")
app.include_router(documents.router, prefix="/v1")
app.include_router(search.router, prefix="/v1")
app.include_router(handlers_route.router, prefix="/v1")


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/v1/health", status_code=307)


@app.exception_handler(AuthError)
async def auth_exception_handler(request: Request, exc: AuthError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content=APIResponse(
            success=False,
            error=ErrorDetail(code=exc.code, message=exc.message),
        ).model_dump(),
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("unhandled_exception", exc_info=exc, path=str(request.url))
    return JSONResponse(
        status_code=500,
        content=APIResponse(
            success=False,
            error=ErrorDetail(code="INTERNAL_ERROR", message="An unexpected error occurred"),
        ).model_dump(),
    )
