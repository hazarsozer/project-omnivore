from __future__ import annotations

import asyncio
import io
import threading
from typing import TYPE_CHECKING, ClassVar

import structlog

from omnivore.config import get_settings
from omnivore.pipeline.context import BlobRef, IngestContext
from omnivore.pipeline.models import ExtractionResult, Fragment, PagePosition

if TYPE_CHECKING:
    import easyocr

logger = structlog.get_logger(__name__)

_ACCEPTED_MIMES: tuple[str, ...] = (
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/tiff",
    "image/bmp",
    "image/gif",
)

_model_lock = threading.Lock()

# EasyOCR's well-known language codes. Restricting to this allowlist prevents cache
# pollution / VRAM exhaustion via attacker-controlled `ocr_languages` from ctx.config.
_SUPPORTED_OCR_LANGUAGES: frozenset[str] = frozenset({
    "en", "ch_sim", "ch_tra", "ja", "ko", "th", "vi",
    "fr", "de", "es", "it", "pt", "nl", "ru", "uk", "pl", "tr", "ar", "fa", "ur", "hi",
    "id", "ms", "tl", "sv", "no", "da", "fi", "cs", "sk", "ro", "hu", "el", "he",
})

# LRU-style bound on cached readers so a misbehaving caller cannot exhaust GPU VRAM
# by submitting many distinct language combinations. ~200 MB per reader.
_MAX_CACHED_READERS = 4


class ImageOcrHandler:
    name: ClassVar[str] = "image-ocr"
    version: ClassVar[str] = "1.0.0"
    accepts: ClassVar[tuple[str, ...]] = _ACCEPTED_MIMES
    cost_class: ClassVar[str] = "gpu"
    timeout_seconds: ClassVar[int] = 300

    # Keyed by language tuple so different language sets each get their own reader instance.
    _readers: ClassVar[dict[tuple[str, ...], easyocr.Reader]] = {}

    @classmethod
    def _get_reader(cls, languages: tuple[str, ...]) -> easyocr.Reader:
        if languages not in cls._readers:
            with _model_lock:
                if languages not in cls._readers:
                    import easyocr as _easyocr
                    import torch

                    if len(cls._readers) >= _MAX_CACHED_READERS:
                        # Evict the oldest insertion (Python 3.7+ dicts preserve insertion order).
                        evict_key = next(iter(cls._readers))
                        cls._readers.pop(evict_key)
                        logger.info("image_ocr.reader_evicted", languages=list(evict_key))

                    gpu = torch.cuda.is_available()
                    cls._readers[languages] = _easyocr.Reader(list(languages), gpu=gpu, verbose=False)
                    logger.info("image_ocr.model.loaded", gpu=gpu, languages=list(languages))
        return cls._readers[languages]

    async def extract(self, blob: BlobRef, ctx: IngestContext) -> ExtractionResult:
        lang_override = ctx.config.get("ocr_languages")
        if lang_override:
            invalid = [lc for lc in lang_override if lc not in _SUPPORTED_OCR_LANGUAGES]
            if invalid:
                raise ValueError(f"Unsupported OCR languages: {invalid}")
            languages = tuple(lang_override)
        else:
            languages = tuple(get_settings().IMAGE_OCR_LANGUAGES)

        data = await ctx.read_blob()

        def _run_ocr(raw: bytes) -> list:
            import numpy as np
            from PIL import Image

            img = Image.open(io.BytesIO(raw)).convert("RGB")
            arr = np.array(img)
            reader = self._get_reader(languages)
            return reader.readtext(arr)

        loop = asyncio.get_running_loop()
        ocr_results = await loop.run_in_executor(None, _run_ocr, data)

        result = ExtractionResult(
            document_id=ctx.document_id,
            source_handler=self.name,
            handler_version=self.version,
            metadata={
                "format": "image",
                "mime_type": blob.mime_type,
                "text_regions": len(ocr_results),
            },
        )

        for bbox_pts, text, confidence in ocr_results:
            text = text.strip()
            if not text:
                continue

            xs = [p[0] for p in bbox_pts]
            ys = [p[1] for p in bbox_pts]
            bbox = (float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys)))

            result.fragments.append(
                Fragment(
                    kind="ocr",
                    content=text,
                    position=PagePosition(page=1, bbox=bbox),
                    confidence=round(float(confidence), 3),
                )
            )

        logger.info(
            "image_ocr.extracted",
            document_id=str(ctx.document_id),
            regions=len(result.fragments),
        )
        return result
