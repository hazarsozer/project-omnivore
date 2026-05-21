"""Unit tests for PdfHandler — uses reportlab (BSD) to build in-memory PDFs.

No disk I/O, no MinIO.
"""
from __future__ import annotations

import io
import uuid
from unittest.mock import AsyncMock, MagicMock

from omnivore.pipeline.context import BlobRef, IngestContext
from omnivore.pipeline.handlers.pdf import PdfHandler
from omnivore.pipeline.models import PagePosition

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _build_pdf(
    paragraphs: list[str] | None = None,
    heading: str | None = None,
    heading_fontsize: float = 24.0,
    body_fontsize: float = 12.0,
) -> bytes:
    """Build a minimal single-page PDF in memory via reportlab (BSD)."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    y = 720.0
    if heading:
        c.setFont("Helvetica-Bold", heading_fontsize)
        c.drawString(72, y, heading)
        y -= heading_fontsize + 8
    for text in (paragraphs or []):
        c.setFont("Helvetica", body_fontsize)
        c.drawString(72, y, text)
        y -= body_fontsize + 4
    c.save()
    return buf.getvalue()


def _make_ctx(blob_bytes: bytes, doc_id: uuid.UUID | None = None) -> tuple[BlobRef, IngestContext]:
    doc_id = doc_id or uuid.uuid4()
    blob = BlobRef(
        bucket="test",
        key="test.pdf",
        mime_type="application/pdf",
        size_bytes=len(blob_bytes),
    )
    ctx = IngestContext(
        document_id=doc_id,
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        blob=blob,
        filename="test.pdf",
        config={},
        _settings=MagicMock(),
    )
    ctx.read_blob = AsyncMock(return_value=blob_bytes)
    return blob, ctx


# ---------------------------------------------------------------------------
# Handler metadata
# ---------------------------------------------------------------------------

def test_pdf_handler_name():
    assert PdfHandler.name == "pdf"


def test_pdf_handler_accepts_pdf_mime():
    assert "application/pdf" in PdfHandler.accepts


def test_pdf_handler_cost_class_is_cpu():
    assert PdfHandler.cost_class == "cpu"


def test_pdf_handler_version_is_string():
    assert isinstance(PdfHandler.version, str) and PdfHandler.version


# ---------------------------------------------------------------------------
# Extraction — metadata
# ---------------------------------------------------------------------------

async def test_pdf_metadata_has_page_count():
    data = _build_pdf(["Some content."])
    blob, ctx = _make_ctx(data)
    result = await PdfHandler().extract(blob, ctx)

    assert result.metadata["page_count"] == 1
    assert result.metadata["format"] == "pdf"


async def test_pdf_document_id_matches_context():
    doc_id = uuid.uuid4()
    data = _build_pdf(["Content."])
    blob, ctx = _make_ctx(data, doc_id=doc_id)
    result = await PdfHandler().extract(blob, ctx)

    assert result.document_id == doc_id


async def test_pdf_source_handler_name():
    data = _build_pdf(["Text."])
    blob, ctx = _make_ctx(data)
    result = await PdfHandler().extract(blob, ctx)

    assert result.source_handler == "pdf"


# ---------------------------------------------------------------------------
# Extraction — paragraphs and fragments
# ---------------------------------------------------------------------------

async def test_pdf_extracts_paragraph_text():
    data = _build_pdf(["First paragraph.", "Second paragraph."])
    blob, ctx = _make_ctx(data)
    result = await PdfHandler().extract(blob, ctx)

    all_content = " ".join(f.content for f in result.fragments)
    assert "First paragraph." in all_content
    assert "Second paragraph." in all_content


async def test_pdf_fragments_have_page_position():
    data = _build_pdf(["Text on page one."])
    blob, ctx = _make_ctx(data)
    result = await PdfHandler().extract(blob, ctx)

    text_frags = [f for f in result.fragments if f.kind == "text"]
    assert len(text_frags) >= 1
    for frag in text_frags:
        assert isinstance(frag.position, PagePosition)
        assert frag.position.page == 1


async def test_pdf_fragment_source_block_ids_populated():
    data = _build_pdf(["Important content."])
    blob, ctx = _make_ctx(data)
    result = await PdfHandler().extract(blob, ctx)

    for frag in result.fragments:
        assert len(frag.source_block_ids) > 0


# ---------------------------------------------------------------------------
# Extraction — headings
# ---------------------------------------------------------------------------

async def test_pdf_heading_detected_in_blocks():
    """A line with a much larger font size than the body is classified as a heading."""
    data = _build_pdf(["Body paragraph."], heading="Chapter One", heading_fontsize=24.0)
    blob, ctx = _make_ctx(data)
    result = await PdfHandler().extract(blob, ctx)

    heading_blocks = [b for b in result.blocks if b.kind == "heading"]
    assert len(heading_blocks) >= 1


async def test_pdf_heading_path_set_on_paragraph_fragments():
    """Paragraph fragments that follow a heading carry its text in heading_path."""
    data = _build_pdf(["Body text after heading."], heading="Introduction", heading_fontsize=24.0)
    blob, ctx = _make_ctx(data)
    result = await PdfHandler().extract(blob, ctx)

    text_frags = [f for f in result.fragments if f.kind == "text"]
    assert any("Introduction" in f.heading_path for f in text_frags)


# ---------------------------------------------------------------------------
# Extraction — empty document
# ---------------------------------------------------------------------------

async def test_pdf_empty_page_no_fragments():
    """A PDF with a blank page should produce no text fragments."""
    data = _build_pdf([])  # no paragraphs, no heading
    blob, ctx = _make_ctx(data)
    result = await PdfHandler().extract(blob, ctx)

    text_frags = [f for f in result.fragments if f.kind == "text"]
    assert text_frags == []


# ---------------------------------------------------------------------------
# Extraction — blocks
# ---------------------------------------------------------------------------

async def test_pdf_blocks_created_for_text_lines():
    data = _build_pdf(["Line one.", "Line two."])
    blob, ctx = _make_ctx(data)
    result = await PdfHandler().extract(blob, ctx)

    assert len(result.blocks) >= 2


async def test_pdf_block_reading_order_monotonically_increases():
    data = _build_pdf(["First.", "Second.", "Third."])
    blob, ctx = _make_ctx(data)
    result = await PdfHandler().extract(blob, ctx)

    orders = [b.reading_order for b in result.blocks]
    assert orders == sorted(orders)
