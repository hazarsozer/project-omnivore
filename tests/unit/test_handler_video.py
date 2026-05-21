"""Unit tests for VideoHandler — ffmpeg and faster-whisper are fully mocked."""
from __future__ import annotations

import math
import uuid
from unittest.mock import MagicMock, patch

from omnivore.pipeline.handlers.video import VideoHandler, _extract_audio
from omnivore.pipeline.models import TimePosition

# ---------------------------------------------------------------------------
# Helpers (reuse pattern from test_handler_audio)
# ---------------------------------------------------------------------------

def _make_ctx(doc_id: uuid.UUID | None = None, data: bytes = b"fake-video-bytes") -> MagicMock:
    ctx = MagicMock()
    ctx.document_id = doc_id or uuid.uuid4()

    async def _stream_blob():
        yield data

    ctx.stream_blob = _stream_blob
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
# Streaming (Phase 2c — stream_blob replaces read_blob + size guard)
# ---------------------------------------------------------------------------

async def test_video_handler_streams_large_blob_in_chunks():
    """Handler assembles multi-chunk stream into the correct transcript."""
    chunk_a = b"fake-" * 100
    chunk_b = b"video-bytes" * 50

    async def _chunked_stream():
        yield chunk_a
        yield chunk_b

    ctx = MagicMock()
    ctx.document_id = uuid.uuid4()
    ctx.stream_blob = _chunked_stream

    segs = [_make_segment(" Hello.", 0.0, 1.0)]
    model = _stub_model(segs, _make_info())

    with (
        patch.object(VideoHandler, "_get_model", return_value=model),
        patch("omnivore.pipeline.handlers.video._extract_audio"),
    ):
        result = await VideoHandler().extract(_make_blob(), ctx)

    assert len(result.fragments) == 1
    assert result.fragments[0].content == "Hello."


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------

def test_video_handler_cost_class_is_gpu():
    assert VideoHandler.cost_class == "gpu"


def test_video_handler_accepts_common_video_mimes():
    for mime in ("video/mp4", "video/quicktime", "video/x-matroska", "video/webm"):
        assert mime in VideoHandler.accepts


# ---------------------------------------------------------------------------
# Vision enrichment — frame extraction and captions (Phase 7)
# ---------------------------------------------------------------------------

def _mock_settings_video(*, llm_on: bool = True, interval: int = 30, max_frames: int = 20):
    s = MagicMock()
    s.LLM_PROVIDER = "anthropic"
    s.LLM_VISION_MODEL = ""
    s.VIDEO_FRAME_SAMPLE_INTERVAL = interval
    s.VIDEO_MAX_VISION_FRAMES = max_frames
    return s


def _stub_frame_dir(monkeypatch_or_patch, frame_count: int = 2) -> dict[str, bytes]:
    """Simulate _extract_frames writing N JPEG files into the output_dir."""
    import os

    frame_contents: dict[str, bytes] = {}
    for i in range(1, frame_count + 1):
        frame_contents[f"frame_{i:04d}.jpg"] = f"fake-frame-{i}".encode()

    def _fake_extract_frames(video_path, output_dir, interval, max_frames):
        for name, data in frame_contents.items():
            with open(os.path.join(output_dir, name), "wb") as f:
                f.write(data)

    return _fake_extract_frames, frame_contents


async def test_vision_captions_added_when_llm_configured():
    """With LLM configured, frame extraction + vision produces vision_caption fragments."""
    segs = [_make_segment(" Narrator speaks.", 0.0, 5.0)]
    model = _stub_model(segs, _make_info())
    fake_extract_frames, _ = _stub_frame_dir(None, frame_count=2)

    call_count = 0

    async def _fake_describe(img_bytes, *, settings, mime_type="image/jpeg"):
        nonlocal call_count
        call_count += 1
        return f"Scene {call_count}: a presenter speaking."

    with (
        patch.object(VideoHandler, "_get_model", return_value=model),
        patch("omnivore.pipeline.handlers.video._extract_audio"),
        patch("omnivore.pipeline.handlers.video._extract_frames", side_effect=fake_extract_frames),
        patch("omnivore.pipeline.handlers.video.get_settings", return_value=_mock_settings_video()),
        patch("omnivore.pipeline.enrichers.llm_client.llm_configured", return_value=True),
        patch("omnivore.pipeline.enrichers.vision.describe_image", side_effect=_fake_describe),
    ):
        result = await VideoHandler().extract(_make_blob(), _make_ctx())

    vision_frags = [f for f in result.fragments if f.kind == "vision_caption"]
    assert len(vision_frags) == 2
    assert result.metadata["vision_frames_captioned"] == 2


