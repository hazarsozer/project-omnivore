"""Integration test for IngestContext.stream_blob() against real MinIO.

Requires docker compose up -d minio

Verifies the headline Phase 2c feature: blob bytes are yielded in streaming
chunks without buffering the full file, and every byte is preserved.

Two size variants are tested:
  - Small (1 KB): fits in a single 64 KB chunk.
  - Large (200 KB): spans three 64 KB chunks, exercising the multi-iteration path.

stream_blob() output is also compared against read_blob() on the same object to
confirm both read paths return identical bytes.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import aioboto3
import pytest

from omnivore.config import get_settings
from omnivore.constants import DEFAULT_TENANT_ID
from omnivore.pipeline.context import BlobRef, IngestContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(settings, bucket: str, key: str, size: int) -> IngestContext:
    return IngestContext(
        document_id=uuid.uuid4(),
        tenant_id=DEFAULT_TENANT_ID,
        blob=BlobRef(bucket=bucket, key=key, mime_type="application/octet-stream", size_bytes=size),
        filename="test.bin",
        config={},
        _settings=settings,
    )


async def _collect(ctx: IngestContext, chunk_size: int = 65_536) -> tuple[list[bytes], bytes]:
    """Consume stream_blob() and return (chunk_list, full_bytes)."""
    chunks: list[bytes] = []
    async for chunk in ctx.stream_blob(chunk_size):
        chunks.append(chunk)
    return chunks, b"".join(chunks)


def _s3_client_kwargs(settings):
    endpoint = f"{'https' if settings.MINIO_SECURE else 'http'}://{settings.MINIO_ENDPOINT}"
    return dict(
        endpoint_url=endpoint,
        aws_access_key_id=settings.MINIO_ACCESS_KEY,
        aws_secret_access_key=settings.MINIO_SECRET_KEY.get_secret_value(),
        region_name="us-east-1",
    )


# ---------------------------------------------------------------------------
# Module-scoped fixture: upload test objects once, clean up after all tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def blob_fixtures():
    """Put two test objects into MinIO; yield metadata; delete on teardown."""
    settings = get_settings()
    bucket = settings.MINIO_BUCKET
    run_id = str(uuid.uuid4())[:8]

    small_key = f"test/stream-small-{run_id}.bin"
    large_key = f"test/stream-large-{run_id}.bin"

    small_data = b"A" * 1_024           # 1 KB — one chunk at default size
    large_data = os.urandom(200_000)    # 200 KB — three+ 64 KB chunks; random to catch corruption

    async def _upload() -> None:
        session = aioboto3.Session()
        async with session.client("s3", **_s3_client_kwargs(settings)) as s3:
            try:
                await s3.head_bucket(Bucket=bucket)
            except Exception:
                await s3.create_bucket(Bucket=bucket)
            await s3.put_object(Bucket=bucket, Key=small_key, Body=small_data)
            await s3.put_object(Bucket=bucket, Key=large_key, Body=large_data)

    asyncio.run(_upload())

    yield settings, bucket, small_key, small_data, large_key, large_data

    async def _delete() -> None:
        session = aioboto3.Session()
        async with session.client("s3", **_s3_client_kwargs(settings)) as s3:
            for key in (small_key, large_key):
                try:
                    await s3.delete_object(Bucket=bucket, Key=key)
                except Exception:
                    pass

    asyncio.run(_delete())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestStreamBlob:

    def test_small_blob_round_trips_exactly(self, blob_fixtures):
        settings, bucket, small_key, small_data, *_ = blob_fixtures
        ctx = _make_ctx(settings, bucket, small_key, len(small_data))

        _, result = asyncio.run(_collect(ctx))

        assert result == small_data

    def test_large_blob_round_trips_exactly(self, blob_fixtures):
        settings, bucket, _, _, large_key, large_data = blob_fixtures
        ctx = _make_ctx(settings, bucket, large_key, len(large_data))

        _, result = asyncio.run(_collect(ctx))

        assert result == large_data

    def test_large_blob_yields_multiple_chunks(self, blob_fixtures):
        """200 KB / 64 KB chunk_size → at least 3 iterations of the async loop."""
        settings, bucket, _, _, large_key, large_data = blob_fixtures
        ctx = _make_ctx(settings, bucket, large_key, len(large_data))

        chunk_list, _ = asyncio.run(_collect(ctx, chunk_size=65_536))

        assert len(chunk_list) >= 3

    def test_smaller_chunk_size_produces_more_chunks(self, blob_fixtures):
        """chunk_size parameter is forwarded to iter_chunks; smaller → more iterations."""
        settings, bucket, _, _, large_key, large_data = blob_fixtures
        ctx = _make_ctx(settings, bucket, large_key, len(large_data))

        chunks_8k, result_8k = asyncio.run(_collect(ctx, chunk_size=8_192))
        chunks_64k, result_64k = asyncio.run(_collect(ctx, chunk_size=65_536))

        # Both should reconstruct the same bytes
        assert result_8k == large_data
        assert result_64k == large_data
        # 8 KB chunk_size should produce more chunks than 64 KB
        assert len(chunks_8k) > len(chunks_64k)

    def test_stream_matches_read_blob(self, blob_fixtures):
        """stream_blob() and read_blob() return identical bytes for the same object."""
        settings, bucket, _, _, large_key, large_data = blob_fixtures
        ctx = _make_ctx(settings, bucket, large_key, len(large_data))

        async def _both():
            _, streamed = await _collect(ctx)
            direct = await ctx.read_blob()
            return streamed, direct

        streamed, direct = asyncio.run(_both())

        assert streamed == direct

    def test_nonexistent_key_raises(self, blob_fixtures):
        """Streaming a missing key propagates the S3 NoSuchKey error."""
        settings, bucket, *_ = blob_fixtures
        ctx = _make_ctx(settings, bucket, "test/does-not-exist.bin", 0)

        with pytest.raises(Exception):  # botocore ClientError: NoSuchKey
            asyncio.run(_collect(ctx))
