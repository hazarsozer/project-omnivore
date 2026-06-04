"""Unit tests for auth/rate_limit.py — token bucket Lua script logic."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import omnivore.auth.rate_limit as rl_module
from omnivore.auth.rate_limit import (
    _FALLBACK_MAX_CONNECTIONS,
    RateLimitResult,
    _get_redis,
    check_rate_limit,
    close_fallback_redis,
)

_TENANT = uuid.uuid4()


def _mock_settings(capacity=100, rate=10.0, upload_cost=10, default_cost=1):
    s = MagicMock()
    s.REDIS_URL = "redis://localhost:6379/0"
    s.RL_CAPACITY = capacity
    s.RL_REFILL_RATE = rate
    s.RL_UPLOAD_COST = upload_cost
    s.RL_DEFAULT_COST = default_cost
    return s


def _make_redis_client(script_result):
    """Return a mock redis client whose Lua script returns script_result."""
    script_mock = AsyncMock(return_value=script_result)
    r = MagicMock()
    r.register_script = MagicMock(return_value=script_mock)
    return r, script_mock


class TestRateLimitResult:
    def test_allowed_true(self):
        rl = RateLimitResult(allowed=True, remaining=90.0, capacity=100, cost=10, refill_rate=10.0)
        assert rl.allowed is True
        assert rl.reset_after_seconds == 0

    def test_denied_reset_after(self):
        rl = RateLimitResult(allowed=False, remaining=0.0, capacity=100, cost=10, refill_rate=10.0)
        assert rl.allowed is False
        assert rl.reset_after_seconds > 0

    def test_remaining_stored(self):
        rl = RateLimitResult(allowed=True, remaining=42.5, capacity=100, cost=1, refill_rate=10.0)
        assert rl.remaining == 42.5


class TestCheckRateLimit:
    async def test_allowed_when_lua_returns_1(self):
        r, _ = _make_redis_client([1, 9000, 100])  # [allowed, remaining*100, capacity]
        with (
            patch("omnivore.auth.rate_limit.get_settings", return_value=_mock_settings()),
            patch("omnivore.auth.rate_limit._redis_client", r),
        ):
            result = await check_rate_limit(_TENANT)
        assert result.allowed is True
        assert result.remaining == 90.0

    async def test_denied_when_lua_returns_0(self):
        r, _ = _make_redis_client([0, 0, 100])
        with (
            patch("omnivore.auth.rate_limit.get_settings", return_value=_mock_settings()),
            patch("omnivore.auth.rate_limit._redis_client", r),
        ):
            result = await check_rate_limit(_TENANT)
        assert result.allowed is False

    async def test_custom_cost_preserves_remaining(self):
        # Lua returns allowed=1, remaining=9000 (= 90.0 after /100), capacity=100
        r, _ = _make_redis_client([1, 9000, 100])
        with (
            patch("omnivore.auth.rate_limit.get_settings", return_value=_mock_settings()),
            patch("omnivore.auth.rate_limit._redis_client", r),
        ):
            result = await check_rate_limit(_TENANT, cost=5)
        assert result.allowed is True

    async def test_custom_capacity_used(self):
        r, _ = _make_redis_client([1, 20000, 200])
        with (
            patch("omnivore.auth.rate_limit.get_settings", return_value=_mock_settings()),
            patch("omnivore.auth.rate_limit._redis_client", r),
        ):
            result = await check_rate_limit(_TENANT, capacity=200)
        assert result.capacity == 200


class TestFallbackClient:
    def setup_method(self):
        rl_module._fallback_client = None

    def teardown_method(self):
        rl_module._fallback_client = None

    def test_fallback_is_bounded_and_cached(self):
        """When no singleton is wired, _get_redis builds one bounded client and reuses it."""
        fake = MagicMock()
        with (
            patch("omnivore.auth.rate_limit._redis_client", None),
            patch("omnivore.auth.rate_limit.aioredis.from_url", return_value=fake) as from_url,
        ):
            first = _get_redis()
            second = _get_redis()

        assert first is fake
        assert second is fake          # reused, not recreated
        from_url.assert_called_once()  # created exactly once
        kwargs = from_url.call_args.kwargs
        assert kwargs["max_connections"] == _FALLBACK_MAX_CONNECTIONS
        assert kwargs["decode_responses"] is True

    def test_singleton_takes_priority_over_fallback(self):
        """An explicitly wired _redis_client is returned without building a fallback."""
        singleton = MagicMock()
        with (
            patch("omnivore.auth.rate_limit._redis_client", singleton),
            patch("omnivore.auth.rate_limit.aioredis.from_url") as from_url,
        ):
            assert _get_redis() is singleton
        from_url.assert_not_called()

    async def test_close_fallback_redis_closes_and_resets(self):
        fake = AsyncMock()
        with (
            patch("omnivore.auth.rate_limit._redis_client", None),
            patch("omnivore.auth.rate_limit.aioredis.from_url", return_value=fake),
        ):
            _get_redis()
            await close_fallback_redis()

        fake.aclose.assert_awaited_once()
        assert rl_module._fallback_client is None

    async def test_close_fallback_redis_noop_when_unset(self):
        # Should not raise when no fallback was ever created.
        await close_fallback_redis()
        assert rl_module._fallback_client is None
