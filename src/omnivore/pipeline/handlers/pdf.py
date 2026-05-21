"""PDF handler — pypdf (MIT) + pdfplumber (MIT).

Both dependencies are MIT-licensed, resolving the previous AGPL-3.0 PyMuPDF
incompatibility with the project's MIT LICENSE (architecture Q4).
"""
from __future__ import annotations

import io
from typing import ClassVar

import structlog

from omnivore.pipeline.context import BlobRef, IngestContext
from omnivore.pipeline.models import Block, ExtractionResult, Fragment, PagePosition, StructuredTable

logger = structlog.get_logger(__name__)


class PdfHandler:
    name: ClassVar[str] = "pdf"
    version: ClassVar[str] = "1.1.0"
    accepts: ClassVar[tuple[str, ...]] = ("application/pdf",)
    cost_class: ClassVar[str] = "cpu"
    timeout_seconds: ClassVar[int] = 300

    async def extract(self, blob: BlobRef, ctx: IngestContext) -> ExtractionResult:
        import pdfplumber
        import pypdf

        data = await ctx.read_blob()
        result = ExtractionResult(
            document_id=ctx.document_id,
            source_handler=self.name,
            handler_version=self.version,
        )

        # Metadata via pypdf (MIT)
        reader = pypdf.PdfReader(io.BytesIO(data))
        info = reader.metadata
        result.metadata = {
            "page_count": len(reader.pages),
            "title": (info.title if info else "") or "",
            "author": (info.author if info else "") or "",
            "format": "pdf",
        }

        # Text + heading extraction via pdfplumber (MIT)
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            avg_font = _avg_font_size(pdf)
            reading_order = 0
            heading_stack: list[tuple[int, str]] = []

            for page_num, page in enumerate(pdf.pages):
                for line in _page_lines(page):
                    text = line["text"]
                    size = line["size"]
                    is_bold = line["is_bold"]
                    bbox = line["bbox"]

                    if size > avg_font * 1.2 or (is_bold and size >= avg_font):
                        level = _size_to_level(size, avg_font)
                        heading_stack = [(lvl, t) for lvl, t in heading_stack if lvl < level]
                        heading_stack.append((level, text))
                        blk = Block(
                            kind="heading",
                            level=level,
                            reading_order=reading_order,
                            page=page_num + 1,
                            bbox=bbox,
                            text=text,
                        )
                    else:
                        blk = Block(
                            kind="paragraph",
                            reading_order=reading_order,
                            page=page_num + 1,
                            bbox=bbox,
                            text=text,
                        )
                        result.fragments.append(
                            Fragment(
                                kind="text",
                                content=text,
                                position=PagePosition(page=page_num + 1, bbox=bbox),
                                source_block_ids=[blk.block_id],
                                heading_path=[t for _, t in heading_stack],
                            )
                        )
                    result.blocks.append(blk)
                    reading_order += 1

                # Tables (pdfplumber, same as before)
                for t_idx, table in enumerate(page.extract_tables() or []):
                    if not table or not table[0]:
                        continue
                    headers = [str(h) if h else f"col_{i}" for i, h in enumerate(table[0])]
                    rows = [
                        {headers[i]: (str(cell) if cell else "") for i, cell in enumerate(row)}
                        for row in table[1:]
                        if any(cell for cell in row)
                    ]
                    result.tables.append(
                        StructuredTable(
                            name=f"page{page_num + 1}_table{t_idx + 1}",
                            headers=headers,
                            rows=rows,
                            page=page_num + 1,
                        )
                    )

        logger.info(
            "pdf.extracted",
            document_id=str(ctx.document_id),
            pages=result.metadata["page_count"],
            fragments=len(result.fragments),
            tables=len(result.tables),
        )
        return result


def _avg_font_size(pdf) -> float:
    sizes: list[float] = []
    for page in pdf.pages[:3]:
        for char in page.chars:
            size = char.get("size")
            if size:
                sizes.append(size)
    return sum(sizes) / len(sizes) if sizes else 12.0


def _page_lines(page) -> list[dict]:
    """Group chars by approximate baseline position into text lines."""
    chars = sorted(page.chars, key=lambda c: (round(c.get("top", 0.0) / 4) * 4, c.get("x0", 0.0)))
    lines: list[dict] = []
    current: list[dict] = []
    current_bucket: float | None = None

    for char in chars:
        bucket = round(char.get("top", 0.0) / 4) * 4
        if current_bucket is None or bucket != current_bucket:
            if current:
                line = _chars_to_line(current)
                if line["text"].strip():
                    lines.append(line)
            current = [char]
            current_bucket = bucket
        else:
            current.append(char)

    if current:
        line = _chars_to_line(current)
        if line["text"].strip():
            lines.append(line)

    return lines


def _chars_to_line(chars: list[dict]) -> dict:
    text = "".join(c.get("text", "") for c in chars)
    sizes = [c["size"] for c in chars if c.get("size")]
    fontnames = [c.get("fontname", "") for c in chars]
    bbox: tuple[float, ...] | None = (
        min(c.get("x0", 0.0) for c in chars),
        min(c.get("top", 0.0) for c in chars),
        max(c.get("x1", 0.0) for c in chars),
        max(c.get("bottom", 0.0) for c in chars),
    ) if chars else None
    return {
        "text": text,
        "size": sum(sizes) / len(sizes) if sizes else 12.0,
        "is_bold": any("Bold" in fn or "bold" in fn for fn in fontnames),
        "bbox": bbox,
    }


def _size_to_level(size: float, avg: float) -> int:
    ratio = size / avg
    if ratio >= 2.0:
        return 1
    if ratio >= 1.5:
        return 2
    if ratio >= 1.3:
        return 3
    return 4
