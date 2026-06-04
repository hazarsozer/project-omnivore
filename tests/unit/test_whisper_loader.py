"""Unit tests for the shared whisper loader CUDA->CPU fallback (_whisper.py).

Regression coverage: torch.cuda.is_available() can be True while ctranslate2
cannot load its CUDA libs (libcublas/libcudnn). Crucially, ctranslate2 loads
those libs lazily on the first inference, so the loader runs a warmup transcribe
to surface the failure at load time and fall back to CPU instead of hard-failing
the first real job.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from omnivore.pipeline.handlers._whisper import load_whisper_model


def test_falls_back_to_cpu_when_cuda_inference_raises():
    cuda_model = MagicMock(name="cuda_model")
    # ctranslate2 raises during the (lazy) first inference, not at construction.
    cuda_model.transcribe.side_effect = RuntimeError(
        "Library libcublas.so.12 is not found or cannot be loaded"
    )
    cpu_model = MagicMock(name="cpu_model")

    def fake_whisper(size, device, compute_type):
        return cuda_model if device == "cuda" else cpu_model

    with (
        patch("torch.cuda.is_available", return_value=True),
        patch("faster_whisper.WhisperModel", side_effect=fake_whisper) as mk,
    ):
        model = load_whisper_model("base")

    assert model is cpu_model
    assert [c.kwargs["device"] for c in mk.call_args_list] == ["cuda", "cpu"]
    cuda_model.transcribe.assert_called_once()  # warmup was attempted on cuda


def test_uses_cuda_when_warmup_succeeds():
    cuda_model = MagicMock(name="cuda_model")
    cuda_model.transcribe.return_value = (iter([]), MagicMock())
    with (
        patch("torch.cuda.is_available", return_value=True),
        patch("faster_whisper.WhisperModel", return_value=cuda_model) as mk,
    ):
        model = load_whisper_model("base")
    assert model is cuda_model
    assert len(mk.call_args_list) == 1  # no CPU fallback when CUDA works
    cuda_model.transcribe.assert_called_once()  # warmup ran


def test_uses_cpu_directly_when_no_cuda():
    cpu_model = MagicMock(name="cpu_model")
    with (
        patch("torch.cuda.is_available", return_value=False),
        patch("faster_whisper.WhisperModel", return_value=cpu_model) as mk,
    ):
        model = load_whisper_model("base")
    assert model is cpu_model
    assert mk.call_args_list[0].kwargs["device"] == "cpu"
    cpu_model.transcribe.assert_not_called()  # CPU path needs no warmup
