from __future__ import annotations

import asyncio
import math
import os
import tempfile
import threading
from typing import TYPE_CHECKING, ClassVar

import structlog

from omnivore.pipeline.context import BlobRef, IngestContext
from omnivore.pipeline.models import ExtractionResult, Fragment, TimePosition

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

logger = structlog.get_logger(__name__)

WHISPER_MODEL_SIZE = "base"

# Maps accepted MIME types to temp-file extensions for ffmpeg decoding.
_MIME_SUFFIXES: dict[str, str] = {
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mp4": ".mp4",
    "audio/m4a": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "audio/webm": ".webm",
}

_model_lock = threading.Lock()


class AudioHandler:
    name: ClassVar[str] = "audio"
    version: ClassVar[str] = "1.0.0"
    accepts: ClassVar[tuple[str, ...]] = tuple(_MIME_SUFFIXES.keys())
    cost_class: ClassVar[str] = "gpu"
    timeout_seconds: ClassVar[int] = 1800

    _model: ClassVar[WhisperModel | None] = None

    @classmethod
    def _get_model(cls) -> WhisperModel:
        if cls._model is None:
            with _model_lock:
                if cls._model is None:
                    import torch
                    from faster_whisper import WhisperModel

                    device = "cuda" if torch.cuda.is_available() else "cpu"
                    compute_type = "float16" if device == "cuda" else "int8"
                    cls._model = WhisperModel(
                        WHISPER_MODEL_SIZE, device=device, compute_type=compute_type
                    )
                    logger.info(
                        "audio.model.loaded",
                        size=WHISPER_MODEL_SIZE,
                        device=device,
                        compute_type=compute_type,
                    )
        return cls._model  # type: ignore[return-value]

    async def extract(self, blob: BlobRef, ctx: IngestContext) -> ExtractionResult:
        data = await ctx.read_blob()
        suffix = _MIME_SUFFIXES.get(blob.mime_type, ".mp3")

        tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        try:
            os.write(tmp_fd, data)
            os.close(tmp_fd)

            def _transcribe(path: str):
                model = self._get_model()
                segments_iter, info = model.transcribe(path, beam_size=5)
                # Consume the lazy generator inside the executor thread so all
                # compute-heavy work stays off the event loop.
                return list(segments_iter), info

            loop = asyncio.get_running_loop()
            segments, info = await loop.run_in_executor(None, _transcribe, tmp_path)
        finally:
            os.unlink(tmp_path)

        result = ExtractionResult(
            document_id=ctx.document_id,
            source_handler=self.name,
            handler_version=self.version,
            metadata={
                "format": "audio",
                "mime_type": blob.mime_type,
                "language": info.language,
                "language_probability": round(info.language_probability, 3),
                "duration_seconds": round(info.duration, 1),
            },
        )

        for seg in segments:
            text = seg.text.strip()
            if not text:
                continue
            result.fragments.append(
                Fragment(
                    kind="transcript",
                    content=text,
                    position=TimePosition(
                        start_ms=int(seg.start * 1000),
                        end_ms=int(seg.end * 1000),
                    ),
                    language=info.language,
                    # avg_logprob is negative; exp() maps it to (0, 1]
                    confidence=round(math.exp(seg.avg_logprob), 3),
                )
            )

        logger.info(
            "audio.extracted",
            document_id=str(ctx.document_id),
            language=info.language,
            segments=len(result.fragments),
            duration_s=result.metadata["duration_seconds"],
        )
        return result
