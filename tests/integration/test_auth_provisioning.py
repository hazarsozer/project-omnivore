"""Integration tests for Phase 4: tenant provisioning, API key auth, RLS isolation.

Requires docker compose up -d postgres redis minio.

Uses the single-asyncio.run() pattern from test_e2e_pipeline.py to avoid
"Future attached to a different loop" from asyncpg connection pool reuse.
All HTTP assertions run inside one event loop; sync test functions read results.
"""
from __future__ import annotations

import asyncio
import io
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.sql import text

from omnivore.api.main import app
from omnivore.auth.api_key import generate_api_key, hash_key
from omnivore.config import get_settings
from omnivore.db.models import ApiKey, Document, Tenant
from omnivore.pipeline.registry import registry

# ---------------------------------------------------------------------------
# Module-scoped fixture — all async I/O in ONE asyncio.run()
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def auth_results():
    """Run all auth scenario checks in a single event loop; return result dict."""
    results: dict = {}

    async def _run():
        settings = get_settings()
        registry.discover()
        app.state.arq_pool = None

        # Use a fresh engine per run to avoid cross-loop pool issues
        engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
        SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

        async def bypass_session():
            return SessionLocal()

        async def create_tenant_with_key(slug, status="active", scopes=None):
            if scopes is None:
                scopes = [
                    "documents:read", "documents:write",
                    "chunks:read", "entities:read",
                    "search:read", "handlers:read", "tenant:manage",
                ]
            async with SessionLocal() as session:
                async with session.begin():
                    await session.execute(text("SET LOCAL app.bypass_rls = 'on'"))
                    tenant = Tenant(slug=slug, display_name=slug, status=status)
                    session.add(tenant)
                    await session.flush()
                    raw_key, prefix = generate_api_key()
                    key = ApiKey(
                        tenant_id=tenant.id,
                        name="default",
                        prefix=prefix,
                        key_hash=hash_key(raw_key),
                        scopes=scopes,
                    )
                    session.add(key)
                    await session.flush()
                    return raw_key, tenant.id

        # Create test tenants
        raw_a, t_id_a = await create_tenant_with_key(f"ta-{uuid.uuid4().hex[:8]}")
        raw_b, t_id_b = await create_tenant_with_key(f"tb-{uuid.uuid4().hex[:8]}")
        raw_c, t_id_c = await create_tenant_with_key(f"tc-{uuid.uuid4().hex[:8]}", status="suspended")
        raw_d, t_id_d = await create_tenant_with_key(
            f"td-{uuid.uuid4().hex[:8]}", scopes=["documents:read"]
        )

        # Create a document owned by Tenant B (via SQL bypass)
        doc_b_id = uuid.uuid4()
        async with SessionLocal() as session:
            async with session.begin():
                await session.execute(text(f"SET LOCAL app.current_tenant_id = '{t_id_b}'"))
                doc = Document(
                    id=doc_b_id,
                    tenant_id=t_id_b,
                    sha256=b"\xab" * 32,
                    filename="b-secret.txt",
                    mime_type="text/plain",
                    size_bytes=10,
                    storage_uri="s3://bucket/raw/b-secret.txt",
                    status="indexed",
                )
                session.add(doc)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            # 1. No auth → 401
            r = await c.get("/v1/documents")
            results["no_auth_status"] = r.status_code
            results["no_auth_code"] = r.json()["error"]["code"]

            # 2. Invalid key → 401
            r = await c.get("/v1/documents", headers={"X-API-Key": "omn_live_notvalid12345xyz"})
            results["invalid_key_status"] = r.status_code

            # 3. Valid key → 200
            r = await c.get("/v1/documents", headers={"X-API-Key": raw_a})
            results["valid_key_status"] = r.status_code
            results["valid_key_success"] = r.json()["success"]

            # 4. Insufficient scope (read-only key tries upload)
            r = await c.post(
                "/v1/documents",
                files={"file": ("test.txt", io.BytesIO(b"hello"), "text/plain")},
                headers={"X-API-Key": raw_d},
            )
            results["insuff_scope_status"] = r.status_code
            results["insuff_scope_code"] = r.json()["error"]["code"]

            # 5. RLS — Tenant A cannot GET Tenant B's document
            r = await c.get(f"/v1/documents/{doc_b_id}", headers={"X-API-Key": raw_a})
            results["rls_get_status"] = r.status_code

            # 6. RLS — Tenant A list excludes Tenant B's doc
            r = await c.get("/v1/documents", headers={"X-API-Key": raw_a})
            results["rls_list_ids"] = [d["document_id"] for d in r.json()["data"]]
            results["doc_b_id"] = str(doc_b_id)

            # 7. Suspended tenant → 403
            r = await c.get("/v1/documents", headers={"X-API-Key": raw_c})
            results["suspended_status"] = r.status_code
            results["suspended_code"] = r.json()["error"]["code"]

            # 8. Handlers without auth → 401
            r = await c.get("/v1/handlers")
            results["handlers_noauth_status"] = r.status_code

            # 9. Handlers with valid key → 200
            r = await c.get("/v1/handlers", headers={"X-API-Key": raw_a})
            results["handlers_valid_status"] = r.status_code

            # 10. Admin endpoint without token → 401
            r = await c.post("/v1/admin/tenants", json={"slug": "fail", "display_name": "Fail"})
            results["admin_notoken_status"] = r.status_code

        await engine.dispose()

    asyncio.run(_run())
    return results


# ---------------------------------------------------------------------------
# Sync test functions read from results dict — no per-test event loops
# ---------------------------------------------------------------------------

def test_no_auth_returns_401(auth_results):
    assert auth_results["no_auth_status"] == 401
    assert auth_results["no_auth_code"] == "UNAUTHENTICATED"


def test_invalid_api_key_returns_401(auth_results):
    assert auth_results["invalid_key_status"] == 401


def test_valid_api_key_allows_request(auth_results):
    assert auth_results["valid_key_status"] == 200
    assert auth_results["valid_key_success"] is True


def test_insufficient_scope_returns_403(auth_results):
    assert auth_results["insuff_scope_status"] == 403
    assert auth_results["insuff_scope_code"] == "INSUFFICIENT_SCOPE"


def test_rls_tenant_a_cannot_see_tenant_b_document(auth_results):
    assert auth_results["rls_get_status"] == 404


def test_rls_tenant_a_list_excludes_tenant_b(auth_results):
    assert auth_results["doc_b_id"] not in auth_results["rls_list_ids"]


def test_suspended_tenant_returns_403(auth_results):
    assert auth_results["suspended_status"] == 403
    assert auth_results["suspended_code"] == "TENANT_SUSPENDED"


def test_handlers_requires_auth(auth_results):
    assert auth_results["handlers_noauth_status"] == 401


def test_handlers_with_valid_key(auth_results):
    assert auth_results["handlers_valid_status"] == 200


def test_admin_create_tenant_requires_token(auth_results):
    assert auth_results["admin_notoken_status"] == 401
