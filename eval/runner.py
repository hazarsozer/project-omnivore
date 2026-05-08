"""Run a single eval fixture through the real pipeline and compute structural metrics."""
from __future__ import annotations

import uuid
from pathlib import Path

import magic

from omnivore.pipeline.chunker import chunk_result
from omnivore.pipeline.context import BlobRef
from omnivore.pipeline.models import ExtractionResult
from omnivore.pipeline.registry import HandlerRegistry

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


class _DiskCtx:
    """IngestContext that reads from a local file instead of MinIO."""

    def __init__(self, path: Path, filename: str, config: dict) -> None:
        self.document_id = uuid.uuid4()
        self.tenant_id = _TENANT
        self.filename = filename
        self.config = config
        self._path = path

    async def read_blob(self) -> bytes:
        return self._path.read_bytes()

    async def stream_blob(self, chunk_size: int = 65_536):  # noqa: ANN201
        data = self._path.read_bytes()
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]


async def run_fixture(source_file: Path, meta: dict, registry: HandlerRegistry) -> dict:
    data = source_file.read_bytes()
    detected_mime = magic.from_buffer(data[:2048], mime=True)
    # meta.expected_mime overrides magic when it's provided — text-based formats
    # (markdown, TSV, etc.) are indistinguishable from text/plain by content alone.
    if "expected_mime" in meta:
        detected_mime = meta["expected_mime"]

    blob = BlobRef(
        bucket="eval",
        key=source_file.name,
        mime_type=detected_mime,
        size_bytes=len(data),
    )
    ctx = _DiskCtx(path=source_file, filename=source_file.name, config={})

    handler_cls = registry.resolve(detected_mime)
    if handler_cls is None:
        return {
            "error": f"No handler for MIME type: {detected_mime}",
            "detected_mime": detected_mime,
            "structural_checks": {},
            "structural_accuracy": 0.0,
        }

    handler = handler_cls()
    result = await handler.extract(blob, ctx)
    chunks = chunk_result(result, _TENANT)

    checks = _check_structural(result, chunks, meta)
    passed = sum(1 for v in checks.values() if v is True)
    total = sum(1 for v in checks.values() if v is not None)
    accuracy = passed / total if total else 1.0

    return {
        "detected_mime": detected_mime,
        "handler_used": handler.name,
        "handler_matched": handler.name == meta.get("expected_handler"),
        "fragment_count": len(result.fragments),
        "chunk_count": len(chunks),
        "table_count": len(result.tables),
        "warnings": result.warnings,
        "structural_checks": checks,
        "structural_accuracy": accuracy,
    }


def _check_structural(result: ExtractionResult, chunks: list, meta: dict) -> dict[str, bool | None]:
    checks: dict[str, bool | None] = {}

    fmin = meta.get("expected_fragment_count_min", 0)
    fmax = meta.get("expected_fragment_count_max", 10**9)
    checks["fragment_count_in_range"] = fmin <= len(result.fragments) <= fmax

    cmin = meta.get("expected_chunk_count_min", 0)
    cmax = meta.get("expected_chunk_count_max", 10**9)
    checks["chunk_count_in_range"] = cmin <= len(chunks) <= cmax

    if "expected_table_count" in meta:
        checks["table_count_matches"] = len(result.tables) == meta["expected_table_count"]

    if "expected_table_headers" in meta and result.tables:
        checks["table_headers_match"] = result.tables[0].headers == meta["expected_table_headers"]
    elif "expected_table_headers" in meta:
        checks["table_headers_match"] = False

    if "expected_row_count" in meta and result.tables:
        checks["row_count_matches"] = len(result.tables[0].rows) == meta["expected_row_count"]
    elif "expected_row_count" in meta:
        checks["row_count_matches"] = False

    if "noise_excluded" in meta:
        all_text = " ".join(f.content for f in result.fragments)
        for noise in meta["noise_excluded"]:
            checks[f"noise_absent:{noise[:30]}"] = noise not in all_text

    return checks
