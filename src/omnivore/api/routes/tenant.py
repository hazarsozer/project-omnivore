"""Tenant self-service endpoints — scoped to the authenticated tenant."""
from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from omnivore.api.schemas import APIResponse
from omnivore.auth.api_key import generate_api_key, hash_key
from omnivore.auth.context import AuthContext
from omnivore.auth.dependencies import get_db_for_tenant, require_scope
from omnivore.db.models import ApiKey, Tenant
from omnivore.db.session import admin_session
from omnivore.pipeline.routing import validate_policy

router = APIRouter(prefix="/tenant", tags=["tenant"])
logger = structlog.get_logger(__name__)


@router.get("")
async def get_tenant_info(
    db: Annotated[AsyncSession, Depends(get_db_for_tenant)],
    auth: Annotated[AuthContext, Depends(require_scope("tenant:manage"))],
) -> APIResponse[dict]:
    tenant = await db.get(Tenant, auth.tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return APIResponse(
        success=True,
        data={
            "tenant_id": str(tenant.id),
            "slug": tenant.slug,
            "display_name": tenant.display_name,
            "status": tenant.status,
            "created_at": tenant.created_at.isoformat() if tenant.created_at else None,
        },
    )


@router.get("/config")
async def get_tenant_config(
    db: Annotated[AsyncSession, Depends(get_db_for_tenant)],
    auth: Annotated[AuthContext, Depends(require_scope("tenant:manage"))],
) -> APIResponse[dict]:
    tenant = await db.get(Tenant, auth.tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return APIResponse(success=True, data=tenant.config or {})


@router.put("/config")
async def update_tenant_config(
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db_for_tenant)],
    auth: Annotated[AuthContext, Depends(require_scope("tenant:manage"))],
) -> APIResponse[dict]:
    # Validate routing_policy before persisting
    if "routing_policy" in body:
        errors = validate_policy(body["routing_policy"])
        if errors:
            raise HTTPException(status_code=422, detail={"routing_policy_errors": errors})

    tenant = await db.get(Tenant, auth.tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    tenant.config = {**(tenant.config or {}), **body}
    await db.commit()

    # Invalidate cached auth for this tenant
    from omnivore.auth.cache import invalidate_tenant_auth
    await invalidate_tenant_auth(auth.tenant_id)

    return APIResponse(success=True, data=tenant.config)


@router.get("/api-keys")
async def list_own_keys(
    db: Annotated[AsyncSession, Depends(get_db_for_tenant)],
    auth: Annotated[AuthContext, Depends(require_scope("tenant:manage"))],
) -> APIResponse[list]:
    rows = (
        await db.scalars(
            select(ApiKey)
            .where(ApiKey.tenant_id == auth.tenant_id, ApiKey.revoked_at.is_(None))
            .order_by(ApiKey.created_at.desc())
        )
    ).all()
    return APIResponse(
        success=True,
        data=[
            {
                "key_id": str(k.id),
                "name": k.name,
                "prefix": k.prefix,
                "scopes": k.scopes,
                "created_at": k.created_at.isoformat() if k.created_at else None,
                "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
            }
            for k in rows
        ],
        meta={"count": len(rows)},
    )


@router.post("/api-keys", status_code=201)
async def create_own_key(
    body: dict,
    auth: Annotated[AuthContext, Depends(require_scope("tenant:manage"))],
) -> APIResponse[dict]:
    name = body.get("name", "key")
    scopes = body.get("scopes", list(auth.scopes))
    # Prevent privilege escalation — can only grant scopes the caller already has
    scopes = [s for s in scopes if s in auth.scopes]

    raw_key, prefix = generate_api_key()
    async with admin_session() as db:
        api_key = ApiKey(
            tenant_id=auth.tenant_id,
            name=name,
            prefix=prefix,
            key_hash=hash_key(raw_key),
            scopes=scopes,
        )
        db.add(api_key)
        await db.flush()
        key_id = api_key.id

    return APIResponse(
        success=True,
        data={"key_id": str(key_id), "api_key": raw_key, "api_key_prefix": prefix, "scopes": scopes},
    )


@router.delete("/api-keys/{key_id}")
async def revoke_own_key(
    key_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db_for_tenant)],
    auth: Annotated[AuthContext, Depends(require_scope("tenant:manage"))],
) -> APIResponse[dict]:
    from datetime import UTC, datetime
    key = await db.get(ApiKey, key_id)
    if not key or key.tenant_id != auth.tenant_id:
        raise HTTPException(status_code=404, detail="API key not found")
    key.revoked_at = datetime.now(UTC)
    await db.commit()
    return APIResponse(success=True, data={"key_id": str(key_id), "revoked": True})
