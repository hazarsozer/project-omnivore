# Contributing to Omnivore

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) — used for all Python dependency management and script execution
- Docker and Docker Compose — for Postgres, Redis, and MinIO
- Node.js 18+ — only if you work on the frontend (`frontend/`)

## Dev setup

```bash
git clone https://github.com/<your-org>/omnivore
cd omnivore

# Install all Python dependencies
uv sync

# Copy and configure environment
cp .env.example .env
# Edit .env: generate JWT keys, set ADMIN_BOOTSTRAP_TOKEN (see README Quickstart)

# Start infrastructure
docker compose up -d postgres redis minio

# Run migrations
uv run alembic upgrade head

# Start API and worker (two terminals)
uv run uvicorn omnivore.api.main:app --reload
uv run python -m omnivore.worker.main
```

## Running tests

The test suite requires the infrastructure to be running.

```bash
# Full suite
ADMIN_BOOTSTRAP_TOKEN=test-admin-bootstrap-for-integration-only uv run pytest tests/ -q

# Specific module
uv run pytest tests/unit/pipeline/ -q

# With coverage
uv run pytest tests/ --cov=src/omnivore --cov-report=term-missing -q
```

Integration tests use a real database and Redis — they are not mocked. The `ADMIN_BOOTSTRAP_TOKEN` value above is a well-known test value checked for in the test fixtures; don't use it in production.

## Linting

```bash
uv run ruff check src/ tests/
```

No warnings are expected on a clean checkout. Fix all issues before opening a PR.

## Project layout

```
src/omnivore/
  api/            FastAPI app, routes, request/response schemas
  auth/           API key hashing, JWT issuance, rate limiting, RLS helpers
  db/             SQLAlchemy 2.0 models, session factory
  pipeline/       Extraction core: handlers, chunker, embeddings, enrichers, routing
    handlers/     One file per format (pdf.py, docx.py, audio.py, …)
    enrichers/    language.py, ner.py, summarizer.py
  worker/         ARQ worker entry points (CPU and GPU queues)
  observability/  OTel setup, Prometheus metrics definitions
  config.py       Settings — pydantic-settings, all env vars defined here
  constants.py    Shared constants (DEFAULT_TENANT_ID, GPU_QUEUE_NAME)

tests/
  unit/           Tests that do not require running infrastructure
  integration/    Tests against a live Postgres + Redis stack

eval/
  fixtures/       Gold-standard test documents and expected outputs
  harness.py      Eval runner — measures recall@1, nDCG@3, chunk_faithfulness

frontend/
  app/            Next.js 15 App Router pages
  components/     shadcn/ui-based components

observability/
  tempo.yaml
  prometheus.yml
  loki-config.yaml
  promtail-config.yaml
  grafana/provisioning/   Auto-provisioned Grafana datasources and dashboards
```

## Adding a format handler

1. Create `src/omnivore/pipeline/handlers/<format>.py`.
2. Implement the `FormatHandler` protocol from `omnivore.pipeline.registry`:
   - Required attributes: `name`, `version`, `accepts` (set of MIME types), `cost_class` (`"cpu"` or `"gpu"`), `timeout_seconds`
   - Required method: `async def extract(blob: BlobRef, ctx: IngestContext) -> ExtractionResult`
3. Read file bytes with `await ctx.read_blob()` — never open MinIO or disk directly.
4. For large files (audio, video), use `ctx.stream_blob()` to stream bytes to a temp file rather than loading everything into RAM.
5. Register the handler in `pyproject.toml`:
   ```toml
   [project.entry-points."omnivore.handlers"]
   my_format = "omnivore.pipeline.handlers.my_format:MyFormatHandler"
   ```
6. Add at least one fixture under `eval/fixtures/` and verify it with:
   ```bash
   uv run python -m eval.harness --fixture-ids <your-fixture-id>
   ```
7. Write unit tests in `tests/unit/pipeline/handlers/test_<format>.py`.

## Commit conventions

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>: <short description>

<optional body>
```

Types: `feat`, `fix`, `docs`, `test`, `chore`, `refactor`, `perf`, `ci`

Examples:
- `feat: add PPTX handler`
- `fix: handle empty PDF pages without crashing`
- `test: add integration test for retry endpoint`

## PR checklist

Before marking a PR ready for review:

- [ ] `uv run pytest tests/ -q` passes (with infra running)
- [ ] `uv run ruff check src/ tests/` reports no issues
- [ ] If a handler was added or changed: eval harness run (`uv run python -m eval.harness --fixture-ids ...`) and results noted in the PR description
- [ ] No hardcoded secrets, credentials, or tokens in any file
- [ ] New env vars added to both `src/omnivore/config.py` and `.env.example`
- [ ] Alembic migration added if the database schema changed
- [ ] New handler registered in `pyproject.toml` entry points
