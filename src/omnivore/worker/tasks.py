from __future__ import annotations

import structlog

from omnivore.worker.context import ArqWorkflowContext

logger = structlog.get_logger(__name__)


async def ingest_dispatch(
    ctx: dict,
    *,
    document_id: str,
    mime: str,
    size: int,
    tenant_id: str,
    config_snapshot: dict,
) -> dict:
    wf_ctx = ArqWorkflowContext(ctx)
    # Phase 1: idempotency check
    # key = sha256(f"ingest_dispatch:{document_id}:{HANDLER_VERSION}:{config_hash}")
    # acquired = await ctx["redis"].set(f"idem:{key}", "1", nx=True, ex=86400)
    # if not acquired:
    #     return {"status": "duplicate", "document_id": document_id}
    logger.info("ingest.dispatch.received", document_id=document_id, mime=mime, size=size, tenant_id=tenant_id)
    await wf_ctx.complete()
    return {"status": "accepted", "document_id": document_id}


async def on_startup(ctx: dict) -> None:
    logger.info("worker.startup")


async def on_shutdown(ctx: dict) -> None:
    logger.info("worker.shutdown")
