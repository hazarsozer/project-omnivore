from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.sql import text

from omnivore.config import get_settings

engine = create_async_engine(
    get_settings().DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def reset_guc(session: AsyncSession, guc: str) -> None:
    """RESET a Postgres GUC and commit so the change persists to the pool.

    SQLAlchemy's default pool_reset_on_return="rollback" rolls back any
    uncommitted statement when the connection returns to the pool — including
    a bare RESET. Wrapping rollback + RESET + commit ensures the unset
    survives back into the pool. Best-effort: silently no-ops if the session
    is in an unrecoverable state.
    """
    try:
        await session.rollback()
        await session.execute(text(f"RESET {guc}"))
        await session.commit()
    except Exception:
        pass


@asynccontextmanager
async def tenant_session(tenant_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
    """Open a session with the tenant GUC set (session-scoped).

    Uses SET (session-level) not SET LOCAL (transaction-level) so the GUC
    survives across multiple explicit commit() calls in worker tasks.
    RESET on exit prevents pool-checkout leakage to the next request.
    UUID values are always hex+dashes — safe to embed in SQL without parameters.
    """
    async with AsyncSessionLocal() as session:
        try:
            await session.execute(text(f"SET app.current_tenant_id = '{tenant_id}'"))
            yield session
        finally:
            await reset_guc(session, "app.current_tenant_id")
            await session.close()


@asynccontextmanager
async def admin_session() -> AsyncIterator[AsyncSession]:
    """Open a session that bypasses RLS — commits on clean exit, rolls back on exception.

    Uses session-level SET (not SET LOCAL) because outbox_relay commits mid-block.
    RESET on exit prevents bypass_rls from leaking to the next pool checkout.
    """
    async with AsyncSessionLocal() as session:
        try:
            await session.execute(text("SET app.bypass_rls = 'on'"))
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await reset_guc(session, "app.bypass_rls")
            await session.close()
