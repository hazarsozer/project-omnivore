from __future__ import annotations

from fastapi import APIRouter

from omnivore.api.schemas import APIResponse
from omnivore.pipeline.registry import registry

router = APIRouter(prefix="/handlers", tags=["admin"])


@router.get("")
async def list_handlers() -> APIResponse[list]:
    return APIResponse(success=True, data=registry.all_handlers())
