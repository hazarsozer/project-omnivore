"""Chunker invariant tests — no I/O, pure pipeline logic."""
from __future__ import annotations

import uuid

from omnivore.pipeline.chunker import chunk_result
from omnivore.pipeline.models import Block, ExtractionResult, Fragment, PagePosition

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


def make_result(fragments: list[Fragment], blocks: list[Block] | None = None) -> ExtractionResult:
    return ExtractionResult(
        document_id=uuid.uuid4(),
        fragments=fragments,
        blocks=blocks or [],
    )


def make_frag(content: str, heading_path: list[str] | None = None, kind: str = "text") -> Fragment:
    return Fragment(
        kind=kind,
        content=content,
        position=PagePosition(page=1),
        source_block_ids=[uuid.uuid4()],
        heading_path=heading_path or [],
    )


def dummy_block() -> Block:
    return Block(kind="paragraph", reading_order=0, text="x")


class TestChunkerPathSelection:
    def test_structured_path_when_blocks_present(self):
        """result.blocks non-empty → structured chunker separates heading boundaries."""
        frags = [
            make_frag("Section A text.", heading_path=["A"]),
            make_frag("Section B text.", heading_path=["B"]),
        ]
        chunks = chunk_result(make_result(frags, blocks=[dummy_block()]), TENANT)
        assert len(chunks) >= 2

    def test_flat_path_when_no_blocks(self):
        """result.blocks empty → flat chunker merges small fragments into one chunk."""
        frags = [make_frag("Alpha content."), make_frag("Beta content.")]
        chunks = chunk_result(make_result(frags), TENANT)
        assert len(chunks) == 1
        assert "Alpha" in chunks[0].content
        assert "Beta" in chunks[0].content


class TestChunkerInvariants:
    def test_empty_result_produces_no_chunks(self):
        chunks = chunk_result(make_result([]), TENANT)
        assert chunks == []

    def test_whitespace_only_fragments_skipped(self):
        frags = [make_frag("   "), make_frag("\n\t\n"), make_frag("\t")]
        chunks = chunk_result(make_result(frags, blocks=[dummy_block()]), TENANT)
        assert chunks == []

    def test_ordinals_are_sequential_from_zero(self):
        frags = [
            make_frag("Alpha.", heading_path=["A"]),
            make_frag("Beta.", heading_path=["B"]),
            make_frag("Gamma.", heading_path=["C"]),
        ]
        chunks = chunk_result(make_result(frags, blocks=[dummy_block()]), TENANT)
        assert [c.ordinal for c in chunks] == list(range(len(chunks)))

    def test_document_id_propagated_to_all_chunks(self):
        doc_id = uuid.uuid4()
        result = ExtractionResult(
            document_id=doc_id,
            fragments=[make_frag("Hello world.")],
            blocks=[dummy_block()],
        )
        chunks = chunk_result(result, TENANT)
        assert all(c.document_id == doc_id for c in chunks)

    def test_tenant_id_propagated_to_all_chunks(self):
        frags = [make_frag("Some content here.")]
        chunks = chunk_result(make_result(frags, blocks=[dummy_block()]), TENANT)
        assert all(c.tenant_id == TENANT for c in chunks)

    def test_source_block_ids_always_populated(self):
        frags = [make_frag("Content.", heading_path=["A"])]
        chunks = chunk_result(make_result(frags, blocks=[dummy_block()]), TENANT)
        for chunk in chunks:
            assert chunk.source_block_ids, f"chunk {chunk.ordinal} has empty source_block_ids"


class TestStructuredChunker:
    def test_heading_boundary_triggers_flush(self):
        """Section change always emits the current bucket as a chunk before starting a new one."""
        frags = [
            make_frag("Alpha content.", heading_path=["Alpha"]),
            make_frag("Beta content.", heading_path=["Beta"]),
        ]
        chunks = chunk_result(make_result(frags, blocks=[dummy_block()]), TENANT)
        assert len(chunks) >= 2
        assert chunks[0].heading_path == ["Alpha"]

    def test_three_sections_produce_three_or_more_chunks(self):
        frags = [
            make_frag("First section content.", heading_path=["One"]),
            make_frag("Second section content.", heading_path=["Two"]),
            make_frag("Third section content.", heading_path=["Three"]),
        ]
        chunks = chunk_result(make_result(frags, blocks=[dummy_block()]), TENANT)
        assert len(chunks) >= 3

    def test_token_overflow_splits_long_section(self):
        """Multiple fragments in one section totalling > 512 tokens → at least 2 chunks."""
        # ~100 tokens per fragment × 7 = ~700 tokens → must overflow 512
        def long_frag() -> Fragment:
            return make_frag("the quick brown fox " * 25, heading_path=["Long"])
        frags = [long_frag() for _ in range(7)]
        chunks = chunk_result(make_result(frags, blocks=[dummy_block()]), TENANT)
        assert len(chunks) >= 2

    def test_single_short_section_is_one_chunk(self):
        frags = [make_frag("Short content.", heading_path=["Only"])]
        chunks = chunk_result(make_result(frags, blocks=[dummy_block()]), TENANT)
        assert len(chunks) == 1
        assert chunks[0].heading_path == ["Only"]
        assert "Short content." in chunks[0].content

    def test_nested_heading_path_preserved(self):
        frag = make_frag("Nested section content.", heading_path=["Chapter 1", "Section 1.2"])
        chunks = chunk_result(make_result([frag], blocks=[dummy_block()]), TENANT)
        assert chunks[0].heading_path == ["Chapter 1", "Section 1.2"]

    def test_overlap_carries_previous_section_content(self):
        """After a section boundary flush, small overlap fragments appear in the next chunk."""
        frag_a = make_frag("overlap me", heading_path=["A"])      # small: ~3 tokens
        frag_b = make_frag("beta main content", heading_path=["B"])
        chunks = chunk_result(
            make_result([frag_a, frag_b], blocks=[dummy_block()]), TENANT
        )
        assert len(chunks) >= 2
        assert "overlap me" in chunks[0].content
        # frag_a is small enough to be carried as overlap into chunk[1]
        assert "beta main content" in chunks[1].content
        assert "overlap me" in chunks[1].content


class TestFlatChunker:
    def test_fragments_without_blocks_use_flat_path(self):
        """No blocks → flat path, no heading boundary enforcement."""
        frags = [
            make_frag("Alpha.", heading_path=["A"]),
            make_frag("Beta.", heading_path=["B"]),
        ]
        # Flat path ignores heading_path; both fit in 512 tokens → 1 chunk
        chunks = chunk_result(make_result(frags), TENANT)
        assert len(chunks) == 1

    def test_flat_overflow_splits_at_token_limit(self):
        """Flat path still splits when total tokens exceed max_tokens."""
        def long_frag() -> Fragment:
            return make_frag("the quick brown fox " * 25)
        frags = [long_frag() for _ in range(7)]
        chunks = chunk_result(make_result(frags), TENANT)
        assert len(chunks) >= 2

    def test_table_lineage_carried_in_structured_path(self):
        lineage = {"table_id": str(uuid.uuid4()), "row_idx": 0}
        frag = make_frag("row content", kind="table_row")
        frag.table_lineage = lineage
        # table_lineage is carried only in _chunk_structured (blocks non-empty)
        chunks = chunk_result(make_result([frag], blocks=[dummy_block()]), TENANT)
        assert chunks[0].table_lineage == lineage
