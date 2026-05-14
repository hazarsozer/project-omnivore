"""Auth token exchange — API key → short-lived JWT."""
from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from omnivore.api.schemas import APIResponse
from omnivore.auth.api_key import extract_prefix, verify_key
from omnivore.auth.jwt import issue_token
from omnivore.config import get_settings
from omnivore.db.models import ApiKey, Tenant
from omnivore.db.session import admin_session

router = APIRouter(prefix="/auth", tags=["auth"])
logger = structlog.get_logger(__name__)


class TokenRequest(BaseModel):
    api_key: str


@router.post("/token")
async def exchange_token(body: TokenRequest) -> APIResponse[dict]:
    settings = get_settings()
    if not settings.JWT_PRIVATE_KEY_PEM.get_secret_value():
        raise HTTPException(status_code=501, detail="JWT not configured on this server")

    prefix = extract_prefix(body.api_key)

    async with admin_session() as db:
        rows = (
            await db.scalars(
                select(ApiKey).where(
                    ApiKey.prefix == prefix,
                    ApiKey.revoked_at.is_(None),
                )
            )
        ).all()

    matched: ApiKey | None = None
    for row in rows:
        if verify_key(row.key_hash, body.api_key):
            matched = row
            break

    if not matched:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    async with admin_session() as db:
        tenant = await db.get(Tenant, matched.tenant_id)

    if not tenant or tenant.status != "active":
        raise HTTPException(status_code=403, detail="Tenant is suspended")

    token = issue_token(
        tenant_id=matched.tenant_id,
        principal_id=str(matched.id),
        scopes=list(matched.scopes),
    )
    return APIResponse(
        success=True,
        data={
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": settings.JWT_ACCESS_TOKEN_EXPIRE_SECONDS,
        },
    )
