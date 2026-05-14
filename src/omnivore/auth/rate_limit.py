from __future__ import annotations

import time
import uuid

import redis.asyncio as aioredis

from omnivore.config import get_settings

# Lua script: atomic token-bucket refill + consume.
# Returns [allowed(0|1), remaining_tokens_float*100, capacity].
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
        # Seconds until <cost> tokens are available again
        self.reset_after_seconds = int((cost - remaining) / refill_rate) + 1 if not allowed else 0


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

    async with aioredis.from_url(settings.REDIS_URL, decode_responses=True) as r:
        script = r.register_script(_LUA_SCRIPT)
        result = await script(keys=[bucket_key], args=[_capacity, _rate, _cost, now_ms])

    allowed = bool(result[0])
    remaining = result[1] / 100.0
    return RateLimitResult(allowed, remaining, _capacity, _cost, _rate)
