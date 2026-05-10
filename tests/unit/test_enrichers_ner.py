from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

from omnivore.pipeline.enrichers.ner import _KEPT_LABELS, ExtractedEntity, extract_entities
from omnivore.pipeline.models import Chunk


def _make_chunk(content: str, kind: str = "text", ordinal: int = 0) -> Chunk:
    return Chunk(
        document_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        ordinal=ordinal,
        kind=kind,
        content=content,
        token_count=len(content.split()),
        position={},
        heading_path=[],
        source_block_ids=[],
    )


class TestExtractedEntity:
    def test_fields(self):
        ent = ExtractedEntity(label="PERSON", value="John Smith", normalized="john smith", confidence=0.9)
        assert ent.label == "PERSON"
        assert ent.value == "John Smith"
        assert ent.normalized == "john smith"
        assert ent.chunk_id is None


class TestExtractEntities:
    def test_returns_empty_when_model_unavailable(self):
        with patch("omnivore.pipeline.enrichers.ner._nlp", return_value=None):
            result = extract_entities([_make_chunk("Apple Inc is in California.")])
        assert result == []

    def test_extracts_person_and_org(self):
        chunks = [_make_chunk("Elon Musk founded Tesla in California.")]
        result = extract_entities(chunks)
        labels = {e.label for e in result}
        # spaCy sm should find at least one of: PERSON, ORG, GPE
        assert labels & _KEPT_LABELS

    def test_deduplication(self):
        # Same entity text in two chunks — should appear only once
        chunks = [
            _make_chunk("Apple Inc is a technology company."),
            _make_chunk("Apple Inc releases new products every year."),
        ]
        result = extract_entities(chunks)
        apple_orgs = [e for e in result if e.normalized == "apple inc"]
        assert len(apple_orgs) == 1

    def test_empty_chunks(self):
        assert extract_entities([]) == []

    def test_chunk_id_is_none(self):
        chunks = [_make_chunk("Microsoft is a software company based in Redmond, Washington.")]
        result = extract_entities(chunks)
        for ent in result:
            assert ent.chunk_id is None

    def test_handles_spacy_exception_gracefully(self):
        mock_nlp = MagicMock()
        mock_nlp.side_effect = Exception("spaCy crashed")
        with patch("omnivore.pipeline.enrichers.ner._nlp", return_value=mock_nlp):
            # Should not raise — returns empty
            result = extract_entities([_make_chunk("Some text.")])
        assert isinstance(result, list)

    def test_kept_labels_subset(self):
        assert "PERSON" in _KEPT_LABELS
        assert "ORG" in _KEPT_LABELS
        assert "GPE" in _KEPT_LABELS
        assert "LOC" in _KEPT_LABELS
