from __future__ import annotations

import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from omnivore.pipeline.models import Chunk

logger = structlog.get_logger(__name__)

# Entity labels emitted by en_core_web_sm that we want to capture.
_KEPT_LABELS = frozenset({
    "PERSON", "ORG", "GPE", "LOC", "PRODUCT", "EVENT",
    "WORK_OF_ART", "LAW", "LANGUAGE", "MONEY", "DATE", "TIME",
})


@dataclass
class ExtractedEntity:
    label: str       # e.g. "PERSON", "ORG"
    value: str       # original surface form
    normalized: str  # lowercased+stripped
    confidence: float
    chunk_id: uuid.UUID | None = None


@lru_cache(maxsize=1)
def _nlp():
    import spacy
    try:
        return spacy.load("en_core_web_sm", disable=["tagger", "parser", "lemmatizer"])
    except OSError:
        logger.warning("ner.model_not_found", model="en_core_web_sm")
        return None


def extract_entities(chunks: list[Chunk]) -> list[ExtractedEntity]:
    """Run spaCy NER over all chunks. Returns deduplicated entity list."""
    nlp = _nlp()
    if nlp is None:
        return []

    seen: set[tuple[str, str]] = set()
    results: list[ExtractedEntity] = []

    for chunk in chunks:
        try:
            doc = nlp(chunk.content[:10_000])  # spaCy limit guard
        except Exception:
            logger.warning("ner.chunk_failed", chunk_id=str(chunk.document_id))
            continue

        for ent in doc.ents:
            if ent.label_ not in _KEPT_LABELS:
                continue
            normalized = ent.text.strip().lower()
            if not normalized:
                continue
            key = (ent.label_, normalized)
            if key in seen:
                continue
            seen.add(key)
            results.append(ExtractedEntity(
                label=ent.label_,
                value=ent.text.strip(),
                normalized=normalized,
                confidence=1.0,  # spaCy sm doesn't expose per-entity confidence
                chunk_id=None,   # chunk linking deferred to Phase 4
            ))

    return results
