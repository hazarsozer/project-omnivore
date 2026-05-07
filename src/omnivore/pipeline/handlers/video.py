from __future__ import annotations

import asyncio
import math
import os
import subprocess
import tempfile
from typing import TYPE_CHECKING, ClassVar

import structlog

from omnivore.pipeline.context import BlobRef, IngestContext
from omnivore.pipeline.models import ExtractionResult, Fragment, TimePosition

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

logger = structlog.get_logger(__name__)

_MIME_SUFFIXES: dict[str, str] = {
    "video/mp4": ".mp4",
    "video/x-msvideo": ".avi",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "video/webm": ".webm",
    "video/mpeg": ".mpeg",
    "video/ogg": ".ogv",
}

_model_lock = asyncio.Lock()  # asyncio-safe; model init runs in executor


class VideoHandler:
    name: ClassVar[str] = "video"
    version: ClassVar[str] = "1.0.0"
    accepts: ClassVar[tuple[str, ...]] = tuple(_MIME_SUFFIXES.keys())
    cost_class: ClassVar[str] = "gpu"
    timeout_seconds: ClassVar[int] = 3600

    _model: ClassVar[WhisperModel | None] = None
    _model_init_lock: ClassVar = None  # set to threading.Lock() lazily

    @classmethod
    def _get_model(cls) -> WhisperModel:
        import threading

        if cls._model_init_lock is None:
            cls._model_init_lock = threading.Lock()

        if cls._model is None:
            with cls._model_init_lock:
                if cls._model is None:
                    import torch
                    from faster_whisper import WhisperModel as _WhisperModel

                    device = "cuda" if torch.cuda.is_available() else "cpu"
                    compute_type = "float16" if device == "cuda" else "int8"
                    cls._model = _WhisperModel("base", device=device, compute_type=compute_type)
                    logger.info(
                        "video.model.loaded",
                        size="base",
                        device=device,
                        compute_type=compute_type,
                    )
        return cls._model  # type: ignore[return-value]

    async def extract(self, blob: BlobRef, ctx: IngestContext) -> ExtractionResult:
        data = await ctx.read_blob()
        video_suffix = _MIME_SUFFIXES.get(blob.mime_type, ".mp4")

        # Write video bytes to a temp file, extract audio track, transcribe.
        vid_fd, vid_path = tempfile.mkstemp(suffix=video_suffix)
        audio_fd, audio_path = tempfile.mkstemp(suffix=".wav")
        try:
            os.write(vid_fd, data)
            os.close(vid_fd)
            os.close(audio_fd)

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _extract_audio, vid_path, audio_path)

            def _transcribe(path: str):
                model = self._get_model()
                segments_iter, info = model.transcribe(path, beam_size=5)
                return list(segments_iter), info

            segments, info = await loop.run_in_executor(None, _transcribe, audio_path)
        finally:
            for path in (vid_path, audio_path):
                if os.path.exists(path):
                    os.unlink(path)

        result = ExtractionResult(
            document_id=ctx.document_id,
            source_handler=self.name,
            handler_version=self.version,
            metadata={
                "format": "video",
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
                    confidence=round(math.exp(seg.avg_logprob), 3),
                )
            )

        logger.info(
            "video.extracted",
            document_id=str(ctx.document_id),
            language=info.language,
            segments=len(result.fragments),
            duration_s=result.metadata["duration_seconds"],
        )
        return result


def _extract_audio(video_path: str, audio_path: str) -> None:
    """Extract mono 16 kHz WAV from video using ffmpeg. Raises on non-zero exit."""
    cmd = [
        "ffmpeg",
        "-y",           # overwrite output without prompting
        "-i", video_path,
        "-vn",          # drop video stream
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        audio_path,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg audio extraction failed (exit {result.returncode}): "
            f"{result.stderr.decode(errors='replace')[:500]}"
        )
