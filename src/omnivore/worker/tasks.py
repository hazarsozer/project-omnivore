from __future__ import annotations

import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import select

from omnivore.config import get_settings
from omnivore.db.models import Chunk as ChunkRow
from omnivore.db.models import Document, ExtractedRow, ExtractedTable
from omnivore.db.session import AsyncSessionLocal
from omnivore.pipeline.chunker import chunk_result
from omnivore.pipeline.context import BlobRef, IngestContext
from omnivore.pipeline.models import StructuredTable
from omnivore.pipeline.registry import registry
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
    settings = get_settings()
    doc_id = uuid.UUID(document_id)
    t_id = uuid.UUID(tenant_id)

    logger.info("ingest.dispatch.received", document_id=document_id, mime=mime)

    async with AsyncSessionLocal() as db:
        doc = await db.get(Document, doc_id)
        if doc is None:
            logger.error("ingest.dispatch.doc_not_found", document_id=document_id)
            return {"status": "error", "reason": "document_not_found"}

        handler_cls = registry.resolve(mime)
        if handler_cls is None:
            await _fail_document(db, doc, f"No handler for MIME type: {mime}")
            return {"status": "error", "reason": "no_handler"}

        handler = handler_cls()
        bucket, key = _parse_storage_uri(doc.storage_uri)
        blob = BlobRef(bucket=bucket, key=key, mime_type=mime, size_bytes=size)
        ingest_ctx = IngestContext(
            document_id=doc_id,
            tenant_id=t_id,
            blob=blob,
            filename=doc.filename,
            config=config_snapshot,
            _settings=settings,
        )

        doc.status = "extracting"
        doc.handler_name = handler.name
        doc.handler_version = handler.version
        await db.commit()

        try:
            result = await handler.extract(blob, ingest_ctx)
        except Exception as exc:
            logger.exception("handler.extract.failed", document_id=document_id, handler=handler.name)
            await _fail_document(db, doc, str(exc))
            return {"status": "error", "reason": str(exc)}

        doc.status = "enriching"
        await db.commit()

        chunks = chunk_result(result, t_id)

        # Persist chunks
        for chunk in chunks:
            db.add(
                ChunkRow(
                    document_id=chunk.document_id,
                    tenant_id=chunk.tenant_id,
                    ordinal=chunk.ordinal,
                    kind=chunk.kind,
                    content=chunk.content,
                    token_count=chunk.token_count,
                    position=chunk.position,
                    heading_path=chunk.heading_path,
                    source_block_ids=chunk.source_block_ids,
                    table_lineage=chunk.table_lineage,
                    language=chunk.language,
                    confidence=chunk.confidence,
                )
            )

        # Persist structured tables
        for table in result.tables:
            et = ExtractedTable(
                document_id=doc_id,
                tenant_id=t_id,
                name=table.name,
                schema={"headers": table.headers},
                row_count=len(table.rows),
            )
            db.add(et)
            await db.flush()  # get et.id
            for i, row in enumerate(table.rows):
                db.add(ExtractedRow(table_id=et.id, ordinal=i, data=row))

        doc.status = "indexed"
        doc.indexed_at = datetime.now(timezone.utc)
        doc.metadata = result.metadata
        await db.commit()

    logger.info(
        "ingest.dispatch.complete",
        document_id=document_id,
        chunks=len(chunks),
        tables=len(result.tables),
        handler=handler.name,
    )
    await wf_ctx.complete()
    return {"status": "indexed", "document_id": document_id, "chunks": len(chunks)}


async def on_startup(ctx: dict) -> None:
    settings = get_settings()
    registry.discover()
    logger.info("worker.startup", handlers=len(registry.all_handlers()))


async def on_shutdown(ctx: dict) -> None:
    logger.info("worker.shutdown")


async def _fail_document(db, doc: Document, reason: str) -> None:
    doc.status = "failed"
    doc.error = {"reason": reason}
    await db.commit()


def _parse_storage_uri(uri: str) -> tuple[str, str]:
    # s3://bucket/path/to/key
    without_scheme = uri.removeprefix("s3://")
    bucket, _, key = without_scheme.partition("/")
    return bucket, key
