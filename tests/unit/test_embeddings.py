from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnivore.pipeline.embeddings import (
    _BATCH_SIZE,
    EMBEDDING_DIM,
    QUERY_PREFIX,
    _cache_key,
    _decode_vec,
    _encode_vec,
    embed_chunks,
    embed_texts,
)

# Convenience: a fake vector of the right dimension.
# Uses integers-as-floats so values survive float32 round-trip exactly.
_VEC = [float(i % 128) for i in range(EMBEDDING_DIM)]


def _patch_embed(return_vecs: list[list[float]] | None = None):
    """Patch the sync model call; default returns a single _VEC."""
    vecs = return_vecs if return_vecs is not None else [_VEC]
    return patch("omnivore.pipeline.embeddings._embed_sync", return_value=vecs)


def _make_redis(cache_hit: bytes | None = None) -> tuple[AsyncMock, AsyncMock]:
    """AsyncMock redis with pipeline support.

    pipeline() is a SYNC call in the real redis client — use MagicMock so
    `async with redis.pipeline(...) as pipe:` works correctly in tests.
    """
    redis = AsyncMock()
    redis.get.return_value = cache_hit
    # Pipeline commands (set, get, …) are sync in redis-py; only execute is awaitable.
    mock_pipe = MagicMock()
    mock_pipe.execute = AsyncMock(return_value=[])
    pipeline_cm = MagicMock()
    pipeline_cm.__aenter__ = AsyncMock(return_value=mock_pipe)
    pipeline_cm.__aexit__ = AsyncMock(return_value=False)
    redis.pipeline = MagicMock(return_value=pipeline_cm)
    return redis, mock_pipe


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------


async def test_embed_texts_empty_returns_empty():
    result = await embed_texts([])
    assert result == []


# ---------------------------------------------------------------------------
# Redis cache hit — model must NOT be called
# ---------------------------------------------------------------------------


async def test_embed_texts_all_cache_hits():
    cached = [0.5] * EMBEDDING_DIM  # 0.5 is exact in float32
    redis, _ = _make_redis(cache_hit=_encode_vec(cached))

    with _patch_embed() as mock_sync:
        result = await embed_texts(["hello"], redis)

    mock_sync.assert_not_called()
    assert result[0] == pytest.approx(cached, abs=1e-6)


async def test_embed_texts_partial_cache_hit():
    """First text cached, second is a miss → model called with only the miss."""
    cached_vec = [1.0] + [0.0] * (EMBEDDING_DIM - 1)  # exact in float32
    miss_vec = [0.0] * (EMBEDDING_DIM - 1) + [1.0]

    async def fake_get(key):
        return _encode_vec(cached_vec) if key == _cache_key("cached") else None

    redis, _ = _make_redis()
    redis.get.side_effect = fake_get

    with _patch_embed([miss_vec]) as mock_sync:
        result = await embed_texts(["cached", "miss"], redis)

    called_with = mock_sync.call_args[0][0]
    assert called_with == ["miss"]
    assert result[0] == pytest.approx(cached_vec, abs=1e-5)
    assert result[1] == miss_vec


# ---------------------------------------------------------------------------
# Model call + cache write (pipeline)
# ---------------------------------------------------------------------------


async def test_embed_texts_calls_model_and_caches():
    redis, mock_pipe = _make_redis()

    with _patch_embed([_VEC]):
        result = await embed_texts(["hello"], redis)

    assert result == [_VEC]
    redis.pipeline.assert_called_once()
    mock_pipe.set.assert_called_once()
    key_arg, value_arg = mock_pipe.set.call_args[0]
    assert key_arg == _cache_key("hello")
    assert _decode_vec(value_arg) == pytest.approx(_VEC, abs=1e-4)


async def test_embed_texts_no_redis_skips_cache():
    with _patch_embed([_VEC]) as mock_sync:
        result = await embed_texts(["hello"])

    mock_sync.assert_called_once()
    assert result == [_VEC]


# ---------------------------------------------------------------------------
# Batching
# ---------------------------------------------------------------------------


async def test_embed_texts_batches_large_input():
    """Input larger than BATCH_SIZE must be processed in a single _embed_sync call."""
    n = _BATCH_SIZE + 5
    vecs = [[float(i % 128)] * EMBEDDING_DIM for i in range(n)]

    with _patch_embed(vecs) as mock_sync:
        result = await embed_texts([f"t{i}" for i in range(n)])

    mock_sync.assert_called_once()
    assert len(result) == n
    assert result[0] == vecs[0]
    assert result[-1] == vecs[-1]


# ---------------------------------------------------------------------------
# embed_chunks delegates to embed_texts
# ---------------------------------------------------------------------------


async def test_embed_chunks_uses_chunk_content():
    class _FakeChunk:
        def __init__(self, text: str):
            self.content = text

    with _patch_embed([[0.0] * EMBEDDING_DIM, [1.0] + [0.0] * (EMBEDDING_DIM - 1)]) as mock_sync:
        result = await embed_chunks([_FakeChunk("alpha"), _FakeChunk("beta")])  # type: ignore[arg-type]

    assert mock_sync.call_args[0][0] == ["alpha", "beta"]
    assert len(result) == 2


# ---------------------------------------------------------------------------
# Query prefix
# ---------------------------------------------------------------------------


async def test_embed_texts_query_prefix_is_applied():
    """When query_prefix is given, the prefixed text is what gets embedded and cached."""
    prefix = "prefix: "
    redis, mock_pipe = _make_redis()

    with _patch_embed([_VEC]) as mock_sync:
        result = await embed_texts(["hello"], redis, query_prefix=prefix)

    assert result == [_VEC]
    assert mock_sync.call_args[0][0] == [prefix + "hello"]
    key_arg = mock_pipe.set.call_args[0][0]
    assert key_arg == _cache_key(prefix + "hello")


# ---------------------------------------------------------------------------
# Cache key properties
# ---------------------------------------------------------------------------


def test_cache_key_stable():
    assert _cache_key("hello") == _cache_key("hello")


def test_cache_key_differs_by_content():
    assert _cache_key("hello") != _cache_key("world")


def test_cache_key_format():
    key = _cache_key("test")
    assert key.startswith("emb:v2:")
    assert len(key) == len("emb:v2:") + 64  # sha256 hex digest


def test_cache_key_changes_with_model(monkeypatch):
    original_key = _cache_key("text")
    monkeypatch.setattr("omnivore.pipeline.embeddings.EMBEDDING_MODEL", "different/model")
    assert _cache_key("text") != original_key


def test_cache_key_differs_with_query_prefix():
    """Query and passage embeddings for the same text must not share a cache entry."""
    assert _cache_key(QUERY_PREFIX + "hello") != _cache_key("hello")


# ---------------------------------------------------------------------------
# Binary encode/decode round-trip
# ---------------------------------------------------------------------------


def test_encode_decode_round_trip():
    v = [float(i % 128) for i in range(EMBEDDING_DIM)]
    assert _decode_vec(_encode_vec(v)) == pytest.approx(v, abs=1e-4)


def test_encoded_size():
    v = [0.0] * EMBEDDING_DIM
    assert len(_encode_vec(v)) == EMBEDDING_DIM * 4  # 4 bytes per float32
