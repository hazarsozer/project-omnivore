from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Annotated

import aioboto3
import magic
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from omnivore.api.schemas import APIResponse
from omnivore.auth.context import AuthContext
from omnivore.auth.dependencies import get_db_for_tenant, rate_limited, require_scope
from omnivore.auth.rate_limit import check_rate_limit
from omnivore.config import get_settings
from omnivore.constants import GPU_QUEUE_NAME
from omnivore.db.models import Document, Entity, Outbox
from omnivore.pipeline.registry import registry

router = APIRouter(prefix="/documents", tags=["documents"])
logger = structlog.get_logger(__name__)

_ALLOWED_STATUSES = {"queued", "routing", "extracting", "enriching", "indexed", "failed", "duplicate"}
_STREAM_CHUNK = 1 << 20  # 1 MB read chunks


@router.post("", status_code=202)
async def upload_document(
    request: Request,
    file: UploadFile,
    db: Annotated[AsyncSession, Depends(get_db_for_tenant)],
    auth: Annotated[AuthContext, Depends(require_scope("documents:write"))],
) -> JSONResponse:
    settings = get_settings()
    tenant_id = auth.tenant_id

    rl = await check_rate_limit(tenant_id, cost=settings.RL_UPLOAD_COST)
    if not rl.allowed:
        return JSONResponse(
            status_code=429,
            headers=_rl_headers(rl),
            content=APIResponse(
                success=False,
                error={"code": "RATE_LIMITED", "message": f"Rate limit exceeded. Retry in {rl.reset_after_seconds}s."},
            ).model_dump(),
        )

    # Stream file in chunks: compute sha256 incrementally, sniff MIME from first 2 KB
    hasher = hashlib.sha256()
    mime_header = b""
    total = 0

    while True:
        chunk = await file.read(_STREAM_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > settings.MAX_UPLOAD_SIZE_BYTES:
            raise HTTPException(status_code=413, detail="File exceeds maximum upload size")
        hasher.update(chunk)
        if len(mime_header) < 2048:
            mime_header += chunk[: 2048 - len(mime_header)]

    sha256_digest = hasher.digest()
    detected_mime = magic.from_buffer(mime_header, mime=True)

    # Dedup check
    existing = await db.scalar(
        select(Document).where(Document.tenant_id == tenant_id, Document.sha256 == sha256_digest)
    )
    if existing:
        return JSONResponse(
            status_code=202,
            headers=_rl_headers(rl),
            content=APIResponse(
                success=True,
                data={
                    "document_id": str(existing.id),
                    "status": "duplicate",
                    "poll_url": f"/v1/documents/{existing.id}",
                },
            ).model_dump(),
        )

    # Backpressure: reject when CPU or GPU queue is over limit
    pool = getattr(request.app.state, "arq_pool", None)
    if pool:
        queue_depth = await pool.zcard(pool.default_queue_name)
        if queue_depth >= settings.MAX_QUEUE_DEPTH:
            raise HTTPException(
                status_code=429,
                detail=f"Queue full ({queue_depth} pending jobs). Retry after some jobs complete.",
            )

        handler_cls = registry.resolve(detected_mime)
        if handler_cls is not None and getattr(handler_cls, "cost_class", None) == "gpu":
            gpu_depth = await pool.zcard(GPU_QUEUE_NAME)
            if gpu_depth >= settings.MAX_GPU_QUEUE_DEPTH:
                raise HTTPException(
                    status_code=429,
                    detail=f"GPU queue full ({gpu_depth} pending jobs). Retry after some jobs complete.",
                )

    document_id = uuid.uuid4()
    ext = (file.filename or "").rsplit(".", 1)[-1] if "." in (file.filename or "") else "bin"
    storage_key = f"raw/{tenant_id}/{datetime.now(UTC).strftime('%Y/%m')}/{document_id}.{ext}"

    await file.seek(0)
    endpoint = f"{'https' if settings.MINIO_SECURE else 'http'}://{settings.MINIO_ENDPOINT}"
    s3_session = aioboto3.Session()
    async with s3_session.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=settings.MINIO_ACCESS_KEY,
        aws_secret_access_key=settings.MINIO_SECRET_KEY.get_secret_value(),
        region_name="us-east-1",
    ) as s3:
        await _ensure_bucket(s3, settings.MINIO_BUCKET)
        await s3.upload_fileobj(
            file.file,
            settings.MINIO_BUCKET,
            storage_key,
            ExtraArgs={"ContentType": detected_mime},
        )

    job_kwargs = {
        "document_id": str(document_id),
        "mime": detected_mime,
        "size": total,
        "tenant_id": str(tenant_id),
        "config_snapshot": {},
    }
    doc = Document(
        id=document_id,
        tenant_id=tenant_id,
        sha256=sha256_digest,
        filename=file.filename or "upload",
        mime_type=detected_mime,
        size_bytes=total,
        storage_uri=f"s3://{settings.MINIO_BUCKET}/{storage_key}",
        status="queued",
    )
    outbox_entry = Outbox(
        aggregate_id=document_id,
        event_type="ingest.dispatch",
        payload={"task": "ingest_dispatch", "kwargs": job_kwargs},
    )
    db.add(doc)
    db.add(outbox_entry)
    await db.commit()
    await db.refresh(outbox_entry)

    if pool:
        try:
            _carrier: dict[str, str] = {}
            TraceContextTextMapPropagator().inject(_carrier)
            await pool.enqueue_job(
                "ingest_dispatch",
                **job_kwargs,
                _otel_traceparent=_carrier.get("traceparent"),
            )
            outbox_entry.published_at = datetime.now(UTC)
            await db.commit()
        except Exception:
            logger.warning("arq.enqueue.failed_will_replay", document_id=str(document_id))
    else:
        logger.warning("arq_pool.not_initialized", document_id=str(document_id))

    logger.info(
        "document.queued",
        document_id=str(document_id), mime=detected_mime, size=total, tenant_id=str(tenant_id),
    )
    return JSONResponse(
        status_code=202,
        headers=_rl_headers(rl),
        content=APIResponse(
            success=True,
            data={
                "document_id": str(document_id),
                "status": "queued",
                "poll_url": f"/v1/documents/{document_id}",
            },
        ).model_dump(),
    )


