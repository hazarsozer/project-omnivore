from __future__ import annotations

import asyncio
import math
import os
import shutil
import subprocess
import tempfile
import threading
from typing import TYPE_CHECKING, ClassVar

import structlog

from omnivore.config import get_settings
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

# Module-level lock: created once at import time (import lock serializes it).
# Matches the audio handler pattern. The previous asyncio.Lock was dead code
# and wrong for executor-thread context.
_model_lock = threading.Lock()


class VideoHandler:
    name: ClassVar[str] = "video"
    version: ClassVar[str] = "1.0.0"
    accepts: ClassVar[tuple[str, ...]] = tuple(_MIME_SUFFIXES.keys())
    cost_class: ClassVar[str] = "gpu"
    timeout_seconds: ClassVar[int] = 3600

    _model: ClassVar[WhisperModel | None] = None

    @classmethod
    def _get_model(cls) -> WhisperModel:
        if cls._model is None:
            with _model_lock:
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
        settings = get_settings()

        video_suffix = _MIME_SUFFIXES.get(blob.mime_type, ".mp4")
        vid_fd, vid_path = tempfile.mkstemp(suffix=video_suffix)
        audio_fd, audio_path = tempfile.mkstemp(suffix=".wav")
        os.close(audio_fd)  # we only need the path; close fd before entering the try block
        try:
            try:
                async for chunk in ctx.stream_blob():
                    os.write(vid_fd, chunk)
            finally:
                os.close(vid_fd)

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _extract_audio, vid_path, audio_path)

            def _transcribe(path: str):
                model = self._get_model()
                segments_iter, info = model.transcribe(path, beam_size=5)
                return list(segments_iter), info

            segments, info = await loop.run_in_executor(None, _transcribe, audio_path)

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

            # Vision enrichment: sample frames and describe each one via Claude.
            # Acts as an enhancer alongside the transcript and as the only content
            # source for silent videos or visual-only content (slides, demos).
            api_key = settings.ANTHROPIC_API_KEY.get_secret_value() if settings.ANTHROPIC_API_KEY else None
            if api_key:
                await self._add_vision_captions(
                    vid_path=vid_path,
                    loop=loop,
                    api_key=api_key,
                    result=result,
                    ctx=ctx,
                    interval=settings.VIDEO_FRAME_SAMPLE_INTERVAL,
                    max_frames=settings.VIDEO_MAX_VISION_FRAMES,
                )

            # Interleave transcript and vision captions by timestamp so chunks
            # around the same moment in the video contain both kinds of content.
            result.fragments.sort(
                key=lambda f: (
                    f.position.start_ms
                    if isinstance(f.position, TimePosition)
                    else 0
                )
            )

        finally:
            for path in (vid_path, audio_path):
                if os.path.exists(path):
                    os.unlink(path)

        logger.info(
            "video.extracted",
            document_id=str(ctx.document_id),
            language=info.language,
            transcript_segments=len([f for f in result.fragments if f.kind == "transcript"]),
            vision_captions=len([f for f in result.fragments if f.kind == "vision_caption"]),
            duration_s=result.metadata["duration_seconds"],
        )
        return result

    async def _add_vision_captions(
        self,
        *,
        vid_path: str,
        loop: asyncio.AbstractEventLoop,
        api_key: str,
        result: ExtractionResult,
        ctx: IngestContext,
        interval: int,
        max_frames: int,
    ) -> None:
        from omnivore.pipeline.enrichers.vision import describe_image

        frame_dir = tempfile.mkdtemp()
        try:
            try:
                await loop.run_in_executor(
                    None, _extract_frames, vid_path, frame_dir, interval, max_frames
                )
            except Exception:
                logger.warning(
                    "video.frame_extraction.failed",
                    document_id=str(ctx.document_id),
                    exc_info=True,
                )
                return

            frame_files = sorted(f for f in os.listdir(frame_dir) if f.endswith(".jpg"))
            caption_count = 0
            for fname in frame_files:
                # frame_%04d.jpg → 1-indexed; convert to 0-indexed timestamp
                idx = int(fname.replace("frame_", "").replace(".jpg", "")) - 1
                ts_s = idx * interval
                frame_path = os.path.join(frame_dir, fname)
                with open(frame_path, "rb") as fh:
                    frame_bytes = fh.read()
                try:
                    caption = await describe_image(
                        frame_bytes, api_key=api_key, mime_type="image/jpeg"
                    )
                    if caption:
                        result.fragments.append(
                            Fragment(
                                kind="vision_caption",
                                content=caption,
                                position=TimePosition(
                                    start_ms=ts_s * 1000,
                                    end_ms=(ts_s + interval) * 1000,
                                ),
                            )
                        )
                        caption_count += 1
                except Exception:
                    logger.warning(
                        "video.vision_caption.failed",
                        document_id=str(ctx.document_id),
                        timestamp_s=ts_s,
                        exc_info=True,
                    )
            result.metadata["vision_frames_captioned"] = caption_count
        finally:
            shutil.rmtree(frame_dir, ignore_errors=True)


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


def _extract_frames(video_path: str, output_dir: str, interval: int, max_frames: int) -> None:
    """Extract one JPEG frame every `interval` seconds, capped at `max_frames`."""
    cmd = [
        "ffmpeg",
        "-y",
        "-i", video_path,
        "-vf", f"fps=1/{interval}",
        "-vframes", str(max_frames),
        "-q:v", "2",    # high-quality JPEG
        os.path.join(output_dir, "frame_%04d.jpg"),
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg frame extraction failed (exit {result.returncode}): "
            f"{result.stderr.decode(errors='replace')[:500]}"
        )
