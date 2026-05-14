from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select, update

from omnivore.config import get_settings
from omnivore.constants import GPU_QUEUE_NAME
from omnivore.db.models import Chunk as ChunkRow
from omnivore.db.models import Document, ExtractedRow, ExtractedTable, Outbox, Tenant
from omnivore.db.models import Entity as EntityRow
from omnivore.db.session import admin_session, tenant_session
from omnivore.pipeline.chunker import chunk_result
from omnivore.pipeline.context import BlobRef, IngestContext
from omnivore.pipeline.embeddings import EMBEDDING_MODEL, embed_chunks
from omnivore.pipeline.enrichers import detect_language, extract_entities, summarize_document
from omnivore.pipeline.models import Chunk
from omnivore.pipeline.registry import registry
from omnivore.pipeline.routing import evaluate_policy
from omnivore.worker.context import ArqWorkflowContext

logger = structlog.get_logger(__name__)

GPU_COST_CLASSES: frozenset[str] = frozenset({"gpu"})


async def ingest_dispatch(
    ctx: dict,
    *,
    document_id: str,
    mime: str,
    size: int,
    tenant_id: str,
    config_snapshot: dict,
) -> dict:
    _job_kwargs = {
        "document_id": document_id, "mime": mime, "size": size,
        "tenant_id": tenant_id, "config_snapshot": config_snapshot,
    }
    t_id = uuid.UUID(tenant_id)
    handler_cls = registry.resolve(mime)
    if handler_cls is None:
        async with tenant_session(t_id) as db:
            doc = await db.get(Document, uuid.UUID(document_id))
            if doc:
                await _fail_document(db, doc, f"No handler for MIME type: {mime}", _job_kwargs)
        return {"status": "error", "reason": "no_handler"}

    if handler_cls.cost_class in GPU_COST_CLASSES:
        logger.info("ingest.dispatch.routed_to_gpu", document_id=document_id, mime=mime)
        async with tenant_session(t_id) as db:
            doc = await db.get(Document, uuid.UUID(document_id))
            if doc:
                if doc.status != "queued":
                    logger.info(
                        "ingest.dispatch.routing_skipped",
                        document_id=document_id,
                        status=doc.status,
                    )
                    return {"status": doc.status, "document_id": document_id, "reason": "already_processed"}
                doc.status = "routing"
                await db.commit()
        await ctx["redis"].enqueue_job(
            "gpu_ingest_dispatch",
            _queue_name=GPU_QUEUE_NAME,
            document_id=document_id,
            mime=mime,
            size=size,
            tenant_id=tenant_id,
            config_snapshot=config_snapshot,
        )
        return {"status": "routed_to_gpu", "document_id": document_id}

    return await _run_ingest(ctx, handler_cls, document_id, mime, size, tenant_id, config_snapshot)


async def gpu_ingest_dispatch(
    ctx: dict,
    *,
    document_id: str,
    mime: str,
    size: int,
    tenant_id: str,
    config_snapshot: dict,
) -> dict:
    _job_kwargs = {
        "document_id": document_id, "mime": mime, "size": size,
        "tenant_id": tenant_id, "config_snapshot": config_snapshot,
    }
    t_id = uuid.UUID(tenant_id)
    handler_cls = registry.resolve(mime)
    if handler_cls is None:
        async with tenant_session(t_id) as db:
            doc = await db.get(Document, uuid.UUID(document_id))
            if doc:
                await _fail_document(db, doc, f"No handler for MIME type: {mime}", _job_kwargs)
        return {"status": "error", "reason": "no_handler"}

    return await _run_ingest(ctx, handler_cls, document_id, mime, size, tenant_id, config_snapshot)


