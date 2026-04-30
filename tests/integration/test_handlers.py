"""Handler integration tests — real handler code, in-memory bytes, no MinIO."""
from __future__ import annotations

import json
import uuid

from omnivore.pipeline.context import BlobRef

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


class FakeCtx:
    """Replaces IngestContext: reads bytes from memory instead of MinIO."""

    def __init__(self, data: bytes, filename: str = "test.bin", config: dict | None = None):
        self.document_id = uuid.uuid4()
        self.tenant_id = TENANT
        self.filename = filename
        self.config = config or {}
        self._data = data

    async def read_blob(self) -> bytes:
        return self._data


def blob(mime: str, data: bytes) -> BlobRef:
    return BlobRef(bucket="test", key="test", mime_type=mime, size_bytes=len(data))


# ---------------------------------------------------------------------------
# PlainTextHandler
# ---------------------------------------------------------------------------

class TestPlainTextHandler:
    async def test_splits_on_double_newline(self):
        from omnivore.pipeline.handlers.text import PlainTextHandler

        content = b"First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
        ctx = FakeCtx(content, filename="doc.txt")
        result = await PlainTextHandler().extract(blob("text/plain", content), ctx)

        assert result.document_id == ctx.document_id
        assert len(result.fragments) == 3
        assert result.fragments[0].content == "First paragraph."
        assert result.fragments[1].content == "Second paragraph."
        assert result.fragments[2].content == "Third paragraph."

    async def test_empty_file_produces_no_fragments(self):
        from omnivore.pipeline.handlers.text import PlainTextHandler

        result = await PlainTextHandler().extract(blob("text/plain", b""), FakeCtx(b""))
        assert result.fragments == []
        assert result.blocks == []

    async def test_source_handler_name(self):
        from omnivore.pipeline.handlers.text import PlainTextHandler

        result = await PlainTextHandler().extract(blob("text/plain", b"hello"), FakeCtx(b"hello"))
        assert result.source_handler == "text"

    async def test_metadata_contains_char_count(self):
        from omnivore.pipeline.handlers.text import PlainTextHandler

        content = b"Hello world."
        result = await PlainTextHandler().extract(blob("text/plain", content), FakeCtx(content))
        assert result.metadata["char_count"] == len("Hello world.")

    async def test_source_block_ids_populated(self):
        from omnivore.pipeline.handlers.text import PlainTextHandler

        content = b"Para one.\n\nPara two."
        result = await PlainTextHandler().extract(blob("text/plain", content), FakeCtx(content))
        for frag in result.fragments:
            assert frag.source_block_ids


# ---------------------------------------------------------------------------
# MarkdownHandler
# ---------------------------------------------------------------------------

class TestMarkdownHandler:
    async def test_heading_path_set_on_paragraph_fragments(self):
        from omnivore.pipeline.handlers.text import MarkdownHandler

        content = b"# Main\n\nParagraph under main.\n\n## Sub\n\nParagraph under sub."
        ctx = FakeCtx(content, filename="doc.md")
        result = await MarkdownHandler().extract(blob("text/markdown", content), ctx)

        text_frags = [f for f in result.fragments if f.kind == "text"]
        assert len(text_frags) == 2
        assert text_frags[0].heading_path == ["Main"]
        assert text_frags[1].heading_path == ["Main", "Sub"]

    async def test_code_fence_produces_code_fragment(self):
        from omnivore.pipeline.handlers.text import MarkdownHandler

        content = b"# Example\n\n```python\nprint('hello')\n```\n"
        ctx = FakeCtx(content, filename="doc.md")
        result = await MarkdownHandler().extract(blob("text/markdown", content), ctx)

        code_frags = [f for f in result.fragments if f.kind == "code"]
        assert len(code_frags) == 1
        assert "print" in code_frags[0].content

    async def test_no_heading_gives_empty_path(self):
        from omnivore.pipeline.handlers.text import MarkdownHandler

        content = b"Just a paragraph with no heading."
        result = await MarkdownHandler().extract(blob("text/markdown", content), FakeCtx(content))
        text_frags = [f for f in result.fragments if f.kind == "text"]
        assert len(text_frags) == 1
        assert text_frags[0].heading_path == []

    async def test_source_handler_name(self):
        from omnivore.pipeline.handlers.text import MarkdownHandler

        result = await MarkdownHandler().extract(blob("text/markdown", b"# Hi\n"), FakeCtx(b"# Hi\n"))
        assert result.source_handler == "markdown"


# ---------------------------------------------------------------------------
# CsvHandler
# ---------------------------------------------------------------------------