@router.get("/{document_id}")
async def get_document(
    document_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db_for_tenant)],
    auth: Annotated[AuthContext, Depends(require_scope("documents:read"))],
    _rl: Annotated[None, Depends(rate_limited())] = None,
) -> APIResponse[dict]:
    doc = await db.get(Document, document_id)
    if doc is None or doc.tenant_id != auth.tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")
    return APIResponse(
        success=True,
        data={
            "document_id": str(doc.id),
            "filename": doc.filename,
            "mime_type": doc.mime_type,
            "size_bytes": doc.size_bytes,
            "status": doc.status,
            "handler": doc.handler_name,
            "created_at": doc.created_at.isoformat() if doc.created_at else None,
            "indexed_at": doc.indexed_at.isoformat() if doc.indexed_at else None,
            "error": doc.error,
            "metadata": doc.doc_metadata,
            "summary": doc.doc_metadata.get("summary") if doc.doc_metadata else None,
            "routing_decision": doc.routing_decision,
        },
    )


@router.get("/{document_id}/entities")
async def get_document_entities(
    document_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db_for_tenant)],
    auth: Annotated[AuthContext, Depends(require_scope("entities:read"))],
    _rl: Annotated[None, Depends(rate_limited())] = None,
) -> APIResponse[list]:
    doc = await db.get(Document, document_id)
    if doc is None or doc.tenant_id != auth.tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")

    rows = (
        await db.scalars(
            select(Entity)
            .where(Entity.document_id == document_id)
            .order_by(Entity.label, Entity.normalized)
        )
    ).all()

    return APIResponse(
        success=True,
        data=[
            {
                "label": e.label,
                "value": e.value,
                "normalized": e.normalized,
                "confidence": e.confidence,
            }
            for e in rows
        ],
        meta={"count": len(rows)},
    )


