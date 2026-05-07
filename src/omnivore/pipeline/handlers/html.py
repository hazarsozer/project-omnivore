from __future__ import annotations

from typing import ClassVar

import structlog

from omnivore.pipeline.context import BlobRef, IngestContext
from omnivore.pipeline.models import Block, ExtractionResult, Fragment, PagePosition

logger = structlog.get_logger(__name__)

_NOISE_TAGS = {"nav", "aside", "footer", "header", "script", "style", "noscript", "form", "button"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_BLOCK_TAGS = {"p", "li", "blockquote", "pre", "td", "th"}


class HtmlHandler:
    name: ClassVar[str] = "html"
    version: ClassVar[str] = "1.0.0"
    accepts: ClassVar[tuple[str, ...]] = ("text/html", "application/xhtml+xml")
    cost_class: ClassVar[str] = "io"
    timeout_seconds: ClassVar[int] = 60

    async def extract(self, blob: BlobRef, ctx: IngestContext) -> ExtractionResult:
        from selectolax.parser import HTMLParser

        data = await ctx.read_blob()
        html = data.decode("utf-8", errors="replace")
        tree = HTMLParser(html)

        # Remove noise nodes
        for tag in _NOISE_TAGS:
            for node in tree.css(tag):
                node.decompose()

        # Find main content container
        main = tree.css_first("main, article, [role=main]") or tree.body

        result = ExtractionResult(
            document_id=ctx.document_id,
            source_handler=self.name,
            handler_version=self.version,
            metadata={"format": "html", "title": _extract_title(tree)},
        )

        if main is None:
            return result

        reading_order = 0
        heading_stack: list[tuple[int, str]] = []

        for node in main.traverse():
            if node.tag is None or not hasattr(node, "text"):
                continue
            tag = node.tag.lower()
            text = (node.text(deep=True) or "").strip()
            if not text:
                continue

            if tag in _HEADING_TAGS:
                level = int(tag[1])
                heading_stack = [(lvl, t) for lvl, t in heading_stack if lvl < level]
                heading_stack.append((level, text))
                blk = Block(kind="heading", level=level, reading_order=reading_order, text=text)
                result.blocks.append(blk)
                reading_order += 1

            elif tag in _BLOCK_TAGS:
                kind = "code" if tag == "pre" else "text"
                blk = Block(
                    kind="code" if tag == "pre" else "paragraph",
                    reading_order=reading_order,
                    text=text,
                )
                result.blocks.append(blk)
                result.fragments.append(
                    Fragment(
                        kind=kind,
                        content=text,
                        position=PagePosition(page=1),
                        source_block_ids=[blk.block_id],
                        heading_path=[t for _, t in heading_stack],
                    )
                )
                reading_order += 1

        logger.info("html.extracted", document_id=str(ctx.document_id), fragments=len(result.fragments))
        return result


def _extract_title(tree) -> str:
    title_node = tree.css_first("title")
    return title_node.text() if title_node else ""
