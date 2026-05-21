"""Unit tests for pipeline/enrichers/vision.py.

Tests cover:
  - describe_image routing through complete_vision
  - TIFF/BMP → JPEG conversion (happens before the LLM call)
"""
from __future__ import annotations

import io
from unittest.mock import AsyncMock, MagicMock, patch


def _settings() -> MagicMock:
    s = MagicMock()
    s.LLM_PROVIDER = "anthropic"
    s.LLM_VISION_MODEL = ""
    return s


# ---------------------------------------------------------------------------
# describe_image basics
# ---------------------------------------------------------------------------

async def test_describe_image_returns_caption():
    from omnivore.pipeline.enrichers.vision import describe_image

    with patch(
        "omnivore.pipeline.enrichers.vision.complete_vision",
        new_callable=AsyncMock,
        return_value="A golden retriever playing fetch in a sunny park.",
    ):
        caption = await describe_image(b"fake-jpeg-bytes", settings=_settings(), mime_type="image/jpeg")

    assert caption == "A golden retriever playing fetch in a sunny park."


async def test_describe_image_returns_empty_string_when_complete_vision_returns_none():
    from omnivore.pipeline.enrichers.vision import describe_image

    with patch(
        "omnivore.pipeline.enrichers.vision.complete_vision",
        new_callable=AsyncMock,
        return_value=None,
    ):
        caption = await describe_image(b"fake", settings=_settings())

    assert caption == ""


async def test_describe_image_passes_prompt_to_complete_vision():
    from omnivore.pipeline.enrichers.vision import _PROMPT, describe_image

    captured: list[str] = []

    async def _capture(img, mime, prompt, *, settings):
        captured.append(prompt)
        return "ok"

    with patch("omnivore.pipeline.enrichers.vision.complete_vision", side_effect=_capture):
        await describe_image(b"img", settings=_settings(), mime_type="image/jpeg")

    assert captured[0] == _PROMPT


async def test_describe_image_passes_settings_to_complete_vision():
    from omnivore.pipeline.enrichers.vision import describe_image

    captured_settings: list[MagicMock] = []

    async def _capture(img, mime, prompt, *, settings):
        captured_settings.append(settings)
        return "ok"

    with patch("omnivore.pipeline.enrichers.vision.complete_vision", side_effect=_capture):
        s = _settings()
        await describe_image(b"img", settings=s)

    assert captured_settings[0] is s


# ---------------------------------------------------------------------------
# TIFF / BMP conversion (happens inside describe_image before complete_vision)
# ---------------------------------------------------------------------------

async def test_tiff_converted_to_jpeg_before_vision_call():
    from PIL import Image

    from omnivore.pipeline.enrichers.vision import describe_image

    img = Image.new("RGB", (1, 1), color=(0, 128, 0))
    buf = io.BytesIO()
    img.save(buf, format="TIFF")
    tiff_bytes = buf.getvalue()

    captured: list[str] = []

    async def _capture(img_bytes, mime, prompt, *, settings):
        captured.append(mime)
        return "A green square."

    with patch("omnivore.pipeline.enrichers.vision.complete_vision", side_effect=_capture):
        caption = await describe_image(tiff_bytes, settings=_settings(), mime_type="image/tiff")

    assert captured[0] == "image/jpeg"
    assert caption == "A green square."


async def test_bmp_converted_to_jpeg_before_vision_call():
    from PIL import Image

    from omnivore.pipeline.enrichers.vision import describe_image

    img = Image.new("RGB", (2, 2), color=(0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="BMP")
    bmp_bytes = buf.getvalue()

    captured: list[str] = []

    async def _capture(img_bytes, mime, prompt, *, settings):
        captured.append(mime)
        return "A blue square."

    with patch("omnivore.pipeline.enrichers.vision.complete_vision", side_effect=_capture):
        await describe_image(bmp_bytes, settings=_settings(), mime_type="image/bmp")

    assert captured[0] == "image/jpeg"


async def test_jpeg_not_converted():
    from omnivore.pipeline.enrichers.vision import describe_image

    captured: list[str] = []

    async def _capture(img_bytes, mime, prompt, *, settings):
        captured.append(mime)
        return "JPEG image."

    with patch("omnivore.pipeline.enrichers.vision.complete_vision", side_effect=_capture):
        await describe_image(b"\xff\xd8\xff\xe0", settings=_settings(), mime_type="image/jpeg")

    assert captured[0] == "image/jpeg"
