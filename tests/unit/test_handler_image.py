"""Unit tests for ImageOcrHandler — EasyOCR is fully mocked."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from omnivore.pipeline.handlers.image import ImageOcrHandler
from omnivore.pipeline.models import PagePosition

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(doc_id: uuid.UUID | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.document_id = doc_id or uuid.uuid4()
    # Return a minimal valid PNG so PIL can open it
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (200, 50), color="white").save(buf, format="PNG")
    ctx.read_blob = AsyncMock(return_value=buf.getvalue())
    return ctx


def _make_blob(mime: str = "image/png") -> MagicMock:
    blob = MagicMock()
    blob.mime_type = mime
    blob.size_bytes = 1024
    return blob


def _easyocr_result(
    text: str = "Hello",
    bbox: list | None = None,
    confidence: float = 0.95,
) -> tuple:
    if bbox is None:
        bbox = [[10, 5], [80, 5], [80, 20], [10, 20]]
    return (bbox, text, confidence)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

async def test_image_ocr_returns_ocr_fragments():
    doc_id = uuid.uuid4()
    ocr_out = [_easyocr_result("Invoice #1234"), _easyocr_result("Total: $99.99")]
    reader = MagicMock()
    reader.readtext = MagicMock(return_value=ocr_out)

    with patch.object(ImageOcrHandler, "_get_reader", return_value=reader):
        result = await ImageOcrHandler().extract(_make_blob(), _make_ctx(doc_id))

    assert result.document_id == doc_id
    assert len(result.fragments) == 2
    assert all(f.kind == "ocr" for f in result.fragments)
    assert result.fragments[0].content == "Invoice #1234"
    assert result.fragments[1].content == "Total: $99.99"


async def test_image_ocr_bbox_converted_to_axis_aligned():
    # 4-corner bbox: top-left, top-right, bottom-right, bottom-left
    bbox_pts = [[10, 20], [90, 20], [90, 40], [10, 40]]
    reader = MagicMock()
    reader.readtext = MagicMock(return_value=[_easyocr_result("Text", bbox=bbox_pts)])

    with patch.object(ImageOcrHandler, "_get_reader", return_value=reader):
        result = await ImageOcrHandler().extract(_make_blob(), _make_ctx())

    pos = result.fragments[0].position
    assert isinstance(pos, PagePosition)
    assert pos.page == 1
    assert pos.bbox == (10.0, 20.0, 90.0, 40.0)


async def test_image_ocr_confidence_stored():
    reader = MagicMock()
    reader.readtext = MagicMock(return_value=[_easyocr_result("Conf test", confidence=0.876)])

    with patch.object(ImageOcrHandler, "_get_reader", return_value=reader):
        result = await ImageOcrHandler().extract(_make_blob(), _make_ctx())

    assert result.fragments[0].confidence == 0.876


async def test_image_ocr_metadata_populated():
    reader = MagicMock()
    reader.readtext = MagicMock(return_value=[_easyocr_result("A"), _easyocr_result("B")])

    with patch.object(ImageOcrHandler, "_get_reader", return_value=reader):
        result = await ImageOcrHandler().extract(_make_blob("image/jpeg"), _make_ctx())

    assert result.metadata["format"] == "image"
    assert result.metadata["mime_type"] == "image/jpeg"
    assert result.metadata["text_regions"] == 2


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

async def test_image_ocr_empty_image_no_fragments():
    reader = MagicMock()
    reader.readtext = MagicMock(return_value=[])

    with patch.object(ImageOcrHandler, "_get_reader", return_value=reader):
        result = await ImageOcrHandler().extract(_make_blob(), _make_ctx())

    assert result.fragments == []


async def test_image_ocr_whitespace_only_regions_skipped():
    reader = MagicMock()
    reader.readtext = MagicMock(return_value=[
        _easyocr_result("   "),
        _easyocr_result("Real text"),
    ])

    with patch.object(ImageOcrHandler, "_get_reader", return_value=reader):
        result = await ImageOcrHandler().extract(_make_blob(), _make_ctx())

    assert len(result.fragments) == 1
    assert result.fragments[0].content == "Real text"


async def test_image_ocr_tilted_bbox_still_axis_aligned():
    # A rotated bounding box — min/max conversion must still be correct
    bbox_pts = [[5, 15], [25, 5], [35, 25], [15, 35]]
    reader = MagicMock()
    reader.readtext = MagicMock(return_value=[_easyocr_result("Rotated", bbox=bbox_pts)])

    with patch.object(ImageOcrHandler, "_get_reader", return_value=reader):
        result = await ImageOcrHandler().extract(_make_blob(), _make_ctx())

    pos = result.fragments[0].position
    assert pos.bbox == (5.0, 5.0, 35.0, 35.0)


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------

def test_image_ocr_cost_class_is_gpu():
    assert ImageOcrHandler.cost_class == "gpu"


def test_image_ocr_accepts_common_image_mimes():
    for mime in ("image/jpeg", "image/png", "image/webp", "image/tiff"):
        assert mime in ImageOcrHandler.accepts


def test_image_ocr_handler_name():
    assert ImageOcrHandler.name == "image-ocr"
