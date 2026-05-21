"""Unit tests for pipeline/enrichers/vision.py — Anthropic client fully mocked."""
from __future__ import annotations

import io
from unittest.mock import AsyncMock, MagicMock, patch


def _make_anthropic_response(text: str) -> MagicMock:
    from anthropic.types import TextBlock

    block = MagicMock(spec=TextBlock)
    block.text = text
    response = MagicMock()
    response.content = [block]
    return response


# ---------------------------------------------------------------------------
# Basic describe_image behaviour
# ---------------------------------------------------------------------------

async def test_describe_image_returns_caption():
    from omnivore.pipeline.enrichers.vision import describe_image

    response = _make_anthropic_response("A golden retriever playing fetch in a sunny park.")
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=response)

    with patch("omnivore.pipeline.enrichers.vision.AsyncAnthropic", return_value=mock_client):
        caption = await describe_image(b"fake-jpeg-bytes", api_key="test-key", mime_type="image/jpeg")

    assert caption == "A golden retriever playing fetch in a sunny park."


async def test_describe_image_strips_whitespace():
    from omnivore.pipeline.enrichers.vision import describe_image

    response = _make_anthropic_response("  A screenshot of code.  \n")
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=response)

    with patch("omnivore.pipeline.enrichers.vision.AsyncAnthropic", return_value=mock_client):
        caption = await describe_image(b"fake", api_key="test-key")

    assert caption == "A screenshot of code."


async def test_describe_image_returns_empty_on_no_text_block():
    from omnivore.pipeline.enrichers.vision import describe_image

    response = MagicMock()
    response.content = []  # no TextBlock
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=response)

    with patch("omnivore.pipeline.enrichers.vision.AsyncAnthropic", return_value=mock_client):
        caption = await describe_image(b"fake", api_key="test-key")

    assert caption == ""


async def test_describe_image_uses_correct_model():
    from omnivore.pipeline.enrichers.vision import _VISION_MODEL, describe_image

    response = _make_anthropic_response("some description")
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=response)

    with patch("omnivore.pipeline.enrichers.vision.AsyncAnthropic", return_value=mock_client):
        await describe_image(b"fake", api_key="key")

    call_kwargs = mock_client.messages.create.call_args
    assert call_kwargs.kwargs["model"] == _VISION_MODEL


async def test_describe_image_encodes_bytes_as_base64():
    import base64

    from omnivore.pipeline.enrichers.vision import describe_image

    img_bytes = b"\x89PNG\r\nfake-png-data"
    expected_b64 = base64.standard_b64encode(img_bytes).decode()

    response = _make_anthropic_response("A PNG image.")
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=response)

    with patch("omnivore.pipeline.enrichers.vision.AsyncAnthropic", return_value=mock_client):
        await describe_image(img_bytes, api_key="key", mime_type="image/png")

    content_sent = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    image_block = next(b for b in content_sent if b["type"] == "image")
    assert image_block["source"]["data"] == expected_b64
    assert image_block["source"]["media_type"] == "image/png"


# ---------------------------------------------------------------------------
# TIFF / BMP conversion
# ---------------------------------------------------------------------------

def _make_minimal_png() -> bytes:
    """Return a 1×1 red PNG (valid PIL-readable bytes)."""
    from PIL import Image

    img = Image.new("RGB", (1, 1), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def test_tiff_converted_to_jpeg_before_api_call():
    """TIFF is not supported by Claude vision — must be converted to JPEG first."""
    from PIL import Image

    from omnivore.pipeline.enrichers.vision import describe_image

    # Build a valid 1×1 TIFF
    img = Image.new("RGB", (1, 1), color=(0, 128, 0))
    buf = io.BytesIO()
    img.save(buf, format="TIFF")
    tiff_bytes = buf.getvalue()

    response = _make_anthropic_response("A green square.")
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=response)

    with patch("omnivore.pipeline.enrichers.vision.AsyncAnthropic", return_value=mock_client):
        await describe_image(tiff_bytes, api_key="key", mime_type="image/tiff")

    content_sent = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    image_block = next(b for b in content_sent if b["type"] == "image")
    assert image_block["source"]["media_type"] == "image/jpeg"


async def test_bmp_converted_to_jpeg():
    from PIL import Image

    from omnivore.pipeline.enrichers.vision import describe_image

    img = Image.new("RGB", (2, 2), color=(0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="BMP")
    bmp_bytes = buf.getvalue()

    response = _make_anthropic_response("A blue square.")
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=response)

    with patch("omnivore.pipeline.enrichers.vision.AsyncAnthropic", return_value=mock_client):
        await describe_image(bmp_bytes, api_key="key", mime_type="image/bmp")

    content_sent = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    image_block = next(b for b in content_sent if b["type"] == "image")
    assert image_block["source"]["media_type"] == "image/jpeg"


async def test_jpeg_not_converted():
    from omnivore.pipeline.enrichers.vision import describe_image

    response = _make_anthropic_response("A JPEG image.")
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=response)

    jpeg_bytes = b"\xff\xd8\xff\xe0fake-jpeg"
    with patch("omnivore.pipeline.enrichers.vision.AsyncAnthropic", return_value=mock_client):
        await describe_image(jpeg_bytes, api_key="key", mime_type="image/jpeg")

    content_sent = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    image_block = next(b for b in content_sent if b["type"] == "image")
    # Byte content unchanged (still fake JPEG bytes, just base64-encoded)
    assert image_block["source"]["media_type"] == "image/jpeg"
