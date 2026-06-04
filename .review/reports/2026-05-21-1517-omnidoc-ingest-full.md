# Crucible Review — omnidoc-ingest-full

_Review ID: 2026-05-21-1517-omnidoc-ingest-full · Generated: 2026-05-21T15:17:00Z · Project: api (python, typescript, sql / fastapi, arq, sqlalchemy, alembic, nextjs)_

---

## Verdict

**BLOCKED — 4.0/10**

Two independent critical findings each, on their own, block the stated 'safe to publish as-is' aim. team-privacy-compliance-reviewer flags that the MIT LICENSE is legally incompatible with the AGPL-3.0 PyMuPDF dependency — the only PDF backend, with no replaceable seam — making open-sourcing as-is not legally permissible. team-security-reviewer flags that ADMIN_BOOTSTRAP_TOKEN='change-me-before-first-run', SECRET_KEY='change-me-in-production', and MINIO_SECRET_KEY='omnivore123' ship as live fallbacks in config.py with no startup guard, meaning any repo reader can hit /v1/admin/tenants with the published default token. lead-senior-architect (BLOCK, 5/10) and lead-project-manager (BLOCK, 3/10 aim alignment) both converge on the same two blockers, and the PM's framing is decisive: two of four stated success criteria are hard-failed today, regardless of how clean the internals are.

---

## Executive Summary

This is a full-codebase open-source safety review of Omnivore, a Python/FastAPI document ingestion pipeline with a Next.js 15 admin frontend, seven shipped phases, and an ML-heavy enrichment layer. The user's question is narrow: is this safe to publish as-is under MIT?

The internals are genuinely strong. peer-python-reviewer returned a clean approve (9/10) across 100+ files — comprehensive type hints, structlog throughout, SecretStr for credentials, a Protocol-based handler registry, and an atomic Lua rate limiter. lead-senior-architect explicitly called out the architecture as internally well-structured: handler registry, NIR/Pydantic separation, WorkflowContext protocol, transactional outbox, and the RLS GUC pattern all hold together across seven phases. The structural problems are at the publication boundary, not inside the system.

Two blockers prevent publication today. The MIT LICENSE is legally incompatible with the AGPL-3.0 PyMuPDF dependency, and PyMuPDF is the only PDF backend with no PdfBackend protocol to swap it out — architecture.md Q4 flagged this risk and it materialized. Separately, three default secrets including ADMIN_BOOTSTRAP_TOKEN ship as live fallbacks in config.py with no startup guard, so any reader of the public repo can immediately hit the admin tenant endpoint.

Both are concrete, ~1-day fixes per the PM: pair them with the remaining HIGH-severity items (auth rate-limit, sessionStorage token storage, CORS wildcard, GITHUB_OUTPUT JWT key leak, LLM-off-by-default with disclosure) and the codebase clears the 'safe to publish' bar. The work is good; the publication boundary is not yet ready.

---

## What's Good

- Python implementation is idiomatically clean — peer-python-reviewer returned APPROVE (9/10) with zero findings across 100+ files, citing comprehensive type hints, structlog, dataclasses, context managers, Protocol-based handler registry, SecretStr for credentials, and atomic Lua rate-limit script.
- Architecture is internally well-structured across seven shipped phases — lead-senior-architect explicitly noted handler registry, NIR/Pydantic separation, WorkflowContext protocol, transactional outbox, and RLS GUC pattern all hold together; structural problems live at the publication boundary, not inside the system.
- Data/ML enrichment layer scored 7/10 from team-data-ml-reviewer with no HIGH findings — pluggable LLM provider layer (Anthropic/OpenAI/Google/Ollama), BGE-base embeddings, and Whisper/EasyOCR integration are sound.
- Security primitives are correctly chosen even where the surface around them is wrong — Argon2id for API key storage, RS256 JWT, FORCE ROW LEVEL SECURITY on six tables, and an atomic Lua token-bucket rate limiter are all in place.

---

## What's Concerning

