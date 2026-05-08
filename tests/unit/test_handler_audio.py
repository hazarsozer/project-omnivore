"""Unit tests for the AudioHandler — faster-whisper is fully mocked."""
from __future__ import annotations

import math
import uuid
from unittest.mock import MagicMock, patch

from omnivore.pipeline.handlers.audio import AudioHandler
from omnivore.pipeline.models import TimePosition

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(doc_id: uuid.UUID | None = None, data: bytes = b"fake-audio-bytes") -> MagicMock:
    ctx = MagicMock()
    ctx.document_id = doc_id or uuid.uuid4()

    async def _stream_blob():
        yield data

    ctx.stream_blob = _stream_blob
    return ctx


def _make_blob(mime: str = "audio/mpeg") -> MagicMock:
    blob = MagicMock()
    blob.mime_type = mime
    blob.size_bytes = 1024
    return blob


def _make_segment(text: str, start: float, end: float, avg_logprob: float = -0.5) -> MagicMock:
    seg = MagicMock()
    seg.text = text
    seg.start = start
    seg.end = end
    seg.avg_logprob = avg_logprob
    return seg


def _make_transcribe_info(language: str = "en", prob: float = 0.99, duration: float = 10.0) -> MagicMock:
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
# Basic extraction
# ---------------------------------------------------------------------------

async def test_audio_handler_returns_transcript_fragments():
    doc_id = uuid.uuid4()
    segs = [
        _make_segment(" Hello world.", 0.0, 2.5),
        _make_segment(" How are you?", 2.5, 5.0),
    ]
    info = _make_transcribe_info()
    model = _stub_model(segs, info)

    with patch.object(AudioHandler, "_get_model", return_value=model):
        handler = AudioHandler()
        result = await handler.extract(_make_blob(), _make_ctx(doc_id))

    assert result.document_id == doc_id
    assert len(result.fragments) == 2
    assert all(f.kind == "transcript" for f in result.fragments)
    assert result.fragments[0].content == "Hello world."
    assert result.fragments[1].content == "How are you?"


async def test_audio_handler_time_positions_correct():
    segs = [_make_segment(" Test.", 1.2, 3.8)]
    info = _make_transcribe_info()
    model = _stub_model(segs, info)

    with patch.object(AudioHandler, "_get_model", return_value=model):
        result = await AudioHandler().extract(_make_blob(), _make_ctx())

    pos = result.fragments[0].position
    assert isinstance(pos, TimePosition)
    assert pos.start_ms == 1200
    assert pos.end_ms == 3800


async def test_audio_handler_confidence_is_exp_of_avg_logprob():
    avg_logprob = -0.3
    segs = [_make_segment(" Text.", 0.0, 1.0, avg_logprob=avg_logprob)]
    info = _make_transcribe_info()
    model = _stub_model(segs, info)

    with patch.object(AudioHandler, "_get_model", return_value=model):
        result = await AudioHandler().extract(_make_blob(), _make_ctx())

    expected = round(math.exp(avg_logprob), 3)
    assert result.fragments[0].confidence == expected


async def test_audio_handler_metadata_populated():
    segs = [_make_segment(" Hi.", 0.0, 1.0)]
    info = _make_transcribe_info(language="fr", prob=0.95, duration=42.0)
    model = _stub_model(segs, info)

    with patch.object(AudioHandler, "_get_model", return_value=model):
        result = await AudioHandler().extract(_make_blob("audio/wav"), _make_ctx())

    assert result.metadata["language"] == "fr"
    assert result.metadata["language_probability"] == 0.95
    assert result.metadata["duration_seconds"] == 42.0
    assert result.metadata["format"] == "audio"


async def test_audio_handler_language_set_on_fragments():
    segs = [_make_segment(" Bonjour.", 0.0, 1.0)]
    info = _make_transcribe_info(language="fr")
    model = _stub_model(segs, info)

    with patch.object(AudioHandler, "_get_model", return_value=model):
        result = await AudioHandler().extract(_make_blob(), _make_ctx())

    assert result.fragments[0].language == "fr"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

async def test_audio_handler_empty_segments_produces_no_fragments():
    model = _stub_model([], _make_transcribe_info())

    with patch.object(AudioHandler, "_get_model", return_value=model):
        result = await AudioHandler().extract(_make_blob(), _make_ctx())

    assert result.fragments == []


async def test_audio_handler_whitespace_only_segments_skipped():
    segs = [_make_segment("   ", 0.0, 1.0), _make_segment(" Real text. ", 1.0, 2.0)]
    info = _make_transcribe_info()
    model = _stub_model(segs, info)

    with patch.object(AudioHandler, "_get_model", return_value=model):
        result = await AudioHandler().extract(_make_blob(), _make_ctx())

    assert len(result.fragments) == 1
    assert result.fragments[0].content == "Real text."


# ---------------------------------------------------------------------------
# Streaming (Phase 2c — stream_blob replaces read_blob + size guard)
# ---------------------------------------------------------------------------

async def test_audio_handler_streams_large_blob_in_chunks():
    """Handler assembles multi-chunk stream into the correct transcript."""
    chunk_a = b"fake-" * 100
    chunk_b = b"audio-bytes" * 50

    async def _chunked_stream():
        yield chunk_a
        yield chunk_b

    ctx = MagicMock()
    ctx.document_id = uuid.uuid4()
    ctx.stream_blob = _chunked_stream

    segs = [_make_segment(" Hello.", 0.0, 1.0)]
    model = _stub_model(segs, _make_transcribe_info())

    with patch.object(AudioHandler, "_get_model", return_value=model):
        result = await AudioHandler().extract(_make_blob(), ctx)

    assert len(result.fragments) == 1
    assert result.fragments[0].content == "Hello."


# ---------------------------------------------------------------------------
# Handler protocol compliance
# ---------------------------------------------------------------------------

def test_audio_handler_cost_class_is_gpu():
    assert AudioHandler.cost_class == "gpu"


def test_audio_handler_accepts_common_audio_mimes():
    for mime in ("audio/mpeg", "audio/wav", "audio/flac", "audio/ogg", "audio/m4a"):
        assert mime in AudioHandler.accepts, f"{mime} not in accepts"


def test_audio_handler_source_handler_name():
    assert AudioHandler.name == "audio"
