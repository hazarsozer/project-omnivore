from __future__ import annotations

import json
import textwrap
from typing import TYPE_CHECKING

import structlog

from omnivore.pipeline.enrichers.llm_client import complete_text

if TYPE_CHECKING:
    from omnivore.config import Settings
    from omnivore.pipeline.models import Chunk

logger = structlog.get_logger(__name__)

_MAX_INPUT_CHARS = 12_000  # ~3 000 tokens of chunk content sent to the model
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
    settings: Settings,
    filename: str = "",
) -> DocumentSummary | None:
    """Summarize document content via the configured LLM provider.

    Returns None when no provider is configured or content is empty.
    """
    content = _build_content(chunks)
    if not content:
        return None

    prompt = _SUMMARY_PROMPT.format(content=content)
    raw = await complete_text(prompt, settings=settings)
    if not raw:
        logger.debug("summarizer.skipped", reason="no_response", filename=filename)
        return None

    try:
        data = json.loads(_strip_fences(raw))
        return DocumentSummary(
            title=str(data.get("title", "")),
            abstract=str(data.get("abstract", "")),
            key_points=[str(p) for p in data.get("key_points", [])],
            topics=[str(t) for t in data.get("topics", [])],
        )
    except Exception:
        logger.warning("summarizer.parse_failed", filename=filename, exc_info=True)
        return None


def _build_content(chunks: list[Chunk], max_chars: int = _MAX_INPUT_CHARS) -> str:
    parts: list[str] = []
    total = 0
    for chunk in chunks:
        if total >= max_chars:
            break
        text = chunk.content
        remaining = max_chars - total
        parts.append(text[:remaining])
        total += len(text[:remaining])
    return "\n\n".join(parts)


def _strip_fences(text: str) -> str:
    """Strip markdown code fences that some models wrap JSON output in."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        # drop opening fence (```json or ```) and closing fence (```)
        inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        return "\n".join(inner)
    return stripped
