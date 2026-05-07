from __future__ import annotations

import asyncio
import io
import threading
from typing import TYPE_CHECKING, ClassVar

import structlog

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


class ImageOcrHandler:
    name: ClassVar[str] = "image-ocr"
    version: ClassVar[str] = "1.0.0"
    accepts: ClassVar[tuple[str, ...]] = _ACCEPTED_MIMES
    cost_class: ClassVar[str] = "gpu"
    timeout_seconds: ClassVar[int] = 300

    _reader: ClassVar[easyocr.Reader | None] = None

    @classmethod
    def _get_reader(cls) -> easyocr.Reader:
        if cls._reader is None:
            with _model_lock:
                if cls._reader is None:
                    import easyocr as _easyocr
                    import torch

                    gpu = torch.cuda.is_available()
                    cls._reader = _easyocr.Reader(["en"], gpu=gpu, verbose=False)
                    logger.info("image_ocr.model.loaded", gpu=gpu)
        return cls._reader  # type: ignore[return-value]

    async def extract(self, blob: BlobRef, ctx: IngestContext) -> ExtractionResult:
        data = await ctx.read_blob()

        def _run_ocr(raw: bytes) -> list:
            import numpy as np
            from PIL import Image

            img = Image.open(io.BytesIO(raw)).convert("RGB")
            arr = np.array(img)
            reader = self._get_reader()
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
