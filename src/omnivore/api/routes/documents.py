from __future__ import annotations

import hashlib
import io
import uuid
from datetime import datetime, timezone
from typing import Annotated

import aioboto3
import magic
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from omnivore.api.schemas import APIResponse, ErrorDetail
from omnivore.config import get_settings
from omnivore.db.models import Document
from omnivore.db.session import get_db

router = APIRouter(prefix="/documents", tags=["documents"])
logger = structlog.get_logger(__name__)

_ALLOWED_STATUSES = {"queued", "extracting", "enriching", "indexed", "failed", "duplicate"}


@router.post("", status_code=202)
async def upload_document(
    request: Request,
    file: UploadFile,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    settings = get_settings()
    tenant_id = uuid.UUID("00000000-0000-0000-0000-000000000001")  # Phase 4: real tenant from JWT

    raw = await file.read()
    if len(raw) > settings.MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds maximum upload size")

    sha256 = hashlib.sha256(raw).digest()
    detected_mime = magic.from_buffer(raw[:2048], mime=True)

    # Dedup check
    existing = await db.scalar(
        select(Document).where(Document.tenant_id == tenant_id, Document.sha256 == sha256)
    )
    if existing:
        return JSONResponse(
            status_code=202,
            content=APIResponse(
                success=True,
                data={"document_id": str(existing.id), "status": "duplicate", "poll_url": f"/v1/documents/{existing.id}"},
            ).model_dump(),
        )

    document_id = uuid.uuid4()
    ext = (file.filename or "").rsplit(".", 1)[-1] if "." in (file.filename or "") else "bin"
    storage_key = f"raw/{tenant_id}/{datetime.now(timezone.utc).strftime('%Y/%m')}/{document_id}.{ext}"

    # Upload to MinIO
    endpoint = f"{'https' if settings.MINIO_SECURE else 'http'}://{settings.MINIO_ENDPOINT}"
    session = aioboto3.Session()
    async with session.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=settings.MINIO_ACCESS_KEY,
        aws_secret_access_key=settings.MINIO_SECRET_KEY.get_secret_value(),
        region_name="us-east-1",
    ) as s3:
        await _ensure_bucket(s3, settings.MINIO_BUCKET)
        await s3.put_object(
            Bucket=settings.MINIO_BUCKET,
            Key=storage_key,
            Body=raw,
            ContentType=detected_mime,
        )

    doc = Document(
        id=document_id,
        tenant_id=tenant_id,
        sha256=sha256,
        filename=file.filename or "upload",
        mime_type=detected_mime,
        size_bytes=len(raw),
        storage_uri=f"s3://{settings.MINIO_BUCKET}/{storage_key}",
        status="queued",
    )
    db.add(doc)
    await db.commit()

    # Enqueue extraction job
    pool = getattr(request.app.state, "arq_pool", None)
    if pool:
        await pool.enqueue_job(
            "ingest_dispatch",
            document_id=str(document_id),
            mime=detected_mime,
            size=len(raw),
            tenant_id=str(tenant_id),
            config_snapshot={},
        )
    else:
        logger.warning("arq_pool.not_initialized", document_id=str(document_id))

    logger.info("document.queued", document_id=str(document_id), mime=detected_mime, size=len(raw))
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


@router.get("/{document_id}")
async def get_document(
    document_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> APIResponse[dict]:
    doc = await db.get(Document, document_id)
    if doc is None:
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
            "metadata": doc.metadata,
        },
    )


@router.get("")
async def list_documents(
    db: Annotated[AsyncSession, Depends(get_db)],
    status: str | None = Query(None),
    limit: int = Query(50, le=200),
    cursor: str | None = Query(None),
) -> APIResponse[list]:
    tenant_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
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


async def _ensure_bucket(s3, bucket: str) -> None:
    try:
        await s3.head_bucket(Bucket=bucket)
    except Exception:
        await s3.create_bucket(Bucket=bucket)
