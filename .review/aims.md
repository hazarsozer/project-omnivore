# Review Aims — omnidoc-ingest

**Captured:** 2026-05-21  
**Phase:** Full codebase health check / open-source safety review

---

## Overarching Goal

Full codebase health check before going open source. The user is checking whether the codebase is safe to publish as-is — covering secrets, security posture, architectural soundness, license compliance, code quality, and documentation completeness.

## Success Criteria

The codebase is safe to publish when:
- No secrets, credentials, or PII are present in tracked files
- The security posture (auth, rate limiting, RLS, input validation) is sound for a publicly visible implementation
- License is clear and dependencies are compatible with the stated license (MIT, per `LICENSE`)
- Documentation (README, CONTRIBUTING, `.env.example`) is accurate and complete enough for a new contributor to get started
- The architecture and code quality are representative of work the user is comfortable putting their name on

## Non-Goals (explicitly out of scope for this review)

- OAuth / 2FA / account recovery (not planned for this project)
- Docling PDF pilot (feature-flagged, not default)
- ColPali / visual retrieval (deferred)
- RAPTOR / GraphRAG (deferred)
- Frontend accessibility deep-dive (admin-only UI, not user-facing)
- Performance benchmarking of the ML models themselves (EasyOCR, faster-whisper, BGE)
- Grading the remaining deferred medium/low items already documented in CLAUDE.md (M-4, M-5, L-1, L-2, L-3) — they are known and accepted

## Project Context

- **Type:** API service (Python FastAPI + ARQ worker) + Next.js 15 admin frontend
- **Phase:** 7 (complete) — all planned phases shipped
- **Compliance signals:** Multi-tenant API keys (Argon2id), RS256 JWT, RLS on 6 PostgreSQL tables, Lua token-bucket rate limiter — auth-sensitive codebase
- **PII exposure surface:** User-uploaded documents may contain PII; the system stores chunks, entities, and embeddings in PostgreSQL
- **License concern:** PyMuPDF is AGPL-3.0 (documented in `docs/architecture.md §9 Q4`) — this must be surfaced before open-sourcing
- **Test coverage:** 380+ tests (unit + integration + chaos); GitHub Actions CI passes

## Review Scope

Full codebase — all source files across all phases.
