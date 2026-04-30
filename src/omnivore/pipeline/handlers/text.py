from __future__ import annotations

from typing import ClassVar

import structlog

from omnivore.pipeline.context import BlobRef, IngestContext
from omnivore.pipeline.models import Block, ExtractionResult, Fragment, PagePosition

logger = structlog.get_logger(__name__)


class PlainTextHandler:
    name: ClassVar[str] = "text"
    version: ClassVar[str] = "1.0.0"
    accepts: ClassVar[tuple[str, ...]] = ("text/plain",)
    cost_class: ClassVar[str] = "io"
    timeout_seconds: ClassVar[int] = 30

    async def extract(self, blob: BlobRef, ctx: IngestContext) -> ExtractionResult:
        data = await ctx.read_blob()
        text = data.decode("utf-8", errors="replace")
        result = ExtractionResult(
            document_id=ctx.document_id,
            source_handler=self.name,
            handler_version=self.version,
            metadata={"format": "txt", "char_count": len(text)},
        )

        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        for i, para in enumerate(paragraphs):
            blk = Block(kind="paragraph", reading_order=i, text=para)
            result.blocks.append(blk)
            result.fragments.append(
                Fragment(
                    kind="text",
                    content=para,
                    position=PagePosition(page=1),
                    source_block_ids=[blk.block_id],
                )
            )

        logger.info("text.extracted", document_id=str(ctx.document_id), paragraphs=len(paragraphs))
        return result


class MarkdownHandler:
    name: ClassVar[str] = "markdown"
    version: ClassVar[str] = "1.0.0"
    accepts: ClassVar[tuple[str, ...]] = ("text/markdown", "text/x-markdown")
    cost_class: ClassVar[str] = "io"
    timeout_seconds: ClassVar[int] = 30

    async def extract(self, blob: BlobRef, ctx: IngestContext) -> ExtractionResult:
        from markdown_it import MarkdownIt

        data = await ctx.read_blob()
        text = data.decode("utf-8", errors="replace")
        md = MarkdownIt()
        tokens = md.parse(text)

        result = ExtractionResult(
            document_id=ctx.document_id,
            source_handler=self.name,
            handler_version=self.version,
            metadata={"format": "md"},
        )

        reading_order = 0
        heading_stack: list[tuple[int, str]] = []
        i = 0

        while i < len(tokens):
            tok = tokens[i]

            if tok.type == "heading_open":
                level = int(tok.tag[1])  # h1 -> 1
                inline = tokens[i + 1] if i + 1 < len(tokens) else None
                heading_text = inline.content if inline else ""
                heading_stack = [(l, t) for l, t in heading_stack if l < level]
                heading_stack.append((level, heading_text))
                blk = Block(kind="heading", level=level, reading_order=reading_order, text=heading_text)
                result.blocks.append(blk)
                reading_order += 1
                i += 3  # heading_open, inline, heading_close
                continue

            if tok.type == "paragraph_open":
                inline = tokens[i + 1] if i + 1 < len(tokens) else None
                para_text = inline.content if inline else ""
                if para_text.strip():
                    blk = Block(kind="paragraph", reading_order=reading_order, text=para_text)
                    result.blocks.append(blk)
                    result.fragments.append(
                        Fragment(
                            kind="text",
                            content=para_text,
                            position=PagePosition(page=1),
                            source_block_ids=[blk.block_id],
                            heading_path=[t for _, t in heading_stack],
                        )
                    )
                    reading_order += 1
                i += 3
                continue

            if tok.type == "fence":
                blk = Block(kind="code", reading_order=reading_order, text=tok.content)
                result.blocks.append(blk)
                result.fragments.append(
                    Fragment(
                        kind="code",
                        content=tok.content,
                        position=PagePosition(page=1),
                        source_block_ids=[blk.block_id],
                        heading_path=[t for _, t in heading_stack],
                    )
                )
                reading_order += 1

            i += 1

        logger.info("markdown.extracted", document_id=str(ctx.document_id), fragments=len(result.fragments))
        return result
