from __future__ import annotations

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from omnivore.api.schemas import APIResponse
from omnivore.config import get_settings
from omnivore.db.session import AsyncSessionLocal

router = APIRouter(tags=["health"])
logger = structlog.get_logger(__name__)


@router.get("/health", response_model=APIResponse[dict])
async def health() -> APIResponse[dict]:
    return APIResponse(success=True, data={"status": "ok", "service": "omnivore-api"})


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    checks: dict[str, str] = {}

    # PostgreSQL
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:
        logger.warning("readiness.postgres.failed", error=str(exc))
        checks["postgres"] = "error"

    # Redis — reuse the ARQ pool wired up in lifespan
    try:
        pool = getattr(request.app.state, "arq_pool", None)
        if pool is None:
            raise RuntimeError("arq_pool not initialised")
        await pool.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        logger.warning("readiness.redis.failed", error=str(exc))
        checks["redis"] = "error"

    # MinIO / S3
    try:
        import aioboto3
        settings = get_settings()
        endpoint = f"{'https' if settings.MINIO_SECURE else 'http'}://{settings.MINIO_ENDPOINT}"
        session = aioboto3.Session()
        async with session.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=settings.MINIO_ACCESS_KEY,
            aws_secret_access_key=settings.MINIO_SECRET_KEY.get_secret_value(),
            region_name="us-east-1",
        ) as s3:
            await s3.head_bucket(Bucket=settings.MINIO_BUCKET)
        checks["minio"] = "ok"
    except Exception as exc:
        logger.warning("readiness.minio.failed", error=str(exc))
        checks["minio"] = "error"

    all_ok = all(v == "ok" for v in checks.values())
    logger.debug("readiness_check", checks=checks)
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content=APIResponse(success=all_ok, data=checks).model_dump(),
    )
