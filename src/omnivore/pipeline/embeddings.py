from __future__ import annotations

import asyncio
import hashlib
import struct
import threading
import time
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

from omnivore.observability import EMBEDDINGS_TOTAL, get_tracer
from omnivore.pipeline.models import Chunk

logger = structlog.get_logger(__name__)

EMBEDDING_MODEL = "BAAI/bge-base-en-v1.5"
EMBEDDING_DIM = 768
_BATCH_SIZE = 64
_CACHE_TTL = 86400 * 7  # 7 days

# BGE asymmetric retrieval prefix — apply to queries only, not to passages.
# See: https://huggingface.co/BAAI/bge-base-en-v1.5#usage
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

# Double-checked locking prevents race on first load; device="cpu" avoids
# accidental GPU usage and CUDA context conflicts across executor threads.
_model_lock = threading.Lock()
_st_model: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    global _st_model
    if _st_model is None:
        with _model_lock:
            if _st_model is None:
                from sentence_transformers import SentenceTransformer
                _st_model = SentenceTransformer(EMBEDDING_MODEL, device="cpu")
                logger.info("embeddings.model_loaded", model=EMBEDDING_MODEL)
    return _st_model


def _encode_vec(v: list[float]) -> bytes:
    """Pack a float-list as binary float32 (3 KB vs ~10 KB JSON for 768-dim)."""
    return struct.pack(f"{len(v)}f", *v)


def _decode_vec(b: bytes) -> list[float]:
    return list(struct.unpack(f"{len(b) // 4}f", b))


def _embed_sync(texts: list[str]) -> list[list[float]]:
    model = _get_model()
    vecs = model.encode(texts, batch_size=_BATCH_SIZE, normalize_embeddings=True, show_progress_bar=False)
    return [v.tolist() for v in vecs]


async def embed_texts(texts: list[str], redis=None, query_prefix: str = "") -> list[list[float]]:
    """Embed texts with BGE-base. Caches in Redis by sha256(effective_text+model), 7-day TTL.

    Pass query_prefix=QUERY_PREFIX when embedding search queries; leave empty for passages.
    The prefix is included in the cache key, so query and passage entries never collide.
    """
    if not texts:
        return []

    # Apply prefix to produce the actual strings we embed and hash.
    effective = [query_prefix + t for t in texts] if query_prefix else texts

    results: list[list[float] | None] = [None] * len(effective)
    misses: list[tuple[int, str]] = []

    for i, eff in enumerate(effective):
        if redis is not None:
            cached = await redis.get(_cache_key(eff))
            if cached is not None:
                results[i] = _decode_vec(cached)
                continue
        misses.append((i, eff))

    if misses:
        t0 = time.monotonic()
        miss_indices, miss_texts = zip(*misses)
        loop = asyncio.get_running_loop()
        vectors = await loop.run_in_executor(None, _embed_sync, list(miss_texts))

        for orig_idx, vector in zip(miss_indices, vectors):
            results[orig_idx] = vector

        if redis is not None:
            async with redis.pipeline(transaction=False) as pipe:
                for orig_idx, vector in zip(miss_indices, vectors):
                    pipe.set(_cache_key(effective[orig_idx]), _encode_vec(vector), ex=_CACHE_TTL)
                await pipe.execute()

        duration_ms = int((time.monotonic() - t0) * 1000)
        logger.info("embeddings.complete", count=len(misses), model=EMBEDDING_MODEL, duration_ms=duration_ms)

    return results  # type: ignore[return-value]  # all slots filled above


async def _embed_chunks_impl(chunks: list[Chunk], redis=None) -> list[list[float]]:
    return await embed_texts([c.content for c in chunks], redis)


async def embed_chunks(chunks: list[Chunk], redis=None) -> list[list[float]]:
    with get_tracer().start_as_current_span(
        "omnivore.embeddings.batch",
        attributes={"chunk_count": len(chunks), "model": EMBEDDING_MODEL},
    ):
        result = await _embed_chunks_impl(chunks, redis)
    EMBEDDINGS_TOTAL.labels(model=EMBEDDING_MODEL).inc(len(chunks))
    return result


def _cache_key(text: str) -> str:
    digest = hashlib.sha256(f"{text}\x00{EMBEDDING_MODEL}".encode()).hexdigest()
    return f"emb:v2:{digest}"  # v2 = binary float32 encoding (v1 was JSON)