async def test_no_vision_captions_when_llm_not_configured():
    """When llm_configured() is False, no frame extraction or vision calls happen."""
    segs = [_make_segment(" Hello.", 0.0, 1.0)]
    model = _stub_model(segs, _make_info())

    with (
        patch.object(VideoHandler, "_get_model", return_value=model),
        patch("omnivore.pipeline.handlers.video._extract_audio"),
        patch("omnivore.pipeline.handlers.video.get_settings", return_value=_mock_settings_video()),
        patch("omnivore.pipeline.enrichers.llm_client.llm_configured", return_value=False),
        patch("omnivore.pipeline.handlers.video._extract_frames") as mock_frames,
    ):
        result = await VideoHandler().extract(_make_blob(), _make_ctx())

    mock_frames.assert_not_called()
    assert all(f.kind != "vision_caption" for f in result.fragments)


async def test_fragments_sorted_by_timestamp():
    """Transcript and vision_caption fragments are sorted by start_ms after extraction."""
    segs = [
        _make_segment(" Opening narration.", 0.0, 5.0),
        _make_segment(" Later narration.", 60.0, 65.0),
    ]
    model = _stub_model(segs, _make_info(duration=90.0))
    fake_extract_frames, _ = _stub_frame_dir(None, frame_count=3)  # frames at 0s, 30s, 60s

    async def _fake_describe(img_bytes, *, settings, mime_type="image/jpeg"):
        return "A scene."

    with (
        patch.object(VideoHandler, "_get_model", return_value=model),
        patch("omnivore.pipeline.handlers.video._extract_audio"),
        patch("omnivore.pipeline.handlers.video._extract_frames", side_effect=fake_extract_frames),
        patch("omnivore.pipeline.handlers.video.get_settings",
              return_value=_mock_settings_video(interval=30)),
        patch("omnivore.pipeline.enrichers.llm_client.llm_configured", return_value=True),
        patch("omnivore.pipeline.enrichers.vision.describe_image", side_effect=_fake_describe),
    ):
        result = await VideoHandler().extract(_make_blob(), _make_ctx())

    start_times = [f.position.start_ms for f in result.fragments
                   if isinstance(f.position, TimePosition)]
    assert start_times == sorted(start_times)


async def test_vision_frame_failure_does_not_fail_document():
    """If _extract_frames raises, transcript is still indexed and no exception propagates."""
    segs = [_make_segment(" Transcript survives.", 0.0, 3.0)]
    model = _stub_model(segs, _make_info())

    with (
        patch.object(VideoHandler, "_get_model", return_value=model),
        patch("omnivore.pipeline.handlers.video._extract_audio"),
        patch("omnivore.pipeline.handlers.video._extract_frames",
              side_effect=RuntimeError("ffmpeg not found")),
        patch("omnivore.pipeline.handlers.video.get_settings", return_value=_mock_settings_video()),
        patch("omnivore.pipeline.enrichers.llm_client.llm_configured", return_value=True),
    ):
        result = await VideoHandler().extract(_make_blob(), _make_ctx())

    transcript_frags = [f for f in result.fragments if f.kind == "transcript"]
    assert len(transcript_frags) == 1
    assert transcript_frags[0].content == "Transcript survives."
    assert all(f.kind != "vision_caption" for f in result.fragments)


async def test_extract_frames_ffmpeg_command():
    """_extract_frames issues correct fps filter and vframes cap."""
    import subprocess

    captured_cmds: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        captured_cmds.append(cmd)
        result = MagicMock()
        result.returncode = 0
        return result

    with patch.object(subprocess, "run", side_effect=_fake_run):
        from omnivore.pipeline.handlers.video import _extract_frames
        _extract_frames("/tmp/vid.mp4", "/tmp/frames", interval=30, max_frames=5)

    assert len(captured_cmds) == 1
    cmd = captured_cmds[0]
    assert "fps=1/30" in " ".join(cmd)
    assert "5" in cmd  # -vframes 5
