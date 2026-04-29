# Omnivore

A universal document ingestion pipeline that accepts any file format, extracts structured intelligence (metadata, summaries, sentiment, entities, embeddings), and routes output to a relational database or a RAG (Retrieval-Augmented Generation) vector store based on downstream use.

## Supported Formats

| Category | Formats |
|----------|---------|
| Documents | PDF, DOCX, PPTX, XLSX, TXT, MD, HTML |
| Images | JPEG, PNG, WEBP, TIFF, GIF |
| Audio | MP3, WAV, M4A, FLAC, OGG |
| Video | MP4, MOV, MKV, AVI, WEBM |
| Data | JSON, CSV, XML, YAML |

## Extracted Intelligence

- **Metadata** — file type, size, duration, resolution, author, creation date, EXIF
- **Summary** — LLM-generated abstractive summary
- **Sentiment** — document-level and section-level sentiment scores
- **Entities** — named entities (people, orgs, locations, dates)
- **Embeddings** — dense vector representation for semantic search
- **Transcription** — audio/video speech-to-text (Whisper)
- **OCR** — image and scanned PDF text extraction

## Output Targets

- **Relational DB** — structured metadata, sentiment, entities (PostgreSQL)
- **Vector Store** — chunked embeddings for RAG (pgvector / Qdrant / Weaviate)
- **Both** — default for most document types

## Architecture

See [`docs/architecture.md`](docs/architecture.md) for the full system design.

## Getting Started

> Setup instructions will be added after architecture is finalized.

## License

MIT
