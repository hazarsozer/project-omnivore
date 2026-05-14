from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from omnivore.api.schemas import APIResponse
from omnivore.auth.context import AuthContext
from omnivore.auth.dependencies import require_scope
from omnivore.pipeline.registry import registry

router = APIRouter(prefix="/handlers", tags=["admin"])


@router.get("")
async def list_handlers(
    auth: Annotated[AuthContext, Depends(require_scope("handlers:read"))],
) -> APIResponse[list]:
    return APIResponse(success=True, data=registry.all_handlers())
