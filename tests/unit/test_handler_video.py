"""Unit tests for VideoHandler — ffmpeg and faster-whisper are fully mocked."""
from __future__ import annotations

import math
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from omnivore.pipeline.handlers.video import VideoHandler, _extract_audio
from omnivore.pipeline.models import TimePosition

# ---------------------------------------------------------------------------
# Helpers (reuse pattern from test_handler_audio)
# ---------------------------------------------------------------------------

def _make_ctx(doc_id: uuid.UUID | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.document_id = doc_id or uuid.uuid4()
    ctx.read_blob = AsyncMock(return_value=b"fake-video-bytes")
    return ctx


def _make_blob(mime: str = "video/mp4") -> MagicMock:
    blob = MagicMock()
    blob.mime_type = mime
    blob.size_bytes = 4096
    return blob


def _make_segment(text: str, start: float, end: float, avg_logprob: float = -0.4) -> MagicMock:
    seg = MagicMock()
    seg.text = text
    seg.start = start
    seg.end = end
    seg.avg_logprob = avg_logprob
    return seg


def _make_info(language: str = "en", prob: float = 0.98, duration: float = 20.0) -> MagicMock:
    info = MagicMock()
    info.language = language
    info.language_probability = prob
    info.duration = duration
    return info


def _stub_model(segments: list, info: MagicMock) -> MagicMock:
    model = MagicMock()
    model.transcribe = MagicMock(return_value=(iter(segments), info))
    return model


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

async def test_video_handler_extracts_transcript_fragments():
    doc_id = uuid.uuid4()
    segs = [
        _make_segment(" Welcome to the video.", 0.0, 3.0),
        _make_segment(" This is a test.", 3.0, 6.0),
    ]
    info = _make_info()
    model = _stub_model(segs, info)

    with (
        patch.object(VideoHandler, "_get_model", return_value=model),
        patch("omnivore.pipeline.handlers.video._extract_audio"),
    ):
        result = await VideoHandler().extract(_make_blob(), _make_ctx(doc_id))

    assert result.document_id == doc_id
    assert len(result.fragments) == 2
    assert all(f.kind == "transcript" for f in result.fragments)
    assert result.fragments[0].content == "Welcome to the video."


async def test_video_handler_time_positions():
    segs = [_make_segment(" Segment.", 4.5, 7.25)]
    model = _stub_model(segs, _make_info())

    with (
        patch.object(VideoHandler, "_get_model", return_value=model),
        patch("omnivore.pipeline.handlers.video._extract_audio"),
    ):
        result = await VideoHandler().extract(_make_blob(), _make_ctx())

    pos = result.fragments[0].position
    assert isinstance(pos, TimePosition)
    assert pos.start_ms == 4500
    assert pos.end_ms == 7250


async def test_video_handler_confidence_exp_of_avg_logprob():
    avg_logprob = -0.6
    segs = [_make_segment(" Text.", 0.0, 1.0, avg_logprob=avg_logprob)]
    model = _stub_model(segs, _make_info())

    with (
        patch.object(VideoHandler, "_get_model", return_value=model),
        patch("omnivore.pipeline.handlers.video._extract_audio"),
    ):
        result = await VideoHandler().extract(_make_blob(), _make_ctx())

    assert result.fragments[0].confidence == round(math.exp(avg_logprob), 3)


async def test_video_handler_metadata():
    info = _make_info(language="de", prob=0.92, duration=90.0)
    model = _stub_model([_make_segment(" Hallo.", 0.0, 1.0)], info)

    with (
        patch.object(VideoHandler, "_get_model", return_value=model),
        patch("omnivore.pipeline.handlers.video._extract_audio"),
    ):
        result = await VideoHandler().extract(_make_blob("video/x-matroska"), _make_ctx())

    assert result.metadata["language"] == "de"
    assert result.metadata["duration_seconds"] == 90.0
    assert result.metadata["format"] == "video"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

async def test_video_handler_empty_audio_track():
    model = _stub_model([], _make_info())

    with (
        patch.object(VideoHandler, "_get_model", return_value=model),
        patch("omnivore.pipeline.handlers.video._extract_audio"),
    ):
        result = await VideoHandler().extract(_make_blob(), _make_ctx())

    assert result.fragments == []


async def test_video_handler_whitespace_segments_skipped():
    segs = [_make_segment("  ", 0.0, 1.0), _make_segment(" Real.", 1.0, 2.0)]
    model = _stub_model(segs, _make_info())

    with (
        patch.object(VideoHandler, "_get_model", return_value=model),
        patch("omnivore.pipeline.handlers.video._extract_audio"),
    ):
        result = await VideoHandler().extract(_make_blob(), _make_ctx())

    assert len(result.fragments) == 1
    assert result.fragments[0].content == "Real."


async def test_video_handler_ffmpeg_failure_propagates():
    with (
        patch("omnivore.pipeline.handlers.video._extract_audio", side_effect=RuntimeError("ffmpeg failed")),
    ):
        with __import__("pytest").raises(RuntimeError, match="ffmpeg failed"):
            await VideoHandler().extract(_make_blob(), _make_ctx())


# ---------------------------------------------------------------------------
# _extract_audio unit test
# ---------------------------------------------------------------------------

def test_extract_audio_raises_on_nonzero_ffmpeg():

    failed = MagicMock()
    failed.returncode = 1
    failed.stderr = b"No such file"

    with patch("subprocess.run", return_value=failed):
        with __import__("pytest").raises(RuntimeError, match="ffmpeg audio extraction failed"):
            _extract_audio("/fake/video.mp4", "/fake/audio.wav")


def test_extract_audio_passes_correct_ffmpeg_flags():
    ok = MagicMock()
    ok.returncode = 0

    with patch("subprocess.run", return_value=ok) as mock_run:
        _extract_audio("/v.mp4", "/a.wav")

    cmd = mock_run.call_args.args[0]
    assert "-vn" in cmd
    assert "-ar" in cmd
    assert "16000" in cmd
    assert "-ac" in cmd
    assert "1" in cmd


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------

def test_video_handler_cost_class_is_gpu():
    assert VideoHandler.cost_class == "gpu"


def test_video_handler_accepts_common_video_mimes():
    for mime in ("video/mp4", "video/quicktime", "video/x-matroska", "video/webm"):
        assert mime in VideoHandler.accepts
