# Omnivore Frontend

Next.js 16 + React 19 + Tailwind v4 + shadcn/ui admin UI for the Omnivore document ingestion pipeline.

## Pages

| Route | What |
|---|---|
| `/documents` | Drag-drop upload, document list (auto-refreshes while jobs are active), status filter |
| `/documents/[id]` | Detail view — status, summary, entities, routing decision, metadata, retry button |
| `/search` | BM25 / vector / hybrid (RRF) search across indexed chunks |
| `/admin` | Readiness checks (Postgres, Redis, MinIO) + registered handlers |

## Setup

```bash
npm install
cp .env.example .env.local   # NEXT_PUBLIC_API_URL defaults to http://localhost:8000
npm run dev                  # http://localhost:3000
```

The FastAPI must be running on the configured port — see the root [README](../README.md).

## Stack notes

- **Server vs Client Components**: pages that touch state/effects are `"use client"`; static shells are Server Components.
- **No SSR data fetching**: every screen fetches from the API on mount (the API is the source of truth and CORS is wide-open on the backend).
- **Upload progress**: `XMLHttpRequest` instead of `fetch` because the Fetch API doesn't expose upload progress events.
- **Auto-refresh**: list view and detail view poll every 2–3s while a document is in an active status (`queued`/`routing`/`extracting`/`enriching`).
