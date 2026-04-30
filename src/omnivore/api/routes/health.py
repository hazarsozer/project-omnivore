import structlog
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from omnivore.api.schemas import APIResponse

router = APIRouter(tags=["health"])
logger = structlog.get_logger(__name__)


@router.get("/health", response_model=APIResponse[dict])
async def health() -> APIResponse[dict]:
    return APIResponse(success=True, data={"status": "ok", "service": "omnivore-api"})


@router.get("/ready")
async def ready() -> JSONResponse:
    # Phase 1: replace with real connectivity checks (asyncpg ping, redis ping, minio head-bucket)
    checks = {"postgres": "ok", "redis": "ok", "minio": "ok"}
    all_ok = all(v == "ok" for v in checks.values())
    logger.debug("readiness_check", checks=checks)
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content=APIResponse(success=all_ok, data=checks).model_dump(),
    )
