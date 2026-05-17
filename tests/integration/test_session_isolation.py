"""Regression test for NEW-H-1: GUC pool-checkout isolation.

The Phase 4 audit fixed the narrow case of `current_tenant_id` leaking from
`get_db_for_tenant`, but `bypass_rls=on` set inside `_resolve_api_key`,
`_resolve_jwt`, `_update_last_used`, and `admin_session` was leaking to the
next session that reused the same pool connection. In a non-superuser
production deployment, this is a full RLS bypass.

These tests force pool reuse with `pool_size=1, max_overflow=0` and assert
that GUCs set inside one session do NOT survive into the next session.

Requires docker compose up -d postgres.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.sql import text

from omnivore.config import get_settings


@pytest.fixture(scope="module")
def leak_results():
    """Run all GUC-leak probes in a single event loop; return result dict."""
    results: dict = {}

    async def _run():
        settings = get_settings()
        # Force pool reuse so the second session lands on the same connection.
        engine = create_async_engine(
            settings.DATABASE_URL, pool_size=1, max_overflow=0
        )
        SL = async_sessionmaker(engine, expire_on_commit=False)

        # 1. bypass_rls leak via raw SET (the bug)
        async with SL() as session:
            async with session.begin():
                await session.execute(text("SET app.bypass_rls = 'on'"))

        async with SL() as session:
            r = await session.execute(
                text("SELECT current_setting('app.bypass_rls', true)")
            )
            results["raw_set_leaks"] = r.scalar()

        await engine.dispose()

        # 2. SET LOCAL does NOT leak (the fix used in _resolve_api_key et al.)
        engine = create_async_engine(
            settings.DATABASE_URL, pool_size=1, max_overflow=0
        )
        SL = async_sessionmaker(engine, expire_on_commit=False)

        async with SL() as session:
            async with session.begin():
                await session.execute(text("SET LOCAL app.bypass_rls = 'on'"))

        async with SL() as session:
            r = await session.execute(
                text("SELECT current_setting('app.bypass_rls', true)")
            )
            results["set_local_no_leak"] = r.scalar()

        await engine.dispose()

        # 3. admin_session() resets bypass_rls on exit (the fix)
        import omnivore.db.session as _db_session
        from omnivore.db.session import admin_session

        # Temporarily swap the global engine so admin_session uses pool_size=1
        engine = create_async_engine(
            settings.DATABASE_URL, pool_size=1, max_overflow=0
        )
        _orig_session_local = _db_session.AsyncSessionLocal
        _db_session.AsyncSessionLocal = async_sessionmaker(
            engine, expire_on_commit=False
        )
        try:
            async with admin_session() as db:
                # Verify bypass_rls is on inside the session
                r = await db.execute(
                    text("SELECT current_setting('app.bypass_rls', true)")
                )
                results["admin_session_inside"] = r.scalar()

            # New session on the same pooled connection — must NOT see bypass_rls
            async with _db_session.AsyncSessionLocal() as db:
                r = await db.execute(
                    text("SELECT current_setting('app.bypass_rls', true)")
                )
                results["admin_session_after_exit"] = r.scalar()
        finally:
            _db_session.AsyncSessionLocal = _orig_session_local
            await engine.dispose()

        # 4. tenant_session() resets current_tenant_id on exit (the fix)
        from omnivore.db.session import tenant_session

        engine = create_async_engine(
            settings.DATABASE_URL, pool_size=1, max_overflow=0
        )
        _db_session.AsyncSessionLocal = async_sessionmaker(
            engine, expire_on_commit=False
        )
        try:
            t_id = uuid.uuid4()
            async with tenant_session(t_id) as db:
                r = await db.execute(
                    text("SELECT current_setting('app.current_tenant_id', true)")
                )
                results["tenant_session_inside"] = r.scalar()
                # Commit mimics real worker usage where the SET would persist
                # past session.close() without an explicit RESET+commit.
                await db.commit()

            async with _db_session.AsyncSessionLocal() as db:
                r = await db.execute(
                    text("SELECT current_setting('app.current_tenant_id', true)")
                )
                results["tenant_session_after_exit"] = r.scalar()
        finally:
            _db_session.AsyncSessionLocal = _orig_session_local
            await engine.dispose()

    asyncio.run(_run())
    return results


@pytest.mark.integration
def test_raw_set_leaks_across_pool(leak_results):
    """Sanity check: confirm the documented Postgres behavior we're protecting against."""
    assert leak_results["raw_set_leaks"] == "on", (
        "Expected raw SET to leak across pool checkouts — if this fails, "
        "the underlying pool-reset behavior changed and this regression "
        "test is no longer meaningful."
    )


@pytest.mark.integration
def test_set_local_does_not_leak(leak_results):
    """Confirm SET LOCAL reverts on transaction commit — the fix for _resolve_*."""
    assert leak_results["set_local_no_leak"] == "", (
        "SET LOCAL inside a transaction must NOT survive to the next pool checkout."
    )


@pytest.mark.integration
def test_admin_session_resets_bypass_rls(leak_results):
    """admin_session() must RESET bypass_rls on exit (NEW-H-1 fix)."""
    assert leak_results["admin_session_inside"] == "on", (
        "bypass_rls should be 'on' inside admin_session()"
    )
    assert leak_results["admin_session_after_exit"] == "", (
        "bypass_rls leaked out of admin_session() — NEW-H-1 regressed. "
        "In a non-superuser deployment this is a full RLS bypass."
    )


@pytest.mark.integration
def test_tenant_session_resets_current_tenant_id(leak_results):
    """tenant_session() must RESET current_tenant_id on exit (NEW-H-1 fix)."""
    inside = leak_results["tenant_session_inside"]
    assert inside and inside != "", (
        f"current_tenant_id should be set inside tenant_session(), got {inside!r}"
    )
    assert leak_results["tenant_session_after_exit"] == "", (
        "current_tenant_id leaked out of tenant_session() — "
        "next request on the same pooled connection would inherit it."
    )