@router.get("")
async def list_documents(
    db: Annotated[AsyncSession, Depends(get_db_for_tenant)],
    auth: Annotated[AuthContext, Depends(require_scope("documents:read"))],
    _rl: Annotated[None, Depends(rate_limited())] = None,
    status: str | None = Query(None),
    limit: int = Query(50, le=200),
    cursor: str | None = Query(None),
) -> APIResponse[list]:
    tenant_id = auth.tenant_id
    stmt = select(Document).where(Document.tenant_id == tenant_id).order_by(Document.created_at.desc()).limit(limit)
    if status and status in _ALLOWED_STATUSES:
        stmt = stmt.where(Document.status == status)
    if cursor:
        try:
            stmt = stmt.where(Document.created_at < datetime.fromisoformat(cursor))
        except ValueError:
            pass

    docs = (await db.scalars(stmt)).all()
    return APIResponse(
        success=True,
        data=[
            {
                "document_id": str(d.id),
                "filename": d.filename,
                "mime_type": d.mime_type,
                "status": d.status,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in docs
        ],
        meta={"count": len(docs)},
    )


_ALLOWED_RETRY_TASKS = frozenset({"ingest_dispatch", "gpu_ingest_dispatch"})


@router.post("/{document_id}/retry", status_code=202)
async def retry_document(
    document_id: uuid.UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_for_tenant)],
    auth: Annotated[AuthContext, Depends(require_scope("documents:write"))],
    _rl: Annotated[None, Depends(rate_limited(10))] = None,
) -> JSONResponse:
    doc = await db.get(Document, document_id)
    if doc is None or doc.tenant_id != auth.tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")

    if doc.status != "failed":
        raise HTTPException(
            status_code=409,
            detail=f"Document is not in failed state (current: {doc.status})",
        )

    retry_payload = (doc.error or {}).get("retry_payload")
    if not retry_payload:
        raise HTTPException(
            status_code=404,
            detail="No retry payload available — document failed before a job was enqueued",
        )

    task_name = retry_payload.get("task")
    task_kwargs = retry_payload.get("kwargs")
    if task_name not in _ALLOWED_RETRY_TASKS or not isinstance(task_kwargs, dict):
        raise HTTPException(
            status_code=422,
            detail=f"Malformed retry_payload: task must be one of {sorted(_ALLOWED_RETRY_TASKS)}",
        )

    doc.status = "queued"
    doc.error = None
    outbox_entry = Outbox(
        aggregate_id=document_id,
        event_type="ingest.retry",
        payload=retry_payload,
    )
    db.add(outbox_entry)
    await db.commit()
    await db.refresh(outbox_entry)

    pool = getattr(request.app.state, "arq_pool", None)
    if pool:
        try:
            _carrier: dict[str, str] = {}
            TraceContextTextMapPropagator().inject(_carrier)
            await pool.enqueue_job(
                task_name,
                **task_kwargs,
                _otel_traceparent=_carrier.get("traceparent"),
            )
            outbox_entry.published_at = datetime.now(UTC)
            await db.commit()
        except Exception:
            logger.warning("arq.retry_enqueue.failed", document_id=str(document_id))
    else:
        logger.warning("arq_pool.not_initialized", document_id=str(document_id))

    logger.info("document.retry_queued", document_id=str(document_id))
    return JSONResponse(
        status_code=202,
        content=APIResponse(
            success=True,
            data={
                "document_id": str(document_id),
                "status": "queued",
                "poll_url": f"/v1/documents/{document_id}",
            },
        ).model_dump(),
    )


def _rl_headers(rl) -> dict[str, str]:
    import time
    reset_at = int(time.time()) + rl.reset_after_seconds
    return {
        "X-RateLimit-Limit": str(rl.capacity),
        "X-RateLimit-Remaining": str(int(rl.remaining)),
        "X-RateLimit-Reset": str(reset_at),
        **({"Retry-After": str(rl.reset_after_seconds)} if not rl.allowed else {}),
    }


async def _ensure_bucket(s3, bucket: str) -> None:
    try:
        await s3.head_bucket(Bucket=bucket)
    except Exception:
        await s3.create_bucket(Bucket=bucket)
