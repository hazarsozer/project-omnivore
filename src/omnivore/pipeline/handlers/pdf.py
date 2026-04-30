"""PDF handler — PyMuPDF (AGPL-3.0) + pdfplumber (MIT).

NOTE: PyMuPDF is AGPL-3.0. Confirm distribution model before shipping to customers (architecture Q4).
Fallback to pypdf + pdfminer.six (BSD/MIT) if on-prem distribution requires it.
"""
from __future__ import annotations

import io
import uuid
from typing import ClassVar

import structlog

from omnivore.pipeline.context import BlobRef, IngestContext
from omnivore.pipeline.models import Block, ExtractionResult, Fragment, PagePosition, StructuredTable

logger = structlog.get_logger(__name__)


class PdfHandler:
    name: ClassVar[str] = "pdf"
    version: ClassVar[str] = "1.0.0"
    accepts: ClassVar[tuple[str, ...]] = ("application/pdf",)
    cost_class: ClassVar[str] = "cpu"
    timeout_seconds: ClassVar[int] = 300

    async def extract(self, blob: BlobRef, ctx: IngestContext) -> ExtractionResult:
        import fitz  # PyMuPDF
        import pdfplumber

        data = await ctx.read_blob()
        result = ExtractionResult(
            document_id=ctx.document_id,
            source_handler=self.name,
            handler_version=self.version,
        )

        doc = fitz.open(stream=data, filetype="pdf")
        result.metadata = {
            "page_count": len(doc),
            "title": doc.metadata.get("title", ""),
            "author": doc.metadata.get("author", ""),
            "format": "pdf",
        }

        avg_font = _avg_font_size(doc)
        reading_order = 0
        heading_stack: list[tuple[int, str]] = []

        for page_num, page in enumerate(doc):
            page_dict = page.get_text("dict")
            for raw_block in page_dict.get("blocks", []):
                if raw_block.get("type") != 0:
                    continue
                bbox = raw_block.get("bbox")
                for line in raw_block.get("lines", []):
                    line_text = " ".join(
                        span.get("text", "") for span in line.get("spans", [])
                    ).strip()
                    if not line_text:
                        continue
                    span0 = line["spans"][0] if line["spans"] else {}
                    font_size = span0.get("size", 12)
                    is_bold = "Bold" in span0.get("font", "")

                    if font_size > avg_font * 1.2 or (is_bold and font_size >= avg_font):
                        level = _size_to_level(font_size, avg_font)
                        heading_stack = [(l, t) for l, t in heading_stack if l < level]
                        heading_stack.append((level, line_text))
                        blk = Block(
                            kind="heading",
                            level=level,
                            reading_order=reading_order,
                            page=page_num + 1,
                            bbox=tuple(bbox) if bbox else None,
                            text=line_text,
                        )
                    else:
                        blk = Block(
                            kind="paragraph",
                            reading_order=reading_order,
                            page=page_num + 1,
                            bbox=tuple(bbox) if bbox else None,
                            text=line_text,
                        )
                        result.fragments.append(
                            Fragment(
                                kind="text",
                                content=line_text,
                                position=PagePosition(page=page_num + 1, bbox=tuple(bbox) if bbox else None),
                                source_block_ids=[blk.block_id],
                                heading_path=[t for _, t in heading_stack],
                            )
                        )
                    result.blocks.append(blk)
                    reading_order += 1

        # Tables via pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page_num, page in enumerate(pdf.pages):
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

        doc.close()
        logger.info(
            "pdf.extracted",
            document_id=str(ctx.document_id),
            pages=result.metadata["page_count"],
            fragments=len(result.fragments),
            tables=len(result.tables),
        )
        return result


def _avg_font_size(doc) -> float:
    sizes: list[float] = []
    for page in doc[: min(3, len(doc))]:
        for blk in page.get_text("dict").get("blocks", []):
            if blk.get("type") != 0:
                continue
            for line in blk.get("lines", []):
                for span in line.get("spans", []):
                    sizes.append(span.get("size", 12))
    return sum(sizes) / len(sizes) if sizes else 12.0


def _size_to_level(size: float, avg: float) -> int:
    ratio = size / avg
    if ratio >= 2.0:
        return 1
    if ratio >= 1.5:
        return 2
    if ratio >= 1.3:
        return 3
    return 4
