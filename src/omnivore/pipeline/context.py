from __future__ import annotations

import io
import uuid
from dataclasses import dataclass, field
from typing import Any


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
        import aioboto3

        s = self._settings
        endpoint = f"{'https' if s.MINIO_SECURE else 'http'}://{s.MINIO_ENDPOINT}"
        session = aioboto3.Session()
        async with session.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=s.MINIO_ACCESS_KEY,
            aws_secret_access_key=s.MINIO_SECRET_KEY.get_secret_value(),
            region_name="us-east-1",
        ) as s3:
            resp = await s3.get_object(Bucket=self.blob.bucket, Key=self.blob.key)
            return await resp["Body"].read()

    async def read_blob_stream(self) -> io.BytesIO:
        return io.BytesIO(await self.read_blob())
