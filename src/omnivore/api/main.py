from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from omnivore.api.routes import health
from omnivore.api.schemas import APIResponse, ErrorDetail
from omnivore.config import get_settings
from omnivore.logging_config import configure_logging

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings)
    logger.info("startup", environment=settings.ENVIRONMENT)
    yield
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


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/v1/health", status_code=307)


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
