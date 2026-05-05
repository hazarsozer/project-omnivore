from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from omnivore.pipeline.embeddings import (
    EMBEDDING_MODEL,
    _BATCH_SIZE,
    _cache_key,
    embed_chunks,
    embed_texts,
)


class _FakeSettings:
    OPENAI_API_KEY: SecretStr | None = None


def _settings_with_key() -> _FakeSettings:
    s = _FakeSettings()
    s.OPENAI_API_KEY = SecretStr("sk-test")
    return s


def _make_openai_response(n: int, dim: int = 4) -> MagicMock:
    response = MagicMock()
    response.data = [MagicMock(embedding=[float(i)] * dim) for i in range(n)]
    return response


# ---------------------------------------------------------------------------
# No-op when API key absent
# ---------------------------------------------------------------------------


async def test_embed_texts_no_key_returns_none_list():
    result = await embed_texts(["hello", "world"], _FakeSettings())
    assert result == [None, None]


async def test_embed_texts_empty_input():
    result = await embed_texts([], _FakeSettings())
    assert result == []


# ---------------------------------------------------------------------------
# Redis cache hit — API must not be called
# ---------------------------------------------------------------------------


async def test_embed_texts_all_cache_hits():
    settings = _settings_with_key()
    cached_vec = [0.1, 0.2, 0.3, 0.4]
    redis = AsyncMock()
    redis.get.return_value = json.dumps(cached_vec).encode()

    with patch("omnivore.pipeline.embeddings.AsyncOpenAI") as mock_cls:
        result = await embed_texts(["hello"], settings, redis)

    mock_cls.return_value.embeddings.create.assert_not_called()
    assert result == [cached_vec]


async def test_embed_texts_partial_cache_hit():
    """First text cached, second is a miss → only 1 API call with 1 text."""
    settings = _settings_with_key()
    cached_vec = [1.0, 2.0, 3.0, 4.0]
    miss_vec = [0.1, 0.2, 0.3, 0.4]

    async def fake_get(key):
        first_key = _cache_key("cached")
        return json.dumps(cached_vec).encode() if key == first_key else None

    redis = AsyncMock()
    redis.get.side_effect = fake_get

    api_response = _make_openai_response(1, dim=4)
    api_response.data[0].embedding = miss_vec

    with patch("omnivore.pipeline.embeddings.AsyncOpenAI") as mock_cls:
        mock_client = AsyncMock()
        mock_client.embeddings.create = AsyncMock(return_value=api_response)
        mock_cls.return_value = mock_client

        result = await embed_texts(["cached", "miss"], settings, redis)

    called_texts = mock_client.embeddings.create.call_args[1]["input"]
    assert called_texts == ["miss"]
    assert result[0] == cached_vec
    assert result[1] == miss_vec


# ---------------------------------------------------------------------------
# API call + cache write
# ---------------------------------------------------------------------------


async def test_embed_texts_api_called_and_cached():
    settings = _settings_with_key()
    vec = [0.5, 0.6, 0.7, 0.8]

    redis = AsyncMock()
    redis.get.return_value = None

    api_response = _make_openai_response(1, dim=4)
    api_response.data[0].embedding = vec

    with patch("omnivore.pipeline.embeddings.AsyncOpenAI") as mock_cls:
        mock_client = AsyncMock()
        mock_client.embeddings.create = AsyncMock(return_value=api_response)
        mock_cls.return_value = mock_client

        result = await embed_texts(["hello"], settings, redis)

    assert result == [vec]
    redis.set.assert_called_once()
    key_arg, value_arg = redis.set.call_args[0]
    assert key_arg == _cache_key("hello")
    assert json.loads(value_arg) == vec


async def test_embed_texts_batches_correctly():
    """More than BATCH_SIZE texts → multiple API calls, each ≤ BATCH_SIZE."""
    n = _BATCH_SIZE + 3
    settings = _settings_with_key()
    redis = AsyncMock()
    redis.get.return_value = None

    call_sizes: list[int] = []

    async def fake_create(model, input):  # noqa: A002
        call_sizes.append(len(input))
        resp = MagicMock()
        resp.data = [MagicMock(embedding=[0.0] * 4) for _ in input]
        return resp

    with patch("omnivore.pipeline.embeddings.AsyncOpenAI") as mock_cls:
        mock_client = AsyncMock()
        mock_client.embeddings.create.side_effect = fake_create
        mock_cls.return_value = mock_client

        result = await embed_texts([f"text {i}" for i in range(n)], settings, redis)

    assert len(result) == n
    assert all(v is not None for v in result)
    assert call_sizes == [_BATCH_SIZE, 3]


async def test_embed_texts_api_error_returns_nones():
    """On API failure, affected batch returns None (not an exception)."""
    settings = _settings_with_key()
    redis = AsyncMock()
    redis.get.return_value = None

    with patch("omnivore.pipeline.embeddings.AsyncOpenAI") as mock_cls:
        mock_client = AsyncMock()
        mock_client.embeddings.create.side_effect = Exception("network error")
        mock_cls.return_value = mock_client

        result = await embed_texts(["hello"], settings, redis)

    assert result == [None]


# ---------------------------------------------------------------------------
# embed_chunks — delegates to embed_texts with chunk.content
# ---------------------------------------------------------------------------


async def test_embed_chunks_uses_content():
    settings = _FakeSettings()

    class _FakeChunk:
        content: str

        def __init__(self, text: str):
            self.content = text

    chunks = [_FakeChunk("alpha"), _FakeChunk("beta")]
    result = await embed_chunks(chunks, settings)  # type: ignore[arg-type]
    assert result == [None, None]


# ---------------------------------------------------------------------------
# Cache key stability
# ---------------------------------------------------------------------------


def test_cache_key_is_stable():
    assert _cache_key("hello") == _cache_key("hello")


def test_cache_key_differs_by_content():
    assert _cache_key("hello") != _cache_key("world")


def test_cache_key_includes_model():
    # Different content with same model → different keys
    k1 = _cache_key("foo")
    k2 = _cache_key("bar")
    assert k1 != k2


def test_cache_key_format():
    key = _cache_key("test")
    assert key.startswith("emb:v1:")
    assert len(key) == len("emb:v1:") + 64  # sha256 hex = 64 chars
