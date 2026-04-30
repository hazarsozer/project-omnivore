# Eval Fixtures

Representative documents for evaluating extraction quality, chunking faithfulness, and retrieval recall.

## Directory layout

Each fixture lives in its own subdirectory:

```
eval/fixtures/
  pdf-001/
    meta.json            # required — describes the fixture
    sample.pdf           # the actual document (fetched via download.sh)
    expected_chunks.json # optional — ground-truth chunks for faithfulness eval
    expected_entities.json
    queries.json         # optional — test queries + relevant chunk ids for recall eval
```

## `meta.json` schema

```json
{
  "fixture_id": "pdf-001",
  "format": "pdf",
  "description": "Dense academic paper, multi-column layout",
  "expected_handler": "pdf",
  "expected_chunk_count_range": [20, 80],
  "difficulty": "medium",
  "languages": ["en"],
  "has_tables": true,
  "has_formulas": false,
  "has_images": false
}
```

## Fixture catalog (target: 20 fixtures before Phase 1 ships)

| fixture_id | format | description |
|---|---|---|
| pdf-001 | pdf | Dense academic paper, multi-column |
| pdf-002 | pdf | Scanned document (image-only PDF) |
| pdf-003 | pdf | PDF with embedded tables |
| pdf-004 | pdf | Short single-page PDF |
| docx-001 | docx | Word doc with headings and bullet lists |
| docx-002 | docx | Word doc with embedded table |
| txt-001 | txt | Plain text, long prose |
| md-001 | md | Markdown with code blocks and headings |
| html-001 | html | Webpage with nav/aside noise |
| csv-001 | csv | Tabular data, 500 rows |
| json-001 | json | Array-of-objects (structured) |
| json-002 | json | Nested free-form JSON |
| xlsx-001 | xlsx | Spreadsheet, multiple sheets |
| audio-001 | audio | Short spoken clip (30s) |
| audio-002 | audio | Long interview (10 min) |
| video-001 | video | Short screen recording |
| img-001 | image | Photo with text overlay |
| img-002 | image | Scanned form |
| email-001 | eml | Email with attachment |
| archive-001 | zip | Zip containing multiple formats |

## Source files

Fixture source files are **not committed** (size, licensing). Fetch them:

```bash
bash eval/fixtures/download.sh    # written in Phase 1
```

Files are stored in an object store location defined by `FIXTURE_STORE_URL` in `.env`.

## Metrics evaluated (Phase 3+)

| metric | description |
|---|---|
| extraction_accuracy | fraction of expected content present in extracted fragments |
| chunk_faithfulness | how well chunks preserve source meaning (RAGChecker-style) |
| retrieval_recall@10 | fraction of relevant chunks in top-10 results |
| nDCG@10 | ranking quality of retrieval results |