- MIT LICENSE is legally incompatible with AGPL-3.0 PyMuPDF — the only PDF backend with no replaceable seam — making open-sourcing as-is not legally permissible (team-privacy-compliance-reviewer CRITICAL, lead-senior-architect CRITICAL, architecture.md Q4).
- Three default secrets ship as live fallbacks in config.py with no startup guard: ADMIN_BOOTSTRAP_TOKEN='change-me-before-first-run', SECRET_KEY='change-me-in-production', MINIO_SECRET_KEY='omnivore123' — any open-source repo reader can immediately authenticate against /v1/admin/tenants (team-security-reviewer CRITICAL, lead-senior-architect CRITICAL).
- POST /auth/token has no rate limit, raw long-lived API keys stored in sessionStorage (XSS-exfiltratable), ALLOWED_ORIGINS defaults to wildcard with allow_credentials=True, and the CI workflow writes the JWT private key to GITHUB_OUTPUT where it is visible in Actions logs.
- User document content (including audio transcripts and video frames) is transmitted to Anthropic/OpenAI/Google LLM providers with no consent gate, no per-tenant provider choice, and no per-document audit trail.
- POST /auth/token (exchange_token) has zero tests across any layer and compute_chunk_faithfulness (the eval-harness gating metric) is entirely untested.

---

## Key Notes

🔒 **team-privacy-compliance-reviewer:** "MIT LICENSE conflicts with AGPL-3.0 PyMuPDF dependency — open-sourcing as-is is not legally permissible; any downstream user who installs this pip package and runs it as a network service is bound by AGPL-3.0's copyleft."

🛡️ **team-security-reviewer:** "Weak default secrets (ADMIN_BOOTSTRAP_TOKEN='change-me-before-first-run', SECRET_KEY='change-me-in-production', MINIO_SECRET_KEY='omnivore123') are live fallbacks in config.py with no startup guard — any open-source repo reader can immediately hit /v1/admin/tenants."

📋 **lead-project-manager:** "Project cannot satisfy the stated 'safe to publish as-is' goal — two of four success criteria are hard-blocked: License incompatible with MIT, and ADMIN_BOOTSTRAP_TOKEN ships as a known root credential. Aim alignment: 3/10. Each blocker is ~1 day of focused work."

🏗️ **lead-senior-architect:** "Architecture is internally well-structured across 7 phases — handler registry, NIR/Pydantic separation, WorkflowContext protocol, transactional outbox, RLS GUC pattern. Structural problems are at the publication boundary, not within the system."

🔒 **team-privacy-compliance-reviewer:** "User document content (including audio transcripts, video frames) transmitted to Anthropic/OpenAI/Google with no consent gate or disclosure; PERSON entities extracted by NER stored indefinitely with no erasure path for named individuals."

👨‍💻 **peer-python-reviewer:** "Idiomatically clean across 100+ Python files — comprehensive type hints, structlog, no print(), dataclasses, context managers, Protocol-based handler registry, SecretStr for JWT/API keys, Lua rate limiter atomicity. Zero findings."

---

## Stage Reports

### Stage 1 — Peer Code Review

| Persona | Score | Verdict |
|---|---|---|
| peer-python-reviewer | 9/10 | approve |
| peer-sql-reviewer | 7/10 | concerns |
| peer-typescript-reviewer | 6/10 | concerns |
| peer-quality-engineer | 6/10 | concerns |
| peer-readability-engineer | 7/10 | concerns |

**peer-python-reviewer** (9/10 · approve)

> "Codebase is idiomatically clean across 100+ Python files. Comprehensive type hints, proper exception handling, no mutable defaults, structured logging only (no print), dataclasses for models, context managers for resources. Pre-commit validated. Ready for open source."

---

**peer-sql-reviewer** (7/10 · concerns)

> "Three missing FK indexes (chunks.document_id, jobs.document_id pre-0012, extracted_rows.tenant_id) will cause table scans under RLS filtering and cascade operations. Composite PK on jobs(id, created_at) requires all queries to include created_at in WHERE or risk scanning partitions. Migration 0003 embedding resize is destructive in both directions."

