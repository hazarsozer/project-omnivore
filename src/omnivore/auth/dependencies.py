from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Annotated

import structlog
from fastapi import Depends, Request
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from omnivore.auth.api_key import cache_key_for, extract_prefix, verify_key
from omnivore.auth.cache import get_cached_auth, set_cached_auth
from omnivore.auth.context import AuthContext
from omnivore.auth.errors import InvalidCredentialsError, TenantSuspendedError
from omnivore.auth.jwt import decode_token
from omnivore.db.models import ApiKey, Tenant
from omnivore.db.session import AsyncSessionLocal

logger = structlog.get_logger(__name__)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_bearer = HTTPBearer(auto_error=False)


async def require_auth(
    request: Request,
    api_key_header: Annotated[str | None, Depends(_api_key_header)] = None,
    bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> AuthContext:
    if api_key_header:
        return await _resolve_api_key(api_key_header)
    if bearer:
        return await _resolve_jwt(bearer.credentials)
    raise InvalidCredentialsError()


def require_scope(scope: str):
    async def _dep(auth: Annotated[AuthContext, Depends(require_auth)]) -> AuthContext:
        if scope not in auth.scopes:
            from omnivore.auth.errors import InsufficientScopeError
            raise InsufficientScopeError(scope)
        return auth
    return _dep


async def get_db_for_tenant(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> AsyncGenerator[AsyncSession, None]:
    # Plain session — route handlers enforce isolation via explicit tenant_id checks.
    # RLS (SET LOCAL app.current_tenant_id) would work here for non-superuser DB roles,
    # but SET LOCAL is transaction-scoped and conflicts with multi-commit route handlers.
    # The explicit ownership checks (doc.tenant_id != auth.tenant_id) are the primary guard.
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


# --- internal helpers ---

async def _resolve_api_key(raw_key: str) -> AuthContext:
    ck = cache_key_for(raw_key)
    cached = await get_cached_auth(ck)
    if cached:
        return _cached_to_ctx(cached, "api_key")

    prefix = extract_prefix(raw_key)
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(text("SET app.bypass_rls = 'on'"))
            rows = (
                await session.execute(
                    select(ApiKey).where(
                        ApiKey.prefix == prefix,
                        ApiKey.revoked_at.is_(None),
                    )
                )
            ).scalars().all()

    matched: ApiKey | None = None
    for row in rows:
        if verify_key(row.key_hash, raw_key):
            matched = row
            break

    if not matched:
        raise InvalidCredentialsError()
    if matched.expires_at is not None:
        from datetime import UTC, datetime
        if datetime.now(UTC) > matched.expires_at.replace(tzinfo=UTC):
            raise InvalidCredentialsError()

    # Load tenant status
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(text("SET app.bypass_rls = 'on'"))
            tenant = await session.get(Tenant, matched.tenant_id)

    if tenant is None or tenant.status != "active":
        raise TenantSuspendedError()

    payload = {
        "tenant_id": str(matched.tenant_id),
        "principal_id": str(matched.id),
        "scopes": list(matched.scopes),
    }
    await set_cached_auth(ck, payload)

    # Fire-and-forget last_used_at update (best effort)
    import asyncio
    asyncio.ensure_future(_update_last_used(matched.id))

    return AuthContext(
        tenant_id=matched.tenant_id,
        principal_id=str(matched.id),
        principal_type="api_key",
        scopes=frozenset(matched.scopes),
        raw_token_hash=ck,
    )


async def _resolve_jwt(token: str) -> AuthContext:
    import hashlib
    ck = "auth:jwt:" + hashlib.sha256(token.encode()).hexdigest()
    cached = await get_cached_auth(ck)
    if cached:
        return _cached_to_ctx(cached, "jwt")

    payload = decode_token(token)  # raises InvalidCredentialsError on bad token
    tenant_id = uuid.UUID(payload["tenant_id"])
    scopes: list[str] = payload.get("scopes", [])
    principal_id: str = payload["sub"]

    # Verify tenant is active
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(text("SET app.bypass_rls = 'on'"))
            tenant = await session.get(Tenant, tenant_id)

    if tenant is None or tenant.status != "active":
        raise TenantSuspendedError()

    cache_payload = {"tenant_id": str(tenant_id), "principal_id": principal_id, "scopes": scopes}
    await set_cached_auth(ck, cache_payload)

    return AuthContext(
        tenant_id=tenant_id,
        principal_id=principal_id,
        principal_type="jwt",
        scopes=frozenset(scopes),
        raw_token_hash=ck,
    )


def _cached_to_ctx(cached: dict, principal_type: str) -> AuthContext:
    return AuthContext(
        tenant_id=uuid.UUID(cached["tenant_id"]),
        principal_id=cached["principal_id"],
        principal_type=principal_type,  # type: ignore[arg-type]
        scopes=frozenset(cached["scopes"]),
        raw_token_hash="",  # not re-stored from cache
    )


async def _update_last_used(key_id: uuid.UUID) -> None:
    from datetime import UTC, datetime

    from sqlalchemy import update as sa_update
    try:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(text("SET app.bypass_rls = 'on'"))
                await session.execute(
                    sa_update(ApiKey)
                    .where(ApiKey.id == key_id)
                    .values(last_used_at=datetime.now(UTC))
                )
    except Exception:
        pass  # best-effort, must not block request
