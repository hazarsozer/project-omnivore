from __future__ import annotations

import io
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import aioboto3 as _aioboto3

_S3_SESSION = _aioboto3.Session()


@dataclass
class BlobRef:
    bucket: str
    key: str
    mime_type: str
    size_bytes: int


@dataclass
class IngestContext:
    document_id: uuid.UUID
    tenant_id: uuid.UUID
    blob: BlobRef
    filename: str
    config: dict[str, Any]
    _settings: Any = field(repr=False)  # omnivore.config.Settings; avoid circular import

    async def read_blob(self) -> bytes:
        s = self._settings
        endpoint = f"{'https' if s.MINIO_SECURE else 'http'}://{s.MINIO_ENDPOINT}"
        async with _S3_SESSION.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=s.MINIO_ACCESS_KEY,
            aws_secret_access_key=s.MINIO_SECRET_KEY.get_secret_value(),
            region_name="us-east-1",
        ) as s3:
            resp = await s3.get_object(Bucket=self.blob.bucket, Key=self.blob.key)
            return await resp["Body"].read()

    async def stream_blob(self, chunk_size: int = 65_536) -> AsyncIterator[bytes]:
        """Yield blob bytes from MinIO in chunks without buffering the full file."""
        s = self._settings
        endpoint = f"{'https' if s.MINIO_SECURE else 'http'}://{s.MINIO_ENDPOINT}"
        async with _S3_SESSION.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=s.MINIO_ACCESS_KEY,
            aws_secret_access_key=s.MINIO_SECRET_KEY.get_secret_value(),
            region_name="us-east-1",
        ) as s3:
            resp = await s3.get_object(Bucket=self.blob.bucket, Key=self.blob.key)
            async for chunk in resp["Body"].iter_chunks(chunk_size):
                yield chunk

    async def read_blob_stream(self) -> io.BytesIO:
        return io.BytesIO(await self.read_blob())