- **[high]** `alembic/versions/0001_initial_schema.py:30-51` — Foreign key chunks.document_id has no index; RLS and cascade operations will table-scan
- **[high]** `alembic/versions/0001_initial_schema.py:125-137` — Foreign key jobs.document_id has no index until migration 0012; lookups by document_id will table-scan
- **[high]** `alembic/versions/0007_extracted_rows_tenant_id.py:16-33` — Foreign key extracted_rows.tenant_id added in migration 0007 without an index; RLS filtering will not use an index
- **[high]** `alembic/versions/0012_partition_jobs.py:64-80` — Migration 0012 composite PK on jobs(id, created_at) creates query friction; lookups by id alone may scan multiple partitions
- **[medium]** `alembic/versions/0003_resize_embedding_dim.py:16-50` — Migration 0003 embedding resize is destructive in both upgrade and downgrade; data loss occurs in both directions

---

**peer-typescript-reviewer** (6/10 · concerns)

> "APIResponse<T> types data as T|null but the request() function casts env.data as T after only checking env.success — a null data on a successful response silently propagates null into callers typed as T. The XHR onload async handler also lacks a top-level try/catch, leaving refresh failures as unhandled rejections."

- **[high]** `frontend/src/lib/api.ts:159-170` — env.data cast to T without null guard after checking only env.success
- **[high]** `frontend/src/lib/api.ts:218-233` — async xhr.onload handler has no top-level try/catch — refresh failure becomes unhandled rejection
- **[medium]** `frontend/src/lib/api.ts:91-94` — fetchToken falls back to body as TokenResponse with no runtime validation
- **[low]** `frontend/src/app/search/page.tsx:96` — Slider onValueChange accesses v[0] without a non-empty guard — fails noUncheckedIndexedAccess

---

**peer-quality-engineer** (6/10 · concerns)

> "The auth token-exchange endpoint has zero tests across any layer; compute_chunk_faithfulness (the eval harness gating metric) is also entirely untested. Both are real gaps before open-source publication."

