from __future__ import annotations

import hashlib
import json

import structlog
from openai import AsyncOpenAI

from omnivore.config import Settings
from omnivore.pipeline.models import Chunk

logger = structlog.get_logger(__name__)

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536
_BATCH_SIZE = 64
_CACHE_TTL = 86400 * 7  # 7 days


async def embed_texts(
    texts: list[str],
    settings: Settings,
    redis=None,
) -> list[list[float] | None]:
    """Embed texts via OpenAI. Returns None per item when API key is absent.

    Uses Redis keyed by sha256(text + model) with a 7-day TTL.
    Batches API calls to BATCH_SIZE=64.
    """
    if not settings.OPENAI_API_KEY:
        logger.warning("embeddings.skipped", reason="OPENAI_API_KEY not set")
        return [None] * len(texts)

    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY.get_secret_value())
    results: list[list[float] | None] = [None] * len(texts)

    # Resolve cache hits; collect misses
    misses: list[tuple[int, str]] = []
    for i, text in enumerate(texts):
        if redis is not None:
            cached = await redis.get(_cache_key(text))
            if cached is not None:
                results[i] = json.loads(cached)
                continue
        misses.append((i, text))

    # Batch-embed misses
    for batch_start in range(0, len(misses), _BATCH_SIZE):
        batch = misses[batch_start : batch_start + _BATCH_SIZE]
        indices, batch_texts = zip(*batch)
        try:
            response = await client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=list(batch_texts),
            )
        except Exception:
            logger.exception("embeddings.api_call_failed", batch_start=batch_start)
            continue

        for batch_i, emb_obj in enumerate(response.data):
            orig_idx = indices[batch_i]
            vector = emb_obj.embedding
            results[orig_idx] = vector
            if redis is not None:
                await redis.set(_cache_key(texts[orig_idx]), json.dumps(vector), ex=_CACHE_TTL)

        logger.info("embeddings.batch_complete", count=len(batch), model=EMBEDDING_MODEL)

    return results


async def embed_chunks(
    chunks: list[Chunk],
    settings: Settings,
    redis=None,
) -> list[list[float] | None]:
    return await embed_texts([c.content for c in chunks], settings, redis)


def _cache_key(text: str) -> str:
    digest = hashlib.sha256(f"{text}\x00{EMBEDDING_MODEL}".encode()).hexdigest()
    return f"emb:v1:{digest}"
