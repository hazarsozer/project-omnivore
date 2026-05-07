from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Any

import tiktoken

from omnivore.pipeline.models import Chunk, ExtractionResult, Fragment

_ENC = tiktoken.get_encoding("cl100k_base")
DEFAULT_MAX_TOKENS = 512
DEFAULT_OVERLAP_TOKENS = 64


def count_tokens(text: str) -> int:
    return len(_ENC.encode(text))


def chunk_result(
    result: ExtractionResult,
    tenant_id: uuid.UUID,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[Chunk]:
    if result.blocks:
        return _chunk_structured(result, tenant_id, max_tokens, overlap_tokens)
    return _chunk_flat(result, tenant_id, max_tokens, overlap_tokens)


def _chunk_structured(
    result: ExtractionResult,
    tenant_id: uuid.UUID,
    max_tokens: int,
    overlap_tokens: int,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    ordinal = 0
    bucket: list[Fragment] = []
    bucket_tokens = 0
    prev_heading_path: list[str] = []

    def flush() -> None:
        nonlocal ordinal, bucket, bucket_tokens
        if not bucket:
            return
        content = " ".join(f.content for f in bucket)
        chunks.append(
            Chunk(
                document_id=result.document_id,
                tenant_id=tenant_id,
                ordinal=ordinal,
                kind=bucket[0].kind,
                content=content,
                token_count=count_tokens(content),
                position=_pos_to_dict(bucket[0].position),
                heading_path=bucket[0].heading_path,
                source_block_ids=[bid for f in bucket for bid in f.source_block_ids],
                table_lineage=bucket[-1].table_lineage,
                language=bucket[0].language,
                confidence=_min_confidence(bucket),
            )
        )
        ordinal += 1
        # carry overlap
        overlap_frags: list[Fragment] = []
        acc = 0
        for f in reversed(bucket):
            t = count_tokens(f.content)
            if acc + t > overlap_tokens:
                break
            overlap_frags.insert(0, f)
            acc += t
        bucket = overlap_frags
        bucket_tokens = acc

    for fragment in result.fragments:
        if not fragment.content.strip():
            continue
        section_changed = fragment.heading_path != prev_heading_path
        frag_tokens = count_tokens(fragment.content)

        if section_changed and bucket:
            flush()

        if bucket_tokens + frag_tokens > max_tokens and bucket:
            flush()

        bucket.append(fragment)
        bucket_tokens += frag_tokens
        prev_heading_path = fragment.heading_path

    flush()
    return chunks


def _chunk_flat(
    result: ExtractionResult,
    tenant_id: uuid.UUID,
    max_tokens: int,
    overlap_tokens: int,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    ordinal = 0
    bucket: list[Fragment] = []
    bucket_tokens = 0

    def flush() -> None:
        nonlocal ordinal, bucket, bucket_tokens
        if not bucket:
            return
        content = " ".join(f.content for f in bucket)
        chunks.append(
            Chunk(
                document_id=result.document_id,
                tenant_id=tenant_id,
                ordinal=ordinal,
                kind=bucket[0].kind,
                content=content,
                token_count=count_tokens(content),
                position=_pos_to_dict(bucket[0].position),
                heading_path=bucket[0].heading_path,
                source_block_ids=[bid for f in bucket for bid in f.source_block_ids],
                language=bucket[0].language,
                confidence=_min_confidence(bucket),
            )
        )
        ordinal += 1
        overlap_frags: list[Fragment] = []
        acc = 0
        for f in reversed(bucket):
            t = count_tokens(f.content)
            if acc + t > overlap_tokens:
                break
            overlap_frags.insert(0, f)
            acc += t
        bucket = overlap_frags
        bucket_tokens = acc

    for fragment in result.fragments:
        if not fragment.content.strip():
            continue
        frag_tokens = count_tokens(fragment.content)
        if bucket_tokens + frag_tokens > max_tokens and bucket:
            flush()
        bucket.append(fragment)
        bucket_tokens += frag_tokens

    flush()
    return chunks


def _pos_to_dict(position: Any) -> dict:
    if isinstance(position, dict):
        return position
    try:
        return asdict(position)
    except TypeError:
        return {}


def _min_confidence(frags: list[Fragment]) -> float | None:
    vals = [f.confidence for f in frags if f.confidence is not None]
    return min(vals) if vals else None
