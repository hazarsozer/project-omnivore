from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from omnivore.pipeline.enrichers.summarizer import (
    DocumentSummary,
    _build_content,
    _strip_fences,
    summarize_document,
)
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


def _settings(provider: str = "anthropic") -> MagicMock:
    s = MagicMock()
    s.LLM_PROVIDER = provider
    s.LLM_TEXT_MODEL = ""
    return s


# ---------------------------------------------------------------------------
# DocumentSummary
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# _build_content
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# _strip_fences
# ---------------------------------------------------------------------------


class TestStripFences:
    def test_plain_json_unchanged(self):
        raw = '{"title": "T"}'
        assert _strip_fences(raw) == raw

    def test_strips_json_fence(self):
        raw = '```json\n{"title": "T"}\n```'
        assert _strip_fences(raw) == '{"title": "T"}'

    def test_strips_plain_fence(self):
        raw = '```\n{"title": "T"}\n```'
        assert _strip_fences(raw) == '{"title": "T"}'

    def test_strips_whitespace(self):
        raw = '  {"title": "T"}  '
        assert _strip_fences(raw) == '{"title": "T"}'


# ---------------------------------------------------------------------------
# summarize_document
# ---------------------------------------------------------------------------

_VALID_JSON = (
    '{"title": "PostgreSQL Overview", "abstract": "A database system.",'
    ' "key_points": ["open source"], "topics": ["databases"]}'
)


class TestSummarizeDocument:
    async def test_returns_none_for_empty_chunks(self):
        result = await summarize_document([], settings=_settings())
        assert result is None

    async def test_successful_call(self):
        chunks = [_make_chunk("PostgreSQL is a powerful open source database.")]

        with patch(
            "omnivore.pipeline.enrichers.summarizer.complete_text",
            new_callable=AsyncMock,
            return_value=_VALID_JSON,
        ):
            result = await summarize_document(chunks, settings=_settings(), filename="test.txt")

        assert result is not None
        assert result.title == "PostgreSQL Overview"
        assert result.abstract == "A database system."
        assert "open source" in result.key_points
        assert "databases" in result.topics

    async def test_strips_markdown_fences_from_response(self):
        """Models like Gemini/Ollama sometimes wrap JSON in ```json fences."""
        chunks = [_make_chunk("Content.")]
        fenced = f"```json\n{_VALID_JSON}\n```"

        with patch(
            "omnivore.pipeline.enrichers.summarizer.complete_text",
            new_callable=AsyncMock,
            return_value=fenced,
        ):
            result = await summarize_document(chunks, settings=_settings())

        assert result is not None
        assert result.title == "PostgreSQL Overview"

    async def test_returns_none_when_complete_text_returns_none(self):
        chunks = [_make_chunk("Some content")]

        with patch(
            "omnivore.pipeline.enrichers.summarizer.complete_text",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await summarize_document(chunks, settings=_settings())

        assert result is None

    async def test_returns_none_on_invalid_json(self):
        chunks = [_make_chunk("Some content")]

        with patch(
            "omnivore.pipeline.enrichers.summarizer.complete_text",
            new_callable=AsyncMock,
            return_value="not valid json {",
        ):
            result = await summarize_document(chunks, settings=_settings())

        assert result is None

    async def test_passes_settings_to_complete_text(self):
        chunks = [_make_chunk("Content.")]
        captured: list[MagicMock] = []

        async def _capture(prompt, *, settings):
            captured.append(settings)
            return _VALID_JSON

        with patch("omnivore.pipeline.enrichers.summarizer.complete_text", side_effect=_capture):
            s = _settings(provider="ollama")
            await summarize_document(chunks, settings=s)

        assert len(captured) == 1
        assert captured[0] is s