- **[high]** `src/omnivore/api/routes/auth_route.py:25-68` — POST /auth/token (exchange_token) has no tests at any layer — all three failure branches untested
- **[high]** `eval/metrics.py:30-52` — eval/metrics.py compute_chunk_faithfulness is entirely untested despite gating handler ship decisions
- **[medium]** `src/omnivore/pipeline/enrichers/summarizer.py:86-96` — _build_content off-by-one: total accounting uses pre-truncation length, no multi-chunk truncation test
- **[medium]** `src/omnivore/worker/tasks.py:439-450` — _compute_routing_decision has no unit tests; empty-sinks and tenant-policy-name cases unverified
- **[low]** `src/omnivore/pipeline/enrichers/summarizer.py:99-107` — _strip_fences untested branch: opening fence present but no closing ``` line

---

**peer-readability-engineer** (7/10 · concerns)

> "High-quality, well-organized codebase suitable for open-source. Main readability issues: internal helper functions use leading underscore prefix without clear boundary documentation; magic constant 2048 in MIME sniffing lacks explanation; TYPE_CHECKING imports lack consistent documentation; cost_class literal not enumerated; deeply nested context setup in tasks.py. Otherwise excellent: clear naming, good comment quality, appropriate file organization, comprehensive documentation."

- **[medium]** `src/omnivore/api/main.py:40-62` — Private/internal function namespace unclear for new contributors
- **[medium]** `src/omnivore/api/routes/documents.py:68-69` — Magic constant 2048 in MIME sniffing lacks explanation
- **[low]** `src/omnivore/pipeline/enrichers/summarizer.py:11-13` — TYPE_CHECKING imports lack consistent documentation
- **[low]** `src/omnivore/pipeline/registry.py:19` — Handler cost_class literal not enumerated; string matching error-prone
- **[low]** `src/omnivore/worker/tasks.py:47-112` — Deeply nested function definitions and context managers in worker tasks

---

### Stage 2 — Cross-functional Review

| Persona | Score | Verdict |
|---|---|---|
| team-security-reviewer | 4/10 | block |
| team-privacy-compliance-reviewer | 2/10 | block |
| team-backend-reviewer | 6/10 | concerns |
| team-frontend-reviewer | 6/10 | concerns |
| team-network-reviewer | 5/10 | concerns |
| team-database-reviewer | 6/10 | concerns |
| team-devops-infra-reviewer | 6/10 | concerns |
| team-performance-reviewer | 6/10 | concerns |
| team-observability-reviewer | 6/10 | concerns |
| team-data-ml-reviewer | 7/10 | concerns |

**team-security-reviewer** (4/10 · block)

> "Three ship-blockers for open-source publication: (1) weak default secrets (ADMIN_BOOTSTRAP_TOKEN, SECRET_KEY) are live fallbacks in config.py with no startup guard — any operator who misses .env ships a known root credential; (2) the token-exchange endpoint has no rate limit, enabling systematic API key brute-forcing; (3) the frontend stores the raw long-lived API key in sessionStorage, readable by any same-page XSS."

- **[critical]** `src/omnivore/config.py:19-42` — Weak default secrets are live fallbacks in config.py with no startup guard; deploying without .env ships a known root credential
- **[high]** `src/omnivore/api/routes/auth_route.py:24-69` — POST /v1/auth/token has no rate limit; enables systematic API key brute-forcing and prefix enumeration
- **[high]** `frontend/src/lib/auth.ts:14-19` — Raw long-lived API key stored in sessionStorage; readable by any same-page script on XSS
- **[high]** `src/omnivore/config.py:62` — ALLOWED_ORIGINS defaults to wildcard with allow_credentials=True; open-source deployments inherit a semantically broken CORS posture
- **[medium]** `src/omnivore/auth/jwt.py:43-50` — JWT decode does not enforce the iss claim; any RS256 token from a different Omnivore instance is accepted
- **[medium]** `pyproject.toml:7-46` — No SCA tooling in CI or pre-commit; 45-dependency open-source tree has no automated vulnerability signal

---

**team-privacy-compliance-reviewer** (2/10 · block)

> "Hard license conflict: MIT-declared repo depends on AGPL-3.0 PyMuPDF — open-sourcing as-is is not legally permissible. Separately, user document content is transmitted to external LLM providers with no consent mechanism, and named persons extracted by NER are stored indefinitely with no erasure route."

- **[critical]** `pyproject.toml:24` — MIT LICENSE conflicts with AGPL-3.0 PyMuPDF dependency — open-sourcing as-is is not legally permissible
- **[high]** `src/omnivore/pipeline/enrichers/llm_client.py:65-111` — User document content transmitted to external LLM providers (Anthropic, OpenAI, Google) with no consent gate or disclosure
- **[high]** `src/omnivore/pipeline/enrichers/ner.py:16-19` — NER stores PERSON entities from user documents with no erasure path for the named individuals
- **[medium]** `src/omnivore/db/models.py:60-120` — No retention policy on any table that holds user document content; data accumulates indefinitely
- **[medium]** `src/omnivore/config.py:62` — ALLOWED_ORIGINS defaults to wildcard, enabling any origin to make credentialed API requests

---

**team-backend-reviewer** (6/10 · concerns)

> "Five server-logic gaps: /ready hardcodes 'ok' for all dependencies making it useless as a health gate; PATCH /admin/tenants/{id} accepts arbitrary status strings that can permanently lock out tenants; worker sets status=indexed before embedding commit so failed embeddings leave chunks invisibly unsearchable; list_documents cursor is transparent ISO datetime silently ignored on parse error; POST /tenant/api-keys accepts raw dict with no schema."

- **[high]** `src/omnivore/api/routes/health.py:17-25` — /ready endpoint hardcodes 'ok' for all dependencies — broken health gate
- **[high]** `src/omnivore/api/routes/admin.py:46-58` — PATCH /admin/tenants/{id} accepts arbitrary status strings — can permanently lock out a tenant with no recovery
- **[medium]** `src/omnivore/api/routes/tenant.py:113-122` — POST /tenant/api-keys takes a raw dict body with no schema — name and scopes are not typed or length-constrained
- **[medium]** `src/omnivore/worker/tasks.py:326-363` — Worker sets status=indexed and commits before embedding write — embedding failure leaves chunks permanently unsearchable via vector mode
- **[medium]** `src/omnivore/api/routes/documents.py:270-275` — list_documents cursor is a transparent ISO datetime — invalid cursors silently swallowed, returning unrelated first page

---

**team-frontend-reviewer** (6/10 · concerns)

> "Two polling intervals fire setState after unmount when the user navigates away mid-poll; AuthGuard renders children before the auth check completes, producing a flash of protected content; the search form has an unguarded race on rapid re-submit. All fixable with standard React cancellation patterns."

- **[high]** `frontend/src/components/document-table.tsx:73-80` — Polling interval in DocumentTable calls setState after unmount when user navigates away during active-document polling
- **[high]** `frontend/src/app/documents/[id]/page.tsx:66-71` — Polling interval in DocumentDetailPage fires setState after unmount when user navigates away mid-poll
- **[high]** `frontend/src/components/AuthGuard.tsx:7-19` — AuthGuard renders children immediately before auth check completes, flashing protected content to unauthenticated users
- **[medium]** `frontend/src/app/search/page.tsx:32-47` — Search form has an unguarded race: rapid re-submits can show stale results from an earlier slower request
- **[medium]** `frontend/src/app/documents/[id]/page.tsx:59-63` — DocumentDetailPage load callback suppresses exhaustive-deps lint with incorrect justification comment
- **[medium]** `frontend/src/app/documents/[id]/page.tsx:202-247` — Index keys used on entity badge lists and summary key_points/topics lists that can change between renders
- **[low]** `frontend/src/lib/api.ts:203-263` — XHR upload in api.ts has no abort-on-unmount mechanism; upload-zone cannot cancel in-flight uploads

---

**team-network-reviewer** (5/10 · concerns)

> "LLM clients constructed per-call with no explicit timeout (SDK default 600s); aioboto3 S3 sessions created per blob fetch defeating connection pooling; frontend fetch() calls have no AbortSignal timeout. None are incident-grade alone, but all three compound under concurrent load."

- **[high]** `src/omnivore/pipeline/context.py:28-59` — New aioboto3 Session and S3 client created on every blob read/stream; connection pooling is entirely defeated
- **[high]** `src/omnivore/pipeline/enrichers/llm_client.py:104-163` — LLM client constructed per-call with no explicit timeout; SDK default is 600s per request
- **[medium]** `frontend/src/lib/api.ts:109-120` — frontend fetch() calls have no AbortSignal timeout; hung API responses stall the UI indefinitely
- **[medium]** `src/omnivore/auth/rate_limit.py:15-18` — rate_limit.py fallback Redis client created without pool configuration and not tracked for cleanup

---

**team-database-reviewer** (6/10 · concerns)

> "Migration 0012 composite PK (id, created_at) on the range-partitioned jobs table means any single-key job lookup by id must probe all monthly partitions; migrations 0011/0012 hold exclusive locks for full data-copy duration with no downtime acknowledgement — both are real operational hazards for open-source consumers."

- **[high]** `alembic/versions/0012_partition_jobs.py:64-79` — jobs composite PK (id, created_at) makes single-key lookups partition-scan all months
- **[high]** `alembic/versions/0011_partition_chunks.py:43-189` — Migrations 0011 and 0012 hold ACCESS EXCLUSIVE locks for full data-copy duration with no downtime acknowledgement
- **[medium]** `src/omnivore/api/routes/search.py:105-125` — Multi-tenant vector search returns fewer than top_k results for minority tenants due to HNSW recheck on tenant_id
- **[medium]** `alembic/versions/0001_initial_schema.py:88-101` — entities table has no created_at or updated_at — no way to track when NER output was written or re-processed

---

**team-devops-infra-reviewer** (6/10 · concerns)

> "JWT private key written to GITHUB_OUTPUT (visible in Actions logs); no .dockerignore so build contexts include .git and tests; uv base image uses :latest tag in both Dockerfiles; CI actions pinned to mutable version tags not SHAs; Grafana ships with anonymous Admin role enabled."

- **[high]** `.github/workflows/ci.yml:68-82` — JWT private key written to GITHUB_OUTPUT and readable in Actions workflow logs
- **[medium]** `docker/api/Dockerfile:3` — uv installer pulled with :latest tag in both Dockerfiles; six observability images use :latest in docker-compose
- **[medium]** `docker/api/Dockerfile:1` — No .dockerignore — build context sends .git, tests/, eval/, frontend/, and .env* to the Docker daemon
- **[medium]** `.github/workflows/ci.yml:40-43` — CI actions pinned to mutable version tags, not SHAs
- **[medium]** `docker-compose.yml:177-179` — Grafana ships with anonymous Admin role enabled — a footgun for contributors who bind to 0.0.0.0
- **[low]** `.github/workflows/ci.yml:43-49` — CI has no dependency cache — uv sync re-downloads all packages on every run

---

**team-performance-reviewer** (6/10 · concerns)

> "Video vision captioning is fully sequential: 20 frames x ~500ms LLM RTT = ~10s of serialized waiting per video, compounded by a fresh AsyncAnthropic/AsyncOpenAI client instantiated per LLM call. Per-chunk language detection (lingua) runs synchronously on the asyncio event loop. Three fixable bottlenecks; rest of scope is clean."

- **[high]** `src/omnivore/pipeline/handlers/video.py:189-220` — Video vision frame captioning is fully sequential; up to 20 LLM round trips serialized per video
- **[high]** `src/omnivore/pipeline/enrichers/llm_client.py:101-112` — LLM client instantiates a new AsyncAnthropic/AsyncOpenAI object per call, destroying TCP connection reuse
- **[medium]** `src/omnivore/worker/tasks.py:247-252` — Per-chunk lingua language detection runs synchronously on the asyncio event loop in _run_ingest
- **[medium]** `src/omnivore/auth/rate_limit.py:76-77` — rate_limit.py re-registers the Lua script on every token-bucket check via r.register_script()
- **[medium]** `src/omnivore/pipeline/chunker.py:55-93` — chunker.py re-encodes fragment text multiple times per fragment in the overlap and token-count paths

---

**team-observability-reviewer** (6/10 · concerns)

> "The instrumentation foundation is strong (structlog JSON, full RED metrics, OTel traces, W3C traceparent across the ARQ boundary, four Grafana dashboards) but three gaps will hurt the on-call engineer: the /readyz probe is a hardcoded stub that will mask real dependency failures, no error tracker receives unhandled exceptions, and the entire auth failure path is silent — failed API key lookups emit no log line and no metric."

- **[high]** `src/omnivore/api/routes/health.py:16-25` — /readyz returns hardcoded 200 for all dependencies — it is a no-op probe
- **[high]** `src/omnivore/api/main.py:204-213` — No error tracker integration — unhandled exceptions are log lines only, invisible to grouping/alerting
- **[high]** `src/omnivore/logging_config.py:26` — structlog contextvars are never bound per-request — log lines have no request ID or tenant ID field when OTEL is disabled
- **[medium]** `src/omnivore/auth/dependencies.py:112-119` — Auth failure path emits no log or metric — failed API key lookups are invisible to the operator
- **[medium]** `observability/prometheus.yml:1-30` — No alert rules defined anywhere — Prometheus scrapes metrics but no paging signal exists

---

**team-data-ml-reviewer** (7/10 · concerns)

> "Three medium gaps before open-sourcing: NER confidence=1.0 is a misleading sentinel (not a calibrated score) exposed in the public API; the bakeoff that chose BGE-base-en-v1.5 evaluated only text/markdown fixtures — zero retrieval coverage for PDF, audio, image, video; and the summarizer truncation bug also means the LLM prompt systematically receives less context than the configured 12k-char budget."

- **[medium]** `src/omnivore/pipeline/enrichers/ner.py:67-73` — NER confidence=1.0 is a hardcoded sentinel, not a calibrated score; callers and the public API cannot distinguish 'high confidence' from 'no confidence data available'
- **[medium]** `eval/bakeoff.py:232-260` — Bakeoff eval corpus covers only text/markdown/HTML fixtures; embedding model selection made with zero retrieval coverage for PDF, audio, image, video handlers
- **[medium]** `src/omnivore/pipeline/enrichers/summarizer.py:86-96` — _build_content advances total by len(text) instead of len(text[:remaining]), causing the LLM summarization prompt to receive less content than the 12,000-char budget
- **[low]** `src/omnivore/pipeline/handlers/audio.py:55-64` — Whisper compute_type is hardcoded at model init; float16 on CUDA may silently fall back on unsupported GPU architectures
- **[low]** `eval/harness.py:85-86` — eval/harness.py hardcodes handler_version='1.0.0' for every result; historical runs cannot detect handler version changes from eval output alone

---

### Stage 3 — Leadership

| Persona | Score | Verdict |
|---|---|---|
| lead-senior-architect | 5/10 | block |
| lead-project-manager | 3/10 | block |

**lead-senior-architect** (5/10 · block)

> "Decision: the system is internally well-architected across 7 phases, but the project is being shipped through the open-source publication boundary as a code-quality task rather than a structural one — license-compatibility seam is unresolved (AGPL PyMuPDF under MIT) and the config boundary ships exploitable production defaults. Block publication; both are structural, not code-level, and neither can be fixed in the post-publish patch cycle."

- **[critical]** `src/omnivore/pipeline/handlers/pdf.py:1-35` — MIT LICENSE incompatible with AGPL-3.0 PyMuPDF dependency; no handler-replacement seam exists despite architecture.md Q4 flagging this as load-bearing for distribution model
- **[critical]** `src/omnivore/config.py:15-42` — Configuration boundary ships exploitable production defaults — pattern consistent across SECRET_KEY, ADMIN_BOOTSTRAP_TOKEN, MINIO_SECRET_KEY; no startup-time prod-gate forces correction
- **[high]** `src/omnivore/api/routes/health.py:16-25` — Cross-cutting concerns (rate limiting, readiness, error classification, request correlation) are partially centralized; the unfinished centralization is producing drift visible to multiple Stage 2 reviewers
- **[medium]** `alembic/versions/0011_partition_chunks.py:23-40` — Migrations 0011 and 0012 are correctly documented as destructive but the codebase has no operational seam for safe partition-migration on a populated production database
- **[medium]** `src/omnivore/pipeline/enrichers/llm_client.py:51-75` — LLM provider boundary is well-shaped but the consent boundary for user-content-leaving-the-server is missing — architecture treats LLM enrichment as a configuration choice, not a data-flow boundary

---

**lead-project-manager** (3/10 · block)

> "Aim alignment: 3/10. Scope: on-scope. Verdict: hold. Project cannot publish as-is — license incompatibility (MIT vs AGPL PyMuPDF) and live-default admin credential directly contradict 'safe to publish' criteria. Minimum scope to unblock is small and well-defined."

- **[critical]** `.review/aims.md:13-19` — Project cannot satisfy the stated 'safe to publish as-is' goal — two of four success criteria are hard-blocked by license incompatibility and live default credentials
- **[high]** `.review/aims.md:32` — LLM data-egress with no consent gate directly contradicts the implicit PII safety standard for a publicly-released document-ingestion tool
- **[medium]** `.review/aims.md:30-33` — Scope of work built (7 phases) significantly exceeds what's needed to demonstrate the project — but that's a feature for portfolio framing, not a problem to fix
- **[medium]** `.review/aims.md:13-19` — Post-publication backlog is well-formed; sequence the HIGH-severity Stage 2 findings into a clear order behind the two publication blockers

---

## Aims Snapshot

> Full codebase health check before going open source. Success criteria: (1) No secrets/credentials/PII in tracked files, (2) Security posture sound for publicly visible implementation, (3) License clear and dependencies compatible with MIT, (4) Documentation complete enough for a new contributor, (5) Architecture and code quality representative of work the user is comfortable putting their name on.

---

## Committee

**Stage 1:** peer-python-reviewer, peer-sql-reviewer, peer-typescript-reviewer, peer-quality-engineer, peer-readability-engineer

**Stage 2:** team-security-reviewer, team-privacy-compliance-reviewer, team-backend-reviewer, team-frontend-reviewer, team-network-reviewer, team-database-reviewer, team-devops-infra-reviewer, team-performance-reviewer, team-observability-reviewer, team-data-ml-reviewer

**Stage 3:** lead-senior-architect, lead-project-manager

_Models used: claude-haiku-4-5-20251001, claude-sonnet-4-6, claude-opus-4-7 · Plugin: v0.1.1_
