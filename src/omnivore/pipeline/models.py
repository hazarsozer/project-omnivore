from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class PagePosition:
    page: int
    bbox: tuple[float, float, float, float] | None = None


@dataclass
class TimePosition:
    start_ms: int
    end_ms: int


@dataclass
class RowPosition:
    row_index: int
    sheet: str | None = None


PositionRef = PagePosition | TimePosition | RowPosition | dict


@dataclass
class Block:
    block_id: uuid.UUID = field(default_factory=uuid.uuid4)
    parent_id: uuid.UUID | None = None
    kind: Literal["heading", "paragraph", "list", "table", "figure", "caption", "formula", "code"] = "paragraph"
    level: int | None = None
    reading_order: int = 0
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    text: str | None = None


@dataclass
class Fragment:
    fragment_id: uuid.UUID = field(default_factory=uuid.uuid4)
    kind: Literal["text", "caption", "transcript", "ocr", "code", "table_row", "vision_caption"] = "text"
    content: str = ""
    position: PositionRef = field(default_factory=dict)
    confidence: float | None = None
    language: str | None = None
    source_block_ids: list[uuid.UUID] = field(default_factory=list)
    heading_path: list[str] = field(default_factory=list)
    table_lineage: dict | None = None


@dataclass
class StructuredTable:
    table_id: uuid.UUID = field(default_factory=uuid.uuid4)
    name: str = ""
    headers: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    page: int | None = None
    sheet: str | None = None


@dataclass
class ExtractionResult:
    document_id: uuid.UUID
    fragments: list[Fragment] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    tables: list[StructuredTable] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    source_handler: str = ""
    handler_version: str = ""


@dataclass
class Chunk:
    """Output unit of the chunker — maps 1:1 to a core.chunks row."""
    document_id: uuid.UUID
    tenant_id: uuid.UUID
    ordinal: int
    kind: str
    content: str
    token_count: int
    position: dict
    heading_path: list[str]
    source_block_ids: list[uuid.UUID]
    table_lineage: dict | None = None
    language: str | None = None
    confidence: float | None = None
