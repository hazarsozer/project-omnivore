from __future__ import annotations

import json
import uuid
from typing import Any

import redis.asyncio as aioredis

from omnivore.config import get_settings

_AUTH_CACHE_TTL = 30  # seconds


def _client() -> aioredis.Redis:
    return aioredis.from_url(get_settings().REDIS_URL, decode_responses=True)


async def get_cached_auth(cache_key: str) -> dict[str, Any] | None:
    async with _client() as r:
        raw = await r.get(cache_key)
    return json.loads(raw) if raw else None


async def set_cached_auth(cache_key: str, payload: dict[str, Any]) -> None:
    async with _client() as r:
        await r.set(cache_key, json.dumps(payload), ex=_AUTH_CACHE_TTL)


async def invalidate_tenant_auth(tenant_id: uuid.UUID) -> None:
    """Invalidate all cached auth entries for a tenant (on revoke/suspend)."""
    async with _client() as r:
        pattern = f"auth:tenant:{tenant_id}:*"
        keys = [k async for k in r.scan_iter(pattern)]
        if keys:
            await r.delete(*keys)
