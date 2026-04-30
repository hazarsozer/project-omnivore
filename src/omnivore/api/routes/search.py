"""Search endpoint — stub for Phase 1. Full hybrid search (BM25 + vector + RRF) in Phase 3."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from omnivore.api.schemas import APIResponse

router = APIRouter(prefix="/search", tags=["search"])


class SearchRequest(BaseModel):
    query: str
    mode: str = "hybrid"
    filters: dict = {}
    top_k: int = 20
    rerank: bool = False


@router.post("")
async def search(body: SearchRequest) -> APIResponse[list]:
    # Phase 3: hybrid BM25 + vector search with RRF and optional cross-encoder rerank
    return APIResponse(
        success=True,
        data=[],
        meta={"message": "Search available in Phase 3", "query": body.query},
    )
