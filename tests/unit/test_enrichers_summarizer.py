from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from anthropic.types import TextBlock

from omnivore.pipeline.enrichers.summarizer import DocumentSummary, _build_content, summarize_document
from omnivore.pipeline.models import Chunk


def _make_chunk(content: str, kind: str = "text", ordinal: int = 0) -> Chunk:
    return Chunk(
        document_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        ordinal=ordinal,
        kind=kind,
        content=content,
        token_count=len(content.split()),
        position={},
        heading_path=[],
        source_block_ids=[],
    )


class TestDocumentSummary:
    def test_to_dict(self):
        s = DocumentSummary(
            title="Test Doc",
            abstract="A test document.",
            key_points=["point 1"],
            topics=["testing"],
        )
        d = s.to_dict()
        assert d["title"] == "Test Doc"
        assert d["abstract"] == "A test document."
        assert d["key_points"] == ["point 1"]
        assert d["topics"] == ["testing"]


class TestBuildContent:
    def test_empty_chunks(self):
        assert _build_content([]) == ""

    def test_single_chunk(self):
        chunk = _make_chunk("Hello world")
        result = _build_content([chunk])
        assert result == "Hello world"

    def test_truncates_at_max_chars(self):
        chunk = _make_chunk("a" * 100)
        result = _build_content([chunk], max_chars=50)
        assert len(result) == 50

    def test_joins_multiple_chunks(self):
        chunks = [_make_chunk("first"), _make_chunk("second")]
        result = _build_content(chunks)
        assert "first" in result
        assert "second" in result


class TestSummarizeDocument:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_api_key(self):
        chunks = [_make_chunk("Some content")]
        result = await summarize_document(chunks, api_key=None)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_chunks(self):
        # Even with key, empty content → None
        result = await summarize_document([], api_key="fake-key")
        assert result is None

    @pytest.mark.asyncio
    async def test_successful_call(self):
        chunks = [_make_chunk("PostgreSQL is a powerful open source database.")]
        fake_response_json = (
            '{"title": "PostgreSQL Overview", "abstract": "A database system.",'
            ' "key_points": ["open source"], "topics": ["databases"]}'
        )

        mock_message = MagicMock()
        mock_message.content = [TextBlock(type="text", text=fake_response_json)]

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=mock_message)

        with patch("omnivore.pipeline.enrichers.summarizer.AsyncAnthropic", return_value=mock_client):
            result = await summarize_document(chunks, api_key="test-key", filename="test.txt")

        assert result is not None
        assert result.title == "PostgreSQL Overview"
        assert result.abstract == "A database system."
        assert "open source" in result.key_points
        assert "databases" in result.topics

    @pytest.mark.asyncio
    async def test_returns_none_on_api_error(self):
        chunks = [_make_chunk("Some content")]

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=Exception("API error"))

        with patch("omnivore.pipeline.enrichers.summarizer.AsyncAnthropic", return_value=mock_client):
            result = await summarize_document(chunks, api_key="test-key")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_text_block(self):
        """H-1 regression: if content[0] is ThinkingBlock/ToolUseBlock, return None safely."""
        chunks = [_make_chunk("Some content")]

        # Plain MagicMock is not a TextBlock instance — simulates ThinkingBlock
        non_text_block = MagicMock()
        mock_message = MagicMock()
        mock_message.content = [non_text_block]

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=mock_message)

        with patch("omnivore.pipeline.enrichers.summarizer.AsyncAnthropic", return_value=mock_client):
            result = await summarize_document(chunks, api_key="test-key")

        assert result is None

    @pytest.mark.asyncio
    async def test_uses_first_text_block_when_thinking_comes_first(self):
        """TextBlock filter skips non-text blocks and uses the first text block."""
        chunks = [_make_chunk("Some content")]

        non_text_block = MagicMock()  # not a TextBlock — simulates ThinkingBlock
        real_text_block = TextBlock(
            type="text",
            text='{"title": "T", "abstract": "A.", "key_points": ["k"], "topics": ["t"]}',
        )
        mock_message = MagicMock()
        mock_message.content = [non_text_block, real_text_block]

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=mock_message)

        with patch("omnivore.pipeline.enrichers.summarizer.AsyncAnthropic", return_value=mock_client):
            result = await summarize_document(chunks, api_key="test-key")

        assert result is not None
        assert result.title == "T"

    @pytest.mark.asyncio
    async def test_returns_none_on_invalid_json(self):
        chunks = [_make_chunk("Some content")]

        mock_message = MagicMock()
        mock_message.content = [TextBlock(type="text", text="not valid json {")]

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=mock_message)

        with patch("omnivore.pipeline.enrichers.summarizer.AsyncAnthropic", return_value=mock_client):
            result = await summarize_document(chunks, api_key="test-key")

        assert result is None
