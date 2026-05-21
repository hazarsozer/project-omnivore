"""Unit tests for ImageOcrHandler — EasyOCR is fully mocked."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from omnivore.pipeline.handlers.image import ImageOcrHandler
from omnivore.pipeline.models import PagePosition

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(doc_id: uuid.UUID | None = None, ocr_languages: list[str] | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.document_id = doc_id or uuid.uuid4()
    ctx.config = {"ocr_languages": ocr_languages} if ocr_languages else {}
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
# Multilingual language config (Phase 2c)
# ---------------------------------------------------------------------------

async def test_image_ocr_uses_default_language_from_settings():
    """Handler reads IMAGE_OCR_LANGUAGES from settings when ctx.config has no override."""
    reader = MagicMock()
    reader.readtext = MagicMock(return_value=[])
    captured: list[tuple] = []

    def _capture_reader(languages):
        captured.append(languages)
        return reader

    with (
        patch.object(ImageOcrHandler, "_get_reader", side_effect=_capture_reader),
        patch("omnivore.pipeline.handlers.image.get_settings") as mock_settings,
    ):
        mock_settings.return_value.IMAGE_OCR_LANGUAGES = ["en"]
        await ImageOcrHandler().extract(_make_blob(), _make_ctx())

    assert captured == [("en",)]


async def test_image_ocr_respects_per_request_language_override():
    """ctx.config['ocr_languages'] overrides the global setting."""
    reader = MagicMock()
    reader.readtext = MagicMock(return_value=[])
    captured: list[tuple] = []

    def _capture_reader(languages):
        captured.append(languages)
        return reader

    with patch.object(ImageOcrHandler, "_get_reader", side_effect=_capture_reader):
        await ImageOcrHandler().extract(_make_blob(), _make_ctx(ocr_languages=["fr", "de"]))

    assert captured == [("fr", "de")]


async def test_image_ocr_rejects_unsupported_language_codes():
    """Unsupported lang codes from ctx.config raise rather than loading a reader."""
    import pytest as _pytest
    with _pytest.raises(ValueError, match="Unsupported OCR languages"):
        await ImageOcrHandler().extract(
            _make_blob(),
            _make_ctx(ocr_languages=["en", "../../etc/passwd"]),
        )


def test_image_ocr_reader_cache_evicts_when_full():
    """Cache evicts the oldest reader once _MAX_CACHED_READERS is reached."""
    from omnivore.pipeline.handlers import image as image_mod

    # Reset cache for this test
    ImageOcrHandler._readers.clear()
    fake_readers: list[MagicMock] = []

    def _fake_reader_ctor(langs, gpu, verbose):
        m = MagicMock()
        m.langs = langs
        fake_readers.append(m)
        return m

    fake_easyocr = MagicMock()
    fake_easyocr.Reader = _fake_reader_ctor

    fake_torch = MagicMock(cuda=MagicMock(is_available=lambda: False))
    with patch.dict("sys.modules", {"easyocr": fake_easyocr, "torch": fake_torch}):
        # Load _MAX_CACHED_READERS distinct readers
        for langs in [("en",), ("fr",), ("de",), ("es",)]:
            ImageOcrHandler._get_reader(langs)
        assert len(ImageOcrHandler._readers) == image_mod._MAX_CACHED_READERS

        # Loading one more must evict the oldest ("en",)
        ImageOcrHandler._get_reader(("ja",))
        assert ("en",) not in ImageOcrHandler._readers
        assert ("ja",) in ImageOcrHandler._readers
        assert len(ImageOcrHandler._readers) == image_mod._MAX_CACHED_READERS

    ImageOcrHandler._readers.clear()


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


# ---------------------------------------------------------------------------
# Vision enrichment
# ---------------------------------------------------------------------------

def _mock_settings(*, llm_on: bool = True, ocr_languages: list[str] | None = None):
    """Return a MagicMock that looks like Settings."""
    s = MagicMock()
    s.IMAGE_OCR_LANGUAGES = ocr_languages or ["en"]
    s.LLM_PROVIDER = "anthropic"
    s.LLM_VISION_MODEL = ""
    return s


async def test_vision_caption_added_when_llm_configured():
    """When llm_configured() is True, a vision_caption fragment is appended."""
    reader = MagicMock()
    reader.readtext = MagicMock(return_value=[_easyocr_result("Some text")])

    with (
        patch.object(ImageOcrHandler, "_get_reader", return_value=reader),
        patch("omnivore.pipeline.handlers.image.get_settings", return_value=_mock_settings()),
        patch("omnivore.pipeline.enrichers.llm_client.llm_configured", return_value=True),
        patch(
            "omnivore.pipeline.enrichers.vision.describe_image",
            new_callable=AsyncMock,
            return_value="A white background with the words Some text.",
        ),
    ):
        result = await ImageOcrHandler().extract(_make_blob(), _make_ctx())

    vision_frags = [f for f in result.fragments if f.kind == "vision_caption"]
    assert len(vision_frags) == 1
    assert len(vision_frags[0].content) > 0


async def test_no_vision_caption_when_llm_not_configured():
    """When llm_configured() is False, no vision_caption fragment is produced."""
    reader = MagicMock()
    reader.readtext = MagicMock(return_value=[_easyocr_result("Text")])

    with (
        patch.object(ImageOcrHandler, "_get_reader", return_value=reader),
        patch("omnivore.pipeline.handlers.image.get_settings", return_value=_mock_settings()),
        patch("omnivore.pipeline.enrichers.llm_client.llm_configured", return_value=False),
    ):
        result = await ImageOcrHandler().extract(_make_blob(), _make_ctx())

    assert all(f.kind != "vision_caption" for f in result.fragments)


async def test_vision_caption_on_non_text_image():
    """A photo with no OCR text still gets a vision_caption when LLM is configured."""
    reader = MagicMock()
    reader.readtext = MagicMock(return_value=[])

    with (
        patch.object(ImageOcrHandler, "_get_reader", return_value=reader),
        patch("omnivore.pipeline.handlers.image.get_settings", return_value=_mock_settings()),
        patch("omnivore.pipeline.enrichers.llm_client.llm_configured", return_value=True),
        patch(
            "omnivore.pipeline.enrichers.vision.describe_image",
            new_callable=AsyncMock,
            return_value="A golden retriever playing in a park.",
        ),
    ):
        result = await ImageOcrHandler().extract(_make_blob(), _make_ctx())

    assert [f for f in result.fragments if f.kind == "ocr"] == []
    assert len([f for f in result.fragments if f.kind == "vision_caption"]) == 1
    assert result.metadata.get("vision_enriched") is True


async def test_vision_caption_failure_does_not_fail_document():
    """If describe_image raises, OCR results are still returned."""
    reader = MagicMock()
    reader.readtext = MagicMock(return_value=[_easyocr_result("Safe text")])

    with (
        patch.object(ImageOcrHandler, "_get_reader", return_value=reader),
        patch("omnivore.pipeline.handlers.image.get_settings", return_value=_mock_settings()),
        patch("omnivore.pipeline.enrichers.llm_client.llm_configured", return_value=True),
        patch(
            "omnivore.pipeline.enrichers.vision.describe_image",
            new_callable=AsyncMock,
            side_effect=RuntimeError("LLM unavailable"),
        ),
    ):
        result = await ImageOcrHandler().extract(_make_blob(), _make_ctx())

    assert len([f for f in result.fragments if f.kind == "ocr"]) == 1
    assert all(f.kind != "vision_caption" for f in result.fragments)
    assert result.metadata.get("vision_enriched", False) is False


async def test_vision_enriched_metadata_flag():
    reader = MagicMock()
    reader.readtext = MagicMock(return_value=[])

    with (
        patch.object(ImageOcrHandler, "_get_reader", return_value=reader),
        patch("omnivore.pipeline.handlers.image.get_settings", return_value=_mock_settings()),
        patch("omnivore.pipeline.enrichers.llm_client.llm_configured", return_value=True),
        patch(
            "omnivore.pipeline.enrichers.vision.describe_image",
            new_callable=AsyncMock,
            return_value="A sunset over the ocean.",
        ),
    ):
        result = await ImageOcrHandler().extract(_make_blob(), _make_ctx())

    assert result.metadata["vision_enriched"] is True
