from __future__ import annotations

import asyncio
import secrets
import time
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
import structlog
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Match

import omnivore.auth.cache as _auth_cache
import omnivore.auth.rate_limit as _auth_rl
from omnivore.api.routes import admin, auth_route, documents, handlers_route, health, search, tenant
from omnivore.api.schemas import APIResponse, ErrorDetail
from omnivore.auth.errors import AuthError
from omnivore.config import get_settings
from omnivore.constants import GPU_QUEUE_NAME
from omnivore.logging_config import configure_logging
from omnivore.observability import (
    HTTP_DURATION,
    HTTP_REQUESTS_TOTAL,
    QUEUE_DEPTH,
    setup_tracing,
)
from omnivore.pipeline.registry import registry

logger = structlog.get_logger(__name__)


class _RequestContextMiddleware(BaseHTTPMiddleware):
    """Bind a unique request_id to every structlog log line for this request."""

    async def dispatch(self, request: Request, call_next):
        import structlog.contextvars
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=secrets.token_hex(8),
            method=request.method,
            path=request.url.path,
        )
        return await call_next(request)


class _PrometheusMiddleware(BaseHTTPMiddleware):
    """Record omnivore_http_requests_total and omnivore_http_duration_seconds."""

    async def dispatch(self, request: Request, call_next):
        route = _route_template(request)
        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start
        status = str(response.status_code)
        HTTP_REQUESTS_TOTAL.labels(
            method=request.method, route=route, status_code=status
        ).inc()
        HTTP_DURATION.labels(method=request.method, route=route).observe(duration)
        return response


def _route_template(request: Request) -> str:
    for route in request.app.routes:
        match, _ = route.matches(request.scope)
        if match == Match.FULL:
            return getattr(route, "path", "__unknown__")
    return "__unmatched__"


def _is_valid_pem(value: str, min_length: int) -> bool:
    """Return True if the value looks like a complete PEM block."""
    return (
        len(value) >= min_length
        and "-----BEGIN" in value
        and "-----END" in value
    )


async def _poll_queue_depth(arq_pool) -> None:
    """Background task: update QUEUE_DEPTH gauge every 30 s."""
    while True:
        try:
            default_depth = await arq_pool.zcard(arq_pool.default_queue_name)
            QUEUE_DEPTH.labels(queue="default").set(default_depth)
            gpu_depth = await arq_pool.zcard(GPU_QUEUE_NAME)
            QUEUE_DEPTH.labels(queue="gpu").set(gpu_depth)
        except Exception:
            pass
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings)

    # C-2: Validate JWT keys resolve to non-empty, well-formed PEMs. config.py
    # normalizes base64 / raw / \n-escaped input to real PEM; this catches values
    # that still don't contain a complete BEGIN/END block (truncated or garbage).
    _priv = settings.JWT_PRIVATE_KEY_PEM.get_secret_value()
    _pub = settings.JWT_PUBLIC_KEY_PEM
    if _priv and not _is_valid_pem(_priv, min_length=200):
        raise RuntimeError(
            f"JWT_PRIVATE_KEY_PEM did not resolve to a valid PEM (resolved length {len(_priv)}). "
            "Set it to a base64-encoded PEM (recommended, docker-safe) or a raw PEM "
            "with the '-----BEGIN PRIVATE KEY-----' / '-----END PRIVATE KEY-----' lines intact. "
            "See .env.example / README §2 'Generate the JWT keypair'."
        )
    if _pub and not _is_valid_pem(_pub, min_length=100):
        raise RuntimeError(
            f"JWT_PUBLIC_KEY_PEM did not resolve to a valid PEM (resolved length {len(_pub)}). "
            "Set it to a base64-encoded PEM (recommended, docker-safe) or a raw PEM "
            "with the '-----BEGIN PUBLIC KEY-----' / '-----END PUBLIC KEY-----' lines intact. "
            "See .env.example / README §2 'Generate the JWT keypair'."
        )

    # Observability — set up before anything else so early logs get trace_id
    setup_tracing(settings)

    # M-3: pre-initialise QUEUE_DEPTH time series so Grafana never shows "no data"
    QUEUE_DEPTH.labels(queue="default").set(0)
    QUEUE_DEPTH.labels(queue="gpu").set(0)

    registry.discover()
    logger.info("startup", environment=settings.ENVIRONMENT, handlers=len(registry.all_handlers()))

    # Redis singletons for auth cache + rate limiter
    _auth_redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    _auth_cache._redis_client = _auth_redis
    _auth_rl._redis_client = _auth_redis

    app.state.arq_pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))

    # Start queue-depth poller (best-effort, ignored if Redis is unreachable)
    poller_task = asyncio.create_task(_poll_queue_depth(app.state.arq_pool))

    yield

    poller_task.cancel()
    await _auth_redis.aclose()
    await app.state.arq_pool.close()
    # M-5: flush pending spans before the process exits
    provider = trace.get_tracer_provider()
    if hasattr(provider, "shutdown"):
        provider.shutdown()
    logger.info("shutdown")


app = FastAPI(
    title="Omnivore API",
    version="0.1.0",
    lifespan=lifespan,
)

_origins = get_settings().ALLOWED_ORIGINS
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(_PrometheusMiddleware)
app.add_middleware(_RequestContextMiddleware)

# OTel FastAPI auto-instrumentation — adds http.server spans for every request.
# M-6: only instrument when OTEL_ENABLED=True to avoid silent error swallowing.
if get_settings().OTEL_ENABLED:
    FastAPIInstrumentor.instrument_app(app)

# Prometheus /metrics endpoint — explicit route avoids Starlette's mount trailing-slash redirect
@app.get("/metrics", include_in_schema=False)
async def metrics_endpoint(request: Request) -> Response:
    settings = get_settings()
    token = settings.METRICS_AUTH_TOKEN
    # Treat empty token as "no auth configured" — guards against misconfiguration
    # where METRICS_AUTH_TOKEN="" would otherwise accept "Bearer " as a match.
    if token is not None and token.get_secret_value():
        auth_header = request.headers.get("Authorization", "")
        expected = f"Bearer {token.get_secret_value()}"
        if not secrets.compare_digest(auth_header, expected):
            # 401 Unauthorized + WWW-Authenticate is the correct semantic for
            # "no/invalid auth credentials"; 403 would mean "auth ok but denied".
            return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

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