async def _run_ingest(
    ctx: dict,
    handler_cls: type,
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
    job_kwargs = {
        "document_id": document_id, "mime": mime, "size": size,
        "tenant_id": tenant_id, "config_snapshot": config_snapshot,
    }

    logger.info("ingest.dispatch.received", document_id=document_id, mime=mime)

    async with tenant_session(t_id) as db:
        doc = await db.get(Document, doc_id)
        if doc is None:
            logger.error("ingest.dispatch.doc_not_found", document_id=document_id)
            return {"status": "error", "reason": "document_not_found"}

        if doc.status in ("indexed", "failed", "extracting", "enriching"):
            logger.info(
                "ingest.dispatch.already_processing_or_done",
                document_id=document_id,
                status=doc.status,
            )
            return {"status": doc.status, "document_id": document_id, "reason": "already_processed"}

        # M-2: Read tenant routing policy from config
        tenant = await db.get(Tenant, t_id)
        tenant_policy = (tenant.config or {}).get("routing_policy") if tenant else None

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

        # Atomic claim: only one worker can transition from queued/routing → extracting.
        claim = await db.execute(
            update(Document)
            .where(Document.id == doc_id)
            .where(Document.status.in_(["queued", "routing"]))
            .values(
                status="extracting",
                handler_name=handler.name,
                handler_version=handler.version,
            )
            .returning(Document.id)
        )
        if claim.fetchone() is None:
            logger.info("ingest.dispatch.claim_lost", document_id=document_id)
            return {"status": "extracting", "document_id": document_id, "reason": "already_claimed"}
        await db.commit()

        try:
            result = await handler.extract(blob, ingest_ctx)
        except Exception as exc:
            logger.exception("handler.extract.failed", document_id=document_id, handler=handler.name)
            await _fail_document(db, doc, str(exc), job_kwargs)
            return {"status": "error", "reason": str(exc)}

        doc.status = "enriching"
        await db.commit()

        chunks = chunk_result(result, t_id)

        # Language detection
        for chunk in chunks:
            if chunk.language is None:
                chunk.language = detect_language(chunk.content)

        # M-1/M-3: Compute routing per chunk, store sinks and matched_rule_id
        chunk_sinks: list[frozenset[str]] = []
        chunk_rule_ids: list[str | None] = []
        for chunk in chunks:
            sinks, rule_id = evaluate_policy(chunk.kind, chunk.confidence, doc.doc_metadata, tenant_policy)
            chunk_sinks.append(sinks)
            chunk_rule_ids.append(rule_id)

        # Persist chunks with sinks/matched_rule_id — no embeddings yet
        chunk_rows: list[ChunkRow] = []
        for chunk, sinks, rule_id in zip(chunks, chunk_sinks, chunk_rule_ids):
            row = ChunkRow(
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
                embedding=None,
                embedding_model=None,
                sinks=list(sinks),
                matched_rule_id=rule_id,
            )
            db.add(row)
            chunk_rows.append(row)

        # Persist structured tables (ExtractedRow now includes tenant_id)
        for table in result.tables:
            et = ExtractedTable(
                document_id=doc_id,
                tenant_id=t_id,
                name=table.name,
                schema={"headers": table.headers},
                row_count=len(table.rows),
            )
            db.add(et)
            await db.flush()
            for i, row in enumerate(table.rows):
                db.add(ExtractedRow(table_id=et.id, ordinal=i, data=row, tenant_id=t_id))

        # NER — non-fatal
        try:
            entities = extract_entities(chunks)
            for ent in entities:
                db.add(EntityRow(
                    document_id=doc_id,
                    tenant_id=t_id,
                    label=ent.label,
                    value=ent.value,
                    normalized=ent.normalized,
                    confidence=ent.confidence,
                    chunk_id=None,
                ))
        except Exception:
            logger.warning("ner.failed", document_id=document_id, exc_info=True)

        # LLM summarization — non-fatal
        api_key = settings.ANTHROPIC_API_KEY.get_secret_value() if settings.ANTHROPIC_API_KEY else None
        summary = await summarize_document(chunks, api_key=api_key, filename=doc.filename)

        # Routing summary for document-level audit
        routing_decision = _compute_routing_decision(chunks, chunk_sinks, tenant_policy)

        doc.status = "indexed"
        doc.indexed_at = datetime.now(UTC)
        doc.doc_metadata = {
            **doc.doc_metadata,
            **result.metadata,
            **({"summary": summary.to_dict()} if summary else {}),
        }
        doc.routing_decision = routing_decision
        await db.commit()

        # M-1: Only embed chunks that are routed to the vector sink
        try:
            vectors_to_embed = [
                (i, chunk) for i, (chunk, sinks) in enumerate(zip(chunks, chunk_sinks))
                if "vector" in sinks
            ]
            if vectors_to_embed:
                idxs, vec_chunks = zip(*vectors_to_embed)
                vectors = await embed_chunks(list(vec_chunks), redis=ctx.get("redis"))
                for idx, vector in zip(idxs, vectors):
                    chunk_rows[idx].embedding = vector
                    chunk_rows[idx].embedding_model = EMBEDDING_MODEL
                await db.commit()
        except Exception:
            logger.warning(
                "embeddings.failed_chunks_indexed_without_vectors",
                document_id=document_id,
                chunks=len(chunk_rows),
            )

    logger.info(
        "ingest.dispatch.complete",
        document_id=document_id,
        chunks=len(chunks),
        tables=len(result.tables),
        handler=handler.name,
    )
    await wf_ctx.complete()
    return {"status": "indexed", "document_id": document_id, "chunks": len(chunks)}


async def outbox_relay(ctx: dict) -> dict:
    """Drain unpublished outbox entries into ARQ. outbox has no RLS — uses admin_session."""
    redis = ctx["redis"]

    async with admin_session() as db:
        rows = (
            await db.scalars(
                select(Outbox)
                .where(Outbox.published_at.is_(None))
                .order_by(Outbox.id)
                .limit(50)
                .with_for_update(skip_locked=True)
            )
        ).all()

        if not rows:
            return {"published": 0}

        published = 0
        for row in rows:
            task = row.payload.get("task", "ingest_dispatch")
            kwargs = row.payload.get("kwargs", {})
            try:
                await redis.enqueue_job(task, **kwargs)
                row.published_at = datetime.now(UTC)
                published += 1
            except Exception:
                logger.warning("outbox_relay.enqueue_failed", outbox_id=row.id)

        await db.commit()

    logger.info("outbox_relay.complete", published=published)
    return {"published": published}


async def on_startup(ctx: dict) -> None:
    get_settings()
    registry.discover()
    from omnivore.pipeline.embeddings import _get_model
    from omnivore.pipeline.enrichers.language import _detector, detect_language
    from omnivore.pipeline.enrichers.ner import _nlp
    _get_model()
    _nlp()
    _detector()
    detect_language("warmup text for lingua n-gram loading " * 4)
    logger.info("worker.startup", handlers=len(registry.all_handlers()))


async def on_shutdown(ctx: dict) -> None:
    logger.info("worker.shutdown")


def _compute_routing_decision(
    chunks: list[Chunk],
    chunk_sinks: list[frozenset[str]],
    tenant_policy: dict | None,
) -> dict:
    """Aggregate per-chunk routing decisions into a document-level summary."""
    sink_counts: dict[str, int] = {}
    for sinks in chunk_sinks:
        for sink in sinks:
            sink_counts[sink] = sink_counts.get(sink, 0) + 1
    policy_name = "tenant" if tenant_policy is not None else "default"
    return {"policy": policy_name, "sink_counts": sink_counts}


async def _fail_document(db, doc: Document, reason: str, job_kwargs: dict | None = None) -> None:
    doc.status = "failed"
    doc.error = {
        "reason": reason,
        **({"retry_payload": {"task": "ingest_dispatch", "kwargs": job_kwargs}} if job_kwargs else {}),
    }
    await db.commit()


def _parse_storage_uri(uri: str) -> tuple[str, str]:
    without_scheme = uri.removeprefix("s3://")
    bucket, _, key = without_scheme.partition("/")
    return bucket, key
