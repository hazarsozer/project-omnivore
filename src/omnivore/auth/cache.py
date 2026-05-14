from __future__ import annotations

import json
import uuid
from typing import Any

import redis.asyncio as aioredis

from omnivore.config import get_settings

_AUTH_CACHE_TTL = 30   # seconds
_TENANT_VERSION_TTL = 300  # outlives all auth entries (10× TTL)

# Singleton — set by api/main.py lifespan and worker on_startup.
# Falls back to a fresh connection when None (e.g. in unit tests).
_redis_client: aioredis.Redis | None = None


def _get_redis() -> aioredis.Redis:
    if _redis_client is not None:
        return _redis_client
    return aioredis.from_url(get_settings().REDIS_URL, decode_responses=True)


async def _tenant_version(r: aioredis.Redis, tenant_id: uuid.UUID) -> int:
    v = await r.get(f"auth:tenant:{tenant_id}:v")
    return int(v) if v else 0


async def get_cached_auth(cache_key: str) -> dict[str, Any] | None:
    r = _get_redis()
    raw = await r.get(cache_key)
    if not raw:
        return None
    payload = json.loads(raw)
    # Version check: if tenant version was bumped (revoke/suspend), treat as miss.
    tid = payload.get("tenant_id")
    if tid:
        current_v = await _tenant_version(r, uuid.UUID(tid))
        if current_v != payload.get("_tv", 0):
            return None
    return payload


async def set_cached_auth(cache_key: str, payload: dict[str, Any]) -> None:
    r = _get_redis()
    tid = payload.get("tenant_id")
    tenant_v = 0
    if tid:
        tenant_v = await _tenant_version(r, uuid.UUID(tid))
    await r.set(cache_key, json.dumps({**payload, "_tv": tenant_v}), ex=_AUTH_CACHE_TTL)


async def invalidate_tenant_auth(tenant_id: uuid.UUID) -> None:
    """Bump the tenant version counter so all cached auth entries become stale."""
    r = _get_redis()
    version_key = f"auth:tenant:{tenant_id}:v"
    await r.incr(version_key)
    await r.expire(version_key, _TENANT_VERSION_TTL)