class TestCsvHandler:
    async def test_table_shape_matches_csv(self):
        from omnivore.pipeline.handlers.csv_handler import CsvHandler

        content = b"name,age,city\nAlice,30,Boston\nBob,25,NYC\n"
        ctx = FakeCtx(content, filename="people.csv")
        result = await CsvHandler().extract(blob("text/csv", content), ctx)

        assert len(result.tables) == 1
        table = result.tables[0]
        assert table.headers == ["name", "age", "city"]
        assert len(table.rows) == 2
        assert table.rows[0]["name"] == "Alice"
        assert table.rows[1]["city"] == "NYC"

    async def test_row_fragments_generated_with_table_lineage(self):
        from omnivore.pipeline.handlers.csv_handler import CsvHandler

        content = b"id,value\n1,a\n2,b\n3,c\n"
        ctx = FakeCtx(content)
        result = await CsvHandler().extract(blob("text/csv", content), ctx)

        assert len(result.fragments) == 3
        assert all(f.kind == "table_row" for f in result.fragments)
        assert all(f.table_lineage is not None for f in result.fragments)

    async def test_row_fragment_limit_at_500(self):
        from omnivore.pipeline.handlers.csv_handler import CsvHandler

        rows = "\n".join(f"{i},val{i}" for i in range(600))
        content = ("id,value\n" + rows).encode()
        ctx = FakeCtx(content)
        result = await CsvHandler().extract(blob("text/csv", content), ctx)

        assert len(result.tables[0].rows) == 600  # table has all rows
        assert len(result.fragments) == 500         # RAG fragments capped at 500

    async def test_row_rag_disabled_via_config(self):
        from omnivore.pipeline.handlers.csv_handler import CsvHandler

        content = b"id,value\n1,a\n2,b\n"
        ctx = FakeCtx(content, config={"row_level_rag": False})
        result = await CsvHandler().extract(blob("text/csv", content), ctx)

        assert result.tables  # table still produced
        assert result.fragments == []  # no row fragments

    async def test_metadata_has_row_and_col_count(self):
        from omnivore.pipeline.handlers.csv_handler import CsvHandler

        content = b"a,b,c\n1,2,3\n4,5,6\n"
        result = await CsvHandler().extract(blob("text/csv", content), FakeCtx(content))
        assert result.metadata["row_count"] == 2
        assert result.metadata["col_count"] == 3


# ---------------------------------------------------------------------------
# JsonHandler
# ---------------------------------------------------------------------------

class TestJsonHandler:
    async def test_array_of_objects_produces_table_and_row_fragments(self):
        from omnivore.pipeline.handlers.json_handler import JsonHandler

        data = [{"id": i, "name": f"item{i}"} for i in range(5)]
        content = json.dumps(data).encode()
        result = await JsonHandler().extract(blob("application/json", content), FakeCtx(content))

        assert len(result.tables) == 1
        assert result.tables[0].headers == ["id", "name"]
        assert len(result.tables[0].rows) == 5
        assert len(result.fragments) == 5
        assert all(f.kind == "table_row" for f in result.fragments)

    async def test_free_form_json_produces_flat_fragments(self):
        from omnivore.pipeline.handlers.json_handler import JsonHandler

        data = {"project": "omnivore", "version": "0.1", "nested": {"key": "value"}}
        content = json.dumps(data).encode()
        result = await JsonHandler().extract(blob("application/json", content), FakeCtx(content))

        assert result.tables == []
        assert len(result.fragments) >= 3
        all_content = " ".join(f.content for f in result.fragments)
        assert "omnivore" in all_content
        assert "value" in all_content

    async def test_invalid_json_produces_warning(self):
        from omnivore.pipeline.handlers.json_handler import JsonHandler

        content = b"not valid json {"
        result = await JsonHandler().extract(blob("application/json", content), FakeCtx(content))

        assert len(result.warnings) > 0
        assert result.tables == []
        assert result.fragments == []

    async def test_row_fragments_have_table_lineage(self):
        from omnivore.pipeline.handlers.json_handler import JsonHandler

        data = [{"x": 1}, {"x": 2}]
        content = json.dumps(data).encode()
        result = await JsonHandler().extract(blob("application/json", content), FakeCtx(content))

        for frag in result.fragments:
            assert frag.table_lineage is not None
            assert "table_id" in frag.table_lineage


# ---------------------------------------------------------------------------
# HtmlHandler
# ---------------------------------------------------------------------------

class TestHtmlHandler:
    async def test_nav_aside_footer_excluded(self):
        from omnivore.pipeline.handlers.html import HtmlHandler

        content = b"""<!DOCTYPE html><html><body>
            <nav>Nav link one Nav link two</nav>
            <main>
                <h1>Main Title</h1>
                <p>Important content here.</p>
            </main>
            <aside>Sidebar noise to exclude</aside>
            <footer>Footer noise to exclude</footer>
        </body></html>"""
        result = await HtmlHandler().extract(blob("text/html", content), FakeCtx(content))

        all_text = " ".join(f.content for f in result.fragments)
        assert "Important content here." in all_text
        assert "Sidebar noise" not in all_text
        assert "Footer noise" not in all_text
        assert "Nav link" not in all_text

    async def test_heading_path_tracked_through_sections(self):
        from omnivore.pipeline.handlers.html import HtmlHandler

        content = b"""<!DOCTYPE html><html><body><main>
            <h1>Main Heading</h1>
            <p>Content under main.</p>
            <h2>Sub Heading</h2>
            <p>Content under sub.</p>
        </main></body></html>"""
        result = await HtmlHandler().extract(blob("text/html", content), FakeCtx(content))

        text_frags = [f for f in result.fragments if f.kind == "text"]
        assert len(text_frags) >= 2
        assert text_frags[0].heading_path == ["Main Heading"]
        assert text_frags[1].heading_path == ["Main Heading", "Sub Heading"]

    async def test_source_handler_name(self):
        from omnivore.pipeline.handlers.html import HtmlHandler

        content = b"<html><body><main><p>Hello</p></main></body></html>"
        result = await HtmlHandler().extract(blob("text/html", content), FakeCtx(content))
        assert result.source_handler == "html"

    async def test_metadata_has_title(self):
        from omnivore.pipeline.handlers.html import HtmlHandler

        content = b"""<html><head><title>My Page</title></head>
        <body><main><p>Content</p></main></body></html>"""
        result = await HtmlHandler().extract(blob("text/html", content), FakeCtx(content))
        assert result.metadata.get("title") == "My Page"

    async def test_empty_main_produces_no_fragments(self):
        from omnivore.pipeline.handlers.html import HtmlHandler

        content = b"<html><body><main></main></body></html>"
        result = await HtmlHandler().extract(blob("text/html", content), FakeCtx(content))
        assert result.fragments == []
