"""Shared faster-whisper loader with CUDA->CPU fallback.

``torch.cuda.is_available()`` can return True while faster-whisper's backend
(ctranslate2) still cannot load its own CUDA libraries — libcublas/libcudnn are
separate from torch's bundled CUDA runtime and are not pulled in by default.

ctranslate2 loads those libs **lazily on the first inference**, not at model
construction, so a simple try around ``WhisperModel(device="cuda")`` is not
enough — it succeeds, and the real "libcublas.so.12 is not found" error only
surfaces during the first ``transcribe()``. We therefore run a tiny warmup
inference here to force CUDA initialisation, and fall back to CPU if it fails.

For GPU acceleration, install ``nvidia-cublas-cu12`` and ``nvidia-cudnn-cu12``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

logger = structlog.get_logger(__name__)


def _warmup(model: WhisperModel) -> None:
    """Force ctranslate2 to initialise its compute backend (and load CUDA libs).

    transcribe() is lazy, so the generator must be consumed. 1s of low-amplitude
    noise at 16 kHz runs the encoder/decoder without VAD short-circuiting it.
    """
    import numpy as np

    audio = (np.random.default_rng(0).standard_normal(16_000) * 0.01).astype("float32")
    segments, _ = model.transcribe(audio, beam_size=1, vad_filter=False)
    for _ in segments:
        pass


def load_whisper_model(size: str) -> WhisperModel:
    """Load a faster-whisper model on CUDA when usable, else fall back to CPU."""
    import torch
    from faster_whisper import WhisperModel

    if torch.cuda.is_available():
        try:
            model = WhisperModel(size, device="cuda", compute_type="float16")
            _warmup(model)  # surfaces lazy CUDA-lib load failures here, not mid-job
            logger.info(
                "whisper.model.loaded", size=size, device="cuda", compute_type="float16"
            )
            return model
        except Exception as exc:
            logger.warning(
                "whisper.cuda_unavailable.fallback_cpu",
                size=size,
                error=str(exc),
                hint="install nvidia-cublas-cu12 + nvidia-cudnn-cu12 for GPU whisper",
            )

    model = WhisperModel(size, device="cpu", compute_type="int8")
    logger.info("whisper.model.loaded", size=size, device="cpu", compute_type="int8")
    return model
