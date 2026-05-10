from __future__ import annotations

import json
import textwrap
from typing import TYPE_CHECKING

import structlog
from anthropic import AsyncAnthropic
from anthropic.types import TextBlock

if TYPE_CHECKING:
    from omnivore.pipeline.models import Chunk

logger = structlog.get_logger(__name__)

_SUMMARY_MODEL = "claude-haiku-4-5-20251001"
_MAX_INPUT_TOKENS = 3_000  # ~12 000 chars of chunk content sent to the model
_SUMMARY_PROMPT = textwrap.dedent("""\
    You are a document analyst. Given the text excerpts below, produce a concise summary.
    Reply ONLY with a JSON object matching this schema (no markdown, no explanation):
    {{
      "title": "<concise document title>",
      "abstract": "<1-3 sentence summary>",
      "key_points": ["<point 1>", "<point 2>"],
      "topics": ["<topic 1>", "<topic 2>"]
    }}

    Text excerpts:
    ---
    {content}
    ---
""")


class DocumentSummary:
    __slots__ = ("title", "abstract", "key_points", "topics")

    def __init__(self, title: str, abstract: str, key_points: list[str], topics: list[str]) -> None:
        self.title = title
        self.abstract = abstract
        self.key_points = key_points
        self.topics = topics

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "abstract": self.abstract,
            "key_points": self.key_points,
            "topics": self.topics,
        }


async def summarize_document(
    chunks: list[Chunk],
    *,
    api_key: str | None,
    filename: str = "",
) -> DocumentSummary | None:
    """Call Claude Haiku to summarize document content. Returns None if api_key is absent."""
    if not api_key:
        logger.debug("summarizer.skipped", reason="no_api_key", filename=filename)
        return None

    content = _build_content(chunks)
    if not content:
        return None

    client = AsyncAnthropic(api_key=api_key)
    prompt = _SUMMARY_PROMPT.format(content=content)

    try:
        message = await client.messages.create(
            model=_SUMMARY_MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        text_blocks = [b for b in message.content if isinstance(b, TextBlock)]
        if not text_blocks:
            logger.warning("summarizer.no_text_block", filename=filename)
            return None
        raw = text_blocks[0].text.strip()
        data = json.loads(raw)
        return DocumentSummary(
            title=str(data.get("title", "")),
            abstract=str(data.get("abstract", "")),
            key_points=[str(p) for p in data.get("key_points", [])],
            topics=[str(t) for t in data.get("topics", [])],
        )
    except Exception:
        logger.warning("summarizer.failed", filename=filename, exc_info=True)
        return None


def _build_content(chunks: list[Chunk], max_chars: int = _MAX_INPUT_TOKENS * 4) -> str:
    """Concatenate chunk content up to max_chars."""
    parts: list[str] = []
    total = 0
    for chunk in chunks:
        if total >= max_chars:
            break
        text = chunk.content
        remaining = max_chars - total
        parts.append(text[:remaining])
        total += len(text)
    return "\n\n".join(parts)
