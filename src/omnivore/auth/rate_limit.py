from __future__ import annotations

import asyncio
import time
import uuid

import redis.asyncio as aioredis

from omnivore.config import get_settings

# Bounded connection pool for the fallback client, mirroring the app's main
# Redis usage. Prevents the lazily-created fallback from opening an unbounded
# number of connections under load.
_FALLBACK_MAX_CONNECTIONS = 10

# Singleton — set by api/main.py lifespan and worker on_startup.
# Falls back to a single bounded client when None (e.g. in unit tests / worker
# paths that don't wire the lifespan). Cached so it isn't recreated per call.
_redis_client: aioredis.Redis | None = None
_fallback_client: aioredis.Redis | None = None
_fallback_loop: object = None      # event loop the fallback client was created on
_lua_script_obj: object = None     # redis Script — registered once, reused across calls
_lua_script_client: object = None  # the client the script was registered on; invalidates cache on change


def _get_redis() -> aioredis.Redis:
    if _redis_client is not None:
        return _redis_client
    global _fallback_client, _fallback_loop
    # An async Redis client is bound to the event loop it was created on. Cache it
    # per running loop: a stable production loop reuses one client, while tests that
    # spin a fresh loop per asyncio.run() get a fresh client instead of one bound to
    # a closed loop ("Event loop is closed" / "Future attached to a different loop").
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _fallback_client is None or _fallback_loop is not loop:
        _fallback_client = aioredis.from_url(
            get_settings().REDIS_URL,
            decode_responses=True,
            max_connections=_FALLBACK_MAX_CONNECTIONS,
        )
        _fallback_loop = loop
    return _fallback_client


async def close_fallback_redis() -> None:
    """Close the lazily-created fallback client, if any. Safe to call repeatedly."""
    global _fallback_client, _fallback_loop
    if _fallback_client is not None:
        await _fallback_client.aclose()
        _fallback_client = None
        _fallback_loop = None


def _get_script(r: aioredis.Redis) -> object:
    global _lua_script_obj, _lua_script_client
    if _lua_script_obj is None or _lua_script_client is not r:
        _lua_script_obj = r.register_script(_LUA_SCRIPT)
        _lua_script_client = r
    return _lua_script_obj


# Lua script: atomic token-bucket refill + consume.
# Returns [allowed(0|1), remaining_tokens_float*100, capacity].
# Registered lazily on first use; cached per Redis client instance to avoid
# re-computing SHA1 on every call.
_LUA_SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local now = tonumber(ARGV[4])

local data = redis.call('HMGET', key, 'tokens', 'updated_at')
local tokens = tonumber(data[1]) or capacity
local updated = tonumber(data[2]) or now

local delta = (now - updated) / 1000.0 * rate
tokens = math.min(capacity, tokens + delta)

if tokens < cost then
    redis.call('HSET', key, 'tokens', tokens, 'updated_at', now)
    redis.call('EXPIRE', key, 3600)
    return {0, math.floor(tokens * 100), capacity}
end

tokens = tokens - cost
redis.call('HSET', key, 'tokens', tokens, 'updated_at', now)
redis.call('EXPIRE', key, 3600)
return {1, math.floor(tokens * 100), capacity}
"""


class RateLimitResult:
    __slots__ = ("allowed", "remaining", "capacity", "reset_after_seconds")

    def __init__(self, allowed: bool, remaining: float, capacity: int, cost: int, refill_rate: float) -> None:
        self.allowed = allowed
        self.remaining = remaining
        self.capacity = capacity
        self.reset_after_seconds = int((cost - remaining) / refill_rate) + 1 if not allowed else 0


async def check_auth_rate_limit(client_ip: str) -> RateLimitResult:
    """IP-based token-bucket for the pre-auth POST /auth/token endpoint."""
    settings = get_settings()
    bucket_key = f"rl:auth_ip:{client_ip}"
    now_ms = int(time.time() * 1000)

    r = _get_redis()
    script = _get_script(r)
    result = await script(
        keys=[bucket_key],
        args=[settings.RL_AUTH_CAPACITY, settings.RL_AUTH_REFILL_RATE, 1, now_ms],
    )

    allowed = bool(result[0])
    remaining = result[1] / 100.0
    return RateLimitResult(allowed, remaining, settings.RL_AUTH_CAPACITY, 1, settings.RL_AUTH_REFILL_RATE)


async def check_rate_limit(
    tenant_id: uuid.UUID,
    *,
    cost: int | None = None,
    capacity: int | None = None,
    refill_rate: float | None = None,
) -> RateLimitResult:
    settings = get_settings()
    _cost = cost if cost is not None else settings.RL_DEFAULT_COST
    _capacity = capacity if capacity is not None else settings.RL_CAPACITY
    _rate = refill_rate if refill_rate is not None else settings.RL_REFILL_RATE

    bucket_key = f"rl:tenant:{tenant_id}"
    now_ms = int(time.time() * 1000)

    r = _get_redis()
    script = _get_script(r)
    result = await script(keys=[bucket_key], args=[_capacity, _rate, _cost, now_ms])

    allowed = bool(result[0])
    remaining = result[1] / 100.0
    return RateLimitResult(allowed, remaining, _capacity, _cost, _rate)
