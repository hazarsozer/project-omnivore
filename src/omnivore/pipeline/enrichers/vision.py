"""Vision enricher — describes image content via Claude Haiku vision API.

Requires ANTHROPIC_API_KEY. Called from image and video handlers where raw bytes
are available. Non-fatal: callers catch exceptions and log warnings.
"""
from __future__ import annotations

import base64
import io

import structlog
from anthropic import AsyncAnthropic
from anthropic.types import TextBlock

logger = structlog.get_logger(__name__)

_VISION_MODEL = "claude-haiku-4-5-20251001"

# Formats Claude vision does not accept natively — converted to JPEG before sending.
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
    api_key: str,
    mime_type: str = "image/jpeg",
) -> str:
    """Call Claude Haiku vision to describe the image. Returns the caption string."""
    if mime_type in _NEEDS_CONVERSION:
        image_bytes, mime_type = _to_jpeg(image_bytes)

    b64 = base64.standard_b64encode(image_bytes).decode()
    client = AsyncAnthropic(api_key=api_key)
    message = await client.messages.create(
        model=_VISION_MODEL,
        max_tokens=512,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime_type,
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": _PROMPT},
                ],
            }
        ],
    )
    text_blocks = [b for b in message.content if isinstance(b, TextBlock)]
    if not text_blocks:
        logger.warning("vision.no_text_block")
        return ""
    return text_blocks[0].text.strip()


def _to_jpeg(image_bytes: bytes) -> tuple[bytes, str]:
    """Convert TIFF/BMP to JPEG so Claude vision API accepts it."""
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue(), "image/jpeg"
