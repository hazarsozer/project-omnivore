"""Admin endpoints — gated by ADMIN_BOOTSTRAP_TOKEN, not user auth."""
from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, model_validator
from sqlalchemy import select

from omnivore.api.schemas import APIResponse
from omnivore.auth.admin_token import verify_admin_token
from omnivore.auth.api_key import generate_api_key, hash_key
from omnivore.auth.cache import invalidate_tenant_auth
from omnivore.db.models import ApiKey, Tenant
from omnivore.db.session import admin_session

router = APIRouter(prefix="/admin", tags=["admin"])
logger = structlog.get_logger(__name__)

_DEFAULT_SCOPES = [
    "documents:read", "documents:write",
    "chunks:read", "entities:read",
    "search:read", "handlers:read", "tenant:manage",
]


def _require_admin(x_admin_token: Annotated[str | None, Header()] = None) -> None:
    if not x_admin_token or not verify_admin_token(x_admin_token):
        raise HTTPException(status_code=401, detail="Invalid admin token")


class CreateTenantRequest(BaseModel):
    slug: str
    display_name: str
    config: dict = {}


class CreateKeyRequest(BaseModel):
    name: str
    scopes: list[str] = _DEFAULT_SCOPES
    test: bool = False


class UpdateTenantRequest(BaseModel):
    display_name: str | None = None
    status: str | None = None
    config: dict | None = None

    @model_validator(mode="after")
    def _validate_routing_policy(self) -> UpdateTenantRequest:
        if self.config and "routing_policy" in self.config:
            from omnivore.pipeline.routing import validate_policy
            errors = validate_policy(self.config["routing_policy"])
            if errors:
                raise ValueError(f"Invalid routing_policy: {errors}")
        return self


@router.post("/tenants", status_code=201)
async def create_tenant(
    body: CreateTenantRequest,
    _: Annotated[None, Depends(_require_admin)],
) -> APIResponse[dict]:
    raw_key, prefix = generate_api_key()

    async with admin_session() as db:
        existing = await db.scalar(select(Tenant).where(Tenant.slug == body.slug))
        if existing:
            raise HTTPException(status_code=409, detail=f"Tenant slug '{body.slug}' already exists")

        tenant = Tenant(
            slug=body.slug,
            display_name=body.display_name,
            config=body.config,
            status="active",
        )
        db.add(tenant)
        await db.flush()

        api_key = ApiKey(
            tenant_id=tenant.id,
            name="default",
            prefix=prefix,
            key_hash=hash_key(raw_key),
            scopes=_DEFAULT_SCOPES,
        )
        db.add(api_key)
        await db.flush()

        tenant_id = tenant.id
        key_id = api_key.id

    logger.info("tenant.created", tenant_id=str(tenant_id), slug=body.slug)
    return APIResponse(
        success=True,
        data={
            "tenant_id": str(tenant_id),
            "slug": body.slug,
            "display_name": body.display_name,
            "api_key": raw_key,
            "api_key_id": str(key_id),
            "api_key_prefix": prefix,
        },
    )


@router.get("/tenants")
async def list_tenants(
    _: Annotated[None, Depends(_require_admin)],
    limit: int = 50,
) -> APIResponse[list]:
    async with admin_session() as db:
        rows = (await db.scalars(select(Tenant).limit(limit))).all()
    return APIResponse(
        success=True,
        data=[
            {
                "tenant_id": str(t.id),
                "slug": t.slug,
                "display_name": t.display_name,
                "status": t.status,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in rows
        ],
        meta={"count": len(rows)},
    )


@router.patch("/tenants/{tenant_id}")
async def update_tenant(
    tenant_id: uuid.UUID,
    body: UpdateTenantRequest,
    _: Annotated[None, Depends(_require_admin)],
) -> APIResponse[dict]:
    async with admin_session() as db:
        tenant = await db.get(Tenant, tenant_id)
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant not found")
        if body.display_name is not None:
            tenant.display_name = body.display_name
        if body.status is not None:
            tenant.status = body.status
        if body.config is not None:
            tenant.config = {**(tenant.config or {}), **body.config}

    await invalidate_tenant_auth(tenant_id)
    return APIResponse(
        success=True,
        data={"tenant_id": str(tenant.id), "slug": tenant.slug, "status": tenant.status},
    )


@router.post("/tenants/{tenant_id}/api-keys", status_code=201)
async def create_api_key(
    tenant_id: uuid.UUID,
    body: CreateKeyRequest,
    _: Annotated[None, Depends(_require_admin)],
) -> APIResponse[dict]:
    raw_key, prefix = generate_api_key(test=body.test)

    async with admin_session() as db:
        tenant = await db.get(Tenant, tenant_id)
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant not found")

        api_key = ApiKey(
            tenant_id=tenant_id,
            name=body.name,
            prefix=prefix,
            key_hash=hash_key(raw_key),
            scopes=body.scopes,
        )
        db.add(api_key)
        await db.flush()
        key_id = api_key.id

    logger.info("api_key.created", tenant_id=str(tenant_id), key_id=str(key_id))
    return APIResponse(
        success=True,
        data={"api_key_id": str(key_id), "api_key": raw_key, "api_key_prefix": prefix},
    )


@router.delete("/api-keys/{key_id}", status_code=200)
async def revoke_api_key(
    key_id: uuid.UUID,
    _: Annotated[None, Depends(_require_admin)],
) -> APIResponse[dict]:
    from datetime import UTC, datetime
    tenant_id: uuid.UUID | None = None
    async with admin_session() as db:
        key = await db.get(ApiKey, key_id)
        if not key:
            raise HTTPException(status_code=404, detail="API key not found")
        tenant_id = key.tenant_id
        key.revoked_at = datetime.now(UTC)

    if tenant_id:
        await invalidate_tenant_auth(tenant_id)
    logger.info("api_key.revoked", key_id=str(key_id))
    return APIResponse(success=True, data={"key_id": str(key_id), "revoked": True})
