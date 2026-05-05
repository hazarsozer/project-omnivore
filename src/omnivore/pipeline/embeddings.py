from __future__ import annotations

import asyncio
import hashlib
import json

import structlog

from omnivore.pipeline.models import Chunk

logger = structlog.get_logger(__name__)

EMBEDDING_MODEL = "BAAI/bge-base-en-v1.5"
EMBEDDING_DIM = 768
_BATCH_SIZE = 64
_CACHE_TTL = 86400 * 7  # 7 days

_st_model = None


def _get_model():
    global _st_model
    if _st_model is None:
        from sentence_transformers import SentenceTransformer

        _st_model = SentenceTransformer(EMBEDDING_MODEL)
        logger.info("embeddings.model_loaded", model=EMBEDDING_MODEL)
    return _st_model


def _embed_sync(texts: list[str]) -> list[list[float]]:
    model = _get_model()
    vecs = model.encode(texts, batch_size=_BATCH_SIZE, normalize_embeddings=True, show_progress_bar=False)
    return [v.tolist() for v in vecs]


async def embed_texts(texts: list[str], redis=None) -> list[list[float]]:
    """Embed texts with BGE-base. Caches in Redis by sha256(text+model), 7-day TTL."""
    if not texts:
        return []

    results: list[list[float] | None] = [None] * len(texts)
    misses: list[tuple[int, str]] = []

    for i, text in enumerate(texts):
        if redis is not None:
            cached = await redis.get(_cache_key(text))
            if cached is not None:
                results[i] = json.loads(cached)
                continue
        misses.append((i, text))

    if misses:
        miss_indices, miss_texts = zip(*misses)
        loop = asyncio.get_running_loop()
        vectors = await loop.run_in_executor(None, _embed_sync, list(miss_texts))

        for orig_idx, vector in zip(miss_indices, vectors):
            results[orig_idx] = vector
            if redis is not None:
                await redis.set(_cache_key(texts[orig_idx]), json.dumps(vector), ex=_CACHE_TTL)

        logger.info("embeddings.complete", count=len(misses), model=EMBEDDING_MODEL)

    return results  # type: ignore[return-value]  # all slots filled above


async def embed_chunks(chunks: list[Chunk], redis=None) -> list[list[float]]:
    return await embed_texts([c.content for c in chunks], redis)


def _cache_key(text: str) -> str:
    digest = hashlib.sha256(f"{text}\x00{EMBEDDING_MODEL}".encode()).hexdigest()
    return f"emb:v1:{digest}"
