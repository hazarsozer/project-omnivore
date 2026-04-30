from __future__ import annotations

import json
from typing import Any, ClassVar

import structlog

from omnivore.pipeline.context import BlobRef, IngestContext
from omnivore.pipeline.models import ExtractionResult, Fragment, PagePosition, StructuredTable

logger = structlog.get_logger(__name__)


class JsonHandler:
    name: ClassVar[str] = "json"
    version: ClassVar[str] = "1.0.0"
    accepts: ClassVar[tuple[str, ...]] = ("application/json", "text/json")
    cost_class: ClassVar[str] = "io"
    timeout_seconds: ClassVar[int] = 60

    async def extract(self, blob: BlobRef, ctx: IngestContext) -> ExtractionResult:
        data = await ctx.read_blob()
        text = data.decode("utf-8", errors="replace")
        result = ExtractionResult(
            document_id=ctx.document_id,
            source_handler=self.name,
            handler_version=self.version,
        )

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            result.warnings.append(f"JSON parse error: {exc}")
            return result

        if _is_array_of_objects(parsed):
            headers = list(parsed[0].keys())
            rows = [{str(k): _stringify(v) for k, v in row.items()} for row in parsed]
            result.tables.append(
                StructuredTable(
                    name="data",
                    headers=headers,
                    rows=rows,
                )
            )
            result.metadata = {"format": "json", "shape": "array_of_objects", "row_count": len(rows)}

            # Per-row fragments for row-level RAG (capped at 500 rows)
            for i, row in enumerate(rows[:500]):
                content = " | ".join(f"{k}: {v}" for k, v in row.items())
                result.fragments.append(
                    Fragment(
                        kind="table_row",
                        content=content,
                        position={"row_index": i},
                        table_lineage={"table_id": str(result.tables[0].table_id), "row_idx": i},
                    )
                )
        else:
            result.metadata = {"format": "json", "shape": "free_form"}
            for key, value in _flatten(parsed):
                content = f"{key}: {_stringify(value)}"
                result.fragments.append(
                    Fragment(
                        kind="text",
                        content=content,
                        position=PagePosition(page=1),
                    )
                )

        logger.info(
            "json.extracted",
            document_id=str(ctx.document_id),
            shape=result.metadata.get("shape"),
            fragments=len(result.fragments),
        )
        return result


def _is_array_of_objects(data: Any) -> bool:
    return (
        isinstance(data, list)
        and len(data) > 0
        and all(isinstance(row, dict) for row in data[:10])
    )


def _stringify(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value) if value is not None else ""


def _flatten(obj: Any, prefix: str = "") -> list[tuple[str, Any]]:
    items: list[tuple[str, Any]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            full_key = f"{prefix}.{k}" if prefix else k
            items.extend(_flatten(v, full_key))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:100]):  # cap list expansion
            items.extend(_flatten(v, f"{prefix}[{i}]"))
    else:
        items.append((prefix, obj))
    return items
