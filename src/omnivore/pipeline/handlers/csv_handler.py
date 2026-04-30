from __future__ import annotations

import io
from typing import ClassVar

import structlog

from omnivore.pipeline.context import BlobRef, IngestContext
from omnivore.pipeline.models import ExtractionResult, Fragment, StructuredTable

logger = structlog.get_logger(__name__)

_ROW_FRAGMENT_LIMIT = 500


class CsvHandler:
    name: ClassVar[str] = "csv"
    version: ClassVar[str] = "1.0.0"
    accepts: ClassVar[tuple[str, ...]] = ("text/csv", "text/tab-separated-values", "application/csv")
    cost_class: ClassVar[str] = "io"
    timeout_seconds: ClassVar[int] = 60

    async def extract(self, blob: BlobRef, ctx: IngestContext) -> ExtractionResult:
        import polars as pl

        data = await ctx.read_blob()
        separator = "\t" if blob.mime_type == "text/tab-separated-values" else ","

        result = ExtractionResult(
            document_id=ctx.document_id,
            source_handler=self.name,
            handler_version=self.version,
        )

        try:
            df = pl.read_csv(io.BytesIO(data), separator=separator, infer_schema_length=500)
        except Exception as exc:
            result.warnings.append(f"CSV parse error: {exc}")
            return result

        headers = df.columns
        rows = [
            {col: (str(row[col]) if row[col] is not None else "") for col in headers}
            for row in df.to_dicts()
        ]
        table = StructuredTable(
            name=ctx.filename,
            headers=headers,
            rows=rows,
        )
        result.tables.append(table)
        result.metadata = {
            "format": "csv",
            "row_count": len(rows),
            "col_count": len(headers),
            "columns": headers,
        }

        row_rag_enabled = ctx.config.get("row_level_rag", True)
        if row_rag_enabled:
            for i, row in enumerate(rows[:_ROW_FRAGMENT_LIMIT]):
                content = " | ".join(f"{k}: {v}" for k, v in row.items() if v)
                result.fragments.append(
                    Fragment(
                        kind="table_row",
                        content=content,
                        position={"row_index": i},
                        table_lineage={"table_id": str(table.table_id), "row_idx": i},
                    )
                )

        logger.info(
            "csv.extracted",
            document_id=str(ctx.document_id),
            rows=len(rows),
            cols=len(headers),
            fragments=len(result.fragments),
        )
        return result
