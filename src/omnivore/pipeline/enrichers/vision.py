"""Vision enricher — describes image content via the configured LLM provider.

Delegates to llm_client.complete_vision() so the provider (Anthropic, OpenAI,
Google, Ollama) is chosen by settings.LLM_PROVIDER. TIFF/BMP images are converted
to JPEG before being sent because not all providers accept those formats natively.
"""
from __future__ import annotations

import io
from typing import TYPE_CHECKING

import structlog

from omnivore.pipeline.enrichers.llm_client import complete_vision

if TYPE_CHECKING:
    from omnivore.config import Settings

logger = structlog.get_logger(__name__)

# Formats not accepted by all vision providers — convert to JPEG before sending.
_NEEDS_CONVERSION: frozenset[str] = frozenset({"image/tiff", "image/bmp"})

_PROMPT = """\
Describe everything visible in this image in detail. Include:
- Main subjects (people, objects, animals, places, scenes)
- Setting or environment (indoors/outdoors, room type, landscape, etc.)
- Any activities, actions, or events depicted
- Visual characteristics (colors, styles, composition, lighting)
- Any visible text, labels, signs, or UI elements

Be specific and concrete so that a search query like "dog running in park",
"Python code snippet", "birthday cake with candles", or "mountain sunset"
would match this description if appropriate.
"""


async def describe_image(
    image_bytes: bytes,
    *,
    settings: Settings,
    mime_type: str = "image/jpeg",
) -> str:
    """Return a natural-language description of the image via the configured LLM."""
    if mime_type in _NEEDS_CONVERSION:
        image_bytes, mime_type = _to_jpeg(image_bytes)

    caption = await complete_vision(image_bytes, mime_type, _PROMPT, settings=settings)
    return caption or ""


def _to_jpeg(image_bytes: bytes) -> tuple[bytes, str]:
    """Convert TIFF/BMP to JPEG so all vision providers can accept it."""
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue(), "image/jpeg"
